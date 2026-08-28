"""Shared test fixtures: a tiny HTTP server factory for URL-based specs."""

import http.server
import json
import threading

import pytest


class SpecHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path == "/spec.json":
            body = json.dumps(self.server.spec_document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


class SpecServer(http.server.HTTPServer):
    def __init__(self, document):
        super().__init__(("127.0.0.1", 0), SpecHandler)
        self.spec_document = document


@pytest.fixture()
def SpecServerFactory():
    servers = []

    def factory(document):
        server = SpecServer(document)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        server.url = f"http://127.0.0.1:{server.server_port}/spec.json"
        return server

    yield factory
    for server in servers:
        server.shutdown()
