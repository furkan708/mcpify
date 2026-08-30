"""`mcpify diff` — compare two versions of an OpenAPI document from the
agent's point of view.

The unit of change is the *tool surface*: method+path pairs enriched
with the details agents depend on (operationId, summary, deprecation,
parameters and their requiredness, request-body presence). A change is
**breaking** when an agent's existing call pattern would fail:

- operation removed
- required parameter added (old calls no longer valid)
- request body became required
- response scheme... deliberately not compared deeply: HTTP APIs drift
  in a thousand non-breaking ways; the report pins the contract-level
  breaks. Deprecations are warnings, not breaks.

Exit-code contract (used by CI): 0 clean/warnings, 1 breaking changes,
2 usage error.
"""

from __future__ import annotations

import json
from typing import Any

from .spec import iter_operations


def summarize(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Tool-level view of one document, keyed by 'METHOD path'."""
    surface: dict[str, dict[str, Any]] = {}
    for method, path, operation in iter_operations(spec):
        parameters = [
            {
                "name": param.get("name", ""),
                "in": param.get("in", ""),
                "required": bool(param.get("required")),
            }
            for param in (operation.get("parameters") or [])
            if isinstance(param, dict) and param.get("in") != "path"
        ]
        body = operation.get("requestBody") or {}
        surface[f"{method.upper()} {path}"] = {
            "operationId": operation.get("operationId"),
            "summary": operation.get("summary", ""),
            "deprecated": bool(operation.get("deprecated")),
            "parameters": parameters,
            "required_params": sorted(
                param["name"] for param in parameters if param["required"]
            ),
            "body_required": bool(body.get("required")),
            "has_body": bool(body),
        }
    return surface


def _param_map(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{param['in']}:{param['name']}": param
        for param in operation.get("parameters", [])
        if param.get("in") != "path"
    }


def diff_specs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Structural diff between two documents. Returns added/removed/
    changed operations with per-op change details and a breaking flag."""
    before = summarize(old)
    after = summarize(new)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed: list[dict[str, Any]] = []

    raw_before = {f"{m.upper()} {p}": op for m, p, op in iter_operations(old)}
    raw_after = {f"{m.upper()} {p}": op for m, p, op in iter_operations(new)}

    for key in sorted(set(before) & set(after)):
        details: list[str] = []
        breaking = False
        old_params = _param_map(raw_before[key])
        new_params = _param_map(raw_after[key])

        for param_key in sorted(set(new_params) - set(old_params)):
            required = bool(new_params[param_key].get("required"))
            details.append(
                f"parameter added: {param_key}" + (" (required)" if required else "")
            )
            if required:
                breaking = True
        details += [
            f"parameter removed: {param_key}"
            for param_key in sorted(set(old_params) - set(new_params))
        ]
        for param_key in sorted(set(old_params) & set(new_params)):
            was = bool(old_params[param_key].get("required"))
            now = bool(new_params[param_key].get("required"))
            if not was and now:
                details.append(f"parameter became required: {param_key}")
                breaking = True

        old_body = bool((raw_before[key].get("requestBody") or {}).get("required"))
        new_body = bool((raw_after[key].get("requestBody") or {}).get("required"))
        if not old_body and new_body:
            details.append("request body became required")
            breaking = True
        if not raw_before[key].get("requestBody") and raw_after[key].get("requestBody"):
            details.append("request body added")

        if not before[key]["deprecated"] and after[key]["deprecated"]:
            details.append("operation deprecated")
        if before[key]["operationId"] != after[key]["operationId"]:
            details.append(
                f"operationId changed: {before[key]['operationId']!r} -> {after[key]['operationId']!r}"
            )

        if details:
            changed.append({"operation": key, "changes": details, "breaking": breaking})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "breaking": bool(removed) or any(item["breaking"] for item in changed),
    }


def migration_guide(report: dict[str, Any]) -> list[str]:
    """One actionable line per breaking change (agent-consumer view)."""
    lines: list[str] = [
        f"remove usage of `{key}` — the operation no longer exists"
        for key in report["removed"]
    ]
    for item in report["changed"]:
        if not item["breaking"]:
            continue
        op = item["operation"]
        for detail in item["changes"]:
            if "parameter added" in detail or "became required" in detail:
                # keys are "in:name" (query:limit); agents pass the bare name
                raw = detail.split(": ", 1)[-1].split(" (")[0].replace("parameter became required: ", "")
                param = raw.split(":", 1)[1] if ":" in raw else raw
                lines.append(f"pass `{param}` when calling `{op}`")
            elif "body became required" in detail or "body added" in detail:
                lines.append(f"provide a request body when calling `{op}`")
    return sorted(set(lines))


def render(report: dict[str, Any]) -> str:
    """Human-readable report (the --json form is the dict itself)."""
    lines: list[str] = []
    if not (report["added"] or report["removed"] or report["changed"]):
        return "no tool-surface changes"
    if report["added"]:
        lines.append(f"added ({len(report['added'])}):")
        lines += [f"  + {key}" for key in report["added"]]
    if report["removed"]:
        lines.append(f"removed ({len(report['removed'])}):")
        lines += [f"  - {key}" for key in report["removed"]]
    if report["changed"]:
        lines.append(f"changed ({len(report['changed'])}):")
        for item in report["changed"]:
            mark = "BREAKING" if item["breaking"] else "ok"
            lines.append(f"  ~ {item['operation']} [{mark}]")
            lines.extend(f"      {detail}" for detail in item["changes"])
    guide = migration_guide(report)
    if guide:
        lines.append("migration guide:")
        lines += [f"  * {line}" for line in guide]
    lines.append("BREAKING CHANGES PRESENT" if report["breaking"] else "no breaking changes")
    return "\n".join(lines)


def diff_documents(old_text: str, new_text: str) -> dict[str, Any]:
    """Convenience for tests/embedders: parse two JSON documents."""
    return diff_specs(json.loads(old_text), json.loads(new_text))
