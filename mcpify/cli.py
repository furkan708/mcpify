"""mcpify command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from . import __version__
from .config import api_sections, apply_to_namespace, load_config, resolve, validate
from .http_client import ResponseCache
from .spec import SpecError, discover_spec, iter_operations, load_spec, spec_servers
from .tools import AuthConfig, spec_to_tools

if TYPE_CHECKING:
    from types import SimpleNamespace

    from .http_client import OAuth2ClientCredentials

USE_COLOR = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and (
        os.name != "nt"
        or os.environ.get("WT_SESSION")
        or os.environ.get("ANSICON")
        or os.environ.get("TERM_PROGRAM") == "vscode"
    )
)


def _fail(message: str, code: int = 2) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def filter_tools(tools: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """Apply --tag / --include / --exclude / --read-only / --allow / --deny filters.

    Policy layering, weakest to strongest:
      1. --read-only keeps GET operations only (a heuristic, not a guarantee).
      2. --allow PATH_REGEX re-includes operations that --read-only dropped
         (e.g. read-style POST endpoints).
      3. --deny PATH_REGEX always excludes, and wins over everything.
    """
    include = [p.rstrip("/") for p in (args.include or [])]
    exclude = [p.rstrip("/") for p in (args.exclude or [])]
    allow = [re.compile(p) for p in (getattr(args, "allow", None) or [])]
    deny = [re.compile(p) for p in (getattr(args, "deny", None) or [])]

    def path_matches(path: str, patterns: list[str]) -> bool:
        return any(path == p or path.startswith(p + "/") for p in patterns)

    def regex_matches(path: str, patterns: list[re.Pattern[str]]) -> bool:
        return any(p.search(path) for p in patterns)

    kept = []
    for tool in tools:
        meta = tool["_meta"]
        read_only_dropped = args.read_only and meta["method"] != "GET"
        if read_only_dropped and not regex_matches(meta["path"], allow):
            continue
        if args.tag and args.tag not in (meta.get("tags") or []):
            continue
        if include and not path_matches(meta["path"], include):
            continue
        if exclude and path_matches(meta["path"], exclude):
            continue
        if regex_matches(meta["path"], deny):
            continue
        kept.append(tool)
    return kept


def _pick_server(servers: list[Any], choice: str) -> dict[str, Any]:
    """Resolve the --server flag to one of the spec's declared servers.

    Accepts a 1-based index or a name matched against each server's
    description (exact or whole-word, case-insensitive) or URL
    (substring). Fails with the full listing so the fix is obvious."""
    entries = [s if isinstance(s, dict) else {"url": str(s)} for s in servers]
    listing = "; ".join(
        f"{index + 1}: {entry.get('url', '')}"
        + (f" ({entry['description']})" if entry.get("description") else "")
        for index, entry in enumerate(entries)
    )
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(entries):
            return entries[index - 1]
        raise SpecError(
            f"--server {choice}: index out of range (the spec declares "
            f"{len(entries)} server(s): {listing})"
        )
    needle = choice.lower()
    for entry in entries:
        description = str(entry.get("description", "")).lower()
        url = str(entry.get("url", "")).lower()
        if needle == description or needle in description.split() or needle in url:
            return entry
    raise SpecError(f"--server {choice}: no server matches (declared: {listing})")


def _base_url(spec: dict[str, Any], override: str | None, server: str | None = None) -> str:
    if override:
        return override
    servers = spec.get("servers") or []
    if servers:
        entry = servers[0] if isinstance(servers[0], dict) else {"url": str(servers[0])}
        if server is not None:
            # explicit selection wins over the servers[0] default; --base-url
            # (checked above) still wins over --server
            entry = _pick_server(servers, server)
        url = str(entry.get("url", ""))
        # substitute server variables from their declared defaults
        variables = entry.get("variables")
        if isinstance(variables, dict):
            for name, var in variables.items():
                default = var.get("default") if isinstance(var, dict) else None
                if default is not None:
                    url = url.replace("{" + name + "}", str(default))
        if "{" in url:
            kalan = re.findall(r"\{([^{}]+)\}", url)
            raise SpecError(
                f"server URL variable(s) {kalan} have no default; "
                "pass --base-url to set the target explicitly"
            )
        if url and not url.startswith(("http://", "https://")):
            raise SpecError(
                f"server URL '{url}' is relative and cannot be called; "
                "pass --base-url with the absolute URL"
            )
        if url:
            return url
    raise SpecError(
        "no base URL: the spec declares no servers and --base-url was not given"
    )


def _add_serve_options(p: argparse.ArgumentParser, with_http: bool) -> None:
    """Attach the shared serve flags. `mcpify serve` and `mcpify try` use
    the same surface (minus --http*, which only makes sense for serve) so
    a config file or flag set means one thing everywhere."""
    p.add_argument("spec", nargs="?", help="path or URL of an OpenAPI document (bare origin auto-discovers; may come from the config)")
    p.add_argument("--base-url", help="API base URL (default: spec servers[0])")
    p.add_argument("--server", help="pick among the spec's declared servers by 1-based index or name (description/URL match), e.g. --server 2 or --server staging")
    p.add_argument("--name", default="mcpify", help="server name reported to clients")
    p.add_argument("--auth-env", help="env variable holding the API credential")
    p.add_argument(
        "--auth-style",
        choices=("bearer", "basic", "header", "query"),
        default=None,
        help="how to send the credential (default: auto-detected from the spec's "
        "security declarations, else bearer)",
    )
    p.add_argument("--auth-name", help="header or query parameter name for non-bearer auth")
    p.add_argument("--write-auth-env", default=None,
                   help="separate credential for NON-GET calls (POST/PUT/PATCH/DELETE): reads go out "
                   "on --auth-env, writes on this one — least-privilege keys instead of one shared "
                   "identity; style/name are inherited from --auth-style/--auth-name unless overridden")
    p.add_argument("--write-auth-style", choices=("bearer", "basic", "header", "query"), default=None,
                   help="style for the write credential (default: inherit --auth-style)")
    p.add_argument("--write-auth-name", default=None,
                   help="header/query name for the write credential (default: inherit --auth-name)")
    p.add_argument("--write-oauth2-token-url", default=None,
                   help="OAuth2 client-credentials token endpoint for NON-GET calls — a second "
                   "token flow so writes authenticate as a different OAuth2 client")
    p.add_argument("--write-oauth2-client-id-env", default=None,
                   help="env variable holding the write-flow OAuth2 client id")
    p.add_argument("--write-oauth2-client-secret-env", default=None,
                   help="env variable holding the write-flow OAuth2 client secret (optional for "
                   "public clients)")
    p.add_argument("--write-oauth2-scope", default=None,
                   help="space-separated scope(s) to request in the write flow")
    p.add_argument("--write-oauth2-client-auth", choices=("basic", "body"), default="basic",
                   help="client authentication style for the write-flow token endpoint")
    p.add_argument("--wait-on-429", type=float, default=0.0, metavar="SEC",
                   help="on 429, honor Retry-After (or the retry delay) once for "
                   "idempotent calls when the wait is at most SEC seconds (default: off)")
    p.add_argument("--oauth2-token-url", help="OAuth2 client-credentials token endpoint (RFC 6749)")
    p.add_argument("--oauth2-client-id-env", help="env variable holding the OAuth2 client id")
    p.add_argument("--oauth2-client-secret-env", help="env variable holding the OAuth2 client secret")
    p.add_argument("--oauth2-scope", help="space-separated scope(s) to request")
    p.add_argument("--oauth2-client-auth", choices=("basic", "body"), default="basic",
                   help="client authentication style for the token endpoint (default: basic)")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    p.add_argument("--tag", help="only operations with this tag")
    p.add_argument("--include", action="append", help="only these path prefixes (repeatable)")
    p.add_argument("--exclude", action="append", help="skip these path prefixes (repeatable)")
    p.add_argument("--read-only", action="store_true", help="expose only GET operations")
    p.add_argument(
        "--lazy",
        action="store_true",
        help="expose search/get-schema/call meta tools instead of the full listing "
        "(for very large APIs; saves the client's context window)",
    )
    p.add_argument(
        "--enable-preview",
        action="store_true",
        help="add mcpify_preview_request, a dry-run tool that shows the exact "
        "request a call would send (credentials masked, nothing sent)",
    )
    p.add_argument("--config", help="config file (auto-discovers .mcpify.toml/.yaml/.json in cwd when omitted)")
    p.add_argument("--env", help="environment section from the config ([envs.NAME])")
    p.add_argument("--verbose", action="store_true", help="log every request/response (status, timing, excerpt) to stderr")
    p.add_argument("--log-file", help="append the same log to a file (bodies truncated, credentials masked)")
    p.add_argument("--cache-ttl", type=float, default=0.0, metavar="SEC",
                   help="cache GET+200 responses in memory for this many seconds")
    p.add_argument("--retry", type=int, default=0, metavar="N",
                   help="retry idempotent requests (GET/PUT/DELETE) up to N times on 502/503/504 or connection failure (max 5)")
    p.add_argument("--retry-delay", type=float, default=1.0, metavar="SEC", help="wait between retries")
    p.add_argument("--strict", action="store_true", help="advertise every argument as required in the tool schemas")
    p.add_argument("--format", choices=("auto", "json", "xml"), default="auto",
                   help="auto: convert XML responses (per Content-Type) to JSON; xml: force conversion")
    p.add_argument("--allow", action="append", metavar="REGEX",
                   help="re-include operations dropped by --read-only (repeatable)")
    p.add_argument("--deny", action="append", metavar="REGEX",
                   help="never expose matching paths, overrides --allow (repeatable)")
    if with_http:
        p.add_argument("--http", metavar="[HOST:]PORT",
                       help="serve MCP over Streamable HTTP (POST JSON-RPC) instead of stdio; "
                       "bare PORT binds 127.0.0.1")
        p.add_argument("--http-token", help="require this bearer token on HTTP POSTs "
                       "(falls back to MCPIFY_HTTP_TOKEN)")
        p.add_argument("--metrics", metavar="[HOST:]PORT", default=None,
                       help="expose Prometheus metrics at http://HOST:PORT/metrics "
                       "(bare PORT binds 127.0.0.1; opt-in, zero overhead when off)")
        p.add_argument("--reload", action="store_true",
                       help="watch the spec file(s) and hot-reload the tool surface on change "
                       "(local paths only; broken specs keep the previous surface)")
        p.add_argument("--http-token-file", metavar="FILE", default=None,
                       help="TOML file mapping named bearer tokens to tool scopes "
                       "([tokens.NAME] token=... allow=[...] deny=[...]); deny wins")
        p.add_argument("--plugin", action="append", metavar="PATH", default=None,
                       help="load a Python plugin module (auth provider via AUTH, "
                       "request/response hooks via on_request/on_result); repeatable")

    p.add_argument("--audit-log", metavar="FILE", default=None,
                   help="append one JSON line per API call (tool, api, status, latency, "
                   "argument fingerprint) — arguments are never written raw")
    p.add_argument("--cache-warm", action="store_true",
                   help="with --cache-ttl: pre-call every GET tool that needs no arguments "
                   "right after startup (background threads)")
    p.add_argument("--fields", default=None, metavar="F1,F2",
                   help="response projection: keep only the requested fields (selected keys keep "
                   "their value, non-selected containers stay transparent, emptied containers "
                   "drop) — cuts tokens without losing the requested data")
    p.add_argument("--redact", default=None, metavar="F1,F2",
                   help="response redaction: values whose key names one of these fields are masked "
                   "with '***' at every level (case-insensitive) — the model never sees secrets")
    p.add_argument("--rate-limit", type=float, default=None, metavar="RPS",
                   help="client-side courtesy throttle: max requests/second toward the upstream "
                   "(every call waits for a slot; retries included)")
    p.add_argument("--otel", nargs="?", const="http://localhost:4318/v1/traces", default=None,
                   metavar="ENDPOINT",
                   help="export one OpenTelemetry span per upstream API call via OTLP/HTTP "
                   "(default endpoint: http://localhost:4318/v1/traces) — needs the optional "
                   "extra: pip install 'mcpify[otel]'")


def _load_plugins(paths: list[str]) -> list[Any]:
    """Load --plugin modules by path. Returns module objects; the
    convention is optional module-level `AUTH`, `on_request`, `on_result`."""
    import importlib.util

    modules: list[Any] = []
    for path in paths:
        if not os.path.isfile(path):
            _fail(f"plugin not found: {path}")
        spec = importlib.util.spec_from_file_location(f"mcpify_plugin_{len(modules)}_{os.path.basename(path)}", path)
        if spec is None or spec.loader is None:
            _fail(f"plugin cannot be loaded: {path}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as err:
            _fail(f"plugin {path} failed to load: {err}")
        modules.append(module)
        print(f"mcpify: plugin loaded: {path}", file=sys.stderr, flush=True)
    return modules


def _read_token_file(path: str) -> dict[str, dict[str, Any]]:
    """Parse --http-token-file: [tokens.NAME] tables -> compiled scopes."""
    from .config import _load_toml
    from .http_transport import compile_token_scopes

    if not os.path.isfile(path):
        _fail(f"token file not found: {path}")
    try:
        data = _load_toml(Path(path))
    except ValueError as err:
        _fail(f"token file {path}: {err}")
    tokens = data.get("tokens") or {}
    if not isinstance(tokens, dict) or not tokens:
        _fail(f"token file {path}: expected at least one [tokens.NAME] table")
    try:
        return compile_token_scopes(tokens)
    except ValueError as err:
        _fail(f"token file {path}: {err}")


def _start_cache_warm(server: Any) -> threading.Thread | None:
    """--cache-warm: pre-call GET tools that need no arguments.

    Only meaningful with --cache-ttl (an in-memory cache): the calls run
    in background threads right after startup so first agent hits are
    served warm. Tools with required parameters are skipped — filling
    them would mean guessing values, which is the agent's job.
    """
    if server.cache is None:
        print("note: --cache-warm has no effect without --cache-ttl", file=sys.stderr)
        return None
    warmable = [
        tool for tool in server.tools
        if tool["_meta"]["method"] == "GET" and not tool["inputSchema"].get("required")
    ]
    if not warmable:
        print("mcpify warm: no argument-free GET tools to warm", file=sys.stderr)
        return None

    def run_one(tool: dict[str, Any]) -> str | None:
        try:
            server.run_tool(tool["name"], {})
            return None
        except Exception as err:  # warm asla serve'i bozmaz
            return f"mcpify warm: {tool['name']} skipped ({err})"

    def warm() -> None:
        notes = [note for note in (run_one(tool) for tool in warmable) if note]
        for note in notes:
            print(note, file=sys.stderr, flush=True)
        print(f"mcpify warm: {len(warmable) - len(notes)}/{len(warmable)} tools pre-called",
              file=sys.stderr, flush=True)

    thread = threading.Thread(target=warm, daemon=True)
    thread.start()
    print(f"mcpify warm: pre-calling {len(warmable)} tools in background", file=sys.stderr, flush=True)
    return thread


def _wire_serve_extras(args: argparse.Namespace, server: Any) -> None:
    """Common serve/ui extras: audit log, tracing, plugins, cache warming."""
    from . import audit

    otel_endpoint = getattr(args, "otel", None)
    if otel_endpoint:
        from .otel import OtelError, enable_otel

        try:
            durum = enable_otel(str(otel_endpoint),
                                service_name=str(getattr(args, "name", None) or "mcpify"))
        except OtelError as err:
            _fail(str(err))
        print(f"mcpify: {durum}", file=sys.stderr, flush=True)
    if getattr(args, "audit_log", None):
        audit.enable(args.audit_log)
        print(f"mcpify: audit log -> {args.audit_log}", file=sys.stderr, flush=True)
    plugin_paths = getattr(args, "plugin", None) or []
    if plugin_paths:
        plugin_auth = None
        for module in _load_plugins([str(path) for path in plugin_paths]):
            candidate = getattr(module, "AUTH", None)
            if candidate is not None:
                plugin_auth = candidate
            if hasattr(module, "on_request"):
                server.request_hooks.append(module.on_request)
            if hasattr(module, "on_result"):
                server.result_hooks.append(module.on_result)
        if plugin_auth is not None:
            if server.auth is not None:
                print("note: plugin AUTH overrides the configured spec/CLI credential",
                      file=sys.stderr)
            server.auth = plugin_auth
    if getattr(args, "cache_warm", False):
        _start_cache_warm(server)


def _scopes_or_fail(args: argparse.Namespace) -> dict[str, dict[str, Any]] | None:
    """Compiled --http-token-file scopes, or None. Mutually exclusive
    with a plain --http-token (a scoped fleet deserves its own file)."""
    path = getattr(args, "http_token_file", None)
    if not path:
        return None
    if getattr(args, "http_token", None):
        _fail("--http-token and --http-token-file are mutually exclusive")
    return _read_token_file(str(path))


def _spec_mtime(path: str) -> int | None:
    """Last-modified stamp for local files; URLs and missing files are None."""
    if path.startswith(("http://", "https://")):
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _reload_once(
    paths: list[str],
    last: dict[str, int | None],
    apply_reload: Callable[[], None],
) -> dict[str, int | None]:
    """Rebuild the surface when any watched spec changed.

    A broken (half-saved) spec keeps the previous surface — the server
    never dies from a bad edit. Returns the stamp map for the next call.
    """
    current = {path: _spec_mtime(path) for path in paths}
    if current != last:
        try:
            apply_reload()
        except Exception as err:  # watch dongusu yasamaya devam eder
            print(f"mcpify reload: kept the previous surface ({err})", file=sys.stderr, flush=True)
        else:
            print("mcpify reload: tool surface refreshed", file=sys.stderr, flush=True)
    return current


def _start_reload(paths: list[str], apply_reload: Callable[[], None], poll: float = 1.0) -> None:
    """Watch local spec paths in a daemon thread (--reload)."""
    watched = [path for path in paths if _spec_mtime(path) is not None]
    skipped = [path for path in paths if _spec_mtime(path) is None]
    if not watched:
        print(
            "note: --reload found no local spec files to watch"
            + (f" (skipped URLs: {', '.join(skipped)})" if skipped else ""),
            file=sys.stderr,
        )
        return

    def watch() -> None:
        last = {path: _spec_mtime(path) for path in watched}
        while True:
            time.sleep(poll)
            last = _reload_once(watched, last, apply_reload)

    threading.Thread(target=watch, daemon=True).start()
    print(f"mcpify reload: watching {len(watched)} spec file(s)", file=sys.stderr, flush=True)


def _start_metrics(bind: str) -> None:
    """Expose /metrics on its own port (--metrics [HOST:]PORT)."""
    from . import metrics as metrics_mod
    from .http_transport import parse_http_bind

    try:
        host, port = parse_http_bind(bind)
    except ValueError as err:
        _fail(f"--metrics {bind}: {err}")
    metrics_mod.enable()
    httpd, _thread = metrics_mod.start_metrics_server(host, port)
    shown = "127.0.0.1" if str(httpd.server_address[0]) == "0.0.0.0" else str(httpd.server_address[0])  # noqa: S104 -- display
    print(f"mcpify: metrics at http://{shown}:{httpd.server_address[1]}/metrics", file=sys.stderr, flush=True)


def _rebuild_single_tools(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Fresh tool list for one spec (--reload in single-spec serve)."""
    from .spec import load_spec

    fresh = load_spec(args.spec)
    all_tools = spec_to_tools(fresh, strict=getattr(args, "strict", False))
    filtered = filter_tools(all_tools, args)
    if not filtered:
        raise ValueError("no operations matched after the change")
    return filtered


