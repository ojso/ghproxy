"""Flask application: cache-first proxy for GitHub downloads."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from .cache import DiskCache, Meta, PartialMeta, cache_key
from .config import Config
from .proxy import BadTarget, Forbidden, Upstream, parse_target
from .version import __version__

logger = logging.getLogger("ghproxy")

CHUNK = 64 * 1024
ERROR_BODY_LIMIT = 64 * 1024
SAFE_RESPONSE_HEADERS = (
    "Content-Type",
    "Content-Length",
    "ETag",
    "Last-Modified",
    "Accept-Ranges",
    "Content-Range",
    "Content-Encoding",
)
KEY_RE = re.compile(r"[0-9a-f]{64}")


def iter_raw(resp: requests.Response, chunk_size: int):
    """Stream a response without letting requests decompress it.

    The raw bytes must be forwarded and cached as-is so Content-Length and
    Content-Encoding keep describing the same payload.
    """
    raw = resp.raw
    if hasattr(raw, "stream"):
        yield from raw.stream(chunk_size, decode_content=False)
        return
    while True:
        chunk = raw.read(chunk_size)
        if not chunk:
            break
        yield chunk


def content_range(resp: requests.Response) -> tuple[int | None, int | None]:
    """Return (start, total) parsed from a Content-Range header."""
    value = resp.headers.get("Content-Range", "")
    if not value.startswith("bytes "):
        return None, None
    span, _, total = value[6:].partition("/")
    start = span.partition("-")[0]
    if not start.isdigit():
        return None, None
    return int(start), int(total) if total.isdigit() else None


def validators_match(partial: PartialMeta, resp: requests.Response) -> bool:
    """Only resume when the stored validators still describe the upstream body."""
    if not partial.etag and not partial.last_modified:
        return False
    if partial.etag and resp.headers.get("ETag") != partial.etag:
        return False
    if partial.last_modified and resp.headers.get("Last-Modified") != partial.last_modified:
        return False
    return (resp.headers.get("Content-Encoding", "") == partial.content_encoding
            and resp.headers.get("Content-Type", "") == partial.content_type)


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    app.config["GH_CONFIG"] = config

    cache = DiskCache(config.cache_dir, config.max_cache_bytes, config.cache_ttl)
    upstream = Upstream(config.allowed_hosts, config.upstream_token)
    networks = [ipaddress.ip_network(cidr, strict=False) for cidr in config.allow_cidrs]
    started_at = time.time()

    app.extensions["gh_cache"] = cache
    app.extensions["gh_upstream"] = upstream

    # ---- helpers ---------------------------------------------------------
    def response_headers(resp: requests.Response) -> dict[str, str]:
        headers = {}
        for name in SAFE_RESPONSE_HEADERS:
            value = resp.headers.get(name)
            if value:
                headers[name] = value
        return headers

    def client_allowed() -> bool:
        if not networks:
            return True
        try:
            address = ipaddress.ip_address(request.remote_addr or "")
        except ValueError:
            return False
        return any(address in network for network in networks)

    def no_content(**headers: str) -> Response:
        response = Response(status=204)
        response.headers.pop("Content-Type", None)
        for name, value in headers.items():
            response.headers[name] = value
        return response

    def serve_cached(meta: Meta, body: Path) -> Response:
        cache.record_hit()
        response = send_file(
            body.resolve(),
            mimetype=meta.content_type or None,
            conditional=True,
            etag=meta.sha256 or None,
            last_modified=meta.created_at,
        )
        response.headers["X-Cache"] = "HIT"
        if meta.content_encoding:
            response.headers["Content-Encoding"] = meta.content_encoding
        if meta.sha256:
            response.headers["X-Content-SHA256"] = meta.sha256
        return response

    def stream_passthrough(url: str, method: str, range_header: str | None) -> Response:
        try:
            upstream_resp, _ = upstream.open(url, method=method, range_header=range_header)
        except (Forbidden, requests.RequestException) as exc:
            cache.record_error()
            return Response(f"upstream error: {exc}", status=502, mimetype="text/plain")

        headers = response_headers(upstream_resp)
        headers["X-Cache"] = "BYPASS"

        def generate():
            try:
                if method != "HEAD":
                    for chunk in iter_raw(upstream_resp, CHUNK):
                        if chunk:
                            yield chunk
            finally:
                upstream_resp.close()

        return Response(
            stream_with_context(generate()),
            status=upstream_resp.status_code,
            headers=headers,
        )

    def fetch_and_cache(url: str) -> Response:
        resume = False
        offset = 0
        total_full: int | None = None
        upstream_resp: requests.Response | None = None

        partial_info = cache.load_partial(url)
        if partial_info is not None and not (
            partial_info[0].etag or partial_info[0].last_modified
        ):
            # Without a validator we cannot prove the bytes still belong to this URL.
            cache.drop_partial(url)
            partial_info = None
        if partial_info is not None:
            partial, _, offset = partial_info
            try:
                candidate, _ = upstream.open(
                    url, method="GET", range_header=f"bytes={offset}-"
                )
            except (Forbidden, requests.RequestException) as exc:
                cache.record_error()
                return Response(f"upstream error: {exc}", status=502, mimetype="text/plain")

            start, total_full = content_range(candidate)
            if (candidate.status_code == 206 and start == offset
                    and validators_match(partial, candidate)):
                upstream_resp = candidate
                resume = True
            elif candidate.status_code == 200:
                # Upstream ignored Range: replace the partial with this full body.
                cache.drop_partial(url)
                upstream_resp = candidate
            else:
                # 416 or a mismatched 206: start over from byte 0.
                if candidate.status_code in (206, 416) or 200 <= candidate.status_code < 300:
                    candidate.close()
                    cache.drop_partial(url)
                    upstream_resp = None
                else:
                    upstream_resp = candidate

        if upstream_resp is None:
            try:
                upstream_resp, _ = upstream.open(url, method="GET")
            except (Forbidden, requests.RequestException) as exc:
                cache.record_error()
                return Response(f"upstream error: {exc}", status=502, mimetype="text/plain")

        if not 200 <= upstream_resp.status_code < 300:
            status = upstream_resp.status_code
            headers = response_headers(upstream_resp)
            body = b""
            # Decode the error body so a truncated payload is still readable;
            # the headers must then describe exactly what we return.
            with contextlib.suppress(requests.RequestException):
                for chunk in upstream_resp.iter_content(8192):
                    body += chunk
                    if len(body) >= ERROR_BODY_LIMIT:
                        body = body[:ERROR_BODY_LIMIT]
                        break
            upstream_resp.close()
            headers.pop("Content-Encoding", None)
            headers.pop("Content-Range", None)
            headers["Content-Length"] = str(len(body))
            return Response(body, status=status, headers=headers)

        if not resume:
            declared = upstream_resp.headers.get("Content-Length")
            total_full = int(declared) if declared and declared.isdigit() else None
        elif total_full is None:
            declared = upstream_resp.headers.get("Content-Length")
            if declared and declared.isdigit():
                total_full = offset + int(declared)

        if total_full is not None and total_full > config.max_object_bytes:
            if resume:
                upstream_resp.close()
                cache.drop_partial(url)
                return fetch_and_cache(url)
            cache.record_bypass()
            headers = response_headers(upstream_resp)
            headers["X-Cache"] = "BYPASS"

            def passthrough():
                try:
                    for chunk in iter_raw(upstream_resp, CHUNK):
                        if chunk:
                            yield chunk
                finally:
                    upstream_resp.close()

            return Response(
                stream_with_context(passthrough()),
                status=upstream_resp.status_code,
                headers=headers,
            )

        writer = cache.begin(url, append=resume)
        cache.write_partial_meta(url, PartialMeta(
            url=url,
            etag=upstream_resp.headers.get("ETag", ""),
            last_modified=upstream_resp.headers.get("Last-Modified", ""),
            content_type=upstream_resp.headers.get("Content-Type", ""),
            content_encoding=upstream_resp.headers.get("Content-Encoding", ""),
            total=total_full,
            created_at=time.time(),
        ))

        hasher = hashlib.sha256()
        headers = response_headers(upstream_resp)
        headers["X-Cache"] = "MISS"
        headers.setdefault("Accept-Ranges", "bytes")
        if resume:
            headers.pop("Content-Range", None)
            if total_full is not None:
                headers["Content-Length"] = str(total_full)
            else:
                headers.pop("Content-Length", None)

        def generate():
            written = offset if resume else 0
            finished = False
            try:
                if resume:
                    with open(writer.path, "rb") as existing:
                        while True:
                            chunk = existing.read(CHUNK)
                            if not chunk:
                                break
                            hasher.update(chunk)
                            yield chunk
                for chunk in iter_raw(upstream_resp, CHUNK):
                    if not chunk:
                        continue
                    hasher.update(chunk)
                    writer.file.write(chunk)
                    written += len(chunk)
                    yield chunk
                finished = True
            finally:
                # Werkzeug may close the iterator once Content-Length is satisfied,
                # so a matching byte count also means the body is complete.
                if total_full is None:
                    complete = finished
                else:
                    complete = written == total_full
                if complete:
                    meta = Meta(
                        url=url,
                        status=200,
                        content_type=upstream_resp.headers.get("Content-Type", ""),
                        sha256=hasher.hexdigest(),
                        etag=upstream_resp.headers.get("ETag", ""),
                        content_encoding=upstream_resp.headers.get("Content-Encoding", ""),
                    )
                    try:
                        cache.commit(writer, meta)
                        if cache.stats()["bytes"] > config.max_cache_bytes:
                            cache.evict()
                    except OSError as exc:
                        logger.warning("commit %s failed: %s", url, exc)
                        cache.record_error()
                else:
                    # Keep the partial so the next request can resume it.
                    cache.abort(writer, keep=True)
                    cache.record_error()
                upstream_resp.close()

        return Response(
            stream_with_context(generate()),
            status=200 if resume else upstream_resp.status_code,
            headers=headers,
        )

    # ---- routes ----------------------------------------------------------
    @app.before_request
    def check_client():
        if not client_allowed():
            return Response("forbidden: client address not allowed", status=403, mimetype="text/plain")
        return None

    @app.get("/_health")
    def health():
        return jsonify(
            status="ok",
            version=__version__,
            uptime_seconds=int(time.time() - started_at),
            cache=cache.stats(),
        )

    @app.get("/_stats")
    def stats():
        return jsonify(cache.stats())

    @app.get("/_entries")
    def entries():
        items = cache.entries()
        return jsonify(
            count=len(items),
            bytes=sum(item["size"] for item in items),
            entries=items,
        )

    @app.delete("/_entries")
    def clear_entries():
        removed = cache.clear()
        return no_content(**{"X-Deleted-Entries": str(removed)})

    @app.delete("/_entries/<key>")
    def delete_entry(key: str):
        if not KEY_RE.fullmatch(key):
            return Response("bad key: expected 64 lowercase hex characters", status=400, mimetype="text/plain")
        if not cache.remove_key(key):
            return Response("cache entry not found", status=404, mimetype="text/plain")
        return no_content()

    @app.route("/<path:target>", methods=["GET", "HEAD", "DELETE"])
    def proxy_route(target: str):
        try:
            url = parse_target("/" + target)
        except BadTarget as exc:
            return Response(f"bad target: {exc}", status=400, mimetype="text/plain")

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in config.allowed_hosts:
            return Response(f"upstream host not allowed: {host}", status=403, mimetype="text/plain")
        if ".." in parts.path:
            return Response("unsafe path", status=400, mimetype="text/plain")

        if request.method == "DELETE":
            if not cache.remove_key(cache_key(url)):
                return Response("cache entry not found", status=404, mimetype="text/plain")
            return no_content()

        if request.method == "GET":
            hit = cache.lookup(url)
            if hit is not None:
                return serve_cached(*hit)
            cache.record_miss()

        range_header = request.headers.get("Range")
        if range_header or request.method == "HEAD":
            cache.record_bypass()
            return stream_passthrough(url, request.method, range_header)

        with cache.lock(url):
            hit = cache.lookup(url)
            if hit is not None:
                return serve_cached(*hit)
            return fetch_and_cache(url)

    return app


def main(argv: list[str] | None = None) -> None:
    from .config import load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(argv)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)
    logger.info(
        "ghproxy %s listening on http://%s:%d (cache=%s max=%d bytes ttl=%ds)",
        __version__, cfg.host, cfg.port, cfg.cache_dir, cfg.max_cache_bytes, int(cfg.cache_ttl),
    )
    app.run(host=cfg.host, port=cfg.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
