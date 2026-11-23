import gzip

import pytest

from ghproxy.app import create_app
from ghproxy.cache import PartialMeta
from ghproxy.config import Config
from ghproxy.proxy import BadTarget, Upstream, parse_target, should_send_token

@pytest.fixture
def make_client(tmp_path, upstream):
    def build(*servers, **overrides):
        options = dict(
            host="127.0.0.1",
            port=0,
            cache_dir=tmp_path / "cache",
            max_cache_bytes=10 << 20,
            max_object_bytes=4 << 20,
            cache_ttl=3600,
            extra_hosts=tuple(server.url for server in servers),
        )
        options.update(overrides)
        cfg = Config(**options)
        application = create_app(cfg)
        application.config.update(TESTING=True)
        return application.test_client(), application.extensions["gh_cache"]

    return build


def test_miss_then_hit(tmp_path, upstream, make_client):
    hits = []

    def handler(req):
        hits.append(req.path)
        req.send_response(200)
        req.send_header("Content-Type", "text/plain")
        req.send_header("Content-Length", "11")
        req.end_headers()
        req.wfile.write(b"hello world")

    server = upstream(handler)
    client, cache = make_client(server)
    target = f"/{server.url}/file.txt"

    first = client.get(target)
    assert first.status_code == 200
    assert first.data == b"hello world"
    assert first.headers["X-Cache"] == "MISS"

    second = client.get(target)
    assert second.data == b"hello world"
    assert second.headers["X-Cache"] == "HIT"

    assert len(hits) == 1
    stats = cache.stats()
    assert stats["entries"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_encoded_body_forwarded_and_cached_verbatim(tmp_path, upstream, make_client):
    payload = gzip.compress(b"compressed payload " * 100)
    hits = []

    def handler(req):
        hits.append(req.path)
        req.send_response(200)
        req.send_header("Content-Type", "text/plain")
        req.send_header("Content-Encoding", "gzip")
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    server = upstream(handler)
    client, cache = make_client(server)
    target = f"/{server.url}/file.txt.gz"

    first = client.get(target)
    assert first.status_code == 200
    assert first.data == payload
    assert first.headers["Content-Encoding"] == "gzip"
    assert first.headers["Content-Length"] == str(len(payload))
    assert first.headers["X-Cache"] == "MISS"

    second = client.get(target)
    assert second.data == payload
    assert second.headers["Content-Encoding"] == "gzip"
    assert second.headers["Content-Length"] == str(len(payload))
    assert second.headers["X-Cache"] == "HIT"
    assert len(hits) == 1
    assert cache.stats()["entries"] == 1


def test_partial_download_resumes_with_range(tmp_path, upstream, make_client):
    body = b"0123456789abcdef"
    prefix = body[:6]
    etag = '"v1"'
    seen = []

    def handler(req):
        range_header = req.headers.get("Range")
        seen.append(range_header)
        start = int(range_header.removeprefix("bytes=").split("-")[0])
        chunk = body[start:]
        req.send_response(206)
        req.send_header("Content-Type", "text/plain")
        req.send_header("ETag", etag)
        req.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        req.send_header("Content-Length", str(len(chunk)))
        req.end_headers()
        req.wfile.write(chunk)

    server = upstream(handler)
    client, cache = make_client(server)
    url = f"{server.url}/file.bin"

    writer = cache.begin(url)
    writer.file.write(prefix)
    writer.file.close()
    cache.write_partial_meta(url, PartialMeta(
        url=url, etag=etag, content_type="text/plain", total=len(body),
    ))

    first = client.get(f"/{url}")
    assert first.status_code == 200
    assert first.data == body
    assert first.headers["X-Cache"] == "MISS"
    assert first.headers["Content-Length"] == str(len(body))
    assert "Content-Range" not in first.headers
    assert seen == [f"bytes={len(prefix)}-"]

    second = client.get(f"/{url}")
    assert second.status_code == 200
    assert second.data == body
    assert second.headers["X-Cache"] == "HIT"
    assert len(seen) == 1


def test_partial_restarts_when_etag_changes(tmp_path, upstream, make_client):
    body = b"new content"
    seen = []

    def handler(req):
        range_header = req.headers.get("Range")
        seen.append(range_header)
        if range_header:
            start = int(range_header.removeprefix("bytes=").split("-")[0])
            chunk = body[start:]
            req.send_response(206)
            req.send_header("ETag", '"new"')
            req.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            req.send_header("Content-Length", str(len(chunk)))
            req.end_headers()
            req.wfile.write(chunk)
            return
        req.send_response(200)
        req.send_header("ETag", '"new"')
        req.send_header("Content-Length", str(len(body)))
        req.end_headers()
        req.wfile.write(body)

    server = upstream(handler)
    client, cache = make_client(server)
    url = f"{server.url}/file.bin"

    writer = cache.begin(url)
    writer.file.write(b"old-partial")
    writer.file.close()
    cache.write_partial_meta(url, PartialMeta(
        url=url, etag='"old"', content_type="text/plain", total=len(body),
    ))

    resp = client.get(f"/{url}")
    assert resp.status_code == 200
    assert resp.data == body
    assert resp.headers["X-Cache"] == "MISS"
    assert seen == ["bytes=11-", None]


def test_interrupted_upstream_download_keeps_partial(tmp_path, upstream, make_client):
    body = b"0123456789abcdef"
    etag = '"v1"'
    calls = []

    def handler(req):
        calls.append(req.headers.get("Range"))
        if len(calls) == 1:
            req.send_response(200)
            req.send_header("ETag", etag)
            req.send_header("Content-Length", str(len(body)))
            req.end_headers()
            req.wfile.write(body[:6])
            return  # close early: the declared length is not satisfied

        start = int(req.headers["Range"].removeprefix("bytes=").split("-")[0])
        chunk = body[start:]
        req.send_response(206)
        req.send_header("ETag", etag)
        req.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        req.send_header("Content-Length", str(len(chunk)))
        req.end_headers()
        req.wfile.write(chunk)

    server = upstream(handler)
    client, cache = make_client(server)
    url = f"{server.url}/file.bin"

    truncated = client.get(f"/{url}")
    with pytest.raises(Exception):
        truncated.get_data()

    loaded = cache.load_partial(url)
    assert loaded is not None
    _, _, offset = loaded
    assert offset == 6

    resp = client.get(f"/{url}")
    assert resp.status_code == 200
    assert resp.data == body
    assert resp.headers["X-Cache"] == "MISS"
    assert calls == [None, "bytes=6-"]


def test_disallowed_host_rejected(tmp_path, make_client):
    client, cache = make_client()
    resp = client.get("/https://evil.example/payload")
    assert resp.status_code == 403
    assert cache.stats()["entries"] == 0


def test_redirect_followed_and_cached_under_original_url(tmp_path, upstream, make_client):
    from helpers import serve_bytes

    asset = upstream(lambda req: serve_bytes(req, b"redirected-body"))

    def redirector(req):
        req.send_response(302)
        req.send_header("Location", f"{asset.url}/asset.bin")
        req.end_headers()

    origin = upstream(redirector)
    client, cache = make_client(origin, asset)

    resp = client.get(f"/{origin.url}/download")
    assert resp.status_code == 200
    assert resp.data == b"redirected-body"

    assert cache.lookup(f"{origin.url}/download") is not None


def test_redirect_to_disallowed_host_fails(tmp_path, upstream, make_client):
    def redirector(req):
        req.send_response(302)
        req.send_header("Location", "https://evil.example/steal")
        req.end_headers()

    origin = upstream(redirector)
    client, _ = make_client(origin)
    resp = client.get(f"/{origin.url}/download")
    assert resp.status_code == 502


def test_range_on_cached_entry(tmp_path, upstream, make_client):
    from helpers import serve_bytes

    server = upstream(lambda req: serve_bytes(req, b"hello world"))
    client, _ = make_client(server)
    target = f"/{server.url}/file.txt"

    warmup = client.get(target)
    assert warmup.status_code == 200
    assert warmup.data == b"hello world"  # consuming the body commits the cache entry
    resp = client.get(target, headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.data == b"hello"
    assert resp.headers["X-Cache"] == "HIT"
    assert resp.headers["Content-Range"] == "bytes 0-4/11"


def test_range_on_miss_bypasses_cache(tmp_path, upstream, make_client):
    from helpers import serve_bytes

    seen = []
    server = upstream(lambda req: (seen.append(req.headers.get("Range")), serve_bytes(req, b"hello world")))
    client, cache = make_client(server)

    resp = client.get(f"/{server.url}/file.txt", headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.data == b"hello"
    assert resp.headers["X-Cache"] == "BYPASS"
    assert seen == ["bytes=0-4"]
    assert cache.stats()["entries"] == 0


def test_client_credentials_not_forwarded(tmp_path, upstream, make_client):
    seen = {}

    def handler(req):
        seen["authorization"] = req.headers.get("Authorization")
        seen["cookie"] = req.headers.get("Cookie")
        req.send_response(200)
        req.send_header("Content-Length", "2")
        req.end_headers()
        req.wfile.write(b"ok")

    server = upstream(handler)
    client, _ = make_client(server)

    client.get(
        f"/{server.url}/f",
        headers={"Authorization": "Bearer client-secret", "Cookie": "session=abc"},
    )
    assert seen["authorization"] is None
    assert seen["cookie"] is None


class _FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def close(self):
        pass


def test_server_token_used(monkeypatch):
    upstream_client = Upstream(frozenset({"github.com"}), token="server-token")
    seen = {}

    def fake_request(method, url, headers, **kwargs):
        seen.update(headers)
        return _FakeResponse()

    monkeypatch.setattr(upstream_client.session, "request", fake_request)
    upstream_client.open("https://github.com/o/r/releases/download/v1/f")
    assert seen["Authorization"] == "Bearer server-token"


def test_token_policy_is_github_https_only():
    assert should_send_token("https://github.com/o/r")
    assert should_send_token("https://api.github.com/repos/o/r")
    assert should_send_token("https://codeload.github.com/o/r/tar.gz/v1")
    assert not should_send_token("http://github.com/o/r")
    assert not should_send_token("https://release-assets.githubusercontent.com/asset")
    assert not should_send_token("https://raw.githubusercontent.com/o/r/main/f")
    assert not should_send_token("https://mirror.example.com/f")


def test_token_not_forwarded_on_redirect(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(302, {"Location": "https://release-assets.githubusercontent.com/asset"}),
        _FakeResponse(200),
    ]

    def fake_request(method, url, headers, **kwargs):
        calls.append(headers.get("Authorization"))
        return responses.pop(0)

    upstream_client = Upstream(
        frozenset({"github.com", "release-assets.githubusercontent.com"}),
        token="server-token",
    )
    monkeypatch.setattr(upstream_client.session, "request", fake_request)
    upstream_client.open("https://github.com/o/r/releases/download/v1/f")
    assert calls == ["Bearer server-token", None]


def test_server_token_not_sent_to_extra_host(tmp_path, upstream, make_client):
    seen = {}

    def handler(req):
        seen["authorization"] = req.headers.get("Authorization")
        req.send_response(200)
        req.send_header("Content-Length", "2")
        req.end_headers()
        req.wfile.write(b"ok")

    server = upstream(handler)
    client, _ = make_client(server, upstream_token="server-token")
    client.get(f"/{server.url}/f")
    assert seen["authorization"] is None


def test_upstream_error_body_length_matches(tmp_path, upstream, make_client):
    payload = b"x" * (128 * 1024)

    def handler(req):
        req.send_response(500)
        req.send_header("Content-Type", "text/plain")
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    server = upstream(handler)
    client, _ = make_client(server)
    response = client.get(f"/{server.url}/f")

    assert response.status_code == 500
    assert len(response.data) == 64 * 1024
    assert response.headers["Content-Length"] == str(len(response.data))
    assert "Content-Encoding" not in response.headers


def test_upstream_error_body_forwarded(tmp_path, upstream, make_client):
    def handler(req):
        req.send_response(404)
        req.send_header("Content-Type", "text/plain")
        req.send_header("Content-Length", "9")
        req.end_headers()
        req.wfile.write(b"not found")

    server = upstream(handler)
    client, _ = make_client(server)
    response = client.get(f"/{server.url}/f")

    assert response.status_code == 404
    assert response.data == b"not found"
    assert response.headers["Content-Length"] == "9"


def test_oversized_object_bypasses_cache(tmp_path, upstream, make_client):
    payload = b"x" * 2048

    def handler(req):
        req.send_response(200)
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    server = upstream(handler)
    client, cache = make_client(server, max_object_bytes=1024)

    resp = client.get(f"/{server.url}/big.bin")
    assert resp.status_code == 200
    assert resp.headers["X-Cache"] == "BYPASS"
    assert cache.stats()["entries"] == 0


def test_method_not_allowed(tmp_path, make_client):
    client, _ = make_client()
    resp = client.post("/https://github.com/x")
    assert resp.status_code == 405


def test_health_and_stats(tmp_path, make_client):
    client, _ = make_client()
    health = client.get("/_health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"
    assert client.get("/_stats").get_json()["entries"] == 0


def test_entries_lists_cached_items(tmp_path, upstream, make_client):
    def handler(req):
        req.send_response(200)
        req.send_header("Content-Type", "text/plain")
        req.send_header("Content-Length", "4")
        req.end_headers()
        req.wfile.write(b"data")

    server = upstream(handler)
    client, _ = make_client(server)
    targets = [f"{server.url}/one.bin", f"{server.url}/two.bin"]
    for target in targets:
        assert client.get(f"/{target}").status_code == 200

    payload = client.get("/_entries").get_json()
    assert payload["count"] == 2
    assert payload["bytes"] == 8
    assert {entry["url"] for entry in payload["entries"]} == set(targets)
    assert all(entry["size"] == 4 for entry in payload["entries"])
    assert all(len(entry["key"]) == 64 for entry in payload["entries"])
    assert len({entry["key"] for entry in payload["entries"]}) == 2


def test_delete_entry_by_url(tmp_path, upstream, make_client):
    from helpers import serve_bytes

    hits = []
    server = upstream(lambda req: (hits.append(req.path), serve_bytes(req, b"data")))
    client, _ = make_client(server)
    target = f"{server.url}/f.bin"

    assert client.get(f"/{target}").status_code == 200
    assert client.delete(f"/{target}").status_code == 204
    assert client.get("/_entries").get_json()["count"] == 0

    assert client.get(f"/{target}").status_code == 200
    assert len(hits) == 2, "after deletion the URL must be fetched again"


def test_delete_entry_by_key(tmp_path, upstream, make_client):
    from helpers import serve_bytes

    server = upstream(lambda req: serve_bytes(req, b"data"))
    client, _ = make_client(server)
    assert client.get(f"/{server.url}/f.bin").status_code == 200

    key = client.get("/_entries").get_json()["entries"][0]["key"]
    assert client.delete(f"/_entries/{key}").status_code == 204
    assert client.get("/_entries").get_json()["count"] == 0
    assert client.delete(f"/_entries/{key}").status_code == 404


def test_delete_missing_entry_returns_404(tmp_path, upstream, make_client):
    from helpers import serve_bytes

    server = upstream(lambda req: serve_bytes(req, b"data"))
    client, _ = make_client(server)
    assert client.delete(f"/{server.url}/never.bin").status_code == 404


def test_delete_rejects_bad_key(tmp_path, make_client):
    client, _ = make_client()
    response = client.delete("/_entries/not-a-key")
    assert response.status_code == 400
    assert b"bad key" in response.data
    assert client.delete("/_entries/" + "A" * 64).status_code == 400


def test_delete_disallowed_host(tmp_path, make_client):
    client, _ = make_client()
    assert client.delete("/https://evil.example.com/x").status_code == 403


def test_delete_all_entries(tmp_path, upstream, make_client):
    from helpers import serve_bytes

    server = upstream(lambda req: serve_bytes(req, b"data"))
    client, _ = make_client(server)
    for name in ("one.bin", "two.bin"):
        assert client.get(f"/{server.url}/{name}").status_code == 200

    response = client.delete("/_entries")
    assert response.status_code == 204
    assert response.headers["X-Deleted-Entries"] == "2"

    assert client.get("/_entries").get_json()["count"] == 0
    stats = client.get("/_stats").get_json()
    assert stats["entries"] == 0
    assert stats["bytes"] == 0


def test_bad_target_rejected(tmp_path, make_client):
    client, _ = make_client()
    assert client.get("/nohost").status_code == 400


def test_parse_target_rules():
    assert parse_target("/https://github.com/a/b") == "https://github.com/a/b"
    assert parse_target("/github.com/a/b") == "https://github.com/a/b"
    for bad in ("/", "/nohost", "/https://github.com/a/../b"):
        with pytest.raises(BadTarget):
            parse_target(bad)
