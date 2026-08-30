"""Auth auto-detection from security declarations + HTTP Basic support.

The spec already says how the API authenticates — mcpify reads it, so
`--auth-env VAR` alone wires the right style (bearer/basic/header/query
name). Paid API-to-MCP platforms gate exactly this configuration layer;
here it is, for free, and pinned by tests.
"""

import base64
import json

import pytest

from mcpify.cli import _auth_hint
from mcpify.cli import main as cli_main
from mcpify.tools import AuthConfig, RequestError, detect_auth


def spec_with(schemes, security=None, operation_security=None):
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1"}],
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
    if schemes is not None:
        spec["components"] = {"securitySchemes": schemes}
    if security is not None:
        spec["security"] = security
    if operation_security is not None:
        spec["paths"]["/pets"]["get"]["security"] = operation_security
    return spec


def write_spec(tmp_path, spec):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# detection rules
# ---------------------------------------------------------------------------

def test_bearer_from_security_requirement(tmp_path):
    spec = spec_with(
        {"tok": {"type": "http", "scheme": "bearer"}},
        security=[{"tok": []}],
    )
    detected = detect_auth(spec)
    assert detected["style"] == "bearer"
    assert detected["oauth2"] is False


def test_apikey_header_with_custom_name(tmp_path):
    spec = spec_with(
        {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        security=[{"key": []}],
    )
    detected = detect_auth(spec)
    assert detected["style"] == "header"
    assert detected["name"] == "X-API-Key"


def test_apikey_query(tmp_path):
    spec = spec_with(
        {"key": {"type": "apiKey", "in": "query", "name": "api_key"}},
        security=[{"key": []}],
    )
    assert detect_auth(spec)["style"] == "query"


def test_http_basic(tmp_path):
    spec = spec_with(
        {"creds": {"type": "http", "scheme": "basic"}},
        security=[{"creds": []}],
    )
    assert detect_auth(spec)["style"] == "basic"


def test_swagger2_security_definitions(tmp_path):
    spec = {
        "swagger": "2.0",
        "info": {"title": "T", "version": "1"},
        "securityDefinitions": {"creds": {"type": "basic"}},
        "security": [{"creds": []}],
        "paths": {},
    }
    assert detect_auth(spec)["style"] == "basic"


def test_oauth2_flagged_and_bearer(tmp_path):
    spec = spec_with(
        {"oauth": {"type": "oauth2", "flows": {}}},
        security=[{"oauth": []}],
    )
    detected = detect_auth(spec)
    assert detected["style"] == "bearer"
    assert detected["oauth2"] is True


def test_operation_level_security_used_when_top_level_absent(tmp_path):
    spec = spec_with(
        {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}},
        operation_security=[{"key": []}],
    )
    assert detect_auth(spec)["style"] == "header"


def test_requirement_order_wins_over_declaration_order(tmp_path):
    spec = spec_with(
        {
            "secondary": {"type": "apiKey", "in": "header", "name": "X-Second"},
            "primary": {"type": "http", "scheme": "bearer"},
        },
        security=[{"secondary": []}],
    )
    assert detect_auth(spec)["style"] == "header"


def test_no_security_returns_none():
    assert detect_auth(spec_with(None)) is None


def test_hint_text_matches_style(tmp_path):
    assert _auth_hint(spec_with(
        {"tok": {"type": "http", "scheme": "bearer"}}, security=[{"tok": []}]
    )) == "--auth-env API_TOKEN"
    assert "X-API-Key" in _auth_hint(spec_with(
        {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}, security=[{"key": []}]
    ))
    assert "username:password" in _auth_hint(spec_with(
        {"creds": {"type": "http", "scheme": "basic"}}, security=[{"creds": []}]
    ))


# ---------------------------------------------------------------------------
# HTTP Basic credential
# ---------------------------------------------------------------------------

def test_basic_headers_are_base64(monkeypatch):
    monkeypatch.setenv("CREDS", "alice:s3cret")
    auth = AuthConfig("CREDS", "basic")
    assert auth.headers() == {
        "Authorization": "Basic " + base64.b64encode(b"alice:s3cret").decode()
    }


def test_basic_rejects_value_without_colon(monkeypatch):
    monkeypatch.setenv("CREDS", "alice-no-password")
    with pytest.raises(RequestError) as err:
        AuthConfig("CREDS", "basic").headers()
    assert "username:password" in str(err.value)


