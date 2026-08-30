"""`mcpify output-server`: generation, guard rails, and a real E2E run.

The generated script is executed as a subprocess and must serve the
identical MCP surface — the temp-spec pattern it uses is the exact
Windows mkstemp trap class already regression-tested in this suite.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcpify.cli import main as cli_main
from mcpify.standalone import generate


@pytest.fixture()
def spec_file(tmp_path):
    target = tmp_path / "spec.json"
    target.write_text(Path("examples/petstore.json").read_text(encoding="utf-8"), encoding="utf-8")
    return str(target)


# ---------------------------------------------------------------------------
# generate(): file-level contract
# ---------------------------------------------------------------------------

def test_generate_embeds_local_spec(spec_file, tmp_path):
    out = tmp_path / "server.py"
    warnings = generate(spec_file, str(out), ["--read-only", "--timeout", "5"])
    assert warnings == []
    source = out.read_text(encoding="utf-8")
    assert "Requires mcpify-openapi" in source
    assert "mcpify serve" in source
    # the spec travels as base64 of the raw file bytes
    marker = 'SPEC_B64 = "'
    start = source.index(marker) + len(marker)
    end = source.index('"', start)
    decoded = base64.b64decode(source[start:end])
    assert json.loads(decoded) == json.loads(Path(spec_file).read_text(encoding="utf-8"))
    assert '"--read-only"' in source
    assert '"--timeout", "5"' in source
    compile(source, str(out), "exec")  # must be valid Python


def test_generate_refuses_existing_file_without_force(spec_file, tmp_path):
    out = tmp_path / "server.py"
    out.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError) as err:
        generate(spec_file, str(out), [])
    assert "already exists" in str(err.value)
    assert out.read_text(encoding="utf-8") == "original"
    generate(spec_file, str(out), [], force=True)
    assert "mcpify" in out.read_text(encoding="utf-8")


def test_generate_rejects_unusable_spec(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "openapi"}', encoding="utf-8")
    with pytest.raises(ValueError) as err:
        generate(str(bad), str(tmp_path / "s.py"), [])
    assert "spec is not usable" in str(err.value)


def test_generate_url_spec_stays_remote(spec_file, tmp_path):
    out = tmp_path / "server.py"
    generate("https://example.com/openapi.json", str(out), [])
    source = out.read_text(encoding="utf-8")
    assert 'SPEC_B64 = None' in source
    assert 'SPEC_URL = "https://example.com/openapi.json"' in source


def test_generate_windows_style_path_compiles(spec_file, tmp_path):
    """Regression: the generated docstring is a raw string, so a Windows
    path (backslashes, \\U escape sequences) must never break it. The
    nested backslash name is legal on both platforms (no drive prefix,
    so pathlib keeps it under tmp_path on Windows too) and pins the
    v1.5.1 escape bug class shut."""
    weird = tmp_path / "win-share\\someone\\spec.json"
    weird.parent.mkdir(parents=True, exist_ok=True)
    weird.write_text(Path(spec_file).read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "server.py"
    generate(str(weird), str(out), ["--log-file", "C:\\logs\\mcp.log"])
    compile(out.read_text(encoding="utf-8"), str(out), "exec")  # must parse


def test_generate_warns_on_embedded_http_token(spec_file, tmp_path):
    out = tmp_path / "server.py"
    warnings = generate(spec_file, str(out), ["--http", "8080", "--http-token", "hunter2"])
    assert any("PLAIN TEXT" in w for w in warnings)
    # env-var NAMES are safe and never warned about
    out2 = tmp_path / "server2.py"
    warnings2 = generate(spec_file, str(out2), ["--auth-env", "API_TOKEN"])
    assert warnings2 == []
    assert "API_TOKEN" in out2.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI guard rails
# ---------------------------------------------------------------------------

def test_cli_rejects_unknown_serve_flag(spec_file, tmp_path, capsys):
    out = tmp_path / "server.py"
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["output-server", spec_file, "-o", str(out), "--", "--timeot", "5"])
    assert exit_info.value.code == 2
    assert "unknown serve flag" in capsys.readouterr().err


def test_cli_rejects_second_spec_in_baked_flags(spec_file, tmp_path, capsys):
    out = tmp_path / "server.py"
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["output-server", spec_file, "-o", str(out), "--", "other.json"])
    assert exit_info.value.code == 2
    assert "must not contain a spec" in capsys.readouterr().err


def test_cli_reports_missing_output(spec_file, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["output-server", spec_file])
    assert exit_info.value.code == 2  # -o is required


def test_cli_success_message(spec_file, tmp_path, capsys):
    out = tmp_path / "server.py"
    cli_main(["output-server", spec_file, "-o", str(out), "--", "--read-only"])
    captured = capsys.readouterr()
    assert f"wrote {out}" in captured.out
    assert "python3" in captured.out


# ---------------------------------------------------------------------------
# E2E: the generated script IS an MCP server
# ---------------------------------------------------------------------------

def test_generated_script_serves_mcp(spec_file, tmp_path):
    out = tmp_path / "petstore_server.py"
    generate(spec_file, str(out), ["--read-only"])
    messages = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    result = subprocess.run(
        [sys.executable, str(out)],
        input=messages.encode(),
        capture_output=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": os.getcwd()},  # simulate installed mcpify
    )
    assert result.returncode == 0, result.stderr.decode()
    lines = [json.loads(line) for line in result.stdout.decode().splitlines() if line.strip()]
    assert lines[0]["result"]["serverInfo"]["name"] == "mcpify"
    names = [tool["name"] for tool in lines[1]["result"]["tools"]]
    assert "list_pets" in names
    assert "create_pet" not in names  # --read-only was baked in
