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
