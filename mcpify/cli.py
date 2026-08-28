"""mcpify command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from typing import NoReturn

from . import __version__
from .spec import SpecError, iter_operations, load_spec, spec_servers
from .tools import AuthConfig, spec_to_tools

USE_COLOR = sys.stdout.isatty()


def _fail(message: str, code: int = 2) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def filter_tools(tools: list[dict], args: argparse.Namespace) -> list[dict]:
    """Apply --tag / --include / --exclude / --read-only filters."""
    include = [p.rstrip("/") for p in (args.include or [])]
    exclude = [p.rstrip("/") for p in (args.exclude or [])]

    def path_matches(path: str, patterns: list[str]) -> bool:
        return any(path == p or path.startswith(p + "/") for p in patterns)

    kept = []
    for tool in tools:
        meta = tool["_meta"]
        if args.read_only and meta["method"] != "GET":
            continue
        if args.tag and args.tag not in (meta.get("tags") or []):
            continue
        if include and not path_matches(meta["path"], include):
            continue
        if exclude and path_matches(meta["path"], exclude):
            continue
        kept.append(tool)
    return kept


def _base_url(spec: dict, override: str | None) -> str:
    if override:
        return override
    servers = spec_servers(spec)
    if servers:
        return servers[0]
    _fail(
        "no base URL: the spec declares no servers and --base-url was not given"
    )


def main(argv: list[str] | None = None) -> None:
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
    p_list.add_argument("--json", action="store_true", help="machine-readable output")

    p_serve = sub.add_parser("serve", help="start the MCP stdio server")
    p_serve.add_argument("spec", help="path or URL of an OpenAPI document")
    p_serve.add_argument("--base-url", help="API base URL (default: spec servers[0])")
    p_serve.add_argument("--name", default="mcpify", help="server name reported to clients")
    p_serve.add_argument("--auth-env", help="env variable holding the API credential")
    p_serve.add_argument(
        "--auth-style",
        choices=("bearer", "header", "query"),
        default="bearer",
        help="how to send the credential (default: bearer)",
    )
    p_serve.add_argument("--auth-name", help="header or query parameter name for non-bearer auth")
    p_serve.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    p_serve.add_argument("--tag", help="only operations with this tag")
    p_serve.add_argument("--include", action="append", help="only these path prefixes (repeatable)")
    p_serve.add_argument("--exclude", action="append", help="skip these path prefixes (repeatable)")
    p_serve.add_argument("--read-only", action="store_true", help="expose only GET operations")

    p_doctor = sub.add_parser("doctor", help="inspect a spec and report problems")
    p_doctor.add_argument("spec", help="path or URL of an OpenAPI document")

    args = parser.parse_args(argv)

    if args.command in ("list", "serve"):
        try:
            spec = load_spec(args.spec)
        except SpecError as err:
            _fail(str(err))
        all_tools = spec_to_tools(spec)
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
            desc = tool["description"]
            desc = desc[len(meta["method"]) + 3:] if desc.startswith("[") else desc
            print(
                f"  {cyan(tool['name']):36} {green(meta['method']):8} "
                f"{meta['path']}{body}"
            )
            if desc:
                print(f"  {'':36} {dim(desc[:90])}")
        print(dim("─" * 78))
        print(dim(f"serve it: mcpify serve {args.spec}"))

    elif args.command == "serve":
        if not args.base_url and not spec_servers(spec):
            _fail(
                "no base URL: the spec declares no servers and --base-url was not given"
            )
        auth = None
        if args.auth_env:
            auth = AuthConfig(args.auth_env, args.auth_style, args.auth_name)
        from .api_server import ApiServer

        base = args.base_url or spec_servers(spec)[0]
        server = ApiServer(
            spec,
            base,
            server_name=args.name,
            auth=auth,
            timeout=args.timeout,
        )
        server.tools = tools
        server.by_name = {tool["name"]: tool for tool in tools}
        print(
            f"mcpify: serving {len(tools)} tools from {args.spec} -> {base}",
            file=sys.stderr,
        )
        server.serve()

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
        def ok(s: str) -> str:
            return f"\033[32m{s}\033[0m" if USE_COLOR else s

        def warn(s: str) -> str:
            return f"\033[33m{s}\033[0m" if USE_COLOR else s

        print(f"openapi: {spec.get('openapi') or spec.get('swagger')}")
        print(f"title:   {spec.get('info', {}).get('title', '(untitled)')}")
        print(f"paths:   {len(spec.get('paths', {}))}")
        print(f"tools:   {len(spec_to_tools(spec))} operations")
        print(f"servers: {', '.join(servers) or warn('none declared (pass --base-url)')}")
        if missing_id:
            print(warn(f"warning: {missing_id}/{total} operations have no operationId (names fall back to method_path)"))
        if no_summary:
            print(warn(f"warning: {no_summary}/{total} operations have no summary (agents see no description)"))
        if variabled:
            print(warn(f"warning: server URL(s) contain variables: {', '.join(variabled)} — pass --base-url"))
        if not missing_id and not no_summary:
            print(ok("all operations carry operationId and summary — agent-friendly ✓"))


if __name__ == "__main__":
    main()