def _build_entries(args: argparse.Namespace, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand [apis.NAME] config sections into execution entries.

    Precedence per key: CLI flags > [apis.NAME] > [serve] > defaults —
    implemented by starting each API from the resolved [serve] settings,
    overlaying its section, and letting apply_to_namespace fill only the
    CLI attrs still at their default. Every execution knob lives on the
    entry so the aggregator can route without touching globals."""

    from .spec import SpecError, load_spec
    from .tools import spec_to_tools

    sections = api_sections(data)
    if getattr(args, "spec", None):
        _fail("pass either a positional spec or [apis.*] sections, not both")
    serve_settings = resolve(data, getattr(args, "env", None))
    serve_settings.pop("_env", None)

    entries: list[dict[str, Any]] = []
    for label, section in sections.items():
        settings = dict(serve_settings)
        settings.update(section if isinstance(section, dict) else {})
        ns = argparse.Namespace(**vars(args))
        ns.spec = None
        # smaller parsers (status) lack serve-only attrs; fill safe defaults
        for attr, default in (
            ("read_only", False), ("tag", None), ("include", None), ("exclude", None),
            ("allow", None), ("deny", None), ("strict", False), ("base_url", None),
            ("server", None), ("auth_env", None), ("auth_style", None), ("auth_name", None),
            ("oauth2_token_url", None), ("oauth2_client_id_env", None),
            ("oauth2_client_secret_env", None), ("oauth2_scope", None),
            ("oauth2_client_auth", "basic"), ("timeout", 30.0), ("cache_ttl", 0.0),
            ("retry", 0), ("retry_delay", 1.0), ("wait_on_429", 0.0),
            ("write_auth_env", None), ("write_auth_style", None), ("write_auth_name", None),
            ("fields", None), ("redact", None), ("rate_limit", None),
            ("write_oauth2_token_url", None), ("write_oauth2_client_id_env", None),
            ("write_oauth2_client_secret_env", None), ("write_oauth2_scope", None),
            ("write_oauth2_client_auth", "basic"),
        ):
            if not hasattr(ns, attr):
                setattr(ns, attr, default)
        apply_to_namespace(settings, ns)
        if not ns.spec:
            _fail(f"apis.{label}: missing required 'spec'")
        try:
            spec = load_spec(ns.spec)
        except SpecError as err:
            _fail(f"apis.{label}: {err}")
        tools = filter_tools(spec_to_tools(spec, strict=bool(ns.strict)), ns)
        if not tools:
            _fail(f"apis.{label}: no operations matched (0 tools)")
        try:
            base = _base_url(spec, ns.base_url, ns.server)
        except SpecError as err:
            _fail(f"apis.{label}: {err}")
        auth = _resolve_auth(ns, spec)
        if auth is None:
            hint = _auth_hint(spec)
            if hint:
                print(
                    f"note: [apis.{label}] declares authentication but no "
                    f"credential is configured — add {hint}",
                    file=sys.stderr,
                )
        write_auth = _resolve_write_auth(ns, auth)
        if write_auth is not None:
            print(
                f"mcpify [{label}]: auth split — reads use {ns.auth_env}, "
                f"writes use {getattr(write_auth, 'env_var', None) or ns.write_oauth2_token_url}",
                file=sys.stderr,
            )
        entries.append({
            "label": label,
            "spec": spec,
            "fields": _parse_fields(ns.fields, f"apis.{label}.fields"),
            "redact": _parse_fields(ns.redact, f"apis.{label}.redact"),
            "rate-limit": (float(ns.rate_limit) if ns.rate_limit else None),
            "spec_path": ns.spec,
            "base": base,
            "auth": auth,
            "write_auth": write_auth,
            "timeout": float(ns.timeout),
            "cache": ResponseCache(ns.cache_ttl) if ns.cache_ttl and ns.cache_ttl > 0 else None,
            "retry": int(ns.retry or 0),
            "retry_delay": float(ns.retry_delay),
            "wait_on_429": float(getattr(ns, "wait_on_429", 0.0) or 0.0),
            "tools": tools,
        })
    return entries


# Tool text is model-facing: phrases that read as instructions to a model
# (not as documentation to a human) get flagged by `mcpify doctor`.
def _parse_fields(raw: Any, where: str) -> frozenset[str] | None:
    from .http_client import parse_fields

    if raw is None or raw == "":
        return None
    try:
        return parse_fields(raw)
    except ValueError as err:
        _fail(f"{where}: {err}")


def _resolve_write_auth(
    args: argparse.Namespace,
    main_auth: AuthConfig | OAuth2ClientCredentials | None,
) -> AuthConfig | OAuth2ClientCredentials | None:
    """Build the dedicated NON-GET credential, or None.

    Two credential kinds, one per flag family (mutually exclusive):
    --write-auth-env for a second static key, --write-oauth2-token-url
    for a second OAuth2 client-credentials flow. Reads and writes then
    carry different server-side identities — the blast radius of a read
    call is the read credential's, not the write credential's. Static
    style/name inherit from the primary static credential unless
    overridden; the OAuth2 write flow is configured independently.
    """
    from .http_client import OAuth2ClientCredentials

    token_url = getattr(args, "write_oauth2_token_url", None)
    env = getattr(args, "write_auth_env", None)
    if token_url and env:
        _fail("--write-auth-env and --write-oauth2-token-url are mutually exclusive: "
              "pick one credential kind for writes")
    if token_url:
        client_id_env = getattr(args, "write_oauth2_client_id_env", None)
        if not client_id_env:
            _fail("--write-oauth2-token-url requires --write-oauth2-client-id-env")
        return OAuth2ClientCredentials(
            token_url,
            client_id_env,
            client_secret_env=getattr(args, "write_oauth2_client_secret_env", None),
            scope=getattr(args, "write_oauth2_scope", None),
            client_auth=getattr(args, "write_oauth2_client_auth", "basic"),
            timeout=getattr(args, "timeout", 30.0),
        )
    if not env:
        return None
    style = getattr(args, "write_auth_style", None)
    name = getattr(args, "write_auth_name", None)
    if style is None and isinstance(main_auth, AuthConfig):
        style = main_auth.style
        name = name or main_auth.name
    return AuthConfig(env, style or "bearer", name)


def _apply_tool_text(tools: list[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply [tool-text.TOOL] description overrides from .mcpify.toml.

    Spec authors write for humans; the operator vouches for the surface,
    so the operator gets the last word on tool descriptions. Tool dict
    objects are shared with the server's by_name index, so in-place
    mutation reaches tools/list, search and get_schema everywhere.
    Overrides key the FINAL tool name (aggregation prefixes included).
    """
    overrides = data.get("tool-text") if isinstance(data, dict) else None
    if not isinstance(overrides, dict) or not overrides:
        return tools
    by_name = {tool["name"]: tool for tool in tools}
    for name, section in overrides.items():
        tool = by_name.get(str(name))
        if tool is None:
            print(
                f"note: [tool-text.{name}] matches no tool — overrides use the final tool "
                "name (check `mcpify list`)",
                file=sys.stderr,
            )
            continue
        description = section.get("description") if isinstance(section, dict) else None
        if isinstance(description, str):
            tool["description"] = description
    return tools


_INSTRUCTION_TEXT_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)"
    r"|\byou\s+(?:must|should|are)\b"
    r"|\bdo\s+not\s+(?:tell|reveal|mention|disclose)\b"
    r"|\bkeep\s+(?:this|it|these)\s+(?:secret|confidential|private)\b"
    r"|\bsystem\s+prompt\b"
    r"|\breveal\s+(?:your|the|its)\b"
    r"|\bnew\s+instructions?\b",
    re.IGNORECASE,
)
_LONG_DESCRIPTION_CHARS = 1200


