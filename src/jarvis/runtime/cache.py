from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass
class CacheEntry(Generic[V]):
    value: V
    expires_at: float


class TTLCache(Generic[K, V]):
    def __init__(self, *, ttl_seconds: float = 60.0, max_entries: int = 500) -> None:
        self.ttl_seconds = max(0.1, ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._values: dict[K, CacheEntry[V]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: K) -> V | None:
        entry = self._values.get(key)
        now = time.monotonic()
        if entry is None or entry.expires_at <= now:
            if entry is not None:
                self._values.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def set(self, key: K, value: V, *, ttl_seconds: float | None = None) -> None:
        if len(self._values) >= self.max_entries:
            self._prune()
            if len(self._values) >= self.max_entries:
                oldest = min(
                    self._values,
                    key=lambda item: self._values[item].expires_at,
                )
                self._values.pop(oldest, None)
        self._values[key] = CacheEntry(
            value=value,
            expires_at=time.monotonic()
            + (self.ttl_seconds if ttl_seconds is None else max(0.001, ttl_seconds)),
        )

    def invalidate(self, key: K) -> bool:
        return self._values.pop(key, None) is not None

    def invalidate_prefix(self, prefix: str) -> int:
        keys = [key for key in self._values if str(key).startswith(prefix)]
        for key in keys:
            self._values.pop(key, None)
        return len(keys)

    def clear(self) -> None:
        self._values.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._values.items()
            if entry.expires_at <= now
        ]
        for key in expired:
            self._values.pop(key, None)
