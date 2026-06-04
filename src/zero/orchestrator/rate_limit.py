"""Per-host token bucket — caps outbound request rate to each target.

Concurrent-safe: a single bucket per host with an asyncio.Lock.

CLI rate is specified as `N/min` or `N/sec` (e.g. `100/min`). At construction
time we convert to tokens-per-second.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Final


_RATE_RE: Final = re.compile(r"^\s*(\d+)\s*/\s*(sec|s|min|m)\s*$", re.IGNORECASE)


def parse_rate(spec: str) -> float:
    """`100/min` → 1.666; `5/sec` → 5.0. Returns tokens/sec."""
    m = _RATE_RE.match(spec)
    if not m:
        raise ValueError(f"bad rate spec: {spec!r}; want '<int>/sec' or '<int>/min'")
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("sec", "s"):
        return float(n)
    return n / 60.0  # min / m


@dataclass
class _Bucket:
    tokens_per_sec: float
    capacity: float
    tokens: float
    last_refill: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, cost: float = 1.0) -> None:
        async with self.lock:
            while True:
                self._refill()
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                # Compute the wait until enough tokens accumulate.
                need = cost - self.tokens
                wait = need / self.tokens_per_sec
                await asyncio.sleep(min(wait, 1.0))

    def _refill(self) -> None:
        now = time.monotonic()
        delta = now - self.last_refill
        self.last_refill = now
        self.tokens = min(self.capacity, self.tokens + delta * self.tokens_per_sec)


class RateLimiter:
    """Per-host buckets. `acquire(host)` blocks until allowed."""

    def __init__(self, *, default_rate: str | None = "100/min", capacity: float = 5.0) -> None:
        self._default_tps = parse_rate(default_rate) if default_rate else 0.0
        self._capacity = capacity
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def disabled(self) -> bool:
        return self._default_tps <= 0

    async def acquire(self, host: str, cost: float = 1.0) -> None:
        if self.disabled():
            return
        async with self._lock:
            b = self._buckets.get(host)
            if b is None:
                b = _Bucket(
                    tokens_per_sec=self._default_tps,
                    capacity=self._capacity,
                    tokens=self._capacity,
                    last_refill=time.monotonic(),
                )
                self._buckets[host] = b
        await b.acquire(cost)
