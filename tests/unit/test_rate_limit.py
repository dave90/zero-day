import asyncio
import time

import pytest

from zero.orchestrator.rate_limit import RateLimiter, parse_rate


def test_parse_rate_per_min():
    assert abs(parse_rate("60/min") - 1.0) < 1e-6
    assert abs(parse_rate("120/min") - 2.0) < 1e-6


def test_parse_rate_per_sec():
    assert parse_rate("5/sec") == 5.0
    assert parse_rate("5/s") == 5.0


def test_parse_rate_bad():
    with pytest.raises(ValueError):
        parse_rate("blah")


async def test_rate_limiter_disabled_with_zero():
    rl = RateLimiter(default_rate="0/sec")
    assert rl.disabled() is True
    # Should not block.
    t = time.monotonic()
    await rl.acquire("host")
    assert time.monotonic() - t < 0.05


async def test_rate_limiter_blocks_when_bucket_drained():
    # 2/sec; capacity defaults to 5.
    rl = RateLimiter(default_rate="2/sec", capacity=2.0)
    t = time.monotonic()
    # First 2 calls drain the bucket immediately.
    await rl.acquire("host")
    await rl.acquire("host")
    # Third call must wait ~0.5s.
    await rl.acquire("host")
    elapsed = time.monotonic() - t
    assert 0.4 < elapsed < 1.5, f"unexpected delay: {elapsed}"


async def test_rate_limiter_per_host_independent():
    rl = RateLimiter(default_rate="1/sec", capacity=1.0)
    await rl.acquire("a")
    await rl.acquire("b")  # different host: should not wait
    t = time.monotonic()
    await rl.acquire("b")  # now drains b's bucket, must wait
    elapsed = time.monotonic() - t
    assert elapsed > 0.5
