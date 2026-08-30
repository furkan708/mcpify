"""Execute built HTTP requests with urllib; never raises on HTTP >= 400.

Operational add-ons, all opt-in and all stdlib:
- response cache (--cache-ttl): in-memory, GET+200 only, bounded size
- retry (--retry): idempotent methods only, 502/503/504 and connection
  failures only — POST/PATCH are NEVER retried (side effects)
- verbose / --log-file: stderr and/or file logging; URLs without query
  strings, Authorization values masked, bodies only as size-capped text
- OAuth2ClientCredentials: RFC 6749 client-credentials flow with a
  thread-safe, expiring token cache (tokens live in memory only)
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from . import metrics
from .tools import RequestError

_LOG_SINKS: list[Callable[[str], None]] = []
_DEBUG = os.environ.get("MCPIFY_DEBUG") == "1"
_VERBOSE = False
_LOG_FILE = None
_LOG_LOCK = threading.Lock()


def set_logging(verbose: bool = False, log_file: str | None = None) -> None:
    """Enable --verbose (stderr detail) and/or --log-file. Called once
    from the CLI before serving; keep stdout untouched either way."""
    global _VERBOSE, _LOG_FILE
    _VERBOSE = verbose
    _LOG_FILE = log_file


def register_log_sink(sink: Callable[[str], None]) -> None:
    """Receive every masked log line (dashboard tail). Never raises out."""
    _LOG_SINKS.append(sink)


def _log(level: str, message: str) -> None:
    """Optional stderr logging (MCPIFY_DEBUG=1 or --verbose). NEVER
    touches stdout: the JSON-RPC stream must stay clean. URLs are logged
    without query strings so query-style auth credentials never hit the
    log or the file. Sinks (dashboard) see lines regardless of verbose."""
    if not (_DEBUG or _VERBOSE or _LOG_FILE or _LOG_SINKS):
        return
    line = f"{level} mcpify: {message}"
    for sink in _LOG_SINKS:
        with contextlib.suppress(Exception):  # sink hatasi asla servise sirayet etmez
            sink(line)
    if not (_DEBUG or _VERBOSE or _LOG_FILE):
        return  # sink-only modu: dashboard gorur, stderr temiz kalir
    with _LOG_LOCK:
        print(line, file=sys.stderr, flush=True)
        if _LOG_FILE:
            with contextlib.suppress(OSError), open(_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def _mask(headers: dict[str, Any]) -> dict[str, Any]:
    masked = {}
    for key, value in (headers or {}).items():
        if key.lower() in ("authorization", "x-api-key", "api-key"):
            masked[key] = value.split(" ", 1)[0] + " ***" if " " in value else "***"
        else:
            masked[key] = value
    return masked


RETRYABLE_STATUS = frozenset({502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE"})
MAX_RETRIES = 5          # hard cap however large --retry is
MAX_CACHE_ENTRIES = 256


class ResponseCache:
    """Tiny TTL cache for GET+200 results. Thread-safe, bounded.

    Entries keep their ``ETag`` when the API sent one, so an expired
    entry can be revalidated with ``If-None-Match`` instead of a full
    re-download (304 -> the stored body is served and its TTL reset).
    """

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._store: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry and entry["expires"] > now:
                result: dict[str, Any] = entry["result"]
                return result
            if entry:
                del self._store[key]
        return None

    def get_stale(self, key: str) -> dict[str, Any] | None:
        """Return a possibly-expired entry (kept for ETag revalidation)."""
        with self._lock:
            entry = self._store.get(key)
            return dict(entry) if entry else None

    def revalidate(self, key: str) -> None:
        """304 arrived: keep the stored body, reset its TTL clock."""
        with self._lock:
            entry = self._store.get(key)
            if entry:
                entry["expires"] = time.monotonic() + self.ttl

    def put(self, key: str, result: dict[str, Any], etag: str | None = None) -> None:
        with self._lock:
            if len(self._store) >= MAX_CACHE_ENTRIES:
                oldest = min(self._store, key=lambda k: self._store[k]["expires"])
                del self._store[oldest]
            self._store[key] = {
                "expires": time.monotonic() + self.ttl,
                "result": result,
                "etag": etag,
            }

    def invalidate(self, pattern: str | None = None) -> int:
        """Drop entries whose key contains ``pattern`` (all when None).
        Returns how many entries were removed."""
        with self._lock:
            if pattern is None:
                removed = len(self._store)
                self._store.clear()
                return removed
            doomed = [key for key in self._store if pattern in key]
            for key in doomed:
                del self._store[key]
            return len(doomed)

    def size(self) -> int:
        """Entry count. Deliberately NOT __len__: an empty cache must
        stay truthy (`if cache:` guards would silently disable it)."""
        with self._lock:
            return len(self._store)


def _retry_after_seconds(result: dict[str, Any], fallback: float) -> float | None:
    """Parse the Retry-After header (integer seconds). HTTP-date form and
    unparsable values return None (nothing sane to wait); a missing
    header falls back to the configured retry delay."""
    value = (result.get("headers") or {}).get("Retry-After")
    if value is None:
        return fallback
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None  # HTTP-date form: not worth a date parser here
    return max(seconds, 0.0)


def execute(
    request: dict[str, Any],
    timeout: float = 30.0,
    cache: ResponseCache | None = None,
    retry: int = 0,
    retry_delay: float = 1.0,
    wait_on_429: float = 0.0,
) -> dict[str, Any]:
    """Perform the request and return {status, body, json, headers}.

    HTTP errors (4xx/5xx) are returned as results instead of raising, so
    the agent can see API error payloads and react to them. With a cache
    attached, GET+200 answers are served from memory within the TTL. With
    retry > 0, idempotent methods are re-attempted on 502/503/504 and on
    connection failures — never on 4xx, never for POST/PATCH. With
    wait_on_429 > 0, a 429 on an idempotent method is honored ONCE: the
    call sleeps min(Retry-After, wait_on_429) seconds (the API explicitly
    asked for the delay, so this is not a blind retry) and tries again;
    a Retry-After longer than the cap returns the 429 untouched.
    """
    attempts = max(0, min(retry, MAX_RETRIES))
    attempt = 0
    waited_429 = False
    while True:
        result = _execute_once(request, timeout, cache)
        method = request.get("method", "GET").upper()
        if (
            result["status"] == 429
            and not waited_429
            and wait_on_429 > 0
            and method in IDEMPOTENT_METHODS
        ):
            delay = _retry_after_seconds(result, retry_delay)
            if delay is not None and delay <= wait_on_429:
                _log("WARNING", f"429 rate limited — honoring Retry-After, waiting {delay:g}s once")
                time.sleep(delay)
                waited_429 = True
                continue
        retryable = result["status"] in RETRYABLE_STATUS or result["status"] == 0
        if attempt < attempts and retryable and method in IDEMPOTENT_METHODS:
            _log("WARNING", f"retry {attempt + 1}/{attempts} for {method} "
                            f"(status {result['status']}, waiting {retry_delay}s)")
            time.sleep(retry_delay)
            attempt += 1
            continue
        return result


def _header_value(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (servers differ on casing)."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _execute_once(request: dict[str, Any], timeout: float, cache: ResponseCache | None = None) -> dict[str, Any]:
    cache_key = request.get("method", "GET").upper() + " " + request["url"]
    is_get = request.get("method", "GET").upper() == "GET"
    revalidate_entry: dict[str, Any] | None = None
    if cache is not None and is_get:
        hit = cache.get(cache_key)
        if hit is None:
            # expired-but-present with an ETag -> conditional revalidation
            stale = cache.get_stale(cache_key)
            if stale and stale.get("etag"):
                revalidate_entry = stale
        metrics.inc("mcpify_cache_requests_total", {"result": "hit" if hit is not None else "miss"})
        if hit is not None:
            _log("INFO", f"cache hit {request['url'].split('?', 1)[0]}")
            return hit
    data = request.get("body")
    url_guvenli = request["url"].split("?", 1)[0]
    send_headers = dict(request["headers"])
    if revalidate_entry:
        send_headers["If-None-Match"] = str(revalidate_entry["etag"])
    req = urllib.request.Request(  # noqa: S310 — URL operatorun spec'inden
        request["url"],
        data=data,
        headers=send_headers,
        method=request["method"],
    )
    basla = time.monotonic()
    try:
        # sema operatorun spec/base-url seciminden gelir; ajandan degil (S310 gerekcesi)
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            status = response.status
            raw = response.read().decode("utf-8", "replace")
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as err:
        status = err.code
        raw = err.read().decode("utf-8", "replace")
        headers = dict(err.headers.items()) if err.headers else {}
    except urllib.error.URLError as err:
        _log("ERROR", f"{request['method']} {url_guvenli} -> connection failed: {err.reason}")
        return {
            "status": 0,
            "body": f"connection failed: {err.reason}",
            "json": None,
            "headers": {},
        }
    sure = time.monotonic() - basla
    if status >= 500:
        _log("ERROR", f"{request['method']} {url_guvenli} -> {status} ({sure:.2f}s)")
    elif status >= 400:
        _log("WARNING", f"{request['method']} {url_guvenli} -> {status} ({sure:.2f}s)")
    else:
        _log("INFO", f"{request['method']} {url_guvenli} -> {status} ({sure:.2f}s)")

    parsed = None
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        parsed = json.loads(raw)
    if _VERBOSE and raw:
        excerpt = raw[:1000]
        _log("INFO", f"response {status} from {url_guvenli} ({len(raw)} bytes): {excerpt}")
    result = {"status": status, "body": raw, "json": parsed, "headers": headers}
    if cache is not None and is_get:
        if status == 304 and revalidate_entry is not None:
            cache.revalidate(cache_key)
            metrics.inc("mcpify_cache_requests_total", {"result": "hit"})
            _log("INFO", f"cache revalidated (304) {url_guvenli}")
            return dict(revalidate_entry["result"])
        if status == 200:
            cache.put(cache_key, result, _header_value(headers, "ETag"))
    return result


MAX_RESULT_CHARS = 40_000


def remediation(result: dict[str, Any], tool: dict[str, Any] | None = None, known_paths: list[str] | None = None) -> str:
    """Turn an HTTP error into corrective guidance the agent can act on.

    The single biggest lever on agent success rate is not preventing
    errors — it is making the next call succeed. Remediation lines tell
    the agent what the API complained about (validation details), what to
    change (auth, wait time), and the closest valid alternatives on 404.
    Returned text is appended to the error body; empty when nothing helps.
    """
    status = result["status"]
    tips: list[str] = []
    if status == 0:
        tips.append("Connection failed — check the base URL and network, then re-run.")
    parsed = result.get("json")
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, str) and detail:
            tips.append(f"API said: {detail[:300]}")
        elif isinstance(detail, list):
            items = []
            for item in detail[:3]:
                if isinstance(item, dict) and item.get("msg"):
                    loc = ".".join(str(part) for part in item.get("loc", []))
                    items.append(f"{loc}: {item['msg']}" if loc else str(item["msg"]))
            if items:
                tips.append("API validation: " + "; ".join(items))
        if "detail" not in parsed:
            for key in ("message", "error"):
                value = parsed.get(key)
                if isinstance(value, str) and value:
                    tips.append(f"API said: {value[:300]}")
                    break
        errors = parsed.get("errors")
        if isinstance(errors, list):
            items = []
            for entry in errors[:3]:
                if isinstance(entry, dict):
                    items.append(str(entry.get("message", entry))[:200])
                else:
                    items.append(str(entry)[:200])
            if items:
                tips.append("API errors: " + "; ".join(items))
    if status in (401, 403):
        tips.append(
            "Check credentials: serve with --auth-env/--auth-style so the "
            "credential is actually sent and accepted by the API."
        )
    elif status == 404 and known_paths and tool is not None:
        import difflib

        path = tool["_meta"]["path"]
        close = difflib.get_close_matches(path, known_paths, n=3, cutoff=0.5)
        if close:
            tips.append("Closest known paths: " + ", ".join(close))
    elif status == 405:
        tips.append("HTTP method not allowed on this path — the operation may have changed upstream.")
    elif status == 429:
        retry_after = (result.get("headers") or {}).get("Retry-After")
        wait = f"waiting {retry_after}s" if retry_after else "backing off"
        tips.append(f"Rate limited — the API suggests {wait}. mcpify never retries automatically.")
    elif status >= 500:
        tips.append(
            "The upstream API failed (not an mcpify error). Re-running the call "
            "yourself is reasonable; mcpify does not retry automatically."
        )
    if not tips:
        return ""
    return "\n" + "\n".join("- " + tip for tip in tips)


def _smart_truncate(body: str, limit: int) -> str:
    """Shrink an oversized body WITHOUT handing the model broken syntax.

    A raw byte cut mid-JSON produces a document that looks complete but
    cannot be parsed — the model then reasons over a fragment. JSON
    bodies are cut along structure instead: arrays keep their first
    items inside a wrapper object, objects keep the top-level keys that
    fit; both carry an explicit truncation marker. The serialized result
    is measured honestly (indentation included) and shrunk until it
    really fits. Non-JSON bodies fall back to the character cut (there
    is no structure to preserve).
    """
    if len(body) <= limit:
        return body

    def byte_cut() -> str:
        kesilen = len(body) - limit
        return body[:limit] + f"\n… [truncated {kesilen:,} more characters]"

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return byte_cut()

    if isinstance(data, list):
        kept_count = len(data)
        while kept_count > 0:
            text = json.dumps(
                {"truncated": True, "showing": kept_count,
                 "omitted": len(data) - kept_count, "items": data[:kept_count]},
                ensure_ascii=False, indent=2,
            )
            if len(text) <= limit:
                return text
            kept_count = (kept_count * 9) // 10 if kept_count > 20 else kept_count - 1
        return byte_cut()  # even a single item overflows

    if isinstance(data, dict):
        # Key-keep alone is not enough for API envelopes like
        # {"features": [3000 alerts]} — metadata keys fit, the payload key
        # is dropped, and the agent learns nothing. So a value that alone
        # exceeds the remaining budget is TRUNCATED IN PLACE (nested
        # array/object logic) instead of being omitted: the agent keeps
        # the first items of the payload it asked for.
        kept_obj: dict[str, Any] = {}
        omitted: list[str] = []
        used = 0
        for key, value in data.items():
            piece_budget = limit - used - len(json.dumps({"mcpify_truncated": True, "mcpify_omitted_keys": ["x" * 40]}, indent=2))
            if piece_budget <= 0:
                omitted.append(key)
                continue
            piece_text = json.dumps({key: value}, ensure_ascii=False, indent=2)
            if len(piece_text) <= piece_budget:
                kept_obj[key] = value
                used += len(piece_text) + 2
                continue
            if isinstance(value, list):
                fitted = _fit_array(key, value, piece_budget)
                if fitted is not None:
                    kept_obj[key], used = fitted
                    continue
                omitted.append(key)
                continue
            if isinstance(value, dict):
                fitted = _fit_object(key, value, piece_budget)
                if fitted is not None:
                    kept_obj[key], used = fitted
                    continue
            omitted.append(key)
        if not kept_obj:
            return byte_cut()  # nothing usable fits
        if omitted:
            kept_obj["mcpify_truncated"] = True
            kept_obj["mcpify_omitted_keys"] = omitted
        text = json.dumps(kept_obj, ensure_ascii=False, indent=2)
        if len(text) <= limit:
            return text
        # the honest measure overshot (marker widths): shrink from the tail
        for key in list(reversed(list(data))):
            kept_obj.pop(key, None)
            kept_obj["mcpify_truncated"] = True
            kept_obj["mcpify_omitted_keys"] = sorted(
                set(kept_obj.get("mcpify_omitted_keys", [])) | {key})
            if not kept_obj.get("mcpify_omitted_keys"):
                kept_obj.pop("mcpify_omitted_keys", None)
            text = json.dumps(kept_obj, ensure_ascii=False, indent=2)
            if len(text) <= limit and kept_obj:
                return text
        return byte_cut()

    return byte_cut()


def _fit_array(key: str, data: list[Any], limit: int) -> tuple[Any, int] | None:
    """First items of `data` that fit `limit` AS `{key: items}` JSON.

    Measures the WRAPPER (nesting adds indentation), returns
    (value, budget_left) or None when even one item does not fit.
    """
    kept_count = len(data)
    while kept_count > 0:
        candidate: list[Any] = list(data[:kept_count])
        if kept_count < len(data):
            candidate.append({"mcpify_item_truncated": True, "omitted": len(data) - kept_count})
        if len(json.dumps({key: candidate}, ensure_ascii=False, indent=2)) <= limit:
            return candidate, limit - len(json.dumps({key: candidate}, ensure_ascii=False, indent=2))
        kept_count = (kept_count * 9) // 10 if kept_count > 20 else kept_count - 1
    return None


def _fit_object(key: str, data: dict[str, Any], limit: int) -> tuple[Any, int] | None:
    """Object shrunk to `limit` AS `{key: {...}}` JSON (first keys +
    marker when shrunk). Returns (value, budget_left) or None."""
    keys = list(data)
    kept_count = len(keys)
    while kept_count > 0:
        candidate = {k: data[k] for k in keys[:kept_count]}
        if kept_count < len(keys):
            candidate["mcpify_truncated"] = True
            candidate["mcpify_omitted_keys"] = keys[kept_count:]
        if len(json.dumps({key: candidate}, ensure_ascii=False, indent=2)) <= limit:
            return candidate, limit - len(json.dumps({key: candidate}, ensure_ascii=False, indent=2))
        kept_count = (kept_count * 9) // 10 if kept_count > 20 else kept_count - 1
    return None


def format_result(result: dict[str, Any]) -> tuple[str, bool]:
    """Format an execute() result for an MCP tool response: (text, is_error).

    Oversized bodies are truncated so a single tool call cannot blow the
    client's context window — JSON is cut along structure (see
    _smart_truncate), never mid-document.
    """
    body = result["body"]
    if result["json"] is not None:
        body = json.dumps(result["json"], ensure_ascii=False, indent=2)
    body = _smart_truncate(body, MAX_RESULT_CHARS)
    is_error = result["status"] == 0 or result["status"] >= 400
    if is_error:
        return f"HTTP {result['status']}\n{body}", is_error
    return body, is_error


# ---------------------------------------------------------------------------
# OAuth2 client-credentials flow (RFC 6749 section 4.4), stdlib only
# ---------------------------------------------------------------------------

OAUTH2_DEFAULT_TTL = 3600     # RFC 6749 recommendation when expires_in is absent
OAUTH2_REFRESH_MARGIN = 30.0  # refresh this many seconds before real expiry


class OAuth2ClientCredentials:
    """Bearer-token provider for the client-credentials grant.

    Fetches, caches and refreshes an access token from ``token_url`` and
    produces the ``Authorization: Bearer ...`` header for outgoing API
    calls. Duck-types AuthConfig (headers/apply_query/describe) so
    ApiServer needs no special casing except the 401 self-heal.

    Design points, all deliberate:
    - credentials come from environment variables, never flags or the
      config file, so nothing secret lands in ps/history/disk
    - the token lives in memory only, guarded by a lock (tools/call can
      run concurrently through the batch path)
    - when ``expires_in`` is missing the RFC-recommended 3600 s is
      assumed; a stale token self-heals via invalidate() on the first 401
    - client authentication defaults to HTTP Basic; ``body`` mode puts
      client_id/client_secret in the form body for token endpoints that
      require it
    """

    def __init__(
        self,
        token_url: str,
        client_id_env: str,
        client_secret_env: str | None = None,
        scope: str | None = None,
        client_auth: str = "basic",
        timeout: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if client_auth not in ("basic", "body"):
            raise ValueError("client_auth must be 'basic' or 'body'")
        self.token_url = token_url
        self.client_id_env = client_id_env
        self.client_secret_env = client_secret_env
        self.scope = scope
        self.client_auth = client_auth
        self.timeout = timeout
        self._clock = clock or time.time
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def style(self) -> str:
        return "oauth2-client-credentials"

    def _credentials(self) -> tuple[str, str | None]:
        import os

        client_id = os.environ.get(self.client_id_env, "")
        if not client_id:
            raise RequestError(
                f"environment variable '{self.client_id_env}' is not set "
                "(required for the OAuth2 client id)"
            )
        secret: str | None = None
        if self.client_secret_env:
            secret = os.environ.get(self.client_secret_env, "")
            if not secret:
                raise RequestError(
                    f"environment variable '{self.client_secret_env}' is not set "
                    "(required for the OAuth2 client secret)"
                )
        return client_id, secret

    def _fetch(self) -> None:
        client_id, secret = self._credentials()
        form: list[tuple[str, str]] = [("grant_type", "client_credentials")]
        if self.scope:
            form.append(("scope", self.scope))
        if self.client_auth == "body":
            # RFC 6749: public clients (no secret) still identify via client_id
            form.append(("client_id", client_id))
            if secret is not None:
                form.append(("client_secret", secret))
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json"}
        if self.client_auth == "basic" and secret is not None:
            import base64

            raw = f"{client_id}:{secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        request = {
            "method": "POST",
            "url": self.token_url,
            "headers": headers,
            "body": urllib.parse.urlencode(form).encode("utf-8"),
        }
        _log("INFO", f"oauth2: requesting token from {self.token_url}")
        result = _execute_once(request, self.timeout, None)
        if result["status"] == 0:
            raise RequestError(
                f"oauth2: token endpoint unreachable ({result['body'][:200]})"
            )
        payload = result.get("json")
        if result["status"] != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error_description") or payload.get("error") or "")[:200]
            raise RequestError(
                f"oauth2: token request failed with HTTP {result['status']}"
                + (f": {detail}" if detail else "")
            )
        # caller (headers()) holds self._lock — assign directly; a
        # non-reentrant Lock here would deadlock on the double-checked path
        self._token = str(payload["access_token"])
        try:
            ttl = float(payload.get("expires_in", OAUTH2_DEFAULT_TTL))
        except (TypeError, ValueError):
            ttl = OAUTH2_DEFAULT_TTL
        self._expires_at = self._clock() + max(ttl, 1.0)
        _log("INFO", "oauth2: token acquired")

    def headers(self) -> dict[str, Any]:
        if self._token is None or self._clock() > self._expires_at - OAUTH2_REFRESH_MARGIN:
            with self._lock:
                if self._token is None or self._clock() > self._expires_at - OAUTH2_REFRESH_MARGIN:
                    self._fetch()
        if self._token is None:  # defensive: -O strips asserts; _fetch raises or sets
            raise RequestError("oauth2: token fetch produced no token")
        return {"Authorization": f"Bearer {self._token}"}

    def apply_query(self, url: str) -> str:
        return url  # bearer token only; never in the query string

    def invalidate(self) -> None:
        """Drop the cached token so the next call fetches a fresh one."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def describe(self) -> dict[str, Any]:
        import os

        return {
            "style": self.style,
            "token_url": self.token_url,
            "client_auth": self.client_auth,
            "scope": self.scope,
            "client_id_env": self.client_id_env,
            "client_id_env_set": bool(os.environ.get(self.client_id_env)),
            "client_secret_env": self.client_secret_env,
            "client_secret_env_set": bool(os.environ.get(self.client_secret_env)) if self.client_secret_env else None,
        }
