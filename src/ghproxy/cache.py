"""Disk cache keyed by the hash of the upstream URL."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

PART_MAX_AGE = 24 * 3600


@dataclass
class Meta:
    url: str
    status: int
    content_type: str = ""
    size: int = 0
    sha256: str = ""
    etag: str = ""
    created_at: float = 0.0
    last_access: float = 0.0
    content_encoding: str = ""

    @classmethod
    def from_json(cls, raw: bytes) -> "Meta | None":
        try:
            data = json.loads(raw)
            return cls(**data)
        except (ValueError, TypeError):
            return None


@dataclass
class PartialMeta:
    url: str
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    content_encoding: str = ""
    total: int | None = None
    created_at: float = 0.0

    @classmethod
    def from_json(cls, raw: bytes) -> "PartialMeta | None":
        try:
            data = json.loads(raw)
            return cls(**data)
        except (ValueError, TypeError):
            return None


@dataclass
class Stats:
    entries: int = 0
    bytes: int = 0
    max_bytes: int = 0
    hits: int = 0
    misses: int = 0
    bypasses: int = 0
    errors: int = 0
    evictions: int = 0

    def as_dict(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": self.entries,
            "bytes": self.bytes,
            "max_bytes": self.max_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "bypasses": self.bypasses,
            "errors": self.errors,
            "evictions": self.evictions,
            "hit_rate": (self.hits / total) if total else 0.0,
        }


@dataclass
class CacheWriter:
    key: str
    path: Path
    file: object
    completed: bool = field(default=False)


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, root: Path, max_bytes: int, ttl: float, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.clock = clock

        self._mutex = threading.RLock()
        self._bytes = 0
        self._entries = 0
        self._key_locks: dict[str, list] = {}
        self._stats = Stats(max_bytes=max_bytes)
        self._recount()
        self._cleanup_parts()

    # ---- paths -----------------------------------------------------------
    def body_path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.body"

    def meta_path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.meta"

    def partial_path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.body.part"

    def partial_meta_path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.body.part.json"

    # ---- accounting ------------------------------------------------------
    def _recount(self) -> None:
        total = 0
        entries = 0
        for path in self.root.rglob("*.meta"):
            try:
                meta = Meta.from_json(path.read_bytes())
            except OSError:
                continue
            if meta is not None:
                total += meta.size
                entries += 1
        with self._mutex:
            self._bytes = total
            self._entries = entries
            self._stats.entries = entries
            self._stats.bytes = total

    def _entry_size(self, key: str) -> tuple[int, bool]:
        path = self.meta_path(key)
        try:
            meta = Meta.from_json(path.read_bytes())
        except OSError:
            return 0, False
        if meta is None:
            return 0, False
        return meta.size, True

    def _cleanup_parts(self) -> None:
        """Remove orphaned partial downloads left by a crashed process."""
        cutoff = time.time() - PART_MAX_AGE
        for path in list(self.root.rglob("*.part")):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
            except OSError:
                continue
            with contextlib.suppress(FileNotFoundError):
                path.with_name(path.name + ".json").unlink()

        for path in list(self.root.rglob("*.part.json")) + list(self.root.rglob("*.part.json.tmp")):
            name = path.name.removesuffix(".tmp").removesuffix(".json")
            part = path.with_name(name)
            try:
                if not part.exists() or part.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _apply_size(self, had_old: bool, old_size: int, new_size: int) -> None:
        with self._mutex:
            if had_old:
                self._bytes -= old_size
            else:
                self._entries += 1
            self._bytes = max(0, self._bytes + new_size)
            self._stats.entries = self._entries
            self._stats.bytes = self._bytes

    # ---- reading ---------------------------------------------------------
    def lookup(self, url: str) -> tuple[Meta, Path] | None:
        key = cache_key(url)
        try:
            meta = Meta.from_json(self.meta_path(key).read_bytes())
        except OSError:
            return None
        if meta is None:
            return None
        now = self.clock()
        if self.ttl > 0 and now - meta.created_at > self.ttl:
            self.remove(key)
            return None
        body = self.body_path(key)
        if not body.exists():
            self.remove(key)
            return None
        if now - meta.last_access > 3600:
            meta.last_access = now
            self._write_meta(key, meta)
        return meta, body

    @contextlib.contextmanager
    def lock(self, url: str) -> Iterator[None]:
        with self.key_lock(cache_key(url)):
            yield

    @contextlib.contextmanager
    def key_lock(self, key: str) -> Iterator[None]:
        with self._mutex:
            entry = self._key_locks.get(key)
            if entry is None:
                entry = [threading.Lock(), 0]
                self._key_locks[key] = entry
            entry[1] += 1
        entry[0].acquire()
        try:
            yield
        finally:
            entry[0].release()
            with self._mutex:
                entry[1] -= 1
                if entry[1] == 0:
                    self._key_locks.pop(key, None)

    # ---- writing ---------------------------------------------------------
    def load_partial(self, url: str) -> tuple[PartialMeta, Path, int] | None:
        """Return (metadata, path, bytes already stored) for a resumable partial."""
        key = cache_key(url)
        path = self.partial_path(key)
        try:
            offset = path.stat().st_size
            partial = PartialMeta.from_json(self.partial_meta_path(key).read_bytes())
        except OSError:
            return None
        if partial is None or partial.url != url or offset <= 0:
            return None
        return partial, path, offset

    def begin(self, url: str, append: bool = False) -> CacheWriter:
        key = cache_key(url)
        path = self.partial_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        offset = path.stat().st_size if append and path.exists() else 0
        mode = "ab" if append else "wb"
        return CacheWriter(key=key, path=path, file=open(path, mode))

    def write_partial_meta(self, url: str, partial: PartialMeta) -> None:
        key = cache_key(url)
        path = self.partial_meta_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(partial), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def drop_partial(self, url: str) -> None:
        key = cache_key(url)
        for path in (self.partial_path(key), self.partial_meta_path(key)):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def commit(self, writer: CacheWriter, meta: Meta) -> Meta:
        writer.file.close()
        target = self.body_path(writer.key)
        os.replace(writer.path, target)
        with contextlib.suppress(FileNotFoundError):
            self.partial_meta_path(writer.key).unlink()
        meta.size = target.stat().st_size
        now = self.clock()
        meta.last_access = now
        if not meta.created_at:
            meta.created_at = now
        self._write_meta(writer.key, meta)
        self._apply_size(False, 0, meta.size)
        writer.completed = True
        return meta

    def abort(self, writer: CacheWriter, keep: bool = False) -> None:
        try:
            writer.file.close()
        except Exception:
            pass
        if keep:
            return
        with contextlib.suppress(FileNotFoundError):
            writer.path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self.partial_meta_path(writer.key).unlink()

    def _write_meta(self, key: str, meta: Meta) -> None:
        path = self.meta_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".meta.tmp")
        tmp.write_text(json.dumps(asdict(meta), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def remove(self, key: str) -> bool:
        """Delete one entry, its metadata and any partial download for that URL."""
        size, existed = self._entry_size(key)
        removed = False
        for path in (
            self.body_path(key),
            self.meta_path(key),
            self.partial_path(key),
            self.partial_meta_path(key),
        ):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        if existed:
            with self._mutex:
                self._bytes = max(0, self._bytes - size)
                self._entries = max(0, self._entries - 1)
                self._stats.entries = self._entries
                self._stats.bytes = self._bytes
        return existed or removed

    def remove_key(self, key: str) -> bool:
        """Delete one entry by cache key, waiting for an in-flight writer."""
        with self.key_lock(key):
            return self.remove(key)

    def clear(self) -> int:
        """Delete every complete entry. Returns how many entries were removed."""
        removed = 0
        for path in list(self.root.rglob("*.meta")):
            if self.remove_key(path.stem):
                removed += 1
        return removed

    # ---- maintenance -----------------------------------------------------
    def evict(self) -> int:
        with self._mutex:
            total = self._bytes
        if total <= self.max_bytes:
            return 0
        target = int(self.max_bytes * 0.9)

        items: list[tuple[float, str, int]] = []
        for path in self.root.rglob("*.meta"):
            try:
                meta = Meta.from_json(path.read_bytes())
            except OSError:
                continue
            if meta is None:
                continue
            items.append((meta.last_access, path.stem, meta.size))
        items.sort()

        removed = 0
        for _, key, size in items:
            if total <= target:
                break
            self.remove(key)
            total -= size
            removed += 1
            with self._mutex:
                self._stats.evictions += 1
        return removed

    # ---- stats -----------------------------------------------------------
    def entries(self) -> list[dict]:
        """Return all complete cache entries, most recently accessed first."""
        items: list[dict] = []
        for path in self.root.rglob("*.meta"):
            try:
                meta = Meta.from_json(path.read_bytes())
            except OSError:
                continue
            if meta is not None:
                items.append({**asdict(meta), "key": path.stem})
        items.sort(key=lambda item: item["last_access"], reverse=True)
        return items

    def record_hit(self) -> None:
        with self._mutex:
            self._stats.hits += 1

    def record_miss(self) -> None:
        with self._mutex:
            self._stats.misses += 1

    def record_bypass(self) -> None:
        with self._mutex:
            self._stats.bypasses += 1

    def record_error(self) -> None:
        with self._mutex:
            self._stats.errors += 1

    def stats(self) -> dict:
        with self._mutex:
            return self._stats.as_dict()
