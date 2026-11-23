"""Configuration for ghproxy: environment variables first, CLI flags override."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOSTS = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "gist.githubusercontent.com",
)

_SIZE_UNITS = {
    "KIB": 1 << 10,
    "MIB": 1 << 20,
    "GIB": 1 << 30,
    "TIB": 1 << 40,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "B": 1,
    "K": 1 << 10,
    "M": 1 << 20,
    "G": 1 << 30,
    "T": 1 << 40,
}

_TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_bytes(value: str) -> int:
    """Parse '20GiB', '512MB' or a plain byte count."""
    text = (value or "").strip().upper()
    if not text:
        raise ValueError("empty size")
    multiplier = 1
    for suffix in sorted(_SIZE_UNITS, key=len, reverse=True):
        if text.endswith(suffix):
            multiplier = _SIZE_UNITS[suffix]
            text = text[: -len(suffix)].strip()
            break
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"invalid size: {value!r}") from exc
    if number < 0:
        raise ValueError(f"size must not be negative: {value!r}")
    return int(number * multiplier)


def parse_ttl(value: str) -> float:
    """Parse '720h', '30m' or seconds into seconds."""
    text = (value or "").strip().lower()
    if not text:
        raise ValueError("empty ttl")
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([smhd]?)", text)
    if not match:
        raise ValueError(f"invalid ttl: {value!r}")
    number, unit = float(match.group(1)), match.group(2) or "s"
    return number * _TTL_UNITS[unit]


def split_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def normalize_host(value: str) -> str:
    text = value.strip().lower()
    if "://" in text:
        from urllib.parse import urlsplit

        host = urlsplit(text).hostname or ""
        return host
    if text.count(":") == 1:  # host:port
        text = text.split(":", 1)[0]
    return text.rstrip("/")


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8080
    cache_dir: Path = Path("./cache")
    max_cache_bytes: int = 20 * (1 << 30)
    max_object_bytes: int = 4 * (1 << 30)
    cache_ttl: float = 720 * 3600
    allow_cidrs: tuple[str, ...] = field(default_factory=tuple)
    extra_hosts: tuple[str, ...] = field(default_factory=tuple)
    upstream_token: str = ""

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {host.lower() for host in DEFAULT_HOSTS}
        hosts.update(normalize_host(host) for host in self.extra_hosts)
        return frozenset(host for host in hosts if host)


def load_config(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(prog="ghproxy", description="GitHub release cache proxy")
    parser.add_argument("--addr", default=os.getenv("ADDR", "127.0.0.1:8080"),
                        help="listen address, host:port")
    parser.add_argument("--cache-dir", default=os.getenv("CACHE_DIR", "./cache"))
    parser.add_argument("--max-cache", default=os.getenv("MAX_CACHE_BYTES", "20GiB"))
    parser.add_argument("--max-object", default=os.getenv("MAX_OBJECT_BYTES", "4GiB"))
    parser.add_argument("--ttl", default=os.getenv("CACHE_TTL", "720h"))
    parser.add_argument("--allow-cidrs", default=os.getenv("ALLOW_CIDRS", ""))
    parser.add_argument("--extra-hosts", default=os.getenv("EXTRA_HOSTS", ""))
    parser.add_argument("--upstream-token", default=os.getenv("UPSTREAM_TOKEN", ""))
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        from .version import __version__

        print(__version__)
        raise SystemExit(0)

    host, _, port = args.addr.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid --addr {args.addr!r}, expected host:port")

    return Config(
        host=host,
        port=int(port),
        cache_dir=Path(args.cache_dir),
        max_cache_bytes=parse_bytes(args.max_cache),
        max_object_bytes=parse_bytes(args.max_object),
        cache_ttl=parse_ttl(args.ttl),
        allow_cidrs=split_list(args.allow_cidrs),
        extra_hosts=split_list(args.extra_hosts),
        upstream_token=args.upstream_token,
    )
