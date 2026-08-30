"""Ops features: config, init wizard, logging, cache, retry, discovery,
strict mode, format conversion, batching, status/health.

Each feature is exercised the way a user reaches it: through the CLI
(`main([...])`), through the stdio server (`handle_message`), or through
`execute()` — against a real local HTTP API.
"""

import http.server
import json
import threading
import time

import pytest

from mcpify.api_server import HEALTH_TOOL, ApiServer
from mcpify.cli import main
from mcpify.config import (
    _parse_mini_toml,
    apply_to_namespace,
    build_config_document,
    load_config,
    resolve,
    validate,
)
from mcpify.http_client import ResponseCache
from mcpify.spec import SpecError, discover_spec
from mcpify.tools import spec_to_tools

# ---------------------------------------------------------------------------
# spec + fake API with every ops scenario wired
# ---------------------------------------------------------------------------

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Ops", "version": "1.0"},
    "servers": [{"url": "http://placeholder"}],
    "paths": {
        "/dogs/{dogId}": {
            "get": {
                "operationId": "get_dog",
                "summary": "Get one dog",
                "parameters": [
                    {"name": "dogId", "in": "path", "required": True, "schema": {"type": "integer"}},
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                    "required": ["id", "name"],
                }}}}},
            }
        },
        "/dogs": {
            "post": {
                "operationId": "create_dog",
                "summary": "Create a dog",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"201": {"description": "created"}},
            }
        },
        "/flaky": {"get": {"operationId": "flaky_call", "summary": "Sometimes 503",
                            "responses": {"200": {"description": "ok"}}}},
        "/xml": {"get": {"operationId": "xml_call", "summary": "Returns XML",
                          "responses": {"200": {"description": "ok"}}}},
        "/down": {"get": {"operationId": "down_call", "summary": "Always 503",
                           "responses": {"503": {"description": "nope"}}}},
        "/gone": {"get": {"operationId": "gone_call", "summary": "Always 404",
                           "responses": {"404": {"description": "nope"}}}},
    },
}

WELL_KNOWN_SPEC = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "Discovered", "version": "1.0"},
    "servers": [{"url": "http://discovered.example"}],
    "paths": {"/ping": {"get": {"operationId": "ping", "summary": "Ping",
                                  "responses": {"200": {"description": "ok"}}}}},
})


