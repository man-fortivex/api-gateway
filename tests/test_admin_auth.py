import pytest

from tests.conftest import TEST_ADMIN_PASSWORD, TEST_ADMIN_USERNAME


@pytest.mark.asyncio
async def test_login_succeeds_with_correct_credentials(anon_client):
    resp = await anon_client.post(
        "/admin/auth/login",
        json={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(anon_client):
    resp = await anon_client.post(
        "/admin/auth/login",
        json={"username": TEST_ADMIN_USERNAME, "password": "wrong-password"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_unknown_username(anon_client):
    resp = await anon_client.post(
        "/admin/auth/login",
        json={"username": "nobody", "password": TEST_ADMIN_PASSWORD},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_tenants_requires_auth(anon_client):
    resp = await anon_client.get("/admin/tenants")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_billing_usage_requires_auth(anon_client):
    resp = await anon_client.get("/billing/tenants/some-id/usage")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_tenants_rejects_garbage_token(anon_client):
    anon_client.headers["Authorization"] = "Bearer not-a-real-token"
    resp = await anon_client.get("/admin/tenants")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_client_can_access_admin(client):
    resp = await client.get("/admin/tenants")
    assert resp.status_code == 200
