"""Redis-backed caches for the two DB lookups that happen on EVERY
proxied request: tenant-by-slug (for the IP allowlist check) and
api-key-by-hash (for auth). Both are cheap to cache because, in the
current API surface, tenants and keys are effectively immutable after
creation except for two explicit actions — revoke and rotate — which
call invalidate() directly. Everything else (short TTL as a backstop)
bounds staleness to a few seconds even if an invalidation call were ever
missed.

This is what turns "3 sequential DB round trips per request" into
"0 DB round trips per request" for the overwhelmingly common case of a
key that's been used before recently — the single biggest latency lever
available without changing what the gateway actually does.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from redis.asyncio import Redis


@dataclass
class CachedPlan:
    value: str


@dataclass
class CachedTenant:
    id: str
    slug: str
    upstream_base_url: str
    plan: CachedPlan
    is_active: bool
    upstream_replicas: list = field(default_factory=list)
    version_upstreams: dict = field(default_factory=dict)
    deprecated_versions: list = field(default_factory=list)
    transform_rules: list = field(default_factory=list)
    graphql_path: str | None = None
    ip_allowlist: list = field(default_factory=list)
    cache_ttl_seconds: int | None = None
    request_schemas: dict = field(default_factory=dict)
    canary_rules: list = field(default_factory=list)


@dataclass
class CachedAPIKey:
    id: str
    tenant_id: str
    rate_limit_per_minute: int
    is_active: bool
    label: str
    allowed_path_prefix: str | None = None
    expires_at: datetime | None = None


def _tenant_to_dict(tenant) -> dict:
    plan_value = tenant.plan.value if hasattr(tenant.plan, "value") else tenant.plan
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "upstream_base_url": tenant.upstream_base_url,
        "plan": plan_value,
        "is_active": tenant.is_active,
        "upstream_replicas": tenant.upstream_replicas,
        "version_upstreams": tenant.version_upstreams,
        "deprecated_versions": tenant.deprecated_versions,
        "transform_rules": tenant.transform_rules,
        "graphql_path": tenant.graphql_path,
        "ip_allowlist": tenant.ip_allowlist,
        "cache_ttl_seconds": tenant.cache_ttl_seconds,
        "request_schemas": tenant.request_schemas,
        "canary_rules": tenant.canary_rules,
    }


def _dict_to_cached_tenant(data: dict) -> CachedTenant:
    return CachedTenant(
        id=data["id"],
        slug=data["slug"],
        upstream_base_url=data["upstream_base_url"],
        plan=CachedPlan(value=data["plan"]),
        is_active=data["is_active"],
        upstream_replicas=data.get("upstream_replicas", []),
        version_upstreams=data.get("version_upstreams", {}),
        deprecated_versions=data.get("deprecated_versions", []),
        transform_rules=data.get("transform_rules", []),
        graphql_path=data.get("graphql_path"),
        ip_allowlist=data.get("ip_allowlist", []),
        cache_ttl_seconds=data.get("cache_ttl_seconds"),
        request_schemas=data.get("request_schemas", {}),
        canary_rules=data.get("canary_rules", []),
    )


class TenantCache:
    def __init__(self, redis: Redis, ttl_seconds: int = 30):
        self._redis = redis
        self._ttl = ttl_seconds

    def _key(self, slug: str) -> str:
        return f"tenant_cache:slug:{slug}"

    async def get(self, slug: str) -> CachedTenant | None:
        raw = await self._redis.get(self._key(slug))
        if raw is None:
            return None
        return _dict_to_cached_tenant(json.loads(raw))

    async def set(self, tenant) -> None:
        await self._redis.set(self._key(tenant.slug), json.dumps(_tenant_to_dict(tenant)), ex=self._ttl)

    async def invalidate(self, slug: str) -> None:
        await self._redis.delete(self._key(slug))


class KeyCache:
    def __init__(self, redis: Redis, ttl_seconds: int = 10):
        self._redis = redis
        self._ttl = ttl_seconds

    def _key(self, key_hash: str) -> str:
        return f"key_cache:{key_hash}"

    async def get(self, key_hash: str) -> CachedAPIKey | None:
        raw = await self._redis.get(self._key(key_hash))
        if raw is None:
            return None
        data = json.loads(raw)
        expires_at = datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
        return CachedAPIKey(
            id=data["id"],
            tenant_id=data["tenant_id"],
            rate_limit_per_minute=data["rate_limit_per_minute"],
            is_active=data["is_active"],
            label=data["label"],
            allowed_path_prefix=data.get("allowed_path_prefix"),
            expires_at=expires_at,
        )

    async def set(self, api_key) -> None:
        payload = {
            "id": api_key.id,
            "tenant_id": api_key.tenant_id,
            "rate_limit_per_minute": api_key.rate_limit_per_minute,
            "is_active": api_key.is_active,
            "label": api_key.label,
            "allowed_path_prefix": api_key.allowed_path_prefix,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        }
        await self._redis.set(self._key(api_key.key_hash), json.dumps(payload), ex=self._ttl)

    async def invalidate(self, key_hash: str) -> None:
        await self._redis.delete(self._key(key_hash))
