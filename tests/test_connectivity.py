"""CLI-level glue for the new surfaces: --http wiring, OAuth2 flag rules,
config-file keys, wizard option 5, and a `mcpify try` smoke test."""

import argparse
import io
from pathlib import Path

import pytest

from mcpify.cli import main as cli_main
from mcpify.config import apply_to_namespace, load_config, resolve, run_wizard, validate

SPEC = "examples/petstore.json"


def lines(*items):
    queue = list(items)

    def read(_prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read


# ---------------------------------------------------------------------------
# --http wiring (serve_http is patched: no real server starts)
# ---------------------------------------------------------------------------

@pytest.fixture()
def serve_http_recorder(monkeypatch):
    calls = []

    def fake_serve_http(server, host, port, token=None, max_body=None):
        calls.append({"host": host, "port": port, "token": token})

    import mcpify.http_transport as transport

    monkeypatch.setattr(transport, "serve_http", fake_serve_http)
    return calls


def test_serve_http_flag_wiring(tmp_path, serve_http_recorder):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    cli_main(["serve", str(spec), "--http", "8080", "--http-token", "sekret"])
    assert serve_http_recorder == [{"host": "127.0.0.1", "port": 8080, "token": "sekret"}]


def test_serve_http_host_port_split(tmp_path, serve_http_recorder):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    cli_main(["serve", str(spec), "--http", "0.0.0.0:9000"])
    assert serve_http_recorder[0]["host"] == "0.0.0.0"
    assert serve_http_recorder[0]["port"] == 9000


def test_http_token_env_fallback(tmp_path, serve_http_recorder, monkeypatch):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("MCPIFY_HTTP_TOKEN", "from-env")
    cli_main(["serve", str(spec), "--http", "8081"])
    assert serve_http_recorder[0]["token"] == "from-env"


def test_http_bad_bind_exits_with_message(tmp_path, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["serve", str(spec), "--http", "host:port"])
    assert exit_info.value.code == 2
    assert "--http" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# OAuth2 flag rules
# ---------------------------------------------------------------------------

def test_auth_env_and_oauth2_are_mutually_exclusive(tmp_path, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli_main([
            "serve", str(spec),
            "--auth-env", "TOKEN", "--oauth2-token-url", "http://idp/token",
            "--oauth2-client-id-env", "CID",
        ])
    assert exit_info.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_oauth2_requires_client_id_env(tmp_path, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["serve", str(spec), "--oauth2-token-url", "http://idp/token"])
    assert exit_info.value.code == 2
    assert "--oauth2-client-id-env" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# config-file keys
# ---------------------------------------------------------------------------

def test_config_accepts_oauth2_and_http_keys(tmp_path):
    config = tmp_path / ".mcpify.toml"
    config.write_text(
        "[serve]\n"
        "spec = 'examples/petstore.json'\n"
        "oauth2-token-url = 'https://idp.example/token'\n"
        "oauth2-client-id-env = 'OAUTH2_ID'\n"
        "oauth2-client-secret-env = 'OAUTH2_SECRET'\n"
        "oauth2-scope = 'read write'\n"
        "oauth2-client-auth = 'body'\n"
        "http = '8080'\n",
        encoding="utf-8",
    )
    _path, data = load_config(str(config))
    assert validate(data) == []  # no unknown-key complaints
    settings = resolve(data)
    args = argparse.Namespace(
        base_url=None, auth_env=None, auth_style="bearer", auth_name=None,
        read_only=False, enable_preview=False, cache_ttl=0.0, retry_delay=1.0,
        log_file=None, timeout=30.0, oauth2_token_url=None, oauth2_client_id_env=None,
        oauth2_client_secret_env=None, oauth2_scope=None, oauth2_client_auth="basic",
        http=None, http_token=None,
    )
    apply_to_namespace(settings, args)
    assert args.oauth2_token_url == "https://idp.example/token"
    assert args.oauth2_client_auth == "body"
    assert args.http == "8080"


# ---------------------------------------------------------------------------
# wizard: option 5 writes OAuth2 settings
# ---------------------------------------------------------------------------

def test_wizard_oauth2_option(monkeypatch):
    monkeypatch.delenv("OAUTH2_ID", raising=False)
    monkeypatch.delenv("OAUTH2_SECRET", raising=False)
    answers = iter([
        "examples/petstore.json",  # spec
        "https://api.example.com",  # base url
        "5",  # auth choice: oauth2
        "https://idp.example/token",  # token url
        "OAUTH2_ID",  # client id env
        "OAUTH2_SECRET",  # client secret env
        "read write",  # scope
        "n",  # read-only
        "0",  # cache ttl
        "0",  # retry
        "n",  # lazy
        "auto",  # format
    ])
    settings, warnings = run_wizard(answers, None)
    assert settings["oauth2-token-url"] == "https://idp.example/token"
    assert settings["oauth2-client-id-env"] == "OAUTH2_ID"
    assert settings["oauth2-client-secret-env"] == "OAUTH2_SECRET"
    assert settings["oauth2-scope"] == "read write"
    # unset env vars surface as actionable warnings
    assert any("OAUTH2_ID" in w for w in warnings)
    assert any("OAUTH2_SECRET" in w for w in warnings)


# ---------------------------------------------------------------------------
# `mcpify try` smoke test through the real CLI
# ---------------------------------------------------------------------------

def test_try_smoke_through_cli(tmp_path, monkeypatch, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(":q\n"))
    cli_main(["try", str(spec)])
    captured = capsys.readouterr()
    assert "mcpify try" in captured.out
    assert "bye" in captured.out


def test_try_reads_config(tmp_path, monkeypatch, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(Path(SPEC).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".mcpify.toml").write_text(
        f"[serve]\nspec = '{spec}'\nname = 'from-config'\n", encoding="utf-8"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(":q\n"))
    monkeypatch.chdir(tmp_path)
    cli_main(["try"])
    captured = capsys.readouterr()
    assert "config: " in captured.err
    assert "from-config" in captured.err
