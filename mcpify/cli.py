"""mcpify command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import NoReturn

from . import __version__
from .spec import SpecError, iter_operations, load_spec, spec_servers
from .tools import AuthConfig, spec_to_tools

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


def _base_url(spec: dict, override: str | None) -> str:
    if override:
        return override
    servers = spec.get("servers") or []
    if servers:
        entry = servers[0] if isinstance(servers[0], dict) else {"url": str(servers[0])}
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
    p_list.add_argument("--allow", action="append", metavar="REGEX",
                        help="re-include operations dropped by --read-only (repeatable)")
    p_list.add_argument("--deny", action="append", metavar="REGEX",
                        help="never expose matching paths, overrides --allow (repeatable)")
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
    p_serve.add_argument("--allow", action="append", metavar="REGEX",
                         help="re-include operations dropped by --read-only (repeatable)")
    p_serve.add_argument("--deny", action="append", metavar="REGEX",
                         help="never expose matching paths, overrides --allow (repeatable)")

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
        auth = None
        if args.auth_env:
            auth = AuthConfig(args.auth_env, args.auth_style, args.auth_name)
        from .api_server import ApiServer

        try:
            base = _base_url(spec, args.base_url)
        except SpecError as err:
            _fail(str(err))
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
        if servers and not servers[0].startswith(("http://", "https://")):
            print(warn(f"warning: server URL '{servers[0]}' is relative — pass --base-url with the absolute URL"))
        security = ((spec.get("components") or {}).get("securitySchemes") or {})
        if security:
            print(warn(f"warning: spec declares security schemes ({', '.join(sorted(security))}) — serve with --auth-env/--auth-style or calls will 401"))
        deprecated = sum(1 for _m, _p, op in iter_operations(spec) if op.get("deprecated"))
        if deprecated:
            print(warn(f"warning: {deprecated} deprecated operation(s) will be exposed (filter with --tag/--exclude if unintended)"))
        if total > 50:
            print(warn(f"warning: {total} operations is a large tool surface — consider --tag/--include/--exclude to protect the model's context"))
        if not missing_id and not no_summary:
            print(ok("all operations carry operationId and summary — agent-friendly ✓"))


if __name__ == "__main__":
    main()