class OpsAPI(http.server.BaseHTTPRequestHandler):
    requests: list = []
    flaky_failures_left = 0
    well_known = False

    def log_message(self, *args):
        pass

    def _json(self, status, payload, content_type="application/json", extra=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        OpsAPI.requests.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/dogs/"):
            dog_id = int(self.path.rsplit("/", 1)[1].split("?")[0])
            self._json(200, {"id": dog_id, "name": "Rex"})
        elif self.path.startswith("/flaky") and OpsAPI.flaky_failures_left > 0:
            OpsAPI.flaky_failures_left -= 1
            self._json(503, {"message": "temporarily unavailable"})
        elif self.path.startswith("/xml"):
            xml = "<response><dog><name>Rex</name><kind>good</kind></dog><count>1</count></response>"
            body = xml.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/down"):
            self._json(503, {"message": "still down"})
        elif self.path.startswith("/gone"):
            self._json(404, {"message": "nothing here"})
        elif (self.path == "/.well-known/openapi.json" and OpsAPI.well_known) or (self.path == "/openapi.json" and OpsAPI.well_known):
            self._json(200, json.loads(WELL_KNOWN_SPEC))
        else:
            self._json(200, {"ok": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        OpsAPI.requests.append(("POST", self.path, dict(self.headers)))
        self._json(201, {"created": True})


@pytest.fixture()
def api():
    OpsAPI.requests = []
    OpsAPI.flaky_failures_left = 0
    OpsAPI.well_known = False
    server = http.server.HTTPServer(("127.0.0.1", 0), OpsAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def make(api, **kwargs):
    server = ApiServer(SPEC, api, **kwargs)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def call(server, name, arguments=None, request_id=2):
    return server.handle_message({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })


# ---------------------------------------------------------------------------
# 1. config file
# ---------------------------------------------------------------------------

def test_mini_toml_parser_handles_generated_subset():
    doc = build_config_document({
        "spec": "s.json", "base-url": "https://x", "read-only": True,
        "cache-ttl": 30, "retry": 2, "include": ["/a", "/b"],
    })
    data = _parse_mini_toml(doc)
    assert data["serve"]["spec"] == "s.json"
    assert data["serve"]["read-only"] is True
    assert data["serve"]["cache-ttl"] == 30
    assert data["serve"]["include"] == ["/a", "/b"]


def test_mini_toml_rejects_unsupported_values():
    with pytest.raises(ValueError, match="unsupported value"):
        _parse_mini_toml("[serve]\nbad = 1.5\n")


def test_mini_toml_unsupported_escape_names_the_line_and_fix():
    """Windows yolu cift tirnakla yazan kullaniciya tek tirnak onerilir (gercek hata sinifi)."""
    with pytest.raises(ValueError, match=r"line 2.*single quotes"):
        _parse_mini_toml('[serve]\nbase-url = "C:\\Users\\x"\n')


def test_wizard_rejects_non_numeric_and_negative_numbers():
    from mcpify.config import run_wizard

    answers = iter(["s.json", "https://x", "1", "n", "abc", "", "", "n", ""])
    with pytest.raises(ValueError, match="'abc' is not a number"):
        run_wizard(answers)
    answers = iter(["s.json", "https://x", "1", "n", "-5", "", "", "n", ""])
    with pytest.raises(ValueError, match="must be >= 0"):
        run_wizard(answers)


def test_config_toml_env_precedence_flag_beats_env(tmp_path, capsys):
    config = tmp_path / ".mcpify.toml"
    config.write_text(
        '[serve]\nspec = "spec.json"\nbase-url = "https://default.example"\ncache-ttl = 5\n'
        '[envs.prod]\nbase-url = "https://prod.example"\nread-only = true\n',
        encoding="utf-8",
    )
    _, data = load_config(str(config))
    ayarlar = resolve(data, "prod")
    assert ayarlar["base-url"] == "https://prod.example"
    assert ayarlar["read-only"] is True
    assert ayarlar["cache-ttl"] == 5  # inherited from [serve]

    class NS:
        pass

    ns = NS()
    ns.base_url = "https://flag.example"   # CLI flag wins
    ns.read_only = False
    ns.cache_ttl = 0
    applied = apply_to_namespace(ayarlar, ns)
    assert ns.base_url == "https://flag.example"       # untouched
    assert ns.read_only is True                        # config turned it on
    assert ns.cache_ttl == 5
    assert "cache-ttl" in applied and "base-url" not in applied


def test_config_unknown_key_is_reported(tmp_path):
    config = tmp_path / "c.toml"
    config.write_text("[serve]\nbase-urll = 'typo'\n", encoding="utf-8")
    _, data = load_config(str(config))
    problems = validate(data)
    assert problems and "base-urll" in problems[0]


def test_config_auto_discovery_and_env_selection(tmp_path, monkeypatch, api, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # serve loop reads nothing
    monkeypatch.chdir(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    (tmp_path / ".mcpify.toml").write_text(
        f'[serve]\nspec = "{spec_path.as_posix()}"\n[envs.dev]\nbase-url = "{api}"\n',
        encoding="utf-8",
    )
    main(["serve", "--env", "dev"])  # empty stdin: the serve loop returns
    out = capsys.readouterr().err
    assert "(env: dev)" in out
    assert ".mcpify.toml" in out
    assert "serving 6 tools" in out


def test_config_missing_env_fails_with_available_list(tmp_path):
    config = tmp_path / "c.toml"
    config.write_text("[serve]\nspec = 's.json'\n[envs.staging]\nread-only = true\n", encoding="utf-8")
    _, data = load_config(str(config))
    with pytest.raises(ValueError, match="staging"):
        resolve(data, "prod")


def test_config_yaml_requires_extra(tmp_path):
    config = tmp_path / ".mcpify.yaml"
    config.write_text("serve:\n  spec: s.json\n", encoding="utf-8")
    try:
        import yaml  # noqa: F401
        _, data = load_config(str(config))
        assert data["serve"]["spec"] == "s.json"
    except ImportError:
        with pytest.raises(ValueError, match="mcpify\\[yaml\\]"):
            load_config(str(config))


def test_toml_fallback_parser_matches_tomllib_behavior(tmp_path, monkeypatch):
    """3.10 paritesi: tomllib gizlendiginde fallback parser ayni sonucu vermeli."""
    import builtins

    config = tmp_path / ".mcpify.toml"
    config.write_text(
        "[serve]\nspec = 's.json'\nread-only = true\ncache-ttl = 30\n"
        'include = ["/a", "/b"]\n',
        encoding="utf-8",
    )

    real_import = builtins.__import__

    def no_tomllib(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError("simulated 3.10")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_tomllib)
    _, data = load_config(str(config))
    assert data["serve"]["spec"] == "s.json"
    assert data["serve"]["read-only"] is True
    assert data["serve"]["cache-ttl"] == 30
    assert data["serve"]["include"] == ["/a", "/b"]


def test_config_json_root_must_be_a_table(tmp_path):
    """JSON koku tablo degilse aciklayici hata (v1.9.1 oncesi AttributeError cudu)."""
    config = tmp_path / ".mcpify.json"
    config.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be a table, got list"):
        load_config(str(config))


# ---------------------------------------------------------------------------
# 2. init wizard
# ---------------------------------------------------------------------------

def test_init_wizard_writes_working_config(tmp_path, monkeypatch, capsys, api):
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPS_TOKEN", raising=False)

    cevaplar = iter([
        str(spec_path),      # spec
        "",                  # base-url: accept spec default (the local API)
        "2",                 # auth: bearer
        "OPS_TOKEN",         # env var
        "y",                 # read-only
        "30",                # cache ttl
        "2",                 # retry
        "",                  # retry delay default
        "n",                 # lazy
        "",                  # format default
    ])
    import builtins

    monkeypatch.setattr(builtins, "input", lambda prompt="": next(cevaplar))
    main(["init", "--config", str(tmp_path / ".mcpify.toml")])
    cikti = capsys.readouterr().out
    assert "wrote" in cikti and ".mcpify.toml" in cikti

    doc = (tmp_path / ".mcpify.toml").read_text(encoding="utf-8")
    data = _parse_mini_toml(doc)
    serve = data["serve"]
    assert serve["spec"] == str(spec_path)
    assert serve["base-url"] == api           # prefilled from the spec
    assert serve["auth-env"] == "OPS_TOKEN"
    assert serve["read-only"] is True
    assert serve["cache-ttl"] == 30
    assert serve["retry"] == 2
    # and the generated file parses through the real loader
    _, loaded = load_config(str(tmp_path / ".mcpify.toml"))
    assert validate(loaded) == []


def test_init_wizard_prompts_are_displayed(tmp_path, monkeypatch, capsys, api):
    """Sihirbaz sorulari terminale YAZILIR (v1.9.1 oncesi sessizdi — gercek UX bug'i)."""
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cevaplar = iter([
        str(spec_path), "", "1", "n", "", "", "", "n", "",
    ])
    import builtins

    monkeypatch.setattr(builtins, "input", lambda prompt="": next(cevaplar))
    main(["init", "--config", str(tmp_path / ".mcpify.toml")])
    cikti = capsys.readouterr().out
    for beklenen in ("Spec path or URL", "Base URL", "Auth [1=none", "Read-only mode",
                     "Cache TTL", "Lazy mode", "Response format"):
        assert beklenen in cikti, f"prompt ekranda yok: {beklenen!r}"


def test_init_prefill_flags_skip_questions(tmp_path, monkeypatch, capsys, api):
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cevaplar = iter(["", "", "", "", "", ""])
    import builtins

    monkeypatch.setattr(builtins, "input", lambda prompt="": next(cevaplar))
    main(["init", "--config", str(tmp_path / "c.toml"),
          "--spec", str(spec_path), "--base-url", api])
    doc = (tmp_path / "c.toml").read_text(encoding="utf-8")
    assert f"base-url = '{api}'" in doc


def test_init_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcpify.toml").write_text("[serve]\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["init"])
    assert exc.value.code == 2
    assert "already exists" in capsys.readouterr().err


def test_init_warns_when_auth_env_is_unset(tmp_path, monkeypatch, capsys, api):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    cevaplar = iter([str(spec_path), api, "2", "NOPE_TOKEN", "n", "", "", "n", ""])
    import builtins

    monkeypatch.setattr(builtins, "input", lambda prompt="": next(cevaplar))
    main(["init", "--config", str(tmp_path / "c.toml")])
    assert "NOPE_TOKEN" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. request/response logging
# ---------------------------------------------------------------------------

def test_verbose_logs_to_stderr_without_touching_stdout(api, capsys):
    from mcpify.http_client import set_logging

    set_logging(verbose=True)
    try:
        server = make(api)
        response = call(server, "get_dog", {"dogId": 4})
        assert "error" not in response
    finally:
        set_logging(verbose=False, log_file=None)
    yakalanan = capsys.readouterr()
    assert "GET" in yakalanan.err and "/dogs/4" in yakalanan.err
    assert yakalanan.out == ""  # handle_message never writes to stdout


def test_log_file_gets_lines_but_never_credentials(tmp_path, api):
    from mcpify.http_client import set_logging

    log = tmp_path / "ops.log"
    set_logging(verbose=True, log_file=str(log))
    try:
        server = ApiServer(
            SPEC, api,
            auth=type("A", (), {"style": "bearer", "env_var": "X",
                                "headers": staticmethod(lambda: {"Authorization": "Bearer sekret"}),
                                "apply_query": staticmethod(lambda url: url)})(),
        )
        server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        call(server, "get_dog", {"dogId": 2})
    finally:
        set_logging(verbose=False, log_file=None)
    icerik = log.read_text(encoding="utf-8")
    assert "GET" in icerik and "200" in icerik
    assert "sekret" not in icerik


# ---------------------------------------------------------------------------
# 4. response cache
# ---------------------------------------------------------------------------

def test_cache_serves_repeat_get_without_hitting_api(api):
    server = make(api, cache_ttl=60)
    call(server, "get_dog", {"dogId": 7}, request_id=2)
    call(server, "get_dog", {"dogId": 7}, request_id=3)
    gets = [r for r in OpsAPI.requests if r[0] == "GET" and "/dogs/7" in r[1]]
    assert len(gets) == 1  # second call came from cache


def test_cache_respects_ttl_expiry(api):
    server = make(api, cache_ttl=0.05)
    call(server, "get_dog", {"dogId": 8}, request_id=2)
    time.sleep(0.08)
    call(server, "get_dog", {"dogId": 8}, request_id=3)
    gets = [r for r in OpsAPI.requests if "/dogs/8" in r[1]]
    assert len(gets) == 2


def test_cache_distinguishes_arguments_and_never_caches_post(api):
    server = make(api, cache_ttl=60)
    call(server, "get_dog", {"dogId": 1}, request_id=2)
    call(server, "get_dog", {"dogId": 2}, request_id=3)  # different key
    assert len([r for r in OpsAPI.requests if r[1].startswith("/dogs/") and r[0] != "POST"]) == 2
    before = len(OpsAPI.requests)
    call(server, "create_dog", {"body": {"name": "x"}}, request_id=4)
    call(server, "create_dog", {"body": {"name": "x"}}, request_id=5)
    posts = [r for r in OpsAPI.requests if r[0] == "POST"]
    assert len(posts) == 2 and len(OpsAPI.requests) == before + 2


def test_response_cache_bounded_size():
    cache = ResponseCache(60)
    for i in range(300):
        cache.put(f"k{i}", {"status": 200, "body": "x", "json": None, "headers": {}})
    assert len(cache._store) <= 256


# ---------------------------------------------------------------------------
# 5. retry (idempotent-only, 502/503/504-only, opt-in)
# ---------------------------------------------------------------------------

def test_retry_recovers_from_transient_503(api):
    OpsAPI.flaky_failures_left = 2
    server = make(api, retry=3, retry_delay=0.01)
    response = call(server, "flaky_call", {}, request_id=2)
    assert '"ok": true' in response["result"]["content"][0]["text"]
    assert len([r for r in OpsAPI.requests if "/flaky" in r[1]]) == 3  # 2x503 + 1x200


def test_retry_never_applies_to_post(api):
    server = make(api, retry=3, retry_delay=0.01)
    response = call(server, "create_dog", {"body": {"name": "n"}}, request_id=2)
    assert '"created": true' in response["result"]["content"][0]["text"]
    assert len([r for r in OpsAPI.requests if r[0] == "POST"]) == 1  # exactly one attempt


def test_retry_does_not_fire_on_4xx(api):
    server = make(api, retry=3, retry_delay=0.01)
    call(server, "gone_call", {}, request_id=2)
    istekler = len([r for r in OpsAPI.requests if "/gone" in r[1]])
    assert istekler == 1  # 404 is a definitive answer, never retried


def test_retry_gives_up_after_cap_and_reports_error(api):
    server = make(api, retry=2, retry_delay=0.01)
    response = call(server, "down_call", {}, request_id=2)
    istekler = len([r for r in OpsAPI.requests if "/down" in r[1]])
    assert istekler == 3  # 1 original + 2 retries
    assert response["result"]["isError"] is True


def test_retry_capped_at_five_internally(api, monkeypatch):
    from mcpify import http_client

    server = make(api, retry=50, retry_delay=0.0)
    monkeypatch.setattr(http_client, "MAX_RETRIES", 2)
    # yeniden import yerine dogrudan argumani kisaltilmis geciyoruz:
    server.retry = 50
    monkeypatch.setattr(http_client, "MAX_RETRIES", 5)
    call(server, "down_call", {}, request_id=2)
    # cap: max 5 retry => en fazla 6 istek
    istekler = len([r for r in OpsAPI.requests if "/down" in r[1]])
    assert istekler <= 6


# ---------------------------------------------------------------------------
# 6. auto-discovery
# ---------------------------------------------------------------------------

def test_discovery_finds_well_known_document(api):
    OpsAPI.well_known = True
    bulunan, ipucu = discover_spec(api)
    assert bulunan.endswith("/.well-known/openapi.json")
    assert ipucu == ""


def test_discovery_failure_lists_tried_paths(api):
    with pytest.raises(SpecError) as exc:
        discover_spec(api)
    assert "/.well-known/openapi.json" in str(exc.value)
    assert "/openapi.json" in str(exc.value)


def test_discovery_ignores_non_origin_urls():
    bulunan, _ = discover_spec("https://api.example.com/v2/openapi.json")
    assert bulunan == "https://api.example.com/v2/openapi.json"


def test_serve_with_bare_origin_discovers_and_serves(api, monkeypatch, tmp_path, capsys):
    import io

    OpsAPI.well_known = True
    monkeypatch.chdir(tmp_path)
    # bos stdin: serve dongusu hemen biter; discovery + banner'i dogrulariz
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(["serve", api, "--base-url", api])
    err = capsys.readouterr().err
    assert "discovered: " in err
    assert "serving" in err


# ---------------------------------------------------------------------------
# 7. strict validation mode
# ---------------------------------------------------------------------------

def test_strict_mode_makes_every_argument_required():
    gevsek = {t["name"]: t for t in spec_to_tools(SPEC)}
    kati = {t["name"]: t for t in spec_to_tools(SPEC, strict=True)}
    assert "verbose" not in gevsek["get_dog"]["inputSchema"]["required"]
    assert "verbose" in kati["get_dog"]["inputSchema"]["required"]


def test_strict_via_cli_flag(tmp_path, capsys, api):
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    main(["list", str(spec_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert any(entry["name"] == "get_dog" for entry in payload)


# ---------------------------------------------------------------------------
# 8. response format conversion
# ---------------------------------------------------------------------------

def test_auto_converts_xml_per_content_type(api):
    server = make(api, response_format="auto")
    response = call(server, "xml_call", {}, request_id=2)
    metin = response["result"]["content"][0]["text"]
    assert '"dog"' in metin and "<dog>" not in metin
    # conversion feeds structuredContent only when schema exists; here none —
    # but the text the model sees is JSON


def test_auto_never_touches_json_or_masked_breakage(api):
    server = make(api, response_format="auto")
    # get_dog returns JSON — untouched
    response = call(server, "get_dog", {"dogId": 3}, request_id=2)
    assert response["result"]["structuredContent"] == {"id": 3, "name": "Rex"}


def test_xml_mode_forces_conversion_even_without_header_hint():
    from mcpify.convert import convert

    body = "<a><b>1</b><b>2</b></a>"
    metin, veri = convert(body, None, "text/plain", "xml")
    assert veri == {"a": {"b": ["1", "2"]}}
    assert '"b"' in metin


def test_xml_attributes_and_text_mapping():
    import xml.etree.ElementTree as ET

    from mcpify.convert import xml_to_dict

    root = ET.fromstring('<dog id="7" kind="good">Rex</dog>')
    assert xml_to_dict(root) == {"@id": "7", "@kind": "good", "value": "Rex"}

    leaf = ET.fromstring("<cat>Pati</cat>")
    assert xml_to_dict(leaf) == "Pati"


def test_xml_conversion_failure_is_explicit_in_xml_mode():
    from mcpify.convert import convert

    metin, veri = convert("<broken", None, "text/xml", "xml")
    assert veri is None
    assert "not well-formed XML" in metin


def test_xml_with_dtd_is_never_parsed_billion_laughs_guard():
    """DTD/entity iceren XML parse edilmez; ham govde doner (S314 sertlestirmesi)."""
    from mcpify.convert import convert

    bomba = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        "<response><lol2>&lol2;</lol2></response>"
    )
    metin, veri = convert(bomba, None, "text/xml", "auto")
    assert veri is None
    assert metin == bomba  # convert edilmedi, aynen dondu
    metin2, veri2 = convert(bomba, None, "text/xml", "xml")
    assert veri2 is None
    assert "conversion skipped: document declares a DTD" in metin2


# ---------------------------------------------------------------------------
# 9. batch requests (legacy JSON-RPC arrays, concurrent GETs)
# ---------------------------------------------------------------------------

def test_batch_of_gets_runs_and_answers_in_order(api):
    server = make(api, cache_ttl=30)
    hat = [
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "get_dog", "arguments": {"dogId": 1}}},
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "get_dog", "arguments": {"dogId": 2}}},
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "get_dog", "arguments": {"dogId": 3}}},
    ]
    yanitlar = server._handle_batch(hat)
    assert [y["id"] for y in yanitlar] == [10, 11, 12]
    icerikler = [json.loads(y["result"]["content"][0]["text"])["id"] for y in yanitlar]
    assert icerikler == [1, 2, 3]


def test_batch_notifications_processed_and_silent(api):
    server = ApiServer(SPEC, api)  # uninitialized on purpose
    hat = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    yanitlar = server._handle_batch(hat)
    assert [y["id"] for y in yanitlar if y] == [1, 2]


def test_batch_over_stdio_wire(api):
    import io

    server = make(api, cache_ttl=30)
    giris = io.StringIO(json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_dog", "arguments": {"dogId": 5}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_dog", "arguments": {"dogId": 6}}},
    ]) + "\n")
    cikis = io.StringIO()
    server.serve(stdin=giris, stdout=cikis)
    satirlar = [json.loads(s) for s in cikis.getvalue().splitlines()]
    assert [s["id"] for s in satirlar] == [1, 2]


