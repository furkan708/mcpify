"""`mcpify try` REPL: driven with injected stdin, captured stdout.

The loop must behave identically when stdin is piped (non-interactive):
same prompts, same errors, clean EOF exit — that is exactly what these
tests lock in.
"""

import http.server
import io
import json
import threading

import pytest

from mcpify.api_server import ApiServer
from mcpify.repl import _cast, run
from mcpify.spec import load_spec


class Upstream(http.server.BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Upstream.requests.append(self.path)
        payload = json.dumps({"id": 7, "name": "Pet7", "kind": "cat"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        Upstream.requests.append(("POST", self.path, json.loads(body or b"{}")))
        payload = json.dumps({"id": 42, "created": True}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def upstream():
    Upstream.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


@pytest.fixture()
def server(upstream):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]
    api = ApiServer(spec, upstream)
    api.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    api.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return api


def feed(lines):
    """An input() replacement serving the given lines, EOF afterwards."""
    queue = list(lines)

    def read(_prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read


def drive(server, lines):
    out = io.StringIO()
    run(server, input_fn=feed(lines), output=out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# session lifecycle
# ---------------------------------------------------------------------------

def test_quit_command_exits_cleanly(server):
    out = drive(server, [":q"])
    assert "bye" in out


def test_eof_exits_cleanly(server):
    out = drive(server, [])
    assert "bye" in out


def test_welcome_lists_numbered_tools(server):
    out = drive(server, [":q"])
    assert "mcpify try" in out
    assert "1. list_pets" in out
    assert "GET" in out


def test_help_shows_commands(server):
    out = drive(server, [":h", ":q"])
    assert ":raw" in out
    assert ":info" in out


def test_keyboard_interrupt_at_prompt_is_graceful(server):
    out = io.StringIO()

    def interrupt(_prompt):
        raise KeyboardInterrupt

    run(server, input_fn=interrupt, output=out)
    assert "bye" in out.getvalue()


# ---------------------------------------------------------------------------
# selection and execution
# ---------------------------------------------------------------------------

def test_call_by_number_with_arguments(server):
    out = drive(server, ["3", "7", ":q"])  # 3 = get_pet, petId = 7
    assert "✓" in out
    assert "Pet7" in out
    assert Upstream.requests[-1].endswith("/v1/pets/7")


def test_call_by_name(server):
    out = drive(server, ["get_pet", "3", ":q"])
    assert "✓" in out
    assert "/v1/pets/3" in Upstream.requests[-1]


def test_call_by_name_case_insensitive(server):
    out = drive(server, ["Get_Pet", "3", ":q"])
    assert "✓" in out


def test_invalid_number_is_reported(server):
    out = drive(server, ["99", ":q"])
    assert "no tool matches '99'" in out


def test_unknown_name_is_reported(server):
    out = drive(server, ["nope", ":q"])
    assert "no tool matches 'nope'" in out


def test_post_tool_with_json_body(server):
    out = drive(server, ["2", '{"name": "Rex", "kind": "dog"}', ":q"])  # 2 = create_pet
    assert "✓" in out
    assert Upstream.requests[-1][0] == "POST"
    assert Upstream.requests[-1][2] == {"name": "Rex", "kind": "dog"}


def test_invalid_body_json_reprompts_then_succeeds(server):
    out = drive(server, ["2", "not-json", '{"name": "Rex"}', ":q"])
    assert "!" in out  # the rejection
    assert "✓" in out  # the retry worked
    assert Upstream.requests[-1][2] == {"name": "Rex"}


def test_missing_required_path_param_shows_agent_error(server):
    out = drive(server, ["3", "", ":q"])  # get_pet with empty petId
    assert "✗" in out
    assert "petId" in out


def test_meta_tool_callable_via_raw(server):
    out = drive(server, [":raw mcpify_health {}", ":q"])
    assert "✓" in out
    assert "base_url" in out


# ---------------------------------------------------------------------------
# info / raw commands
# ---------------------------------------------------------------------------

def test_info_after_selection(server):
    out = drive(server, ["3", "7", ":info", ":q"])
    assert '"petId"' in out


def test_info_by_name(server):
    out = drive(server, [":info get_pet", ":q"])
    assert '"petId"' in out


def test_info_without_selection_hints(server):
    out = drive(server, [":info", ":q"])
    assert "select a tool first" in out


def test_info_unknown_name(server):
    out = drive(server, [":info ghost", ":q"])
    assert "unknown tool 'ghost'" in out


def test_raw_with_bad_json(server):
    out = drive(server, [":raw get_pet {bad", ":q"])
    assert "invalid JSON" in out


def test_raw_usage_line(server):
    out = drive(server, [":raw", ":q"])
    assert "usage" in out


def test_raw_unknown_tool(server):
    out = drive(server, [":raw ghost {}", ":q"])
    assert "unknown tool 'ghost'" in out


# ---------------------------------------------------------------------------
# read-only policy flows through
# ---------------------------------------------------------------------------

def test_read_only_surface_lists_only_gets(server):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": "http://127.0.0.1:1/v1"}]
    import argparse as _argparse

    from mcpify.cli import filter_tools

    args = _argparse.Namespace(
        read_only=True, tag=None, include=None, exclude=None, allow=None, deny=None
    )
    from mcpify.tools import spec_to_tools

    filtered = filter_tools(spec_to_tools(spec), args)
    api = ApiServer(spec, "http://127.0.0.1:1/v1", tools=filtered)
    api.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    api.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    out = drive(api, [":q"])
    assert "create_pet" not in out
    assert "list_pets" in out


# ---------------------------------------------------------------------------
# argument casting units
# ---------------------------------------------------------------------------

def test_cast_integer():
    assert _cast("integer", "42") == 42
    with pytest.raises(ValueError):
        _cast("integer", "x")


def test_cast_boolean():
    assert _cast("boolean", "true") is True
    assert _cast("boolean", "n") is False
    with pytest.raises(ValueError):
        _cast("boolean", "maybe")


def test_cast_object_and_array():
    assert _cast("object", '{"a": 1}') == {"a": 1}
    assert _cast("array", "[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        _cast("object", "[1]")  # array is not an object
    with pytest.raises(ValueError):
        _cast("array", '{"a": 1}')  # object is not an array


def test_cast_string_passthrough():
    assert _cast("string", "hello") == "hello"
    assert _cast("unknown-kind", "x") == "x"
