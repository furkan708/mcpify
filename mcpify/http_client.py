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

from .tools import RequestError

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


def _log(level: str, message: str) -> None:
    """Optional stderr logging (MCPIFY_DEBUG=1 or --verbose). NEVER
    touches stdout: the JSON-RPC stream must stay clean. URLs are logged
    without query strings so query-style auth credentials never hit the
    log or the file."""
    if not (_DEBUG or _VERBOSE):
        return
    line = f"{level} mcpify: {message}"
    with _LOG_LOCK:
        print(line, file=sys.stderr, flush=True)
        if _LOG_FILE:
            with contextlib.suppress(OSError), open(_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def _mask(headers: dict) -> dict:
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
    """Tiny TTL cache for GET+200 results. Thread-safe, bounded."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry and entry["expires"] > now:
                return entry["result"]
            if entry:
                del self._store[key]
        return None

    def put(self, key: str, result: dict) -> None:
        with self._lock:
            if len(self._store) >= MAX_CACHE_ENTRIES:
                oldest = min(self._store, key=lambda k: self._store[k]["expires"])
                del self._store[oldest]
            self._store[key] = {"expires": time.monotonic() + self.ttl, "result": result}


def execute(
    request: dict,
    timeout: float = 30.0,
    cache: ResponseCache | None = None,
    retry: int = 0,
    retry_delay: float = 1.0,
) -> dict:
    """Perform the request and return {status, body, json, headers}.

    HTTP errors (4xx/5xx) are returned as results instead of raising, so
    the agent can see API error payloads and react to them. With a cache
    attached, GET+200 answers are served from memory within the TTL. With
    retry > 0, idempotent methods are re-attempted on 502/503/504 and on
    connection failures — never on 4xx, never for POST/PATCH.
    """
    attempts = max(0, min(retry, MAX_RETRIES))
    result = {}
    for attempt in range(attempts + 1):
        result = _execute_once(request, timeout, cache)
        method = request.get("method", "GET").upper()
        retryable = result["status"] in RETRYABLE_STATUS or result["status"] == 0
        if attempt < attempts and retryable and method in IDEMPOTENT_METHODS:
            _log("WARNING", f"retry {attempt + 1}/{attempts} for {method} "
                            f"(status {result['status']}, waiting {retry_delay}s)")
            time.sleep(retry_delay)
            continue
        return result
    return result  # unreachable; keeps mypy happy


def _execute_once(request: dict, timeout: float, cache: ResponseCache | None = None) -> dict:
    cache_key = request.get("method", "GET").upper() + " " + request["url"]
    if cache is not None and request.get("method", "GET").upper() == "GET":
        hit = cache.get(cache_key)
        if hit is not None:
            _log("INFO", f"cache hit {request['url'].split('?', 1)[0]}")
            return hit
    data = request.get("body")
    url_guvenli = request["url"].split("?", 1)[0]
    req = urllib.request.Request(
        request["url"],
        data=data,
        headers=request["headers"],
        method=request["method"],
    )
    basla = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", "replace")
            headers = {k: v for k, v in response.headers.items()}
    except urllib.error.HTTPError as err:
        status = err.code
        raw = err.read().decode("utf-8", "replace")
        headers = {k: v for k, v in err.headers.items()} if err.headers else {}
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
    if cache is not None and request.get("method", "GET").upper() == "GET" and status == 200:
        cache.put(cache_key, result)
    return result


MAX_RESULT_CHARS = 40_000


def remediation(result: dict, tool: dict | None = None, known_paths: list[str] | None = None) -> str:
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


def format_result(result: dict) -> tuple[str, bool]:
    """Format an execute() result for an MCP tool response: (text, is_error).

    Oversized bodies are truncated so a single tool call cannot blow the
    client's context window.
    """
    body = result["body"]
    if result["json"] is not None:
        body = json.dumps(result["json"], ensure_ascii=False, indent=2)
    if len(body) > MAX_RESULT_CHARS:
        kesilen = len(body) - MAX_RESULT_CHARS
        body = body[:MAX_RESULT_CHARS] + f"\n… [truncated {kesilen:,} more characters]"
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

    def headers(self) -> dict:
        if self._token is None or self._clock() > self._expires_at - OAUTH2_REFRESH_MARGIN:
            with self._lock:
                if self._token is None or self._clock() > self._expires_at - OAUTH2_REFRESH_MARGIN:
                    self._fetch()
        assert self._token is not None  # _fetch raised or set it
        return {"Authorization": f"Bearer {self._token}"}

    def apply_query(self, url: str) -> str:
        return url  # bearer token only; never in the query string

    def invalidate(self) -> None:
        """Drop the cached token so the next call fetches a fresh one."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def describe(self) -> dict:
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
