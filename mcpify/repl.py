"""`mcpify try` — an interactive terminal REPL for the generated tools.

Try an API's tools without installing an agent client: pick a tool, fill
its arguments field by field, see the real response. Same execution path
as MCP `tools/call` (the identical policy, auth, retry and formatting
layers), so what you see is exactly what an agent would get.

Commands:
  <number> | <name>   select a tool and fill its arguments
  :raw NAME {json}    call a tool with arguments given as inline JSON
  :info [SEL]         show the full schema of the selected tool
  :ls                 re-list the tools
  :h | :help          this help
  :q | :quit          leave (Ctrl+C / Ctrl+D also work)

Input is read through an injectable `input` function and results are
written to an injectable output stream, so the loop is fully testable
and behaves identically when stdin is piped.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from typing import Any

MAX_DISPLAY_CHARS = 6000

HELP_TEXT = """\
commands:
  <number> | <name>   select a tool and fill its arguments
  :raw NAME {json}    call a tool with inline JSON arguments
  :info [SEL]         show the full schema of the selected tool
  :ls                 re-list the tools
  :h | :help          this help
  :q | :quit          leave"""


def _fmt_type(prop: dict[str, Any]) -> str:
    kind = prop.get("type", "string")
    if "enum" in prop:
        return "one of: " + " | ".join(str(v) for v in prop["enum"])
    return str(kind)


def _cast(kind: str, raw: str) -> Any:
    """Cast a raw line to the schema type; ValueError means re-ask."""
    if kind == "integer":
        return int(raw)
    if kind == "number":
        return float(raw)
    if kind == "boolean":
        low = raw.strip().lower()
        if low in ("true", "yes", "y", "1"):
            return True
        if low in ("false", "no", "n", "0"):
            return False
        raise ValueError("use true/false")
    if kind == "object":
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("must be a JSON object")
        return value
    if kind == "array":
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError("must be a JSON array")
        return value
    return raw


def _collect_arguments(
    tool: dict[str, Any],
    prompt: Callable[[str], str],
    say: Callable[[str], None],
) -> dict[str, Any] | None:
    """Prompt for each input-schema property. Returns the arguments dict,
    or None when the user aborts the tool (empty value for a required
    path parameter is left to build_request's error path)."""
    schema = tool.get("inputSchema") or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[Any] = schema.get("required") or []
    arguments: dict[str, Any] = {}
    for name, prop in properties.items():
        prop = prop or {}
        kind = str(prop.get("type", "string"))
        is_required = name in required
        label = f"{name}"
        if name == "body" and kind == "object":
            hint = "JSON object, single line"
        elif "enum" in prop:
            hint = " | ".join(str(v) for v in prop["enum"])
        else:
            hint = kind
        default = prop.get("default")
        suffix = f" [{default}]" if default is not None else ""
        star = "*" if is_required else ""
        while True:
            try:
                raw = prompt(f"  {label}{star} ({hint}){suffix}: ").strip()
            except EOFError:
                raise
            if not raw:
                if default is not None:
                    arguments[name] = default
                    break
                if not is_required:
                    break  # optional and skipped
                # required with no value: let build_request produce its
                # precise error (missing path parameter / body) — same
                # message an agent would receive
                return arguments or {}
            try:
                arguments[name] = _cast(kind, raw)
                break
            except ValueError as err:
                say(f"    ! {err} — try again (empty line skips)")
    return arguments


def _render_result(payload: dict[str, Any], elapsed: float, out: Callable[[str], None]) -> None:
    text = ""
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", ""))
            break
    if payload.get("isError"):
        out(f"  ✗ error ({elapsed:.2f}s)")
    else:
        out(f"  ✓ ({elapsed:.2f}s)")
    if len(text) > MAX_DISPLAY_CHARS:
        text = text[:MAX_DISPLAY_CHARS] + f"\n… [truncated, {len(text):,} chars total]"
    out("  " + text.replace("\n", "\n  "))


def run(
    server: Any,
    input_fn: Callable[[str], str] | None = None,
    output: Any = None,
) -> None:
    """Run the REPL until :q, EOF or Ctrl+C. `server` is an ApiServer."""
    read_input = input_fn or input
    out_stream = output if output is not None else sys.stdout

    def out(text: str = "") -> None:
        out_stream.write(text + "\n")

    # internal dicts (not public_tools) so the listing can show method/path
    lookup = {**server.by_name, **server.meta_tools}
    tools = [lookup[name] for name in server.listed_names]
    out(f"mcpify try — {len(tools)} tools. Type :h for help, :q to quit.")
    selected: dict[str, Any] | None = None

    def list_tools() -> None:
        for index, tool in enumerate(tools, 1):
            meta = tool.get("_meta") or {}
            where = f"  {meta.get('method', ''):7} {meta.get('path', '')}" if meta else ""
            readonly = " [read-only]" if (tool.get("annotations") or {}).get("readOnlyHint") else ""
            out(f"  {index:>2}. {tool['name']}{readonly}{where}")

    list_tools()
    while True:
        try:
            line = read_input("mcpify try> ").strip()
        except (EOFError, KeyboardInterrupt):
            out("\nbye")
            return
        if not line:
            continue
        if line in (":q", ":quit", ":exit"):
            out("bye")
            return
        if line in (":h", ":help"):
            out(HELP_TEXT)
            continue
        if line == ":ls":
            list_tools()
            continue
        if line == ":info" and selected is None:
            out("  ! select a tool first (number or name), or use :info NAME")
            continue
        if line.startswith(":info"):
            name = line[len(":info"):].strip()
            target = _resolve(name, tools) if name else selected
            if target is None:
                out(f"  ! unknown tool '{name}'")
            else:
                out(json.dumps({k: v for k, v in target.items() if not k.startswith("_")},
                               ensure_ascii=False, indent=2))
            continue
        if line.startswith(":raw"):
            parts = line[len(":raw"):].strip().split(" ", 1)
            if len(parts) != 2:
                out("  ! usage: :raw TOOL_NAME {\"json\": \"args\"}")
                continue
            name, raw_args = parts
            tool = _resolve(name, tools)
            if tool is None:
                out(f"  ! unknown tool '{name}'")
                continue
            try:
                arguments = json.loads(raw_args)
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as err:
                out(f"  ! invalid JSON arguments: {err}")
                continue
            _execute(server, tool["name"], arguments, out)
            continue

        target = _resolve(line, tools)
        if target is None:
            out(f"  ! no tool matches '{line}' — type :ls to list, :q to quit")
            continue
        selected = target
        out(f"→ {target['name']}")
        try:
            arguments = _collect_arguments(
                target,
                prompt=lambda label: read_input(label),
                say=out,
            )
        except (EOFError, KeyboardInterrupt):
            out("\n  (cancelled)")
            continue
        if arguments is None:
            continue
        _execute(server, target["name"], arguments, out)


def _resolve(reference: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a selection by 1-based number or exact/case-insensitive name."""
    reference = reference.strip()
    if reference.isdigit():
        index = int(reference)
        if 1 <= index <= len(tools):
            return tools[index - 1]
        return None
    for tool in tools:
        if tool["name"] == reference:
            return tool
    lowered = reference.lower()
    for tool in tools:
        if tool["name"].lower() == lowered:
            return tool
    return None


def _execute(server: Any, name: str, arguments: dict[str, Any], out: Callable[[str], None]) -> None:
    started = time.monotonic()
    try:
        payload = server.run_tool(name, arguments)
    except Exception as err:  # RequestError and anything upstream-raised
        out(f"  ✗ {err}")
        return
    _render_result(payload, time.monotonic() - started, out)
