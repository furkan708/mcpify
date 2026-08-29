"""CLI tests: list / doctor / serve."""

import json

import pytest

from mcpify.cli import main

SPEC = "examples/petstore.json"


def test_list_shows_all_tools(capsys):
    main(["list", SPEC])
    out = capsys.readouterr().out
    assert "7 tools" in out
    for name in ["list_pets", "create_pet", "get_pet", "delete_pet", "add_vaccination"]:
        assert name in out


def test_list_read_only_filters_writes(capsys):
    main(["list", SPEC, "--read-only"])
    out = capsys.readouterr().out
    assert "list_pets" in out
    assert "create_pet" not in out
    assert "delete_pet" not in out
    assert "4 tools" in out


def test_list_tag_filter(capsys):
    main(["list", SPEC, "--tag", "vaccinations"])
    out = capsys.readouterr().out
    assert "add_vaccination" in out
    assert "list_pets" not in out


def test_list_exclude(capsys):
    main(["list", SPEC, "--exclude", "/pets/{petId}/vaccinations"])
    out = capsys.readouterr().out
    assert "add_vaccination" not in out
    assert "list_pets" in out


def test_list_json(capsys):
    main(["list", SPEC, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert {p["method"] for p in payload} <= {"GET", "POST", "DELETE"}
    assert len(payload) == 7


def test_list_zero_tools_fails(tmp_path):
    spec_path = tmp_path / "empty.json"
    spec_path.write_text(json.dumps({"openapi": "3.0.0", "info": {}, "paths": {"/x": {}}}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["list", str(spec_path)])
    assert exc.value.code == 2


def test_list_bad_spec_fails():
    with pytest.raises(SystemExit) as exc:
        main(["list", "/does/not/exist.json"])
    assert exc.value.code == 2


def test_doctor_reports(tmp_path, capsys):
    # copy of the petstore spec; all operations have ids and summaries
    import shutil

    shutil.copy(SPEC, tmp_path / "p.json")
    main(["doctor", str(tmp_path / "p.json")])
    out = capsys.readouterr().out
    assert "7 operations" in out
    assert "agent-friendly" in out


def test_doctor_warns_on_missing_ids(tmp_path, capsys):
    spec_path = tmp_path / "anon.json"
    spec_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "A", "version": "1"},
                "servers": [{"url": "https://x.test"}],
                "paths": {"/a": {"get": {}}},
            }
        ),
        encoding="utf-8",
    )
    main(["doctor", str(spec_path)])
    out = capsys.readouterr().out
    assert "no operationId" in out


def test_doctor_warns_on_variabled_server(tmp_path, capsys):
    spec_path = tmp_path / "var.json"
    spec_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "A", "version": "1"},
                "servers": [{"url": "https://{tenant}.x.test"}],
                "paths": {"/a": {"get": {"operationId": "a", "summary": "s"}}},
            }
        ),
        encoding="utf-8",
    )
    main(["doctor", str(spec_path)])
    out = capsys.readouterr().out
    assert "--base-url" in out


def test_doctor_json_is_machine_readable_and_returns_warning_code(tmp_path, capsys):
    spec_path = tmp_path / "anon.json"
    spec_path.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "A", "version": "1"},
        "paths": {"/a": {"get": {}}},
    }), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["doctor", str(spec_path), "--json"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["operations"] == 1
    assert payload["missing_operation_id"] == 1
    assert payload["warnings"]
    assert captured.err == ""


def test_doctor_json_clean_spec_returns_zero(tmp_path, capsys):
    import shutil

    shutil.copy(SPEC, tmp_path / "p.json")
    main(["doctor", str(tmp_path / "p.json"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["warnings"] == []


def test_serve_requires_base_url(tmp_path):
    spec_path = tmp_path / "noserver.json"
    spec_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "A", "version": "1"},
                "paths": {"/a": {"get": {"operationId": "a", "summary": "s"}}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        main(["serve", str(spec_path)])
    assert exc.value.code == 2


def test_serve_runs_one_roundtrip(tmp_path, capsys, monkeypatch):
    import io

    spec_path = tmp_path / "mini.json"
    spec_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "A", "version": "1"},
                "servers": [{"url": "https://api.example.test"}],
                "paths": {"/a": {"get": {"operationId": "get_a", "summary": "Get a"}}},
            }
        ),
        encoding="utf-8",
    )
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    import sys as _sys

    monkeypatch.setattr(_sys, "stdin", io.StringIO(request + "\n"))
    main(["serve", str(spec_path), "--read-only"])
    captured = capsys.readouterr()
    err, out = captured.err, captured.out
    assert "serving 1 tools" in err
    response = json.loads(out.splitlines()[0])
    assert response["result"]["serverInfo"]["name"] == "mcpify"
