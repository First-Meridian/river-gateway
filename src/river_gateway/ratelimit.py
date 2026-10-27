"""Per-key request rate (token bucket) and in-flight request limits.

State is in process memory: run one gateway process, or put a shared store
such as Redis behind this interface before scaling out.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    rpm: int
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._inflight: dict[str, int] = defaultdict(int)

    def check_rate(self, key_id: str, rpm: int | None) -> float:
        """Take one request from the key's bucket.

        Returns 0 when allowed, otherwise the seconds until a request is allowed.
        """
        if not rpm:
            return 0.0
        now = self._clock()
        bucket = self._buckets.get(key_id)
        if bucket is None or bucket.rpm != rpm:
            bucket = self._buckets[key_id] = _Bucket(rpm=rpm, tokens=float(rpm), updated=now)
        bucket.tokens = min(float(rpm), bucket.tokens + (now - bucket.updated) * rpm / 60.0)
        bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) * 60.0 / rpm

    def try_acquire(self, key_id: str, limit: int | None) -> bool:
        if limit and self._inflight[key_id] >= limit:
            return False
        self._inflight[key_id] += 1
        return True

    def release(self, key_id: str) -> None:
        self._inflight[key_id] = max(0, self._inflight[key_id] - 1)

    def inflight(self, key_id: str) -> int:
        return self._inflight.get(key_id, 0)

    def total_inflight(self) -> int:
        return sum(self._inflight.values())