def _tool_text_issues(spec: dict[str, Any]) -> tuple[int, int]:
    """(instruction_like, overlong) counts across operation summaries and
    descriptions — the 'spec authors become prompt authors' audit."""
    instruction = 0
    overlong = 0
    for _method, _path, operation in iter_operations(spec):
        text = f"{operation.get('summary') or ''} {operation.get('description') or ''}"
        if _INSTRUCTION_TEXT_RE.search(text):
            instruction += 1
        if len(operation.get("description") or "") > _LONG_DESCRIPTION_CHARS:
            overlong += 1
    return instruction, overlong


def _auth_hint(spec: dict[str, Any] | None) -> str | None:
    """Exact, copy-pasteable serve flags for the spec's declared auth."""
    if spec is None:
        return None
    from .tools import detect_auth

    detected = detect_auth(spec)
    if detected is None:
        return None
    style = detected["style"]
    if style == "bearer":
        return "--auth-env API_TOKEN"
    if style == "basic":
        return "--auth-env API_CREDENTIALS  # holds 'username:password'"
    if style == "header":
        return f"--auth-env API_KEY --auth-style header --auth-name {detected['name']}"
    return f"--auth-env API_KEY --auth-style query --auth-name {detected['name']}"


def _resolve_auth(args: argparse.Namespace | SimpleNamespace, spec: dict[str, Any] | None = None) -> AuthConfig | OAuth2ClientCredentials | None:
    """Build the auth provider from flags. OAuth2 (client-credentials) and
    the static --auth-env credential are mutually exclusive modes. With
    --auth-env and no explicit style, the spec's security declarations
    pick the style (bearer/basic/header-name/query-name) — zero-config
    auth in the common case; plain bearer stays the final fallback."""
    from .http_client import OAuth2ClientCredentials
    from .tools import detect_auth

    token_url = getattr(args, "oauth2_token_url", None)
    if token_url:
        if getattr(args, "auth_env", None):
            _fail(
                "--auth-env and --oauth2-token-url are mutually exclusive: "
                "pick one credential mode per server"
            )
        client_id_env = getattr(args, "oauth2_client_id_env", None)
        if not client_id_env:
            _fail("--oauth2-token-url requires --oauth2-client-id-env")
        return OAuth2ClientCredentials(
            token_url,
            client_id_env,
            client_secret_env=getattr(args, "oauth2_client_secret_env", None),
            scope=getattr(args, "oauth2_scope", None),
            client_auth=getattr(args, "oauth2_client_auth", "basic"),
            timeout=args.timeout,
        )
    if getattr(args, "auth_env", None):
        style = getattr(args, "auth_style", None)
        name = getattr(args, "auth_name", None)
        if style is None:
            detected = detect_auth(spec) if spec is not None else None
            if detected is not None and detected["style"] in ("bearer", "basic", "header", "query"):
                style = detected["style"]
                name = name or detected.get("name")
                print(
                    f"auth: style auto-detected from the spec -> {style}"
                    + (f" ({name})" if name else ""),
                    file=sys.stderr,
                )
        return AuthConfig(args.auth_env, style or "bearer", name)
    return None


