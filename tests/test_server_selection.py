"""`--server INDEX|NAME`: picking among a spec's declared servers.

Issue #13: specs with several servers[] entries (prod/staging/dev)
used to take the first or require a hand-typed --base-url. These tests
pin the selection rules, the precedence (--base-url > --server >
servers[0]), every failure listing, and the CLI/config wiring.
"""

import json

import pytest

from mcpify.cli import _base_url
from mcpify.cli import main as cli_main
from mcpify.config import validate
from mcpify.spec import SpecError, load_spec


def multi_spec(tmp_path, servers):
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "servers": servers,
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "list_pets",
                    "summary": "List pets",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


THREE = [
    {"url": "https://prod.example.com/v1", "description": "Production"},
    {"url": "https://staging.example.com/v1", "description": "Staging"},
    {"url": "https://dev.example.com/v1"},
]


# ---------------------------------------------------------------------------
# selection rules
# ---------------------------------------------------------------------------

def test_pick_by_one_based_index(tmp_path):
    spec = load_spec(multi_spec(tmp_path, THREE))
    assert _base_url(spec, None, "2") == "https://staging.example.com/v1"
    assert _base_url(spec, None, "1") == "https://prod.example.com/v1"
    assert _base_url(spec, None, "3") == "https://dev.example.com/v1"


def test_pick_by_description_word_case_insensitive(tmp_path):
    spec = load_spec(multi_spec(tmp_path, THREE))
    assert _base_url(spec, None, "staging") == "https://staging.example.com/v1"
    assert _base_url(spec, None, "production") == "https://prod.example.com/v1"


def test_pick_by_url_substring(tmp_path):
    spec = load_spec(multi_spec(tmp_path, THREE))
    assert _base_url(spec, None, "prod") == "https://prod.example.com/v1"
    assert _base_url(spec, None, "dev") == "https://dev.example.com/v1"


def test_index_out_of_range_lists_servers(tmp_path):
    spec = load_spec(multi_spec(tmp_path, THREE))
    with pytest.raises(SpecError) as err:
        _base_url(spec, None, "9")
    message = str(err.value)
    assert "out of range" in message
    assert "3 server(s)" in message
    assert "staging.example.com" in message


def test_unknown_name_lists_servers(tmp_path):
    spec = load_spec(multi_spec(tmp_path, THREE))
    with pytest.raises(SpecError) as err:
        _base_url(spec, None, "qa")
    assert "no server matches" in str(err.value)
    assert "1: https://prod.example.com/v1 (Production)" in str(err.value)


def test_no_servers_with_choice_fails(tmp_path):
    spec = load_spec(multi_spec(tmp_path, []))
    with pytest.raises(SpecError):
        _base_url(spec, None, "1")


def test_base_url_overrides_server_choice(tmp_path):
    spec = load_spec(multi_spec(tmp_path, THREE))
    assert _base_url(spec, "https://override.example", "staging") == "https://override.example"


def test_none_choice_keeps_first_server(tmp_path):
    spec = load_spec(multi_spec(tmp_path, THREE))
    assert _base_url(spec, None, None) == "https://prod.example.com/v1"


def test_selected_server_variables_still_require_defaults(tmp_path):
    servers = [
        {"url": "https://a.example.com/v1"},
        {"url": "https://{region}.example.com/v1", "description": "Regional",
         "variables": {"region": {"enum": ["eu", "us"]}}},
    ]
    spec = load_spec(multi_spec(tmp_path, servers))
    with pytest.raises(SpecError) as err:
        _base_url(spec, None, "2")
    assert "region" in str(err.value)
    assert "--base-url" in str(err.value)


def test_selected_server_variables_use_defaults(tmp_path):
    servers = [
        {"url": "https://a.example.com/v1"},
        {"url": "https://{region}.example.com/v1", "description": "Regional",
         "variables": {"region": {"default": "eu"}}},
    ]
    spec = load_spec(multi_spec(tmp_path, servers))
    assert _base_url(spec, None, "regional") == "https://eu.example.com/v1"


def test_string_server_entries_are_supported(tmp_path):
    spec = load_spec(multi_spec(tmp_path, ["https://one.example.com", "https://two.example.com"]))
    assert _base_url(spec, None, "2") == "https://two.example.com"
    assert _base_url(spec, None, "one") == "https://one.example.com"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

@pytest.fixture()
def serve_http_recorder(monkeypatch):
    calls = []

    def fake_serve_http(server, host, port, token=None, max_body=None):
        calls.append({"base": server.base_url, "host": host, "port": port})

    import mcpify.http_transport as transport

    monkeypatch.setattr(transport, "serve_http", fake_serve_http)
    return calls


def test_serve_flag_selects_server(tmp_path, serve_http_recorder):
    cli_main(["serve", multi_spec(tmp_path, THREE), "--server", "2", "--http", "8080"])
    assert serve_http_recorder[0]["base"] == "https://staging.example.com/v1"


def test_serve_bad_choice_exits_with_listing(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["serve", multi_spec(tmp_path, THREE), "--server", "qa"])
    assert exit_info.value.code == 2
    assert "no server matches" in capsys.readouterr().err


def test_status_flag_selects_server(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class OK(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = HTTPServer(("127.0.0.1", 0), OK)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    servers = [
        {"url": "https://prod.example.com/v1", "description": "Production"},
        {"url": f"http://127.0.0.1:{server.server_port}", "description": "Local"},
    ]
    try:
        cli_main(["status", multi_spec(tmp_path, servers), "--server", "local", "--timeout", "3"])
    except SystemExit as exit_info:
        assert exit_info.code == 0, "the selected (local) server should be reachable"
    else:
        raise AssertionError("status should exit 0 on reachability")
    finally:
        server.shutdown()


def test_config_file_accepts_server_key(tmp_path, serve_http_recorder):
    spec = multi_spec(tmp_path, THREE)
    (tmp_path / ".mcpify.toml").write_text(
        f"[serve]\nspec = '{spec}'\nserver = 'staging'\n", encoding="utf-8"
    )
    assert validate({"serve": {"spec": spec, "server": "staging"}}) == []
    cli_main(["serve", "--config", str(tmp_path / ".mcpify.toml"), "--http", "8080"])
    assert serve_http_recorder[0]["base"] == "https://staging.example.com/v1"


def test_doctor_hints_when_multiple_servers(tmp_path, capsys):
    cli_main(["doctor", multi_spec(tmp_path, THREE)])
    captured = capsys.readouterr()
    assert "--server INDEX|NAME" in captured.out


# ---------------------------------------------------------------------------
# try command wiring (stdin REPL over a selected server)
# ---------------------------------------------------------------------------

def test_try_uses_selected_server(tmp_path, monkeypatch, capsys):
    spec = multi_spec(tmp_path, THREE)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(":q\n"))
    cli_main(["try", spec, "--server", "3"])
    captured = capsys.readouterr()
    assert "https://dev.example.com/v1" in captured.err
