import httpx
import pytest
import respx

TEST_ADMIN_USERNAME = "admin"
TEST_ADMIN_PASSWORD = "test-password-123"


async def _create_tenant_and_key(client, rate_limit=3):
    resp = await client.post(
        "/admin/tenants",
        json={
            "name": "Acme Inc",
            "slug": "acme",
            "email": "ops@acmeinc.com",
            "upstream_base_url": "https://upstream.acmeinc.com",
        },
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


@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_list_tenants_empty_then_populated(client):
    resp = await client.get("/admin/tenants")
    assert resp.status_code == 200
    assert resp.json() == []

    await _create_tenant_and_key(client)

    resp = await client.get("/admin/tenants")
    assert resp.status_code == 200
    tenants = resp.json()
    assert len(tenants) == 1
    assert tenants[0]["slug"] == "acme"


@pytest.mark.asyncio
async def test_dashboard_root_serves_html(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Gateway Console" in resp.text


@pytest.mark.asyncio
async def test_create_tenant_rejects_duplicate_slug(client):
    await _create_tenant_and_key(client)
    resp = await client.post(
        "/admin/tenants",
        json={
            "name": "Acme Again",
            "slug": "acme",
            "email": "dup@acmeinc.com",
            "upstream_base_url": "https://upstream.acmeinc.com",
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_proxy_forwards_and_returns_upstream_response(client):
    tenant, key = await _create_tenant_and_key(client, rate_limit=5)

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/v1/ping").mock(
            return_value=httpx.Response(200, json={"pong": True})
        )
        resp = await client.get(
            "/gw/acme/v1/ping",
            headers={"X-API-Key": key["plaintext_key"]},
        )

    assert resp.status_code == 200
    assert resp.json() == {"pong": True}
    assert "X-RateLimit-Remaining" in resp.headers


@pytest.mark.asyncio
async def test_proxy_rejects_missing_api_key(client):
    tenant, key = await _create_tenant_and_key(client)
    resp = await client.get("/gw/acme/v1/ping")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_proxy_rejects_invalid_api_key(client):
    tenant, key = await _create_tenant_and_key(client)
    resp = await client.get("/gw/acme/v1/ping", headers={"X-API-Key": "agw_not_a_real_key"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_proxy_rejects_key_used_against_wrong_tenant_slug(client):
    tenant, key = await _create_tenant_and_key(client)
    resp = await client.get("/gw/some-other-tenant/v1/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_is_rejected(client):
    tenant, key = await _create_tenant_and_key(client)
    resp = await client.delete(f"/admin/api-keys/{key['id']}")
    assert resp.status_code == 204

    resp = await client.get("/gw/acme/v1/ping", headers={"X-API-Key": key["plaintext_key"]})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rate_limit_enforced_end_to_end(client):
    tenant, key = await _create_tenant_and_key(client, rate_limit=2)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://upstream.acmeinc.com/v1/ping").mock(
            return_value=httpx.Response(200, json={"pong": True})
        )

        r1 = await client.get("/gw/acme/v1/ping", headers={"X-API-Key": key["plaintext_key"]})
        r2 = await client.get("/gw/acme/v1/ping", headers={"X-API-Key": key["plaintext_key"]})
        r3 = await client.get("/gw/acme/v1/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3.headers["Retry-After"] == "60"


@pytest.mark.asyncio
async def test_api_key_header_is_not_leaked_upstream(client):
    """The caller's gateway API key must never be forwarded to the tenant's
    own backend — the tenant has no business seeing it."""
    tenant, key = await _create_tenant_and_key(client, rate_limit=5)

    captured_headers = {}

    def _capture(request):
        captured_headers.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/v1/ping").mock(side_effect=_capture)
        resp = await client.get(
            "/gw/acme/v1/ping",
            headers={"X-API-Key": key["plaintext_key"]},
        )

    assert resp.status_code == 200
    assert "x-api-key" not in {k.lower() for k in captured_headers}


@pytest.mark.asyncio
async def test_upstream_unreachable_returns_502(client):
    tenant, key = await _create_tenant_and_key(client, rate_limit=5)

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/v1/down").mock(side_effect=httpx.ConnectError("boom"))
        resp = await client.get("/gw/acme/v1/down", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 502
