import httpx
import pytest
import respx

from app.config import get_settings

settings = get_settings()


async def _create_tenant_and_key(client, rate_limit=1000, upstream="https://upstream.acmeinc.com"):
    resp = await client.post(
        "/admin/tenants",
        json={"name": "Acme Inc", "slug": "acme", "email": "ops@acmeinc.com", "upstream_base_url": upstream},
    )
    assert resp.status_code == 201, resp.text
    tenant = resp.json()

    resp = await client.post(
        f"/admin/tenants/{tenant['id']}/api-keys",
        json={"label": "prod", "rate_limit_per_minute": rate_limit},
    )
    assert resp.status_code == 201, resp.text
    key = resp.json()
    return tenant, key


# --------------------------------------------------------------------------
# Monthly quota
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monthly_quota_blocks_after_limit(client, monkeypatch):
    monkeypatch.setitem(settings.PLAN_MONTHLY_QUOTA, "free", 2)
    tenant, key = await _create_tenant_and_key(client)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))

        r1 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
        r2 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
        r3 = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 402


@pytest.mark.asyncio
async def test_unlimited_plan_quota_never_blocks(client, monkeypatch):
    monkeypatch.setitem(settings.PLAN_MONTHLY_QUOTA, "free", None)
    tenant, key = await _create_tenant_and_key(client)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        for _ in range(5):
            resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
            assert resp.status_code == 200


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_repeated_failures(client, monkeypatch):
    monkeypatch.setattr(settings, "CIRCUIT_BREAKER_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(settings, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 30)
    monkeypatch.setattr(settings, "PROXY_MAX_RETRIES", 0)
    tenant, key = await _create_tenant_and_key(client)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://upstream.acmeinc.com/down").mock(side_effect=httpx.ConnectError("boom"))

        r1 = await client.get("/gw/acme/down", headers={"X-API-Key": key["plaintext_key"]})
        r2 = await client.get("/gw/acme/down", headers={"X-API-Key": key["plaintext_key"]})
        assert r1.status_code == 502
        assert r2.status_code == 502

        # Third request: circuit should now be open and reject WITHOUT
        # touching the upstream at all (no respx route needed for /still-down).
        r3 = await client.get("/gw/acme/still-down", headers={"X-API-Key": key["plaintext_key"]})

    assert r3.status_code == 503
    assert "Retry-After" in r3.headers


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(client, monkeypatch):
    monkeypatch.setattr(settings, "CIRCUIT_BREAKER_FAILURE_THRESHOLD", 3)
    tenant, key = await _create_tenant_and_key(client)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://upstream.acmeinc.com/flaky").mock(side_effect=httpx.ConnectError("boom"))
        r1 = await client.get("/gw/acme/flaky", headers={"X-API-Key": key["plaintext_key"]})
        assert r1.status_code == 502

        mock.get("https://upstream.acmeinc.com/ok").mock(return_value=httpx.Response(200, json={"ok": True}))
        r2 = await client.get("/gw/acme/ok", headers={"X-API-Key": key["plaintext_key"]})
        assert r2.status_code == 200

        # Failure counter should have been cleared by the success above.
        r3 = await client.get("/gw/acme/flaky", headers={"X-API-Key": key["plaintext_key"]})
        assert r3.status_code == 502  # still just a normal 502, not circuit-open 503


# --------------------------------------------------------------------------
# Self-serve signup
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_creates_tenant_and_working_key(anon_client):
    resp = await anon_client.post(
        "/signup",
        json={
            "name": "New Co",
            "slug": "newco",
            "email": "ops@newco.com",
            "upstream_base_url": "https://upstream.newco.com",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "newco"
    assert body["plaintext_key"].startswith("agw_")

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.newco.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        gw_resp = await anon_client.get("/gw/newco/ping", headers={"X-API-Key": body["plaintext_key"]})
    assert gw_resp.status_code == 200


@pytest.mark.asyncio
async def test_signup_rejects_duplicate_slug(anon_client):
    payload = {
        "name": "Dup Co",
        "slug": "dupco",
        "email": "ops@dupco.com",
        "upstream_base_url": "https://upstream.dupco.com",
    }
    resp1 = await anon_client.post("/signup", json=payload)
    assert resp1.status_code == 201

    resp2 = await anon_client.post("/signup", json=payload)
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_signup_is_rate_limited_per_ip(anon_client, monkeypatch):
    from app.routers import signup as signup_module

    monkeypatch.setattr(signup_module, "_SIGNUP_LIMIT_PER_HOUR", 2)

    for i in range(2):
        resp = await anon_client.post(
            "/signup",
            json={
                "name": f"Co {i}",
                "slug": f"co-{i}",
                "email": f"ops@co{i}.com",
                "upstream_base_url": "https://upstream.example.com",
            },
        )
        assert resp.status_code == 201

    resp = await anon_client.post(
        "/signup",
        json={"name": "Co 3", "slug": "co-3", "email": "ops@co3.com", "upstream_base_url": "https://upstream.example.com"},
    )
    assert resp.status_code == 429


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_request_counter(client, anon_client):
    tenant, key = await _create_tenant_and_key(client)

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
        gw_resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert gw_resp.status_code == 200

    metrics_resp = await anon_client.get("/metrics")
    assert metrics_resp.status_code == 200
    assert "gateway_requests_total" in metrics_resp.text
    assert 'tenant_slug="acme"' in metrics_resp.text
