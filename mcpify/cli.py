"""mcpify command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from . import __version__
from .config import apply_to_namespace, load_config, resolve, validate
from .spec import SpecError, discover_spec, iter_operations, load_spec, spec_servers
from .tools import AuthConfig, spec_to_tools

if TYPE_CHECKING:
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


def filter_tools(tools: list[dict], args: argparse.Namespace) -> list[dict]:
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

    def regex_matches(path: str, patterns: list[re.Pattern]) -> bool:
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


def _pick_server(servers: list, choice: str) -> dict:
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


def _base_url(spec: dict, override: str | None, server: str | None = None) -> str:
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


def _auth_hint(spec: dict | None) -> str | None:
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


def _resolve_auth(args: argparse.Namespace, spec: dict | None = None) -> AuthConfig | OAuth2ClientCredentials | None:
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
    p_list.add_argument("spec", help="path or URL of an OpenAPI document")
    p_list.add_argument("--tag", help="only operations with this tag")
    p_list.add_argument("--include", action="append", help="only these path prefixes (repeatable)")
    p_list.add_argument("--exclude", action="append", help="skip these path prefixes (repeatable)")
    p_list.add_argument("--read-only", action="store_true", help="only GET operations")
    p_list.add_argument("--allow", action="append", metavar="REGEX",
                        help="re-include operations dropped by --read-only (repeatable)")
    p_list.add_argument("--deny", action="append", metavar="REGEX",
                        help="never expose matching paths, overrides --allow (repeatable)")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")

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

    p_doctor = sub.add_parser("doctor", help="inspect a spec and report problems")
    p_doctor.add_argument("spec", help="path or URL of an OpenAPI document")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args(argv)
    tools: list[dict] = []  # status computes its own count

    config_path = None
    if args.command in ("serve", "status", "try"):
        try:
            config_path, data = load_config(getattr(args, "config", None))
            if config_path is not None:
                for problem in validate(data):
                    print(f"config warning: {problem}", file=sys.stderr)
                settings = resolve(data, args.env)
                apply_to_namespace(settings, args)
                if settings.get("spec") and getattr(args, "spec", None) is None:
                    args.spec = settings["spec"]
                tag = f" (env: {settings['_env']})" if settings.get("_env") else ""
                print(f"config: {config_path}{tag}", file=sys.stderr)
        except ValueError as err:
            _fail(str(err))

    if args.command in ("list", "serve", "try"):
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
        tools = filter_tools(all_tools, args)
        if not tools:
            _fail("no operations matched (the API would expose 0 tools)")

    if args.command == "list":
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "name": t["name"],
                            "method": t["_meta"]["method"],
                            "path": t["_meta"]["path"],
                            "deprecated": t["_meta"]["deprecated"],
                            "description": t["description"],
                        }
                        for t in tools
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
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
                print(f"  {'':36} {dim(desc[:90])}")
        print(dim("─" * 78))
        print(dim(f"serve it: mcpify serve {args.spec}"))

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
        if getattr(args, "http", None):
            from .http_transport import parse_http_bind, serve_http

            try:
                host, port = parse_http_bind(args.http)
            except ValueError as err:
                _fail(f"--http {args.http}: {err}")
            token = args.http_token or os.environ.get("MCPIFY_HTTP_TOKEN") or None
            serve_http(server, host, port, token)
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

    elif args.command == "try":
        from .http_client import set_logging

        set_logging(args.verbose, getattr(args, "log_file", None))
        auth = _resolve_auth(args, spec)
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

        def loader(spec_arg: str) -> tuple[dict, str]:
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
        if total > 50:
            warnings.append(f"{total} operations is a large tool surface")
        if args.json:
            print(json.dumps({
                "ok": not warnings,
                "operations": total,
                "missing_operation_id": missing_id,
                "missing_summary": no_summary,
                "warnings": warnings,
                "exit_hints": ["pass --base-url"] if not servers else [],
            }, ensure_ascii=False))
            if warnings:
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
        if total > 50:
            print(warn(f"warning: {total} operations is a large tool surface — consider --tag/--include/--exclude to protect the model's context"))
        if not missing_id and not no_summary:
            print(ok("all operations carry operationId and summary — agent-friendly ✓"))


if __name__ == "__main__":
    main()
