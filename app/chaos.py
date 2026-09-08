import json

from redis.asyncio import Redis


class ChaosInjector:
    """Admin-triggered fault injection, scoped per tenant and time-boxed
    (auto-expires via Redis TTL so a forgotten chaos test can't take down
    a tenant's traffic indefinitely).

    Modes:
      "fail"    — every request fails at the proxy stage as if the
                  upstream were unreachable (drives the circuit breaker
                  and retry logic exactly like a real outage would).
      "latency" — every request is delayed by extra_ms before the real
                  upstream call, to exercise timeout/retry behavior
                  without actually breaking anything.
    """

    def __init__(self, redis: Redis):
        self._redis = redis

    def _key(self, tenant_id: str) -> str:
        return f"chaos:{tenant_id}"

    def redis_key(self, tenant_id: str) -> str:
        """Public accessor, so callers can combine this read with other
        checks (e.g. circuit breaker state) into one pipelined round trip."""
        return self._key(tenant_id)

    async def enable(self, tenant_id: str, mode: str, duration_seconds: int, extra_ms: int = 0) -> None:
        if mode not in ("fail", "latency"):
            raise ValueError(f"Unknown chaos mode: {mode}")
        payload = json.dumps({"mode": mode, "extra_ms": extra_ms})
        await self._redis.set(self._key(tenant_id), payload, ex=duration_seconds)

    async def disable(self, tenant_id: str) -> None:
        await self._redis.delete(self._key(tenant_id))

    async def get_active(self, tenant_id: str) -> dict | None:
        raw = await self._redis.get(self._key(tenant_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
