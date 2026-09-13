"""Tiny in-memory token buckets, keyed by an arbitrary string (client IP, "global", ...).

Single replica (SQLite on RWO), so process memory is the whole picture. Keys are pruned
once they have refilled completely, and the table is hard-capped so a flood of distinct
keys cannot grow it without bound.
"""

from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, *, capacity: int, refill_per_second: float, max_keys: int = 10_000) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill_per_second > 0")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill_monotonic)

    def allow(self, key: str, now: float | None = None) -> bool:
        """Consumes one token for ``key`` if available. Returns False when the bucket is empty."""
        if now is None:
            now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(float(self.capacity), tokens + (now - last) * self.refill_per_second)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[key] = (tokens, now)
        if len(self._buckets) > self.max_keys:
            self._prune(now)
        return allowed

    def retry_after_seconds(self) -> int:
        return max(1, int(1.0 / self.refill_per_second + 0.999))

    def _prune(self, now: float) -> None:
        full_after = self.capacity / self.refill_per_second
        for key, (_, last) in list(self._buckets.items()):
            if now - last >= full_after:
                del self._buckets[key]
        # Still over the cap (everyone active at once): drop the oldest half.
        if len(self._buckets) > self.max_keys:
            for key, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[: len(self._buckets) // 2]:
                del self._buckets[key]
