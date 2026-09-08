import hashlib
import json

from redis.asyncio import Redis


class ResponseCache:
    """Caches idempotent GET responses per tenant. Disabled unless a
    tenant sets cache_ttl_seconds. Only successful (200) responses under
    a size cap are cached — large or error responses skip the cache
    entirely rather than filling Redis with them."""

    def __init__(self, redis: Redis, max_body_bytes: int):
        self._redis = redis
        self._max_body_bytes = max_body_bytes

    def _key(self, tenant_id: str, method: str, path: str, query_string: str) -> str:
        raw = f"{method}:{path}:{query_string}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"response_cache:{tenant_id}:{digest}"

    async def get(self, tenant_id: str, method: str, path: str, query_string: str) -> dict | None:
        if method != "GET":
            return None
        raw = await self._redis.get(self._key(tenant_id, method, path, query_string))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set(
        self,
        tenant_id: str,
        method: str,
        path: str,
        query_string: str,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        ttl_seconds: int,
    ) -> None:
        if method != "GET" or status_code != 200 or ttl_seconds <= 0:
            return
        if len(body) > self._max_body_bytes:
            return

        payload = json.dumps(
            {
                "status_code": status_code,
                "headers": headers,
                "body": body.decode("latin-1"),
            }
        )
        await self._redis.set(self._key(tenant_id, method, path, query_string), payload, ex=ttl_seconds)

    async def purge_tenant(self, tenant_id: str) -> int:
        """Purges only this tenant's cached entries. Uses SCAN rather than
        KEYS to avoid blocking Redis on a large keyspace. tenant_id is
        embedded in plaintext in the key prefix specifically so this scan
        can be scoped correctly — a fully-hashed key would make per-tenant
        purging impossible without a separate index."""
        count = 0
        async for key in self._redis.scan_iter(match=f"response_cache:{tenant_id}:*"):
            await self._redis.delete(key)
            count += 1
        return count
