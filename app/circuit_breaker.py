from redis.asyncio import Redis


class CircuitOpenError(Exception):
    """Raised when a tenant's circuit is open — the upstream should not be called."""


class CircuitBreaker:
    """Per-tenant circuit breaker backed by Redis, so it works correctly
    across multiple gateway replicas (not just one process).

    States are implicit rather than stored directly:
      - CLOSED: failure count below threshold — requests pass through.
      - OPEN: failure count reached threshold — requests are rejected
        immediately without touching upstream, until the cooldown key expires.
      - HALF-OPEN: cooldown key just expired — the next request is allowed
        through as a probe; success clears the failure counter, failure
        re-opens the circuit for another cooldown period.
    """

    def __init__(self, redis: Redis, failure_threshold: int, cooldown_seconds: int):
        self._redis = redis
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds

    def _failure_key(self, tenant_id: str) -> str:
        return f"circuit:failures:{tenant_id}"

    def _open_key(self, tenant_id: str) -> str:
        return f"circuit:open:{tenant_id}"

    def open_key(self, tenant_id: str) -> str:
        """Public accessor for the Redis key used to mark a circuit open —
        lets callers combine this check with other reads (e.g. chaos
        state) into a single pipelined round trip instead of two
        sequential ones."""
        return self._open_key(tenant_id)

    async def before_request(self, tenant_id: str) -> None:
        """Raises CircuitOpenError if the circuit is currently open."""
        is_open = await self._redis.get(self._open_key(tenant_id))
        if is_open is not None:
            raise CircuitOpenError(f"Circuit open for tenant {tenant_id}; upstream is failing")

    async def record_success(self, tenant_id: str) -> None:
        await self._redis.delete(self._failure_key(tenant_id))
        await self._redis.delete(self._open_key(tenant_id))

    async def record_failure(self, tenant_id: str) -> bool:
        """Returns True if this call caused the circuit to open (i.e. this
        was the failure that crossed the threshold) — callers use this to
        fire a one-time notification rather than one per rejected request."""
        key = self._failure_key(tenant_id)
        failures = await self._redis.incr(key)
        if failures == 1:
            await self._redis.expire(key, self._cooldown * 10)

        if failures >= self._threshold:
            await self._redis.set(self._open_key(tenant_id), "1", ex=self._cooldown)
            return failures == self._threshold
        return False
