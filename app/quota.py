import calendar
from datetime import datetime, timezone

from redis.asyncio import Redis


class MonthlyQuota:
    """Tracks and enforces a monthly request quota per tenant.

    Independent of the per-minute RateLimiter: the rate limiter smooths
    burst traffic, this enforces the plan's total monthly allowance. Backed
    by a single Redis INCR per request (cheap), with the key set to expire
    at the end of the calendar month so it self-cleans without a cron job.
    """

    def __init__(self, redis: Redis):
        self._redis = redis

    @staticmethod
    def _month_key(tenant_id: str, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        return f"quota:{tenant_id}:{now.strftime('%Y-%m')}"

    @staticmethod
    def _seconds_until_month_end(now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        last_day = calendar.monthrange(now.year, now.month)[1]
        month_end = now.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0)
        return max(1, int((month_end - now).total_seconds()))

    async def check_and_increment(self, tenant_id: str, monthly_limit: int | None) -> tuple[bool, int]:
        """Returns (allowed, current_count_this_month).

        monthly_limit=None means unlimited (always allowed, still counted
        for visibility/reporting).
        """
        key = self._month_key(tenant_id)
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, self._seconds_until_month_end())

        if monthly_limit is not None and count > monthly_limit:
            return False, count
        return True, count
