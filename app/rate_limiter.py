import time
import uuid

from redis.asyncio import Redis


class RateLimiter:
    """Sliding-window request rate limiter backed by Redis sorted sets.

    Uses a Redis MULTI/EXEC pipeline (rather than a Lua EVAL script) so it
    works identically against real Redis, Redis Cluster, and fakeredis in
    tests — some managed Redis providers and cluster modes restrict EVAL,
    and this keeps the limiter portable and fully unit-testable.

    Algorithm (sliding window log):
      1. Drop entries older than the window.
      2. Optimistically add this request's timestamp.
      3. Count entries in the window.
      4. If over limit, remove the entry we just added and reject.
    Steps 1-3 run inside a single MULTI/EXEC transaction so no other
    request can be counted between the trim and the count.
    """

    def __init__(self, redis: Redis, window_seconds: int = 60):
        self._redis = redis
        self._window_ms = window_seconds * 1000

    async def check(self, identity_key: str, limit_per_window: int) -> tuple[bool, int]:
        """Returns (allowed, current_count_in_window)."""
        now_ms = int(time.time() * 1000)
        window_start = now_ms - self._window_ms
        redis_key = f"ratelimit:{identity_key}"
        member = f"{now_ms}-{uuid.uuid4().hex}"

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(redis_key, 0, window_start)
            pipe.zadd(redis_key, {member: now_ms})
            pipe.zcard(redis_key)
            pipe.pexpire(redis_key, self._window_ms)
            results = await pipe.execute()

        count = int(results[2])

        if count > limit_per_window:
            await self._redis.zrem(redis_key, member)
            return False, limit_per_window

        return True, count
