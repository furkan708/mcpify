"""Multi-API aggregation: one MCP server fronting several OpenAPI specs.

This is the composition layer paid MCP gateways bill for: each API
keeps its own base URL, auth, cache and retry tuning; agents see a
single tool surface. Design:

- one entry per API (label + everything execution needs); tools are
  merged in entry order, and every tool carries an `api` label so the
  agent can see which API it belongs to
- name collisions across APIs are resolved by prefixing ALL tools of
  the conflicting APIs with their label (`petstore_get_pet`), so neither
  API silently wins; a leftover collision gets the usual `_2` suffix
- meta tools (search/schema/call/preview/health) stay global — search
  spans APIs, calls route to the owning API through `_context_for`
- health is aggregated: all upstreams are probed concurrently and the
  report names every API's reachability
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .api_server import ApiServer
from .http_client import execute
from .tools import META_TOOL_NAMES, slugify


def merge_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Merge per-API tool lists into one surface.

    Mutates the tools: sets `tool["api"] = label`, renames colliding
    names with the label prefix (both sides — neither API silently
    wins), and suffixes any leftover collision. Returns the merged,
    order-preserving tool list and an owner map name → entry index.
    """
    claimed: dict[str, set[int]] = {}
    for index, entry in enumerate(entries):
        for tool in entry["tools"]:
            tool["api"] = entry["label"]
            claimed.setdefault(tool["name"], set()).add(index)
    conflicts = {name for name, owners in claimed.items() if len(owners) > 1}

    taken: set[str] = set(META_TOOL_NAMES)
    merged: list[dict[str, Any]] = []
    owners: dict[str, int] = {}
    for index, entry in enumerate(entries):
        for tool in entry["tools"]:
            name = tool["name"]
            if name in conflicts:
                name = f"{slugify(entry['label'])}_{name}"
            base = name
            counter = 2
            while name in taken:
                name = f"{base}_{counter}"
                counter += 1
            taken.add(name)
            tool["name"] = name
            owners[name] = index
            merged.append(tool)
    return merged, owners


class AggregatedServer(ApiServer):
    """ApiServer over several per-API entries, routed by tool owner."""

    def __init__(
        self,
        entries: list[dict[str, Any]],
        server_name: str = "mcpify",
        lazy: bool = False,
        enable_preview: bool = False,
        response_format: str = "auto",
    ) -> None:
        if not entries:
            raise ValueError("aggregation needs at least one API entry")
        self.entries = list(entries)  # caller's list'ten bagimsiz kopya
        merged, owners = merge_entries(entries)
        self._owners = owners
        first = entries[0]
        # The parent constructor wants a spec/base pair; the aggregator
        # routes every execution through _context_for, so these first
        # entry values are only used where the parent's own paths need
        # a default (they are never executed against directly).
        super().__init__(
            first["spec"],
            first["base"],
            server_name=server_name,
            timeout=first["timeout"],
            tools=merged,
            lazy=lazy,
            enable_preview=enable_preview,
            response_format=response_format,
        )

    def _context_for(self, tool: dict[str, Any]) -> dict[str, Any]:
        entry = self.entries[self._owners[tool["name"]]]
        return {
            "base": entry["base"],
            "auth": entry["auth"],
            "timeout": entry["timeout"],
            "cache": entry["cache"],
            "retry": entry["retry"],
            "retry_delay": entry["retry_delay"],
            "wait_on_429": entry["wait_on_429"],
        }

    def _health(self) -> dict[str, Any]:
        """Probe every API concurrently and report per-API reachability."""

        def probe(entry: dict[str, Any]) -> dict[str, Any]:
            started = time.monotonic()
            result = execute(
                {
                    "method": "GET",
                    "url": entry["base"].rstrip("/") + "/",
                    "headers": {"Accept": "application/json"},
                    "body": None,
                },
                timeout=min(entry["timeout"], 10.0),
                retry=entry["retry"],
                retry_delay=entry["retry_delay"],
            )
            latency = time.monotonic() - started
            return {
                "api": entry["label"],
                "api_reachable": result["status"] != 0,
                "api_status": result["status"],
                "latency_seconds": round(latency, 3),
                "base_url": entry["base"],
                "auth": entry["auth"].describe() if entry["auth"] is not None else None,
            }

        with ThreadPoolExecutor(max_workers=min(8, len(self.entries))) as pool:
            apis = list(pool.map(probe, self.entries))
        dead = [item["api"] for item in apis if not item["api_reachable"]]
        report: dict[str, Any] = {
            "apis": apis,
            "api_count": len(apis),
            "tools": len(self.tools),
            "format": self.response_format,
            "all_reachable": not dead,
        }
        if dead:
            report["hint"] = (
                "unreachable API(s): " + ", ".join(dead)
                + " — check their base URLs, networks, or --timeout"
            )
            return self._text(json.dumps(report, ensure_ascii=False, indent=2), is_error=True)
        return self._text(json.dumps(report, ensure_ascii=False, indent=2))
