"""v1.15.0: authenticated `doctor --probe` (+ --fail-on-http-error CI gate),
projection/redaction metrics, lazy-surface pricing, `init --probe`, and
dashboard-form token-economics keys."""

import builtins
import http.server
import json
import threading

import pytest

import mcpify.metrics as metrics
from mcpify.api_server import ApiServer
from mcpify.cli import main as cli_main
from mcpify.config import _load_toml, validate
from mcpify.http_client import count_redact_targets, redact_json
from mcpify.probe import pick_probe_operation, run_probe
from mcpify.spec import load_spec
from mcpify.tools import AuthConfig
from mcpify.ui import write_config_from_form

SPEC_DOC = {
    "openapi": "3.0.0",
    "info": {"title": "T", "version": "1"},
    "servers": [{"url": "http://api.example.com"}],
    "paths": {
        "/things": {"get": {"operationId": "listThings", "summary": "List things",
                            "parameters": [], "responses": {"200": {"description": "ok"}}}},
        "/locked": {"get": {"operationId": "getLocked", "summary": "Needs a param",
                            "parameters": [{"name": "k", "in": "query", "required": True,
                                            "schema": {"type": "string"}}],
                            "responses": {"200": {"description": "ok"}}}},
    },
}


class ProbeUpstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the test run
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good-token":
            body = b'{"error":"unauthorized"}'
            self.send_response(401)
        else:
            body = json.dumps([{"id": 1, "password": "x", "client_secret": "y"}]).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def upstream():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), ProbeUpstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def spec_file(tmp_path, upstream):
    path = tmp_path / "spec.json"
    doc = dict(SPEC_DOC)
    doc["servers"] = [{"url": upstream}]
    path.write_text(json.dumps(doc))
    return str(path)


def rpc(server, method, params=None, request_id=2):
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


# ---------------------------------------------------------------------------
# probe: authentication + strict mode
# ---------------------------------------------------------------------------

def test_probe_without_auth_is_reachable_at_401(upstream, spec_file):
    report = run_probe(load_spec(spec_file), upstream, 5.0)
    assert report["ok"] is True and report["status"] == 401
    assert report["authenticated"] is False
    assert report["path"] == "/things"


def test_probe_with_auth_proves_the_credential(monkeypatch, upstream, spec_file):
    monkeypatch.setenv("TOK", "good-token")
    auth = AuthConfig("TOK")
    report = run_probe(load_spec(spec_file), upstream, 5.0, auth=auth)
    assert report["ok"] is True and report["status"] == 200
    assert report["authenticated"] is True


def test_probe_strict_mode_fails_on_http_errors(upstream, spec_file):
    report = run_probe(load_spec(spec_file), upstream, 5.0, fail_on_http_error=True)
    assert report["ok"] is False
    assert "--fail-on-http-error" in report["error"]


def test_doctor_cli_auth_gate_passes(monkeypatch, upstream, spec_file, capsys):
    monkeypatch.setenv("TOK", "good-token")
    assert cli_main(["doctor", spec_file, "--probe", "--base-url", upstream,
                     "--auth-env", "TOK"]) is None
    assert "authenticated" in capsys.readouterr().out


def test_doctor_cli_auth_gate_strict_exits_1(monkeypatch, upstream, spec_file, capsys):
    monkeypatch.setenv("TOK", "wrong-token")
    with pytest.raises(SystemExit) as exc:
        cli_main(["doctor", spec_file, "--probe", "--base-url", upstream,
                  "--auth-env", "TOK", "--fail-on-http-error"])
    assert exc.value.code == 1


def test_doctor_cli_strict_without_auth_still_reachable(upstream, spec_file):
    # 401 with no credential configured is a working API — default mode passes
    assert cli_main(["doctor", spec_file, "--probe", "--base-url", upstream]) is None


def test_pick_probe_operation_skips_locked(monkeypatch, spec_file):
    assert pick_probe_operation(load_spec(spec_file)) == ("GET", "/things")


# ---------------------------------------------------------------------------
# metrics: projection + redaction counters
# ---------------------------------------------------------------------------

