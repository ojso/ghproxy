import os
import threading
import time

from ghproxy.cache import PART_MAX_AGE, DiskCache, Meta, PartialMeta, cache_key


def test_store_and_lookup(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    url = "https://example.com/asset.bin"
    writer = cache.begin(url)
    writer.file.write(b"payload")
    cache.commit(writer, Meta(url=url, status=200, content_type="application/octet-stream"))

    hit = cache.lookup(url)
    assert hit is not None
    meta, body = hit
    assert meta.size == len(b"payload")
    assert body.read_bytes() == b"payload"
    assert cache.stats()["entries"] == 1
    assert cache.stats()["bytes"] == len(b"payload")


def test_stale_part_files_cleaned_on_startup(tmp_path):
    stale = tmp_path / "ab" / "entry.body.part"
    stale.parent.mkdir()
    stale.write_bytes(b"partial")
    stale_meta = tmp_path / "ab" / "entry.body.part.json"
    stale_meta.write_text("{}")
    old = time.time() - PART_MAX_AGE - 60
    os.utime(stale, (old, old))
    os.utime(stale_meta, (old, old))

    fresh = tmp_path / "ab" / "live.body.part"
    fresh.write_bytes(b"in progress")

    DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)

    assert not stale.exists()
    assert not stale_meta.exists()
    assert fresh.exists()


def test_abort_can_keep_partial_for_resume(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    url = "https://example.com/big.bin"
    writer = cache.begin(url)
    writer.file.write(b"abc")
    cache.write_partial_meta(url, PartialMeta(url=url, etag='"v1"', total=10))

    cache.abort(writer, keep=True)

    loaded = cache.load_partial(url)
    assert loaded is not None
    partial, path, offset = loaded
    assert partial.etag == '"v1"'
    assert partial.total == 10
    assert offset == 3
    assert path.read_bytes() == b"abc"


def test_abort_without_keep_discards_partial(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    url = "https://example.com/big.bin"
    writer = cache.begin(url)
    writer.file.write(b"abc")
    cache.write_partial_meta(url, PartialMeta(url=url, etag='"v1"', total=10))

    cache.abort(writer)

    assert cache.load_partial(url) is None
    assert not cache.partial_path(writer.key).exists()


def test_ttl_expiry(tmp_path):
    now = [1000.0]
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=60, clock=lambda: now[0])
    url = "https://example.com/old"
    writer = cache.begin(url)
    writer.file.write(b"abc")
    cache.commit(writer, Meta(url=url, status=200))

    assert cache.lookup(url) is not None
    now[0] += 120
    assert cache.lookup(url) is None
    assert cache.stats()["entries"] == 0


def test_evict_least_recently_used(tmp_path):
    now = [1000.0]
    cache = DiskCache(tmp_path, max_bytes=150, ttl=3600, clock=lambda: now[0])

    for name in ("old", "new"):
        writer = cache.begin(f"https://example.com/{name}")
        writer.file.write(b"x" * 100)
        cache.commit(writer, Meta(url=f"https://example.com/{name}", status=200))
        now[0] += 60

    removed = cache.evict()
    assert removed == 1
    assert cache.lookup("https://example.com/old") is None
    assert cache.lookup("https://example.com/new") is not None
    assert cache.stats()["entries"] == 1


def test_evict_noop_under_limit(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    writer = cache.begin("https://example.com/a")
    writer.file.write(b"abc")
    cache.commit(writer, Meta(url="https://example.com/a", status=200))
    assert cache.evict() == 0


def test_recount_after_restart(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    for name, size in (("a", 5), ("b", 3)):
        writer = cache.begin(f"https://example.com/{name}")
        writer.file.write(b"x" * size)
        cache.commit(writer, Meta(url=f"https://example.com/{name}", status=200))

    restarted = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    stats = restarted.stats()
    assert stats["entries"] == 2
    assert stats["bytes"] == 8


def test_remove_key_updates_stats(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    url = "https://example.com/a"
    writer = cache.begin(url)
    writer.file.write(b"abc")
    cache.commit(writer, Meta(url=url, status=200))

    assert cache.remove_key(cache_key(url)) is True
    assert cache.stats()["entries"] == 0
    assert cache.stats()["bytes"] == 0
    assert cache.lookup(url) is None
    assert cache.remove_key(cache_key(url)) is False


def test_remove_key_drops_partial_download(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    url = "https://example.com/big.bin"
    writer = cache.begin(url)
    writer.file.write(b"abc")
    cache.write_partial_meta(url, PartialMeta(url=url, etag='"v1"', total=10))

    assert cache.remove_key(writer.key) is True
    assert cache.load_partial(url) is None
    assert not cache.partial_path(writer.key).exists()


def test_clear_removes_all_entries(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    for name in ("a", "b"):
        writer = cache.begin(f"https://example.com/{name}")
        writer.file.write(b"xx")
        cache.commit(writer, Meta(url=f"https://example.com/{name}", status=200))

    assert cache.clear() == 2
    assert cache.stats()["entries"] == 0
    assert cache.stats()["bytes"] == 0
    assert cache.clear() == 0


def test_lock_serialises_same_url(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=1 << 20, ttl=3600)
    url = "https://example.com/x"
    entered = threading.Event()

    def worker():
        with cache.lock(url):
            entered.set()

    with cache.lock(url):
        thread = threading.Thread(target=worker)
        thread.start()
        assert not entered.wait(0.2), "second lock must block while first is held"
    thread.join(timeout=2)
    assert entered.is_set()
