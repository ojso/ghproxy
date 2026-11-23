import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeServer:
    def __init__(self, handler):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def upstream():
    """Factory that starts loopback HTTP servers; all are stopped at teardown."""
    servers: list[FakeServer] = []

    def make(func):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                func(self)

            def do_HEAD(self):
                func(self)

            def log_message(self, *args):
                pass

        server = FakeServer(Handler)
        servers.append(server)
        return server

    yield make

    for server in servers:
        server.close()


def serve_bytes(req, body: bytes, content_type: str = "text/plain") -> None:
    """Serve body with optional single-range support."""
    range_header = req.headers.get("Range")
    if range_header and range_header.startswith("bytes="):
        start_text, _, end_text = range_header[6:].partition("-")
        start = int(start_text or 0)
        end = int(end_text) if end_text else len(body) - 1
        end = min(end, len(body) - 1)
        chunk = body[start : end + 1]
        req.send_response(206)
        req.send_header("Content-Type", content_type)
        req.send_header("Accept-Ranges", "bytes")
        req.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        req.send_header("Content-Length", str(len(chunk)))
        req.end_headers()
        if req.command != "HEAD":
            req.wfile.write(chunk)
        return

    req.send_response(200)
    req.send_header("Content-Type", content_type)
    req.send_header("Accept-Ranges", "bytes")
    req.send_header("Content-Length", str(len(body)))
    req.end_headers()
    if req.command != "HEAD":
        req.wfile.write(body)
