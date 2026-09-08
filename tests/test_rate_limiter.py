import fakeredis.aioredis
import pytest

from app.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_allows_requests_under_limit():
    redis = fakeredis.aioredis.FakeRedis()
    limiter = RateLimiter(redis, window_seconds=60)

    for _ in range(5):
        allowed, _ = await limiter.check("key-a", limit_per_window=5)
        assert allowed is True


@pytest.mark.asyncio
async def test_blocks_requests_over_limit():
    redis = fakeredis.aioredis.FakeRedis()
    limiter = RateLimiter(redis, window_seconds=60)

    for _ in range(3):
        allowed, _ = await limiter.check("key-b", limit_per_window=3)
        assert allowed is True

    allowed, count = await limiter.check("key-b", limit_per_window=3)
    assert allowed is False
    assert count == 3


@pytest.mark.asyncio
async def test_limits_are_isolated_per_key():
    redis = fakeredis.aioredis.FakeRedis()
    limiter = RateLimiter(redis, window_seconds=60)

    for _ in range(2):
        await limiter.check("tenant-1", limit_per_window=2)

    allowed, _ = await limiter.check("tenant-2", limit_per_window=2)
    assert allowed is True