def test_basic_missing_env_names_it(monkeypatch):
    monkeypatch.delenv("CREDS", raising=False)
    with pytest.raises(RequestError) as err:
        AuthConfig("CREDS", "basic").headers()
    assert "CREDS" in str(err.value)


def test_unknown_style_rejected():
    with pytest.raises(ValueError):
        AuthConfig("X", "carrier-pigeon")


def test_basic_end_to_end(tmp_path, monkeypatch):
    """Full path: build_request with a basic AuthConfig produces the
    Authorization header exactly as a browser would send it."""
    monkeypatch.setenv("CREDS", "alice:s3cret")
    from mcpify.tools import build_request

    auth = AuthConfig("CREDS", "basic")
    request = build_request(
        "https://api.example.com/v1",
        {"method": "GET", "path": "/pets", "parameters": [], "has_body": False,
         "deprecated": False, "raw_body_content_type": None, "tags": []},
        {},
        auth,
    )
    assert request["headers"]["Authorization"].startswith("Basic ")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

@pytest.fixture()
def serve_recorder(monkeypatch):
    servers = []

    def fake_serve_http(server, host, port, token=None, max_body=None):
        servers.append(server)

    import mcpify.http_transport as transport

    monkeypatch.setattr(transport, "serve_http", fake_serve_http)
    return servers


def test_cli_autodetects_apikey_header(tmp_path, serve_recorder, monkeypatch):
    monkeypatch.setenv("API_KEY", "k-123")
    spec = write_spec(tmp_path, spec_with(
        {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        security=[{"key": []}],
    ))
    cli_main(["serve", spec, "--auth-env", "API_KEY", "--http", "8080"])
    server = serve_recorder[0]
    assert server.auth.style == "header"
    assert server.auth.name == "X-API-Key"


def test_cli_autodetects_basic(tmp_path, serve_recorder, monkeypatch):
    monkeypatch.setenv("CREDS", "bob:hunter2")
    spec = write_spec(tmp_path, spec_with(
        {"creds": {"type": "http", "scheme": "basic"}}, security=[{"creds": []}]
    ))
    cli_main(["serve", spec, "--auth-env", "CREDS", "--http", "8080"])
    assert serve_recorder[0].auth.style == "basic"


def test_explicit_style_wins_over_detection(tmp_path, serve_recorder, monkeypatch):
    monkeypatch.setenv("API_KEY", "k-123")
    spec = write_spec(tmp_path, spec_with(
        {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        security=[{"key": []}],
    ))
    cli_main([
        "serve", spec, "--auth-env", "API_KEY",
        "--auth-style", "bearer",  # user override: send it as bearer instead
        "--http", "8080",
    ])
    assert serve_recorder[0].auth.style == "bearer"


def test_hint_printed_when_no_credential_given(tmp_path, serve_recorder, capsys):
    spec = write_spec(tmp_path, spec_with(
        {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        security=[{"key": []}],
    ))
    cli_main(["serve", spec, "--http", "8080"])
    captured = capsys.readouterr()
    assert "declares authentication" in captured.err
    assert "--auth-env API_KEY --auth-style header --auth-name X-API-Key" in captured.err
    assert serve_recorder[0].auth is None  # nothing injected silently


def test_silent_spec_gets_no_hint(tmp_path, serve_recorder, capsys):
    spec = write_spec(tmp_path, spec_with(None))
    cli_main(["serve", spec, "--http", "8080"])
    assert "declares authentication" not in capsys.readouterr().err


def test_doctor_prints_exact_flags(tmp_path, capsys):
    cli_main(["doctor", write_spec(tmp_path, spec_with(
        {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        security=[{"key": []}],
    ))])
    out = capsys.readouterr().out
    assert "--auth-env API_KEY --auth-style header --auth-name X-API-Key" in out


def test_detection_applies_in_try(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("API_KEY", "k-123")
    spec = write_spec(tmp_path, spec_with(
        {"key": {"type": "apiKey", "in": "query", "name": "api_key"}},
        security=[{"key": []}],
    ))
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(":q\n"))
    cli_main(["try", spec, "--auth-env", "API_KEY"])
    err = capsys.readouterr().err
    assert "auto-detected from the spec -> query" in err
