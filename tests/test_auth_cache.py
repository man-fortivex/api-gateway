import fakeredis.aioredis
import httpx
import pytest
import respx

from app.auth_cache import CachedAPIKey, CachedTenant, KeyCache, TenantCache


async def _create_tenant(client, **overrides):
    payload = {
        "name": "Acme Inc",
        "slug": "acme",
        "email": "ops@acmeinc.com",
        "upstream_base_url": "https://upstream.acmeinc.com",
    }
    payload.update(overrides)
    resp = await client.post("/admin/tenants", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_key(client, tenant_id, **overrides):
    payload = {"label": "prod", "rate_limit_per_minute": 1000}
    payload.update(overrides)
    resp = await client.post(f"/admin/tenants/{tenant_id}/api-keys", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# TenantCache unit tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_cache_miss_returns_none():
    redis = fakeredis.aioredis.FakeRedis()
    cache = TenantCache(redis)
    assert await cache.get("nonexistent") is None


@pytest.mark.asyncio
async def test_tenant_cache_round_trip():
    from app.models import PlanTier, Tenant

    redis = fakeredis.aioredis.FakeRedis()
    cache = TenantCache(redis)

    tenant = Tenant(
        id="t1", name="Acme", slug="acme", email="a@b.com",
        upstream_base_url="https://x.com", plan=PlanTier.FREE, is_active=True,
        upstream_replicas=[], version_upstreams={}, deprecated_versions=[],
        transform_rules=[], graphql_path=None, ip_allowlist=["10.0.0.0/8"],
        cache_ttl_seconds=None, request_schemas={}, canary_rules=[],
    )
    await cache.set(tenant)

    cached = await cache.get("acme")
    assert isinstance(cached, CachedTenant)
    assert cached.id == "t1"
    assert cached.plan.value == "free"
    assert cached.ip_allowlist == ["10.0.0.0/8"]


@pytest.mark.asyncio
async def test_tenant_cache_invalidate_removes_entry():
    from app.models import PlanTier, Tenant

    redis = fakeredis.aioredis.FakeRedis()
    cache = TenantCache(redis)
    tenant = Tenant(
        id="t1", name="Acme", slug="acme", email="a@b.com",
        upstream_base_url="https://x.com", plan=PlanTier.FREE, is_active=True,
        upstream_replicas=[], version_upstreams={}, deprecated_versions=[],
        transform_rules=[], graphql_path=None, ip_allowlist=[],
        cache_ttl_seconds=None, request_schemas={}, canary_rules=[],
    )
    await cache.set(tenant)
    assert await cache.get("acme") is not None

    await cache.invalidate("acme")
    assert await cache.get("acme") is None


# --------------------------------------------------------------------------
# KeyCache unit tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_cache_round_trip():
    from app.models import APIKey

    redis = fakeredis.aioredis.FakeRedis()
    cache = KeyCache(redis)

    api_key = APIKey(
        id="k1", tenant_id="t1", key_hash="deadbeef", key_prefix="agw_abc",
        label="prod", rate_limit_per_minute=60, allowed_path_prefix=None,
        is_active=True,
    )
    await cache.set(api_key)

    cached = await cache.get("deadbeef")
    assert isinstance(cached, CachedAPIKey)
    assert cached.id == "k1"
    assert cached.tenant_id == "t1"
    assert cached.rate_limit_per_minute == 60


@pytest.mark.asyncio
async def test_key_cache_invalidate_removes_entry():
    from app.models import APIKey

    redis = fakeredis.aioredis.FakeRedis()
    cache = KeyCache(redis)
    api_key = APIKey(
        id="k1", tenant_id="t1", key_hash="deadbeef", key_prefix="agw_abc",
        label="prod", rate_limit_per_minute=60, is_active=True,
    )
    await cache.set(api_key)
    assert await cache.get("deadbeef") is not None

    await cache.invalidate("deadbeef")
    assert await cache.get("deadbeef") is None


# --------------------------------------------------------------------------
# End-to-end: caching doesn't break correctness
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_requests_with_same_key_both_succeed(client):
    """The second request should hit the auth cache rather than the DB —
    functionally this must look identical to the caller either way."""
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        r1 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
        r2 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert r1.status_code == 200
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_revoked_key_rejected_immediately_despite_caching(client):
    """Critical correctness test: revoking a key must invalidate the auth
    cache, not just the DB row — otherwise a revoked key would keep
    working until the cache TTL naturally expired."""
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        # First call populates the auth cache.
        r1 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert r1.status_code == 200

    revoke_resp = await client.delete(f"/admin/api-keys/{key['id']}")
    assert revoke_resp.status_code == 204

    # Must be rejected on the VERY NEXT request, not after some TTL delay.
    r2 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_rotated_key_expiry_change_is_reflected_immediately(client, monkeypatch):
    """Rotation shortens the old key's expiry — the cache must reflect
    that new (sooner) expiry right away, not the pre-rotation value."""
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        # Populate the cache first.
        r1 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert r1.status_code == 200

    rotate_resp = await client.post(f"/admin/api-keys/{key['id']}/rotate")
    assert rotate_resp.status_code == 200

    # Fast-forward past the (short, test-configured) grace period.
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(days=2)

    import app.auth as auth_module

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(auth_module, "datetime", _FakeDatetime)

    r2 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_ip_allowlist_still_enforced_on_cached_tenant(client):
    """The tenant cache must carry ip_allowlist correctly — a cache-hit
    path must not silently bypass IP restrictions."""
    tenant = await _create_tenant(client, ip_allowlist=["203.0.113.0/24"])
    key = await _create_key(client, tenant["id"])

    # First request populates the tenant cache; both should be rejected
    # since the test client's IP (127.0.0.1) never matches the allowlist.
    r1 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    r2 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert r1.status_code == 403
    assert r2.status_code == 403
