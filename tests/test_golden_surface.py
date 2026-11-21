"""The tool surface is a public artifact — freeze its shape.

Agents cache tool listings across restarts, and every description, schema
key and annotation is a token the caller pays for on each session. A
silent change to any of them is a real regression that unit tests written
per feature will happily miss (they assert what they were told to assert).

This is a whole-surface snapshot of ``examples/petstore.json``: diff it in
review, and regenerate deliberately with:

    MCPIFY_UPDATE_GOLDEN=1 pytest tests/test_golden_surface.py
"""

import json
import os
from pathlib import Path

from mcpify.spec import load_spec
from mcpify.tools import spec_to_tools

GOLDEN = Path(__file__).parent / "golden" / "petstore_tools.json"


def _surface() -> str:
    tools = spec_to_tools(load_spec("examples/petstore.json"))
    return json.dumps(tools, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_tool_surface_matches_the_frozen_snapshot():
    current = _surface()
    if os.environ.get("MCPIFY_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(current, encoding="utf-8")
    assert GOLDEN.exists(), "golden file missing — regenerate with MCPIFY_UPDATE_GOLDEN=1"
    expected = GOLDEN.read_text(encoding="utf-8")
    if current == expected:
        return
    # Point at the first divergence instead of dumping two 300-line blobs
    for line_no, (want, got) in enumerate(
        zip(expected.splitlines(), current.splitlines(), strict=False), start=1
    ):
        if want != got:
            raise AssertionError(
                f"tool surface changed at line {line_no}:\n  snapshot: {want}\n  current:  {got}\n"
                "If this change is intentional, regenerate the snapshot and review the diff."
            )
    raise AssertionError("tool surface changed (length differs)")


def test_snapshot_is_stable_across_calls():
    """No set/dict iteration leakage: two builds must be byte-identical."""
    assert _surface() == _surface()