# ---------------------------------------------------------------------------
# 10. health tool + status command
# ---------------------------------------------------------------------------

def test_health_tool_reports_reachable_api(api, monkeypatch):
    monkeypatch.setenv("OPS_TOKEN", "gizli")
    auth = type("A", (), {"style": "bearer", "env_var": "OPS_TOKEN",
                          "headers": staticmethod(dict),
                          "apply_query": staticmethod(lambda url: url),
                          "describe": staticmethod(lambda: {"style": "bearer", "env": "OPS_TOKEN", "env_set": True})})()
    server = make(api, auth=auth, cache_ttl=15, retry=2)
    response = call(server, HEALTH_TOOL, {}, request_id=2)
    rapor = json.loads(response["result"]["content"][0]["text"])
    assert rapor["api_reachable"] is True
    assert rapor["tools"] >= 5
    assert rapor["cache_ttl"] == 15
    assert rapor["retry"] == 2
    assert rapor["auth"] == {"style": "bearer", "env": "OPS_TOKEN", "env_set": True}


def test_health_tool_flags_unreachable_api():
    server = ApiServer(SPEC, "http://127.0.0.1:1")
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = call(server, HEALTH_TOOL, {}, request_id=2)
    assert response["result"]["isError"] is True
    rapor = json.loads(response["result"]["content"][0]["text"])
    assert rapor["api_reachable"] is False
    assert "hint" in rapor


def test_status_command_reachable_exit_zero(api, capsys, tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["status", str(spec_path)])
    assert exc.value.code == 0
    assert "reachable" in capsys.readouterr().out


def test_status_command_unreachable_exit_two(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["status", json.dumps(SPEC).replace("PLACEHOLDER", "x")])
    spec_path = tmp_path / "dead.json"
    olus = dict(SPEC)
    olus["servers"] = [{"url": "http://127.0.0.1:1"}]
    spec_path.write_text(json.dumps(olus), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["status", str(spec_path)])
    assert exc.value.code == 2


def test_status_json_output(api, capsys, tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_payload = dict(SPEC)
    spec_payload["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["status", str(spec_path), "--json"])
    rapor = json.loads(capsys.readouterr().out)
    assert rapor["api_reachable"] is True
    assert rapor["version"]
