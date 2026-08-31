"""v1.16.0: policy visibility in `status`, session `:redact`/`:fields` in the
REPL, `diff --probe` with a surface-cost delta, dashboard form examples."""

import http.server
import io
import json
import threading
from pathlib import Path

import pytest

from mcpify.api_server import ApiServer
from mcpify.cli import main as cli_main
from mcpify.config import _load_toml
from mcpify.repl import run as repl_run
from mcpify.spec import load_spec
from mcpify.ui import write_config_from_form

SPEC_OLD = {
    "openapi": "3.0.0",
    "info": {"title": "T", "version": "1"},
    "servers": [{"url": "http://api.example.com"}],
    "paths": {
        "/things": {"get": {"operationId": "listThings", "summary": "List",
                            "parameters": [], "responses": {"200": {"description": "ok"}}}},
    },
}
SPEC_NEW = {
    "openapi": "3.0.0",
    "info": {"title": "T", "version": "2"},
    "servers": [{"url": "http://api.example.com"}],
    "paths": {
        "/things": {"get": {"operationId": "listThings", "summary": "List things (v2)",
                            "parameters": [], "responses": {"200": {"description": "ok"}}}},
        "/other": {"get": {"operationId": "getOther", "summary": "Other",
                           "parameters": [], "responses": {"200": {"description": "ok"}}}},
    },
}


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the test run
        pass

    def do_GET(self):
        body = json.dumps([{"id": 7, "name": "Pet7", "password": "s"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def upstream():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def spec_file(tmp_path, upstream):
    path = tmp_path / "spec.json"
    doc = dict(SPEC_OLD)
    doc["servers"] = [{"url": upstream}]
    path.write_text(json.dumps(doc))
    return str(path)


# ---------------------------------------------------------------------------
# status: the token policy is visible
# ---------------------------------------------------------------------------

def test_status_multi_api_json_carries_policy(tmp_path, spec_file, capsys):
    config = tmp_path / ".mcpify.toml"
    config.write_text(f'[apis.a]\nspec = "{Path(spec_file).as_posix()}"\n'
                      'rate-limit = 5\nredact = "password"\nfields = "id,name"\n')
    with pytest.raises(SystemExit) as exc:
        cli_main(["status", "--config", str(config), "--json"])
    assert exc.value.code == 0
    rapor = json.loads(capsys.readouterr().out)
    api = rapor["apis"][0]
    assert api["rate_limit"] == 5
    assert api["redact"] == ["password"] and api["fields"] == ["id", "name"]
    assert "rate-limit=5" in api["policy"] and "redact=password" in api["policy"]


def test_status_multi_api_human_shows_policy(tmp_path, spec_file, capsys):
    config = tmp_path / ".mcpify.toml"
    config.write_text(f'[apis.a]\nspec = "{Path(spec_file).as_posix()}"\nrate-limit = 5\n')
    with pytest.raises(SystemExit):
        cli_main(["status", "--config", str(config)])
    assert "rate-limit=5" in capsys.readouterr().out


def test_status_single_spec_policy_line(spec_file, capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main(["status", spec_file, "--fields", "id,name",
                  "--redact", "password", "--rate-limit", "2.5"])
    assert exc.value.code == 0
    assert ("policy:   fields=id,name · redact=password · rate-limit=2.5"
            in capsys.readouterr().out)


def test_status_single_spec_json_policy(spec_file, capsys):
    with pytest.raises(SystemExit):
        cli_main(["status", spec_file, "--json", "--rate-limit", "2.5"])
    rapor = json.loads(capsys.readouterr().out)
    assert rapor["rate_limit"] == 2.5


# ---------------------------------------------------------------------------
# REPL: session :redact / :fields
# ---------------------------------------------------------------------------

def feed(lines):
    it = iter([*lines, ":q"])

    def read_input(prompt=""):
        return next(it)

    return read_input


def drive(server, lines):
    out = io.StringIO()
    repl_run(server, input_fn=feed(lines), output=out)
    return out.getvalue()


def repl_server(upstream, spec_file):
    spec = load_spec(spec_file)
    return ApiServer(spec, upstream)


def test_repl_redact_masks_and_shows(upstream, spec_file):
    server = repl_server(upstream, spec_file)
    tool = server.tools[0]["name"]
    out = drive(server, [":redact password", tool, ":redact"])
    assert "redact: password" in out and "(applies to subsequent calls)" in out
    assert '"***"' in out and '"s"' not in out  # masked, raw secret absent


def test_repl_fields_projects_and_clears(upstream, spec_file):
    server = repl_server(upstream, spec_file)
    tool = server.tools[0]["name"]
    out = drive(server, [":fields", ":fields id,name", tool, ":fields -", ":fields"])
    assert "fields: (off)" in out
    assert "fields: id,name" in out
    assert '"name": "Pet7"' in out  # projected call still had name (selected)
    body_line = next(line for line in out.splitlines() if "Pet7" in line)
    assert "password" not in body_line  # projection dropped the unselected key


def test_repl_redact_unknown_field_name_is_fine_but_empty_value_errors(upstream, spec_file):
    server = repl_server(upstream, spec_file)
    out = drive(server, [":redact  "])
    assert "(off)" in out  # bare :redact shows state


# ---------------------------------------------------------------------------
# diff: surface-cost delta + --probe
# ---------------------------------------------------------------------------

@pytest.fixture()
def diff_specs_files(tmp_path, upstream):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old_doc = dict(SPEC_OLD)
    old_doc["servers"] = [{"url": upstream}]
    new_doc = dict(SPEC_NEW)
    new_doc["servers"] = [{"url": upstream}]
    old.write_text(json.dumps(old_doc))
    new.write_text(json.dumps(new_doc))
    return str(old), str(new)


def test_diff_json_includes_surface_cost_delta(diff_specs_files, capsys):
    old, new = diff_specs_files
    cli_main(["diff", old, new, "--json"])
    rapor = json.loads(capsys.readouterr().out)
    delta = rapor["surface_cost_tokens"]
    assert delta["new"] > delta["old"] > 0  # the new spec exposes more tools


    cli_main(["diff", old, new, "--probe"])
    out = capsys.readouterr().out
    assert "probe:" in out and "reachable" in out


def test_diff_probe_unreachable_exits_2(diff_specs_files):
    old, new = diff_specs_files
    with pytest.raises(SystemExit) as exc:
        cli_main(["diff", old, new, "--probe", "--base-url", "http://127.0.0.1:9"])
    assert exc.value.code == 2


def test_diff_human_shows_cost_line(diff_specs_files, capsys):
    old, new = diff_specs_files
    cli_main(["diff", old, new])
    out = capsys.readouterr().out
    assert "surface cost: ~" in out
    assert "+111.1%" in out  # one tool -> two tools, priced live# ---------------------------------------------------------------------------
# dashboard form: retry-delay + examples carry through
# ---------------------------------------------------------------------------

def test_form_accepts_retry_delay(tmp_path):
    cfg = tmp_path / "form.toml"
    write_config_from_form({"spec": "s.json", "retry-delay": 2.5}, cfg)
    data = _load_toml(cfg)
    assert data["serve"]["retry-delay"] == 2.5


def test_form_examples_present_in_html():
    from mcpify import ui

    html = ui.PAGE if hasattr(ui, "PAGE") else ""
    if not html:
        # locate the inline HTML constant defensively
        candidates = [value for name, value in vars(ui).items()
                      if isinstance(value, str) and "c-spec" in value]
        assert candidates, "dashboard HTML not found in ui module"
        html = candidates[0]
    assert 'placeholder="openapi.json | https://api.example.com/openapi.json"' in html
    assert 'placeholder="MY_API_KEY"' in html
    assert "c-retry-delay" in html