def _run_output_server(rest: list[str]) -> None:
    """`mcpify output-server SPEC -o FILE [--force] [-- <any serve flags>]`.

    Everything after the recognized options is validated against the real
    serve parser, then baked verbatim into the generated script — so any
    current or future serve flag works without this command tracking it."""
    parser = argparse.ArgumentParser(
        prog="mcpify output-server",
        description="Bake a `mcpify serve` command into a shareable standalone script. "
        "Serve flags go after -- (e.g. -- --read-only --timeout 5); the target "
        "environment needs mcpify-openapi installed.",
    )
    parser.add_argument("spec", help="path or URL of the OpenAPI document (a local file is embedded)")
    parser.add_argument("-o", "--output", required=True, help="script file to write")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args, extras = parser.parse_known_args(rest)
    # argparse version differences: some Pythons keep the "--" separator in
    # extras, some strip it. Normalize so validation sees only real flags.
    if extras and extras[0] == "--":
        extras = extras[1:]

    bare = argparse.ArgumentParser(add_help=False)
    _add_serve_options(bare, with_http=True)
    known, unknown = bare.parse_known_args(extras)
    # unknown flags first: an unknown option's value can be misparsed as a
    # positional by parse_known_args, and the flag typo is the real problem
    if unknown:
        _fail(f"unknown serve flag(s) after --: {' '.join(unknown)}")
    if known.spec:
        _fail("the baked flags must not contain a spec — the spec is passed once, before --")

    from .standalone import MIN_MCPIFY, generate

    try:
        warnings = generate(args.spec, args.output, extras, force=args.force)
    except ValueError as err:
        _fail(str(err))
    except OSError as err:
        _fail(f"cannot write {args.output}: {err}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"wrote {args.output}")
    print(f"next: python3 {args.output}   (requires mcpify-openapi >= {MIN_MCPIFY})")


def _pick_probe_operation(spec: dict[str, Any]) -> tuple[str, str] | None:
    """Backward-compat alias; the implementation lives in mcpify.probe."""
    from .probe import pick_probe_operation

    return pick_probe_operation(spec)


def _doctor_probe(spec: dict[str, Any], base_url: str | None, timeout: float,
                  auth: Any = None, fail_on_http_error: bool = False) -> dict[str, Any]:
    """Backward-compat alias; the implementation lives in mcpify.probe."""
    from .probe import run_probe

    return run_probe(spec, base_url, timeout, auth=auth,
                     fail_on_http_error=fail_on_http_error)


def _policy_note(fields: Any, redact: Any, rate_limit: Any) -> str:
    """Human-readable token-policy summary for `status` output."""
    pieces: list[str] = []
    if fields:
        names = sorted(fields) if not isinstance(fields, str) else fields
        pieces.append("fields=" + (",".join(names) if not isinstance(names, str) else names))
    if redact:
        names = sorted(redact) if not isinstance(redact, str) else redact
        pieces.append("redact=" + (",".join(names) if not isinstance(names, str) else names))
    if rate_limit:
        pieces.append(f"rate-limit={rate_limit:g}")
    return " · ".join(pieces)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "output-server":
        _run_output_server(argv[1:])
        return
    parser = argparse.ArgumentParser(
        prog="mcpify",
        description=(
            "Turn any OpenAPI REST API into an MCP server so AI agents "
            "(Claude Code, Cursor, ...) can call it — zero dependencies."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="preview the tools that would be generated")
    p_list.add_argument("spec", nargs="?", help="path or URL of an OpenAPI document "
                        "(optional when the config sets `spec` or [apis.*] sections)")
    p_list.add_argument("--tag", help="only operations with this tag")
    p_list.add_argument("--include", action="append", help="only these path prefixes (repeatable)")
    p_list.add_argument("--exclude", action="append", help="skip these path prefixes (repeatable)")
    p_list.add_argument("--read-only", action="store_true", help="only GET operations")
    p_list.add_argument("--allow", action="append", metavar="REGEX",
                        help="re-include operations dropped by --read-only (repeatable)")
    p_list.add_argument("--deny", action="append", metavar="REGEX",
                        help="never expose matching paths, overrides --allow (repeatable)")
    p_list.add_argument("--cost", action="store_true",
                        help="estimate the context cost of the surface (~4 chars/token) — "
                        "the price every agent pays in every tools/list")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.add_argument("--lazy", action="store_true",
                        help="with --cost: also price the lazy (search-then-call) surface")
    p_list.add_argument("--config", help="config file (auto-discovers .mcpify.toml/.yaml/.json in cwd when omitted)")

    p_serve = sub.add_parser("serve", help="start the MCP server on stdio (or --http for Streamable HTTP)")
    _add_serve_options(p_serve, with_http=True)

    p_try = sub.add_parser("try", help="interactive REPL: call the tools in your terminal, no agent client needed")
    _add_serve_options(p_try, with_http=False)

    # Help-only stub: real invocations are dispatched by _run_output_server
    # before this parser is built (its flags must validate against the serve
    # surface, which argparse cannot express in one parser). Keeping the
    # entry here makes `mcpify --help` list the command.
    p_gen = sub.add_parser(
        "output-server",
        help="bake a serve command into a standalone shareable script (serve flags after --)",
        description="Bake a `mcpify serve` command into a shareable standalone script. "
        "Serve flags go after -- (e.g. -- --read-only --timeout 5); the target "
        "environment needs mcpify-openapi installed.",
    )
    p_gen.add_argument("spec", help="path or URL of the OpenAPI document (a local file is embedded)")
    p_gen.add_argument("-o", "--output", required=True, help="script file to write")
    p_gen.add_argument("--force", action="store_true", help="overwrite an existing file")

    p_init = sub.add_parser("init", help="interactive wizard that writes a .mcpify.toml config")
    p_init.add_argument("--config", default=".mcpify.toml", help="config file to write (default: .mcpify.toml)")
    p_init.add_argument("--probe", action="store_true",
                        help="after writing the config, run a live pre-flight probe against the "
                        "configured API (with the configured credential); exits 1 when unreachable")
    p_init.add_argument("--spec", help="prefill the spec path/URL (skips that question)")
    p_init.add_argument("--base-url", help="prefill the base URL (skips that question)")

    p_status = sub.add_parser("status", help="check that the API behind a spec is reachable")
    p_status.add_argument("spec", nargs="?", help="path or URL of an OpenAPI document (bare origin auto-discovers)")
    p_status.add_argument("--config", help="config file (auto-discovered when omitted)")
    p_status.add_argument("--env", help="environment section from the config ([envs.NAME])")
    p_status.add_argument("--base-url", help="override the API base URL")
    p_status.add_argument("--server", help="pick among the spec's declared servers (index or name)")
    p_status.add_argument("--timeout", type=float, default=10.0, help="probe timeout seconds")
    p_status.add_argument("--json", action="store_true", help="machine-readable output")
    p_status.add_argument("--fields", default=None, metavar="F1,F2",
                          help="report the configured response projection")
    p_status.add_argument("--redact", default=None, metavar="F1,F2",
                          help="report the configured secret masking")
    p_status.add_argument("--rate-limit", type=float, default=None, metavar="RPS",
                          help="report the configured upstream throttle")

    p_ui = sub.add_parser("ui", help="operations dashboard: tool explorer, masked logs, health, config editor")
    _add_serve_options(p_ui, with_http=True)

    p_mock = sub.add_parser("mock", help="serve a fake API generated from the spec (schema-shaped responses)")
    p_mock.add_argument("spec", help="path or URL of an OpenAPI document")
    p_mock.add_argument("--http", metavar="[HOST:]PORT", default="8000",
                        help="bind address (default: 127.0.0.1:8000; '*' or 0.0.0.0 exposes)")
    p_mock.add_argument("--delay-ms", type=int, default=0, help="artificial latency per response (default: 0)")
    p_mock.add_argument("--timeout", type=float, default=15.0, help="spec fetch timeout seconds (default: 15)")

    p_diff = sub.add_parser("diff", help="compare two spec versions from the tool-surface view")
    p_diff.add_argument("old", help="previous spec (path or URL)")
    p_diff.add_argument("new", help="current spec (path or URL)")
    p_diff.add_argument("--json", action="store_true", help="machine-readable report")
    p_diff.add_argument("--fail-on-breaking", action="store_true",
                        help="exit 1 when breaking changes exist (CI gate)")
    p_diff.add_argument("--probe", action="store_true",
                        help="live check of the NEW spec: one argument-free GET against the API "
                        "before you adopt it; unreachable exits 2")
    p_diff.add_argument("--base-url", default=None, help="override the base URL to probe")
    p_diff.add_argument("--auth-env", default=None, help="probe the NEW spec with this credential")
    p_diff.add_argument("--auth-style", default=None, help="bearer|basic|header|query (auto-detected when omitted)")
    p_diff.add_argument("--auth-name", default=None, help="header/query name for non-bearer styles")
    p_diff.add_argument("--timeout", type=float, default=10.0, help="probe timeout seconds")
    p_diff.add_argument("--fail-on-http-error", action="store_true",
                        help="4xx/5xx probe responses count as a failed probe (exit 2)")

    sub.add_parser(
        "config-schema",
        help="print the JSON Schema for .mcpify.toml (wire it into your editor)",
    )

    p_doctor = sub.add_parser("doctor", help="inspect a spec and report problems")
    p_doctor.add_argument("spec", help="path or URL of an OpenAPI document")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable output")
    p_doctor.add_argument("--probe", action="store_true",
                          help="live pre-flight: after the static report, call one safe GET "
                          "operation against the API and report reachability")
    p_doctor.add_argument("--base-url", default=None, help="override the base URL to probe")
    p_doctor.add_argument("--timeout", type=float, default=10.0, help="probe timeout seconds")
    p_doctor.add_argument("--auth-env", default=None,
                          help="probe WITH the credential (env variable) — proves auth works "
                          "end-to-end before you serve, not just that the host answers")
    p_doctor.add_argument("--auth-style", default=None, help="bearer|basic|header|query (auto-detected when omitted)")
    p_doctor.add_argument("--auth-name", default=None, help="header/query name for non-bearer styles")
    p_doctor.add_argument("--fail-on-http-error", action="store_true",
                          help="CI gate: 4xx/5xx probe responses count as a failed pre-flight "
                          "(exit 1); default counts only connection failures")

    args = parser.parse_args(argv)
    tools: list[dict[str, Any]] = []  # status computes its own count

    config_path = None
    config_data: dict[str, Any] = {}
    if args.command in ("list", "serve", "status", "try", "ui"):
        try:
            config_path, config_data = load_config(getattr(args, "config", None))
            if config_path is not None:
                for problem in validate(config_data):
                    print(f"config warning: {problem}", file=sys.stderr)
                settings = resolve(config_data, getattr(args, "env", None))
                apply_to_namespace(settings, args)
                if not api_sections(config_data) and settings.get("spec") and getattr(args, "spec", None) is None:
                    args.spec = settings["spec"]
                tag = f" (env: {settings['_env']})" if settings.get("_env") else ""
                print(f"config: {config_path}{tag}", file=sys.stderr)
        except ValueError as err:
            _fail(str(err))

    entries: list[dict[str, Any]] | None = None
    if args.command in ("list", "serve", "try", "ui") and api_sections(config_data):
        entries = _build_entries(args, config_data)

    if args.command in ("list", "serve", "try", "ui") and entries is None:
        if getattr(args, "spec", None) is None:
            _fail("a spec path or URL is required (or set `spec` in the config)")
        spec_arg = args.spec
        if spec_arg.startswith(("http://", "https://")):
            from urllib.parse import urlparse

            if urlparse(spec_arg).path in ("", "/"):
                try:
                    args.spec, _hint = discover_spec(spec_arg)
                    print(f"discovered: {args.spec}", file=sys.stderr)
                except SpecError as err:
                    _fail(str(err))
        try:
            spec = load_spec(args.spec)
        except SpecError as err:
            _fail(str(err))
        all_tools = spec_to_tools(spec, strict=getattr(args, "strict", False))
        tools = _apply_tool_text(filter_tools(all_tools, args), config_data)
        if not tools:
            _fail("no operations matched (the API would expose 0 tools)")

    if args.command == "config-schema":
        from importlib.resources import files

        print(files("mcpify").joinpath("config-schema.json").read_text(encoding="utf-8"), end="")

    elif args.command == "diff":
        from .diff import diff_specs, render

        try:
            eski = load_spec(args.old)
            yeni = load_spec(args.new)
        except SpecError as err:
            _fail(str(err))
        rapor = diff_specs(eski, yeni)
        from .tools import surface_cost_tokens as _surface_cost

        eski_t = _surface_cost(spec_to_tools(eski))
        yeni_t = _surface_cost(spec_to_tools(yeni))
        rapor["surface_cost_tokens"] = {"old": eski_t, "new": yeni_t}
        if args.probe:
            from .probe import run_probe

            probe_base = args.base_url or next(
                (s for s in spec_servers(yeni) if s.startswith(("http://", "https://"))), None)
            rapor["probe"] = run_probe(yeni, probe_base, args.timeout,
                                       auth=_resolve_auth(args, yeni),
                                       fail_on_http_error=args.fail_on_http_error)
        if args.json:
            print(json.dumps(rapor, indent=2))
        else:
            print(render(rapor))
            delta = yeni_t - eski_t
            pct = (delta / eski_t * 100) if eski_t else 0.0
            not_ = f"\033[2m surface cost: ~{eski_t:,} → ~{yeni_t:,} tokens ({pct:+.1f}%)\033[0m" if USE_COLOR else \
                f"surface cost: ~{eski_t:,} → ~{yeni_t:,} tokens ({pct:+.1f}%)"
            print(not_)
            if args.probe:
                pr = rapor["probe"]
                if pr["ok"]:
                    tag = ", authenticated" if pr.get("authenticated") else ""
                    print(f"probe:    {pr['method']} {pr['path']} → {pr['status']} reachable "
                          f"({pr['latency_seconds']:.2f}s{tag})")
                else:
                    print(f"probe:    {pr.get('error', 'failed')}")
        if args.probe and not rapor["probe"]["ok"]:
            sys.exit(2)  # the NEW spec's API does not answer: infra problem, not a breaking diff
        if args.fail_on_breaking and rapor["breaking"]:
            sys.exit(1)

    elif args.command == "list":
        from .tools import surface_cost_tokens, tool_cost_tokens

        want_cost = bool(getattr(args, "cost", False))
        if entries is not None:
            # multi-API preview: one line per API, the whole surface priced
            if args.json:
                rows = []
                for entry_item in entries:
                    for tool_item in entry_item["tools"]:
                        row = {"api": entry_item["label"],
                               "name": tool_item["name"],
                               "method": tool_item["_meta"]["method"],
                               "path": tool_item["_meta"]["path"],
                               "deprecated": tool_item["_meta"]["deprecated"],
                               "description": tool_item["description"]}
                        if want_cost:
                            row["cost_tokens"] = tool_cost_tokens(tool_item)
                        rows.append(row)
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return
            def _dim(s: str) -> str:
                return f"\033[2m{s}\033[0m" if USE_COLOR else s
            def _cyan(s: str) -> str:
                return f"\033[36m{s}\033[0m" if USE_COLOR else s
            all_tools = [tool_item for entry_item in entries for tool_item in entry_item["tools"]]
            print(_dim(f"mcpify: {len(entries)} APIs, {len(all_tools)} tools"))
            print(_dim("─" * 78))
            for entry_item in entries:
                cost_note = (f"  ~{surface_cost_tokens(entry_item['tools']):,} tok") if want_cost else ""
                print(_dim(f"[{entry_item['label']}] {len(entry_item['tools'])} tools{cost_note}"))
                for tool_item in entry_item["tools"]:
                    meta = tool_item["_meta"]
                    body = _dim(" +body") if meta["has_body"] else ""
                    print(f"  {_cyan(tool_item['name']):36} {meta['method']:8} {meta['path']}{body}")
            print(_dim("─" * 78))
            if want_cost:
                total = surface_cost_tokens(all_tools)
                print(_dim(f"surface cost: ~{total:,} tokens (~{total * 4 / 1024:.0f} KB) — "
                           "paid in EVERY tools/list by every agent; cut it with --tag/--include/--exclude/--lazy"))
            return
        if args.json:
            rows = []
            for t in tools:
                row = {
                    "name": t["name"],
                    "method": t["_meta"]["method"],
                    "path": t["_meta"]["path"],
                    "deprecated": t["_meta"]["deprecated"],
                    "description": t["description"],
                }
                if want_cost:
                    row["cost_tokens"] = tool_cost_tokens(t)
                rows.append(row)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        def bold(s: str) -> str:
            return f"\033[1m{s}\033[0m" if USE_COLOR else s
        def dim(s: str) -> str:
            return f"\033[2m{s}\033[0m" if USE_COLOR else s
        def green(s: str) -> str:
            return f"\033[32m{s}\033[0m" if USE_COLOR else s
        def cyan(s: str) -> str:
            return f"\033[36m{s}\033[0m" if USE_COLOR else s
        print(bold(f"mcpify: {len(tools)} tools from {args.spec}"))
        print(dim("─" * 78))
        for tool in tools:
            meta = tool["_meta"]
            body = dim(" +body") if meta["has_body"] else ""
            deprecated_badge = dim(" DEPRECATED") if meta["deprecated"] else ""
            desc = tool["description"]
            desc = desc[len(meta["method"]) + 3:] if desc.startswith("[") else desc
            print(
                f"  {cyan(tool['name']):36} {green(meta['method']):8} "
                f"{meta['path']}{body}{deprecated_badge}"
            )
            if desc:
                cost = f" {dim(f'~{tool_cost_tokens(tool)} tok')}" if want_cost else ""
                print(f"  {'':36} {dim(desc[:90])}{cost}")
        print(dim("─" * 78))
        if want_cost and getattr(args, "lazy", False):
            from .api_server import CALL_TOOL, SCHEMA_TOOL, SEARCH_TOOL, build_meta_tools

            listed_names = {SEARCH_TOOL, SCHEMA_TOOL, CALL_TOOL}
            listed_meta = [m for m in build_meta_tools() if m["name"] in listed_names]
            lazy_total = surface_cost_tokens(listed_meta)
            total_now = surface_cost_tokens(tools)
            print(dim(f"lazy surface: ~{lazy_total:,} tokens — three meta tools replace "
                      f"the full list (full surface: ~{total_now:,} tokens); enable with --lazy"))
        if want_cost:
            total = surface_cost_tokens(tools)
            print(dim(f"surface cost: ~{total:,} tokens (~{total * 4 / 1024:.0f} KB) — "
                      "paid in EVERY tools/list by every agent; cut it with "
                      "--tag/--include/--exclude/--lazy"))
        print(dim(f"serve it: mcpify serve {args.spec}"))

    elif args.command == "serve" and entries is not None:
        from .http_client import set_logging

        set_logging(args.verbose, args.log_file)
        from .aggregate import AggregatedServer

        agg_server = AggregatedServer(
            entries,
            server_name=args.name,
            lazy=args.lazy,
            enable_preview=args.enable_preview,
            response_format=args.format,
        )
        agg_server.ui_config_defaults = dict(config_data.get("serve") or {})
        if getattr(args, "metrics", None):
            _start_metrics(args.metrics)
        if getattr(args, "reload", False):
            def reload_aggregated() -> None:
                agg_server.reload_entries(_build_entries(args, config_data))
                _apply_tool_text(agg_server.tools, config_data)

            _start_reload(
                [str(entry.get("spec_path", "")) for entry in entries],
                reload_aggregated,
            )
        _apply_tool_text(agg_server.tools, config_data)
        _wire_serve_extras(args, agg_server)
        if getattr(args, "http", None):
            from .http_transport import parse_http_bind, serve_http

            try:
                host, port = parse_http_bind(args.http)
            except ValueError as err:
                _fail(f"--http {args.http}: {err}")
            token = args.http_token or os.environ.get("MCPIFY_HTTP_TOKEN") or None
            serve_http(agg_server, host, port, token, token_scopes=_scopes_or_fail(args))
            return
        labels = ", ".join(entry["label"] for entry in entries)
        print(
            f"mcpify: serving {len(entries)} APIs ({len(agg_server.tools)} tools): {labels}"
            " — agents see one tool surface",
            file=sys.stderr,
        )
        agg_server.serve()

    elif args.command == "serve":
        from .http_client import set_logging

        set_logging(args.verbose, args.log_file)
        auth = _resolve_auth(args, spec)
        if auth is None:
            hint = _auth_hint(spec)
            if hint:
                print(
                    "note: this API declares authentication but no credential is "
                    f"configured — add {hint}",
                    file=sys.stderr,
                )
        write_auth = _resolve_write_auth(args, auth)
        if write_auth is not None:
            print(
                f"mcpify: auth split — reads use {args.auth_env}, "
                f"writes use {getattr(write_auth, 'env_var', None) or args.write_oauth2_token_url}",
                file=sys.stderr,
            )
        from .api_server import ApiServer

        try:
            base = _base_url(spec, args.base_url, getattr(args, "server", None))
        except SpecError as err:
            _fail(str(err))
        server = ApiServer(
            spec,
            base,
            server_name=args.name,
            auth=auth,
            write_auth=write_auth,
            fields=_parse_fields(getattr(args, "fields", None), "--fields"),
            redact=_parse_fields(getattr(args, "redact", None), "--redact"),
            rate_limit=getattr(args, "rate_limit", None),
            timeout=args.timeout,
            tools=tools,
            lazy=args.lazy,
            enable_preview=args.enable_preview,
            cache_ttl=args.cache_ttl,
            retry=args.retry,
            retry_delay=args.retry_delay,
            response_format=args.format,
            wait_on_429=getattr(args, "wait_on_429", 0.0),
        )
        server.ui_config_defaults = dict(config_data.get("serve") or {})
        if getattr(args, "metrics", None):
            _start_metrics(args.metrics)
        if getattr(args, "reload", False):
            def reload_single() -> None:
                server.reload_tools(_apply_tool_text(_rebuild_single_tools(args), config_data))

            _start_reload([args.spec], reload_single)
        _apply_tool_text(server.tools, config_data)
        _wire_serve_extras(args, server)
        if getattr(args, "http", None):
            from .http_transport import parse_http_bind, serve_http

            try:
                host, port = parse_http_bind(args.http)
            except ValueError as err:
                _fail(f"--http {args.http}: {err}")
            token = args.http_token or os.environ.get("MCPIFY_HTTP_TOKEN") or None
            serve_http(server, host, port, token, token_scopes=_scopes_or_fail(args))
            return
        sekil = "lazy surface (search + schema + call + health)" if args.lazy else f"{len(tools)} tools"
        ekstra = []
        if args.cache_ttl:
            ekstra.append(f"cache {args.cache_ttl:g}s")
        if args.retry:
            ekstra.append(f"retry {args.retry}x{args.retry_delay:g}s")
        if getattr(args, "wait_on_429", 0.0):
            ekstra.append(f"429-wait {args.wait_on_429:g}s")
        if args.strict:
            ekstra.append("strict")
        if args.format != "auto":
            ekstra.append(f"format={args.format}")
        print(
            f"mcpify: serving {sekil} from {args.spec} -> {base}"
            + (" [" + ", ".join(ekstra) + "]" if ekstra else ""),
            file=sys.stderr,
        )
        server.serve()

    elif args.command == "try" and entries is not None:
        from .http_client import set_logging

        set_logging(args.verbose, getattr(args, "log_file", None))
        from .aggregate import AggregatedServer

        agg_server = AggregatedServer(
            entries,
            server_name=args.name,
            lazy=args.lazy,
            enable_preview=args.enable_preview,
            response_format=args.format,
        )
        print(
            f"mcpify try [{args.name}]: {len(entries)} APIs, {len(agg_server.tools)} tools "
            "(calls are REAL requests — same path an agent would take)",
            file=sys.stderr,
        )
        from .repl import run as run_repl

        run_repl(agg_server)

    elif args.command == "ui" and entries is not None:
        from .aggregate import AggregatedServer
        from .http_transport import parse_http_bind
        from .ui import serve_ui

        agg_server = AggregatedServer(
            entries,
            server_name=args.name,
            lazy=args.lazy,
            enable_preview=args.enable_preview,
            response_format=args.format,
        )
        agg_server.ui_config_defaults = dict(config_data.get("serve") or {})
        if getattr(args, "metrics", None):
            _start_metrics(args.metrics)
        if getattr(args, "reload", False):
            def reload_aggregated_ui() -> None:
                agg_server.reload_entries(_build_entries(args, config_data))
                _apply_tool_text(agg_server.tools, config_data)

            _start_reload(
                [str(entry.get("spec_path", "")) for entry in entries],
                reload_aggregated_ui,
            )
        _apply_tool_text(agg_server.tools, config_data)
        _wire_serve_extras(args, agg_server)
        if getattr(args, "http_token_file", None):
            print("note: --http-token-file scopes apply to the MCP transport (mcpify serve --http); "
                  "the dashboard keeps its single --http-token", file=sys.stderr)
        try:
            host, port = parse_http_bind(args.http or "8787")
        except ValueError as err:
            _fail(f"--http {args.http}: {err}")
        token = args.http_token or os.environ.get("MCPIFY_HTTP_TOKEN") or None
        serve_ui(agg_server, host, port, token, config_path,
                 reload_cb=reload_aggregated_ui if getattr(args, "reload", False) else None)

    elif args.command == "ui":
        from .http_client import set_logging

        set_logging(args.verbose, getattr(args, "log_file", None))
        from .http_transport import parse_http_bind
        from .ui import serve_ui

        auth = _resolve_auth(args, spec)
        write_auth = _resolve_write_auth(args, auth)
        if write_auth is not None:
            print(
                f"mcpify: auth split — reads use {args.auth_env}, "
                f"writes use {getattr(write_auth, 'env_var', None) or args.write_oauth2_token_url}",
                file=sys.stderr,
            )
        from .api_server import ApiServer

        try:
            base = _base_url(spec, args.base_url, getattr(args, "server", None))
        except SpecError as err:
            _fail(str(err))
        server = ApiServer(
            spec,
            base,
            server_name=args.name,
            auth=auth,
            write_auth=write_auth,
            fields=_parse_fields(getattr(args, "fields", None), "--fields"),
            redact=_parse_fields(getattr(args, "redact", None), "--redact"),
            rate_limit=getattr(args, "rate_limit", None),
            timeout=args.timeout,
            tools=tools,
            lazy=args.lazy,
            enable_preview=args.enable_preview,
            cache_ttl=args.cache_ttl,
            retry=args.retry,
            retry_delay=args.retry_delay,
            response_format=args.format,
            wait_on_429=getattr(args, "wait_on_429", 0.0),
        )
        server.ui_config_defaults = dict(config_data.get("serve") or {})
        if getattr(args, "metrics", None):
            _start_metrics(args.metrics)
        if getattr(args, "reload", False):
            def reload_single_ui() -> None:
                server.reload_tools(_apply_tool_text(_rebuild_single_tools(args), config_data))

            _start_reload([args.spec], reload_single_ui)
        _apply_tool_text(server.tools, config_data)
        _wire_serve_extras(args, server)
        if getattr(args, "http_token_file", None):
            print("note: --http-token-file scopes apply to the MCP transport (mcpify serve --http); "
                  "the dashboard keeps its single --http-token", file=sys.stderr)
        try:
            host, port = parse_http_bind(args.http or "8787")
        except ValueError as err:
            _fail(f"--http {args.http}: {err}")
        token = args.http_token or os.environ.get("MCPIFY_HTTP_TOKEN") or None
        serve_ui(server, host, port, token, config_path,
                 reload_cb=reload_single_ui if getattr(args, "reload", False) else None)

    elif args.command == "try":
        from .http_client import set_logging

        set_logging(args.verbose, getattr(args, "log_file", None))
        auth = _resolve_auth(args, spec)
        write_auth = _resolve_write_auth(args, auth)
        if write_auth is not None:
            print(
                f"mcpify: auth split — reads use {args.auth_env}, "
                f"writes use {getattr(write_auth, 'env_var', None) or args.write_oauth2_token_url}",
                file=sys.stderr,
            )
        from .api_server import ApiServer

        try:
            base = _base_url(spec, args.base_url, getattr(args, "server", None))
        except SpecError as err:
            _fail(str(err))
        server = ApiServer(
            spec,
            base,
            server_name=args.name,
            auth=auth,
            write_auth=write_auth,
            fields=_parse_fields(getattr(args, "fields", None), "--fields"),
            redact=_parse_fields(getattr(args, "redact", None), "--redact"),
            rate_limit=getattr(args, "rate_limit", None),
            timeout=args.timeout,
            tools=tools,
            lazy=args.lazy,
            enable_preview=args.enable_preview,
            cache_ttl=args.cache_ttl,
            retry=args.retry,
            retry_delay=args.retry_delay,
            response_format=args.format,
            wait_on_429=getattr(args, "wait_on_429", 0.0),
        )
        print(
            f"mcpify try [{args.name}]: {len(tools)} tools from {args.spec} -> {base} "
            "(calls are REAL requests — same path an agent would take)",
            file=sys.stderr,
        )
        from .repl import run as run_repl

        run_repl(server)

    elif args.command == "init":
        if os.path.exists(args.config):
            _fail(f"{args.config} already exists — remove it first or pass --config elsewhere")
        sorular: list[str] = []
        if args.spec:
            sorular.append(args.spec)
        if args.base_url:
            sorular.append(args.base_url)
        kalan = iter(sorular + [None] * 20)  # gerisi stdin'den

        def answers() -> Iterator[str]:
            for item in kalan:
                if item is None:
                    yield input()
                else:
                    yield item

        def loader(spec_arg: str) -> tuple[dict[str, Any], str]:
            arg = spec_arg
            if arg.startswith(("http://", "https://")) and urlparse(arg).path in ("", "/"):
                arg, _ = discover_spec(arg)
            spec_data = load_spec(arg)
            return spec_data, (_base_url(spec_data, None) or "")

        from .config import build_config_document, run_wizard

        try:
            ayarlar, uyarilar = run_wizard(answers(), loader)
        except (ValueError, StopIteration, EOFError) as err:
            _fail(f"init cancelled: {err}")
        for uyari in uyarilar:
            print(f"note: {uyari}", file=sys.stderr)
        Path(args.config).write_text(build_config_document(ayarlar), encoding="utf-8")
        print(f"wrote {args.config}")
        print(f"next: mcpify serve --config {args.config}   (or just: mcpify serve)")
        if getattr(args, "probe", False):
            from types import SimpleNamespace

            from .probe import run_probe

            spec_arg = ayarlar.get("spec")
            if not spec_arg:
                _fail("init --probe: the wizard did not record a spec")
            probe_spec = load_spec(spec_arg)
            probe_base = ayarlar.get("base-url") or _base_url(probe_spec, None)
            auth = _resolve_auth(SimpleNamespace(
                auth_env=ayarlar.get("auth-env"), auth_style=ayarlar.get("auth-style"),
                auth_name=ayarlar.get("auth-name"), oauth2_token_url=None,
            ), probe_spec)
            report = run_probe(probe_spec, probe_base, 10.0, auth=auth)
            if report["ok"]:
                print(f"probe:    {report['method']} {report['path']} → {report['status']} "
                      f"reachable ({report['latency_seconds']:.2f}s"
                      + (", authenticated" if report.get("authenticated") else "") + ")")
            else:
                print(f"probe:    {report.get('error', 'failed')}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "status" and api_sections(config_data):
        entries = _build_entries(args, config_data)
        from .http_client import execute as _execute

        results = []
        for entry in entries:
            started = time.monotonic()
            sonuc = _execute(
                {"method": "GET", "url": entry["base"].rstrip("/") + "/",
                 "headers": {"Accept": "application/json"}, "body": None},
                timeout=float(getattr(args, "timeout", 10.0) or 10.0),
            )
            results.append({
                "api": entry["label"],
                "base_url": entry["base"],
                "api_reachable": sonuc["status"] != 0,
                "api_status": sonuc["status"],
                "latency_seconds": round(time.monotonic() - started, 3),
                "tools": len(entry["tools"]),
                "fields": sorted(entry["fields"]) if entry.get("fields") else None,
                "redact": sorted(entry["redact"]) if entry.get("redact") else None,
                "rate_limit": entry.get("rate-limit"),
                "policy": _policy_note(entry.get("fields"), entry.get("redact"),
                                       entry.get("rate-limit")),
            })
        rapor = {"apis": results, "all_reachable": all(r["api_reachable"] for r in results),
                 "version": __version__}
        if args.json:
            print(json.dumps(rapor, ensure_ascii=False, indent=2))
        else:
            for r in results:
                durum = "reachable" if r["api_reachable"] else "UNREACHABLE"
                line = (f"[{r['api']}] {durum} (status {r['api_status']}, "
                        f"{r['latency_seconds']:.2f}s) — {r['base_url']} — {r['tools']} tools")
                if r["policy"]:
                    line += f" · {r['policy']}"
                print(line)
        sys.exit(0 if rapor["all_reachable"] else 2)

    elif args.command == "status":
        if getattr(args, "spec", None) is None:
            _fail("a spec path or URL is required (or set `spec` in the config)")
        spec_arg = args.spec
        if spec_arg.startswith(("http://", "https://")) and urlparse(spec_arg).path in ("", "/"):
            try:
                spec_arg, _ = discover_spec(spec_arg)
                print(f"discovered: {spec_arg}", file=sys.stderr)
            except SpecError as err:
                _fail(str(err))
        try:
            spec = load_spec(spec_arg)
        except SpecError as err:
            _fail(str(err))
        try:
            base = _base_url(spec, args.base_url, getattr(args, "server", None))
        except SpecError as err:
            _fail(str(err))
        from .http_client import execute

        baslangic = time.monotonic()
        sonuc = execute({"method": "GET", "url": base.rstrip("/") + "/",
                         "headers": {"Accept": "application/json"}, "body": None},
                        timeout=args.timeout)
        latency = time.monotonic() - baslangic
        erisilebilir = sonuc["status"] != 0
        auth_env = getattr(args, "auth_env", None)
        rapor = {
            "spec": spec_arg,
            "base_url": base,
            "api_reachable": erisilebilir,
            "api_status": sonuc["status"],
            "latency_seconds": round(latency, 3),
            "tools": len(spec_to_tools(spec, strict=getattr(args, "strict", False))),
            "auth_env": auth_env,
            "auth_env_set": bool(os.environ.get(auth_env)) if auth_env else None,
            "fields": getattr(args, "fields", None),
            "redact": getattr(args, "redact", None),
            "rate_limit": getattr(args, "rate_limit", None),
            "version": __version__,
        }
        if args.json:
            print(json.dumps(rapor, ensure_ascii=False, indent=2))
        else:
            def green(s: str) -> str:
                return f"\033[32m{s}\033[0m" if USE_COLOR else s

            durum = green("reachable") if USE_COLOR else "reachable"
            if not erisilebilir:
                durum = "UNREACHABLE"
            print(f"api:      {durum} (status {sonuc['status']}, {latency:.2f}s)")
            print(f"base url: {base}")
            print(f"tools:    {rapor['tools']}")
            if auth_env:
                setlenmis = "set" if rapor["auth_env_set"] else "NOT SET"
                print(f"auth:     {auth_env} [{setlenmis}]")
            policy = _policy_note(
                _parse_fields(getattr(args, "fields", None), "--fields"),
                _parse_fields(getattr(args, "redact", None), "--redact"),
                getattr(args, "rate_limit", None))
            if policy:
                print(f"policy:   {policy}")
        sys.exit(0 if erisilebilir else 2)

    elif args.command == "doctor":
        try:
            spec = load_spec(args.spec)
        except SpecError as err:
            _fail(str(err))
        total = 0
        missing_id = 0
        no_summary = 0
        for _method, _path, operation in iter_operations(spec):
            total += 1
            if not operation.get("operationId"):
                missing_id += 1
            if not (operation.get("summary") or operation.get("description")):
                no_summary += 1
        servers = spec_servers(spec)
        variabled = [s for s in servers if "{" in s]
        security = ((spec.get("components") or {}).get("securitySchemes") or {})
        deprecated = sum(1 for _m, _p, op in iter_operations(spec) if op.get("deprecated"))
        instruction_like, overlong_desc = _tool_text_issues(spec)
        def ok(s: str) -> str:
            return f"\033[32m{s}\033[0m" if USE_COLOR else s

        def dim(s: str) -> str:
            return f"\033[2m{s}\033[0m" if USE_COLOR else s

        def warn(s: str) -> str:
            return f"\033[33m{s}\033[0m" if USE_COLOR else s

        warnings = []
        if missing_id:
            warnings.append(f"{missing_id}/{total} operations have no operationId")
        if no_summary:
            warnings.append(f"{no_summary}/{total} operations have no summary")
        if variabled:
            warnings.append(f"server URL(s) contain variables: {', '.join(variabled)}")
        if not servers:
            warnings.append("no servers declared")
        elif not servers[0].startswith(("http://", "https://")):
            warnings.append(f"server URL '{servers[0]}' is relative")
        if security:
            hint = _auth_hint(spec)
            warnings.append(
                "spec declares security schemes: " + ", ".join(sorted(security))
                + (f" (serve with {hint})" if hint else "")
            )
        if deprecated:
            warnings.append(f"{deprecated} deprecated operation(s) will be exposed")
        if instruction_like:
            warnings.append(
                f"{instruction_like} operation(s) carry instruction-like text — tool descriptions "
                "are model-facing prompts; review them (override per tool with [tool-text] in .mcpify.toml)"
            )
        if overlong_desc:
            warnings.append(
                f"{overlong_desc} operation(s) have descriptions over {_LONG_DESCRIPTION_CHARS} chars — "
                "docs-grade prose burns the model's context in every tool list"
            )
        if total > 50:
            warnings.append(f"{total} operations is a large tool surface")
        probe_report = None
        if args.probe:
            probe_base = args.base_url or next(
                (s for s in servers if s.startswith(("http://", "https://"))), None)
            probe_report = _doctor_probe(spec, probe_base, args.timeout,
                                         auth=_resolve_auth(args, spec),
                                         fail_on_http_error=args.fail_on_http_error)
        if args.json:
            payload = {
                "ok": not warnings,
                "operations": total,
                "missing_operation_id": missing_id,
                "missing_summary": no_summary,
                "warnings": warnings,
                "instruction_like_text": instruction_like,
                "overlong_descriptions": overlong_desc,
                "exit_hints": ["pass --base-url"] if not servers else [],
            }
            if probe_report is not None:
                payload["probe"] = probe_report
            print(json.dumps(payload, ensure_ascii=False))
            if warnings or (probe_report is not None and not probe_report["ok"]):
                sys.exit(1)
            return

        print(f"openapi: {spec.get('openapi') or spec.get('swagger')}")
        print(f"title:   {spec.get('info', {}).get('title', '(untitled)')}")
        print(f"paths:   {len(spec.get('paths', {}))}")
        print(f"tools:   {len(spec_to_tools(spec))} operations")
        print(f"servers: {', '.join(servers) or warn('none declared (pass --base-url)')}")
        if len(servers) > 1:
            print(dim(f"tip:      {len(servers)} servers declared — pick one with --server INDEX|NAME (e.g. --server 2)"))
        if missing_id:
            print(warn(f"warning: {missing_id}/{total} operations have no operationId (names fall back to method_path)"))
        if no_summary:
            print(warn(f"warning: {no_summary}/{total} operations have no summary (agents see no description)"))
        if variabled:
            print(warn(f"warning: server URL(s) contain variables: {', '.join(variabled)} — pass --base-url"))
        if servers and not servers[0].startswith(("http://", "https://")):
            print(warn(f"warning: server URL '{servers[0]}' is relative — pass --base-url with the absolute URL"))
        if security:
            hint = _auth_hint(spec)
            print(warn(
                f"warning: spec declares security schemes ({', '.join(sorted(security))})"
                + (f" — serve with {hint} and calls authenticate automatically" if hint
                   else " — serve with --auth-env/--auth-style or calls will 401")
            ))
        if deprecated:
            print(warn(f"warning: {deprecated} deprecated operation(s) will be exposed (filter with --tag/--exclude if unintended)"))
        if instruction_like:
            print(warn(f"warning: {instruction_like} operation(s) carry instruction-like text — descriptions become model prompts (review or override with [tool-text])"))
        if overlong_desc:
            print(warn(f"warning: {overlong_desc} operation(s) have descriptions over {_LONG_DESCRIPTION_CHARS} chars — every tool list pays that context cost"))
        if total > 50:
            print(warn(f"warning: {total} operations is a large tool surface — consider --tag/--include/--exclude to protect the model's context"))
        if not missing_id and not no_summary:
            print(ok("all operations carry operationId and summary — agent-friendly ✓"))

        probe_ok = True
        if args.probe:
            probe_base = args.base_url or next(
                (s for s in servers if s.startswith(("http://", "https://"))), None)
            report = _doctor_probe(spec, probe_base, args.timeout,
                                   auth=_resolve_auth(args, spec),
                                   fail_on_http_error=args.fail_on_http_error)
            probe_ok = bool(report["ok"])
            if report["ok"]:
                verdict = ok("reachable") if 200 <= report["status"] < 400 else warn(f"HTTP {report['status']}")
                tag = ", authenticated" if report.get("authenticated") else ""
                print(f"probe:    {report['method']} {report['path']} → {report['status']} {verdict} "
                      f"({report['latency_seconds']:.2f}s{tag})")
            else:
                print(warn(f"probe:    {report.get('error', 'failed')}"))

        if args.probe and not probe_ok:
            # failed pre-flight means every tool call would fail too —
            # stop before serving
            sys.exit(1)


if __name__ == "__main__":
    main()
