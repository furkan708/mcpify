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
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from typing import Any

from . import metrics
from .tools import RequestError


class _CrossHostSanitizingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward caller-supplied headers across origins.

    Credentials can use arbitrary header names, so a denylist is not safe.
    A scheme change also crosses the trust boundary, including HTTPS to HTTP.
    """

    def redirect_request(  # urllib's base signature is untyped; Any keeps the override strict-clean
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if redirected is not None and (old.scheme.lower(), old.netloc.lower()) != (
            new.scheme.lower(), new.netloc.lower()
        ):
            redirected.headers.clear()
            redirected.unredirected_hdrs.clear()
        return redirected


_OPENER = urllib.request.build_opener(_CrossHostSanitizingRedirectHandler)


def _decode_payload(raw: bytes, headers: dict[str, str]) -> str:
    """Decompress gzip/deflate bodies before text decoding.

    urllib does not honor Content-Encoding; a gateway that compresses
    anyway would feed binary garbage into the model's context. Broken
    streams degrade to the raw body (never crash).
    """
    encoding = str(_header_value(headers, "Content-Encoding") or "").strip().lower()
    if encoding in ("gzip", "x-gzip", "deflate"):
        try:
            raw = gzip.decompress(raw) if encoding != "deflate" else zlib.decompress(raw)
        except (OSError, zlib.error) as err:  # broken stream: report raw, never crash
            _log("WARNING", f"could not decompress {encoding} response: {err}")
    return raw.decode("utf-8", "replace")

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


class RateLimiter:
    """Thread-safe client-side throttle: at most ``rps`` requests/second.

    A courtesy limiter for APIs without published quotas: every execute()
    waits for its slot BEFORE the request leaves — including retries, so
    a retrying call cannot burst the upstream either. Bounds this process
    only; it is not a distributed rate limiter.
    """

    def __init__(
        self,
        rps: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rps <= 0:
            raise ValueError("rps must be positive")
        self.rps = rps
        self._min_interval = 1.0 / rps
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> float:
        """Reserve the next slot; sleep until it opens. Returns the delay."""
        with self._lock:
            now = self._clock()
            delay = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._min_interval
        if delay > 0:
            self._sleeper(delay)
        return max(0.0, delay)


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
    value = _header_value(result.get("headers") or {}, "Retry-After")
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
    rate_limit: RateLimiter | None = None,
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
    a Retry-After longer than the cap returns the 429 untouched. With a
    rate_limit attached, every attempt (first call and retries alike)
    first waits for a slot — the upstream never sees a burst.
    """
    attempts = max(0, min(retry, MAX_RETRIES))
    attempt = 0
    waited_429 = False
    while True:
        if rate_limit is not None:
            rate_limit.wait()
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
    # Header parameters (including credentials) can change the representation.
    # Hash them so the cache key does not retain plaintext header secrets, while
    # keeping the URL searchable for the cache invalidation tool.
    header_items = sorted((str(k).lower(), str(v)) for k, v in request["headers"].items())
    header_hash = hashlib.sha256(json.dumps(header_items).encode("utf-8")).hexdigest()
    cache_key = request.get("method", "GET").upper() + " " + request["url"] + " " + header_hash
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
        with _OPENER.open(req, timeout=timeout) as response:
            status = response.status
            headers = dict(response.headers.items())
            raw = _decode_payload(response.read(), headers)
    except urllib.error.HTTPError as err:
        status = err.code
        headers = dict(err.headers.items()) if err.headers else {}
        raw = _decode_payload(err.read(), headers)
    except urllib.error.URLError as err:
        _log("ERROR", f"{request['method']} {url_guvenli} -> connection failed: {err.reason}")
        return {
            "status": 0,
            "body": f"connection failed: {err.reason}",
            "json": None,
            "headers": {},
        }
    except TimeoutError:  # read-phase timeout escapes urllib unwrapped — without this it kills the server
        _log("ERROR", f"{request['method']} {url_guvenli} -> timeout after {timeout}s")
        return {
            "status": 0,
            "body": f"timeout: no response within {timeout}s (client-side --timeout)",
            "json": None,
            "headers": {},
        }
    except OSError as err:  # reset / remote disconnected mid-response — URLError covers connect-phase only
        _log("ERROR", f"{request['method']} {url_guvenli} -> connection failed: {err}")
        return {
            "status": 0,
            "body": f"connection failed: {err}",
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
    # ``url`` is the request's own URL (query string included): the pagination
    # hint needs to resolve a relative "next" against it, and nothing else
    # consumes this key. Query-style credentials never reach the model — the
    # hint only reports parameters the tool already declares.
    result = {"status": status, "body": raw, "json": parsed, "headers": headers,
              "url": request["url"]}
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


def media_type_of(content_type: str | None) -> str:
    """Normalize a Content-Type header to a bare, lowercase media type.

    ``"Application/Problem+JSON; charset=utf-8"`` -> ``"application/problem+json"``.
    Servers are not consistent about casing, and the downstream classifier
    is a string comparison, so normalization belongs in one place.
    """
    return (content_type or "").split(";")[0].strip().lower()


def error_category(status: int) -> str:
    """Classify an HTTP failure into the three actions an agent can take.

    ``retryable``      — the request is fine, the world was temporarily
                         uncooperative (timeouts, resets, 429, 5xx):
                         retry the same call, maybe with backoff.
    ``invalid_request``— this call can never succeed as constructed
                         (400/404/422...): change the input or give up.
    ``forbidden``      — structurally not allowed regardless of input
                         (401/403): stop, surface it to the human.

    The token leads the error text and is mirrored in structuredContent,
    so the category is a string comparison, not a comprehension task.
    """
    if status in (401, 403):
        return "forbidden"
    if status == 0 or status == 429 or status >= 500:
        return "retryable"
    return "invalid_request"


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
        if str(result.get("body", "")).startswith("timeout:"):
            tips.append(
                "Upstream was slow, not unreachable — re-run the call, or raise the "
                "client-side --timeout if this endpoint normally takes longer."
            )
        else:
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
        retry_after = _header_value(result.get("headers") or {}, "Retry-After")
        wait = f"waiting {retry_after}s" if retry_after else "backing off"
        tips.append(f"Rate limited — the API suggests {wait}. Check --wait-on-429 for automatic retry.")
    elif status >= 500:
        tips.append(
            "The upstream API failed (not an mcpify error). Re-running the call "
            "yourself is reasonable; mcpify does not retry automatically."
        )
    if not tips:
        return ""
    return "\n" + "\n".join("- " + tip for tip in tips)


def parse_fields(raw: str | None) -> frozenset[str]:
    """Parse --fields "id,name" into a clean set (CLI + config share it)."""
    if not raw:
        return frozenset()
    fields = frozenset(part.strip() for part in str(raw).split(",") if part.strip())
    if not fields:
        raise ValueError("--fields is empty after parsing (expected comma-separated key names)")
    return fields


def project_json(data: Any, fields: frozenset[str]) -> Any:
    """Keep only the requested fields — at EVERY level of the response.

    Rule, documented and predictable: a selected key keeps its value
    verbatim (arrays it holds are never second-guessed); a non-selected
    container key is TRANSPARENT — it survives only when it still holds
    selected data after projection; everything else is dropped, including
    scalar arrays that carry no selected field and items emptied by the
    projection. This makes API envelopes transparent: ``--fields
    id,event`` on an alert envelope keeps ``features`` (the container),
    each alert's ``id`` and its ``properties.event`` — and drops
    geometry, context and prose. A top-level-only rule was tried first
    and failed the live test: the same query returned an empty object
    because the requested fields lived one level inside the envelope.
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if key in fields:
                out[key] = value
            elif isinstance(value, (dict, list)):
                projected = project_json(value, fields)
                if projected:  # an emptied container is noise, not data
                    out[key] = projected
        return out
    if isinstance(data, list):
        kept: list[Any] = []
        for item in data:
            if isinstance(item, (dict, list)):
                projected = project_json(item, fields)
                if projected:
                    kept.append(projected)
            # scalars inside a non-selected array carry no field: dropped
        return kept
    return data


def redact_json(data: Any, secrets: frozenset[str]) -> Any:
    """Mask values whose KEY names a secret — at EVERY level.

    Case-insensitive key match (``password``, ``Password``, ``Client_Secret``).
    Matching values become ``"***"`` (``null`` stays ``null`` — masking an
    absent value adds nothing); structure is otherwise untouched and array
    lengths never change, so indices stay stable for the agent. Pairs with
    ``--fields``: projection runs first, redaction last — a field you asked
    for by name is still masked when it is a secret.
    """
    if not secrets:
        return data
    if isinstance(data, dict):
        return {
            key: ("***" if key.lower() in secrets and value is not None
                  else redact_json(value, secrets))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_json(item, secrets) for item in data]
    return data


def count_redact_targets(data: Any, secrets: frozenset[str]) -> int:
    """How many values redact_json() would mask — for metrics, before masking."""
    if not secrets:
        return 0
    if isinstance(data, dict):
        return sum(
            (1 if key.lower() in secrets and value is not None else 0)
            + count_redact_targets(value, secrets)
            for key, value in data.items()
        )
    if isinstance(data, list):
        return sum(count_redact_targets(item, secrets) for item in data)
    return 0


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


# ---------------------------------------------------------------------------
# Pagination hints (stdlib only, no parsing of the upstream's pagination scheme)
# ---------------------------------------------------------------------------

_SAFE_NEXT_LINK = re.compile(r"^https?://", re.IGNORECASE)
_SCHEME_LIKE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _absolute_url(url: str, current_url: str) -> str | None:
    """Resolve `url` against `current_url`; None unless it is plain http(s).

    A relative ``next`` is allowed (it stays on the API's own host), but a
    ``javascript:``/``data:`` target must never reach the model as an
    instruction, and a credential-bearing URL must never be echoed back —
    that would put a token into the tool result.
    """
    resolved = urllib.parse.urljoin(current_url, url.strip())
    if not _SAFE_NEXT_LINK.match(resolved):
        return None
    parts = urllib.parse.urlsplit(resolved)
    if parts.username or parts.password:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def next_page_url(
    headers: dict[str, Any],
    body_text: str,
    current_url: str,
    query_params: frozenset[str] | None = None,
) -> str | None:
    """The upstream's own "there is more" signal, if it gave one.

    Four conventions cover nearly every REST API:

    - ``Link: <...>; rel="next"`` (RFC 8288 — GitHub, GitLab, many gateways)
    - ``X-Next-Page`` / ``X-Next-Url`` (GitLab, some proxies)
    - a JSON body field named ``next``/``next_page``/``nextPage``/``next_url``
      holding a URL string
    - a JSON body field named ``next_cursor``/``nextCursor``/``next``/
      ``next_page`` holding an opaque token, which is handed back as a ready
      argument: the caller's URL is reused with only that parameter swapped,
      so the tool's own parameter style (and credentials) are preserved.
      This branch needs ``query_params`` (the parameter names the tool
      declares): a token under a name nobody accepts is not callable, and a
      hint that cannot be followed is worse than none.

    Returns a URL to call for the page, or None. Nothing is fetched here.
    """
    link = _header_value(headers, "Link")
    if link:
        for part in link.split(","):
            target = part.split(";")
            if len(target) < 2:
                continue
            rels = " ".join(
                bit.split("=", 1)[1].strip().strip('"') for bit in target[1:] if "rel=" in bit
            )
            if "next" in rels.split():
                candidate = target[0].strip().strip("<>").strip()
                return _absolute_url(candidate, current_url)

    for header in ("X-Next-Url", "X-Next-Page", "X-Page-Next"):
        value = _header_value(headers, header)
        if value and header == "X-Next-Url":
            return _absolute_url(value, current_url)

    try:
        parsed = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    for key in ("next", "next_page", "nextPage", "next_url", "nextUrl"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip() and _SAFE_NEXT_LINK.match(value.strip()):
            return _absolute_url(value, current_url)

    if not query_params:
        return None
    current = urllib.parse.urlsplit(current_url)
    query = urllib.parse.parse_qsl(current.query, keep_blank_values=True)
    for key in ("nextCursor", "next_cursor", "next", "nextPage", "next_page", "cursor"):
        value = parsed.get(key)
        if not isinstance(value, (str, int)) or not str(value).strip():
            continue
        token = str(value).strip()
        if _SCHEME_LIKE.match(token):
            continue  # "javascript:...", "data:..." — not a cursor
        # A plain loop, not next()+generator: mypy strict mis-binds the
        # builtin name here and that is not worth a cast for three lines.
        param_name = None
        for candidate in _token_names(key):
            if candidate in query_params:
                param_name = candidate
                break
        if param_name is None:
            continue  # the API names a parameter this tool does not expose
        swapped: list[tuple[str, str]] = [(k, v) for k, v in query if k != param_name]
        swapped.append((param_name, token))
        return urllib.parse.urlunsplit(
            (current.scheme, current.netloc, current.path, urllib.parse.urlencode(swapped), "")
        )
    return None


def _token_names(key: str) -> list[str]:
    """Parameter names a cursor key may map to: ``nextCursor`` -> cursor.

    APIs name the value and the argument differently more often than not
    (``next_cursor`` in the body, ``cursor`` in the query string).
    """
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    stripped = re.sub(r"^next_?", "", snake)
    return [key, snake, stripped, re.sub(r"_([a-z])", lambda m: m.group(1).upper(), stripped)]


def page_argument(tool: dict[str, Any], page_url: str) -> dict[str, Any] | None:
    """Turn a next-page URL into the tool arguments that request it.

    Only parameters the tool actually declares are passed through (cursor,
    page, pageToken, limit...). Path variables are taken from the tool's own
    path template when it matches, so ``/items/{id}?page=2`` keeps ``id``.
    Returns None when the page URL says nothing the tool can accept — better
    no hint than a hint that cannot be called.
    """
    declared = {
        (param.get("name"), param.get("in"))
        for param in tool.get("_meta", {}).get("parameters", [])
        if isinstance(param, dict)
    }
    page = urllib.parse.urlsplit(page_url)
    arguments: dict[str, Any] = {
        name: value
        for name, value in urllib.parse.parse_qsl(page.query, keep_blank_values=True)
        if (name, "query") in declared
    }
    return arguments or None


def pagination_hint(tool: dict[str, Any], result: dict[str, Any], url: str) -> str:
    """One-line, model-facing "there is more" note; "" when there is not."""
    if result.get("status") != 200:
        return ""
    declared = {
        str(param.get("name"))
        for param in tool.get("_meta", {}).get("parameters", [])
        if isinstance(param, dict) and param.get("in") == "query"
    }
    page_url = next_page_url(
        result.get("headers") or {}, result.get("body") or "", url, frozenset(declared)
    )
    if page_url is None:
        return ""
    same_url = urllib.parse.urlsplit(page_url)
    current = urllib.parse.urlsplit(url)
    same_path = (same_url.scheme, same_url.netloc, same_url.path) == (
        current.scheme, current.netloc, current.path
    )
    arguments = page_argument(tool, page_url)
    if arguments and same_path:
        rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        return (
            f"[pagination] more results available — call {tool.get('name') or 'this tool'} with "
            f"{rendered} for the next page (nothing was fetched automatically)"
        )
    if not same_path:
        # A next URL on another path cannot be expressed as arguments for
        # THIS tool; saying "there is more" is still better than silence.
        return (
            "[pagination] the API reports another page at a different path than this "
            "tool exposes — use the tool for that path, or raise the page size"
        )
    return (
        "[pagination] the API reports another page, but this tool exposes no "
        "matching parameter for it — raise the limit/pagination argument manually"
    )
