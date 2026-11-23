import pytest

from ghproxy.config import load_config, normalize_host, parse_bytes, parse_ttl


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1024", 1024),
        ("1KiB", 1024),
        ("1KB", 1000),
        ("2GiB", 2 << 30),
        ("1.5MiB", 1572864),
        ("20GB", 20_000_000_000),
    ],
)
def test_parse_bytes(text, expected):
    assert parse_bytes(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "-1"])
def test_parse_bytes_rejects(text):
    with pytest.raises(ValueError):
        parse_bytes(text)


@pytest.mark.parametrize(("text", "expected"), [("720h", 2592000), ("30m", 1800), ("45", 45), ("1d", 86400)])
def test_parse_ttl(text, expected):
    assert parse_ttl(text) == expected


def test_load_config_defaults(monkeypatch):
    for key in ("ADDR", "CACHE_DIR", "MAX_CACHE_BYTES", "MAX_OBJECT_BYTES", "CACHE_TTL",
                "ALLOW_CIDRS", "EXTRA_HOSTS", "UPSTREAM_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config([])
    assert (cfg.host, cfg.port) == ("127.0.0.1", 8080)
    assert cfg.cache_dir.name == "cache"


def test_load_config_env_and_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("ADDR", "0.0.0.0:9000")
    monkeypatch.setenv("EXTRA_HOSTS", "Mirror.Corp:8443")
    monkeypatch.setenv("ALLOW_CIDRS", "10.0.0.0/8, 192.168.0.0/16")
    cfg = load_config(["--cache-dir", str(tmp_path), "--max-cache", "1GiB", "--ttl", "1h"])
    assert (cfg.host, cfg.port) == ("0.0.0.0", 9000)
    assert cfg.max_cache_bytes == 1 << 30
    assert cfg.cache_ttl == 3600
    assert "mirror.corp" in cfg.allowed_hosts
    assert cfg.allow_cidrs == ("10.0.0.0/8", "192.168.0.0/16")


def test_load_config_rejects_bad_addr():
    with pytest.raises(SystemExit):
        load_config(["--addr", "nonsense"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("github.com", "github.com"),
        ("GitHub.com:443", "github.com"),
        ("https://mirror.corp/path", "mirror.corp"),
    ],
)
def test_normalize_host(raw, expected):
    assert normalize_host(raw) == expected