def test_count_redact_targets():
    data = {"a": {"password": "x", "ok": 1, "none": None},
            "list": [{"TOKEN": "y"}, "plain", {"deep": {"client_secret": "z"}}]}
    assert count_redact_targets(data, frozenset({"password", "token", "client_secret"})) == 3
    assert count_redact_targets(data, frozenset()) == 0
    assert count_redact_targets({"password": None}, frozenset({"password"})) == 0


def test_redact_json_and_counter_agree():
    data = {"user": {"Password": "x", "name": "n"}, "token": "t"}
    secrets = frozenset({"password", "token"})
    assert count_redact_targets(data, secrets) == 2
    masked = redact_json(data, secrets)
    assert masked == {"user": {"Password": "***", "name": "n"}, "token": "***"}


def test_metrics_counters_for_projection_and_redaction(monkeypatch, upstream, spec_file):
    monkeypatch.setenv("TOK", "good-token")
    metrics.enable()
    try:
        spec = load_spec(spec_file)
        server = ApiServer(spec, upstream, auth=AuthConfig("TOK"),
                           fields=frozenset({"id", "password", "client_secret"}),
                           redact=frozenset({"password", "client_secret"}))
        rpc(server, "tools/call", {"name": "listthings", "arguments": {}}, 2)
        out = metrics.render_prometheus()
        assert "mcpify_projection_responses_total" in out
        assert "mcpify_redactions_total" in out
        redactions = [line for line in out.splitlines()
                      if line.startswith("mcpify_redactions_total")]
        assert any(line.endswith("2.0") for line in redactions)  # password + client_secret
    finally:
        metrics.disable()


# ---------------------------------------------------------------------------
# list --lazy pricing
# ---------------------------------------------------------------------------

def test_list_cost_lazy_shows_meta_surface(spec_file, capsys):
    cli_main(["list", spec_file, "--cost", "--lazy"])
    out = capsys.readouterr().out
    assert "lazy surface:" in out and "three meta tools" in out
    assert "enable with --lazy" in out
    assert "surface cost:" in out  # the full-surface line stays


def test_list_cost_without_lazy_has_no_meta_line(spec_file, capsys):
    cli_main(["list", spec_file, "--cost"])
    assert "lazy surface:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# init --probe
# ---------------------------------------------------------------------------

def _run_init_probe(tmp_path, monkeypatch, spec_file, base, token):
    cfg = tmp_path / ".mcpify.toml"
    cevaplar = iter(["2", "TOK", "n", "", "", "", "n", ""])  # auth/read-only/ttl/retry/delay/lazy/format
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(cevaplar))
    monkeypatch.setenv("TOK", token)
    cli_main(["init", "--config", str(cfg), "--probe",
              "--spec", spec_file, "--base-url", base])
    return cfg


def test_init_probe_reachable_writes_config_and_probes(tmp_path, monkeypatch, spec_file, upstream, capsys):
    cfg = _run_init_probe(tmp_path, monkeypatch, spec_file, upstream, "good-token")
    out = capsys.readouterr().out
    assert cfg.exists()
    assert "probe:" in out and "authenticated" in out
    data = _load_toml(cfg)
    assert validate(data) == []
    assert data["serve"]["auth-env"] == "TOK"


def test_init_probe_unreachable_exits_1(tmp_path, monkeypatch, spec_file, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_init_probe(tmp_path, monkeypatch, spec_file, "http://127.0.0.1:9", "good-token")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# dashboard form: token-economics keys
# ---------------------------------------------------------------------------

def test_form_accepts_fields_redact_rate_limit(tmp_path):
    cfg = tmp_path / "form.toml"
    target = write_config_from_form(
        {"spec": "s.json", "rate-limit": 2.5, "fields": "id,name", "redact": "password,token"},
        cfg)
    assert target == str(cfg)
    data = _load_toml(cfg)
    assert validate(data) == []
    assert data["serve"]["rate-limit"] == 2.5
    assert data["serve"]["fields"] == "id,name"
    assert data["serve"]["redact"] == "password,token"


def test_form_still_rejects_unknown_keys(tmp_path):
    with pytest.raises(ValueError, match="unknown config key"):
        write_config_from_form({"spec": "s.json", "nonsense": "x"}, tmp_path / "x.toml")
