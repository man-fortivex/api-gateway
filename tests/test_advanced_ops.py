import asyncio
import time

import httpx
import pytest
import respx

from app.config import get_settings

settings = get_settings()


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
# IP allowlist
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ip_allowlist_blocks_disallowed_source(client):
    tenant = await _create_tenant(client, ip_allowlist=["203.0.113.0/24"])
    key = await _create_key(client, tenant["id"])

    # httpx's ASGITransport test client presents as 127.0.0.1, which is
    # NOT in the allowlist above, so this should be rejected.
    resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_ip_allowlist_allows_matching_source(client):
    tenant = await _create_tenant(client, ip_allowlist=["127.0.0.0/8"])
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_empty_allowlist_permits_everything(client):
    tenant = await _create_tenant(client)  # no ip_allowlist set
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Request schema validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_validation_rejects_invalid_body(client):
    schema = {
        "type": "object",
        "properties": {"email": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["email"],
    }
    tenant = await _create_tenant(client, request_schemas={"POST /users": schema})
    key = await _create_key(client, tenant["id"])

    resp = await client.post(
        "/gw/acme/users", headers={"X-API-Key": key["plaintext_key"]}, content='{"age": "not-a-number"}'
    )
    assert resp.status_code == 400
    assert "errors" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_schema_validation_allows_valid_body(client):
    schema = {
        "type": "object",
        "properties": {"email": {"type": "string"}},
        "required": ["email"],
    }
    tenant = await _create_tenant(client, request_schemas={"POST /users": schema})
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://upstream.acmeinc.com/users").mock(return_value=httpx.Response(201, json={"ok": True}))
        resp = await client.post(
            "/gw/acme/users", headers={"X-API-Key": key["plaintext_key"]}, content='{"email": "a@b.com"}'
        )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_schema_validation_only_applies_to_matching_path(client):
    schema = {"type": "object", "required": ["email"]}
    tenant = await _create_tenant(client, request_schemas={"POST /users": schema})
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/other").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/other", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


# --------------------------------------------------------------------------
# API key TTL / expiry / rotation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_with_expiry_works_before_expiring(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"], expires_in_seconds=3600)
    assert key["expires_at"] is not None

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_expired_key_is_rejected(client, monkeypatch):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"], expires_in_seconds=60)

    # Fast-forward past expiry by monkeypatching datetime.now used in auth.
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(seconds=120)

    import app.auth as auth_module

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr(auth_module, "datetime", _FakeDatetime)

    resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_key_rotation_keeps_old_key_valid_during_grace_period(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    resp = await client.post(f"/admin/api-keys/{key['id']}/rotate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new_key = body["new_key"]
    assert new_key["plaintext_key"].startswith("agw_")
    assert body["old_key_id"] == key["id"]

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))

        # Both old and new keys should work during the grace period.
        old_resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
        new_resp = await client.get("/gw/acme/ping", headers={"X-API-Key": new_key["plaintext_key"]})

    assert old_resp.status_code == 200
    assert new_resp.status_code == 200


@pytest.mark.asyncio
async def test_cannot_rotate_already_revoked_key(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])
    await client.delete(f"/admin/api-keys/{key['id']}")

    resp = await client.post(f"/admin/api-keys/{key['id']}/rotate")
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Response caching
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_cache_hit_skips_upstream_on_second_call(client):
    tenant = await _create_tenant(client, cache_ttl_seconds=60)
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://upstream.acmeinc.com/data").mock(
            return_value=httpx.Response(200, json={"value": 42})
        )
        r1 = await client.get("/gw/acme/data", headers={"X-API-Key": key["plaintext_key"]})
        r2 = await client.get("/gw/acme/data", headers={"X-API-Key": key["plaintext_key"]})

    assert r1.status_code == 200
    assert r1.headers.get("X-Cache") == "MISS"
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "HIT"
    assert r2.json() == {"value": 42}
    assert route.call_count == 1  # upstream hit only once, second was served from cache


@pytest.mark.asyncio
async def test_cache_purge_forces_fresh_upstream_call(client):
    tenant = await _create_tenant(client, cache_ttl_seconds=60)
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://upstream.acmeinc.com/data").mock(
            return_value=httpx.Response(200, json={"value": 1})
        )
        await client.get("/gw/acme/data", headers={"X-API-Key": key["plaintext_key"]})

        purge_resp = await client.post(f"/admin/tenants/{tenant['id']}/cache/purge")
        assert purge_resp.status_code == 200
        assert purge_resp.json()["purged_count"] >= 1

        r2 = await client.get("/gw/acme/data", headers={"X-API-Key": key["plaintext_key"]})

    assert r2.headers.get("X-Cache") == "MISS"
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_uncached_tenant_never_shows_cache_header(client):
    tenant = await _create_tenant(client)  # no cache_ttl_seconds
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/data").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/data", headers={"X-API-Key": key["plaintext_key"]})

    assert "X-Cache" not in resp.headers


# --------------------------------------------------------------------------
# Canary rollout
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_100_percent_always_routes_to_canary_version(client, monkeypatch):
    tenant = await _create_tenant(
        client,
        version_upstreams={"v2": "https://canary.acmeinc.com"},
        canary_rules=[{"version": "v2", "percentage": 100}],
    )
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://canary.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"canary": True}))
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    assert resp.json() == {"canary": True}


@pytest.mark.asyncio
async def test_canary_0_percent_never_routes_to_canary(client):
    tenant = await _create_tenant(
        client,
        version_upstreams={"v2": "https://canary.acmeinc.com"},
        canary_rules=[{"version": "v2", "percentage": 0}],
    )
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"canary": False}))
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    assert resp.json() == {"canary": False}


@pytest.mark.asyncio
async def test_explicit_version_in_path_overrides_canary(client):
    tenant = await _create_tenant(
        client,
        version_upstreams={"v1": "https://old.acmeinc.com", "v2": "https://canary.acmeinc.com"},
        canary_rules=[{"version": "v2", "percentage": 100}],
    )
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://old.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"explicit": "v1"}))
        resp = await client.get("/gw/acme/v1/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    assert resp.json() == {"explicit": "v1"}


# --------------------------------------------------------------------------
# Chaos / fault injection
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chaos_fail_mode_forces_502_without_touching_upstream(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    resp = await client.post(
        f"/admin/tenants/{tenant['id']}/chaos", json={"mode": "fail", "duration_seconds": 60}
    )
    assert resp.status_code == 204

    with respx.mock(assert_all_called=False) as mock:
        # No route registered for the real upstream — if chaos correctly
        # short-circuits, respx never needs to see a request at all.
        gw_resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert gw_resp.status_code == 502
    assert "chaos" in gw_resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_chaos_latency_mode_adds_delay_then_succeeds(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    resp = await client.post(
        f"/admin/tenants/{tenant['id']}/chaos",
        json={"mode": "latency", "duration_seconds": 60, "extra_ms": 200},
    )
    assert resp.status_code == 204

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        start = time.perf_counter()
        gw_resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
        elapsed = time.perf_counter() - start

    assert gw_resp.status_code == 200
    assert elapsed >= 0.2


@pytest.mark.asyncio
async def test_disabling_chaos_restores_normal_behavior(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    await client.post(f"/admin/tenants/{tenant['id']}/chaos", json={"mode": "fail", "duration_seconds": 60})
    disable_resp = await client.delete(f"/admin/tenants/{tenant['id']}/chaos")
    assert disable_resp.status_code == 204

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
