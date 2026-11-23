FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . gunicorn

RUN useradd -m -u 10001 ghproxy \
 && mkdir -p /cache && chown ghproxy:ghproxy /cache
USER ghproxy

ENV CACHE_DIR=/cache \
    MAX_CACHE_BYTES=20GiB \
    CACHE_TTL=720h
VOLUME ["/cache"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/_health').read()" || exit 1

# 单 worker + 多线程：缓存与统计在进程内维护，多 worker 会各自缓存。
CMD ["gunicorn", "-w", "1", "--threads", "16", "-b", "0.0.0.0:8080", "ghproxy.wsgi:app"]
