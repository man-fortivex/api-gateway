import json

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
# Transform rules
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_header_rule_reaches_upstream(client):
    tenant = await _create_tenant(
        client, transform_rules=[{"type": "add_header", "name": "X-Injected", "value": "hello"}]
    )
    key = await _create_key(client, tenant["id"])

    captured = {}

    def _capture(request):
        captured.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(side_effect=_capture)
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    assert captured.get("x-injected") == "hello"


@pytest.mark.asyncio
async def test_remove_header_rule_strips_header_before_upstream(client):
    tenant = await _create_tenant(client, transform_rules=[{"type": "remove_header", "name": "X-Secret"}])
    key = await _create_key(client, tenant["id"])

    captured = {}

    def _capture(request):
        captured.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(side_effect=_capture)
        resp = await client.get(
            "/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"], "X-Secret": "shouldnotarrive"}
        )

    assert resp.status_code == 200
    assert "x-secret" not in captured


@pytest.mark.asyncio
async def test_strip_json_field_rule_removes_field_from_response(client):
    tenant = await _create_tenant(client, transform_rules=[{"type": "strip_json_field", "field": "internal_id"}])
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/ping").mock(
            return_value=httpx.Response(200, json={"internal_id": "secret-123", "public": "ok"})
        )
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    body = resp.json()
    assert "internal_id" not in body
    assert body["public"] == "ok"


@pytest.mark.asyncio
async def test_rewrite_path_prefix_rule(client):
    tenant = await _create_tenant(
        client, transform_rules=[{"type": "rewrite_path_prefix", "from": "public", "to": "internal/v3"}]
    )
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/internal/v3/users").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        resp = await client.get("/gw/acme/public/users", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


# --------------------------------------------------------------------------
# API versioning
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_routes_to_version_specific_upstream(client):
    tenant = await _create_tenant(
        client,
        version_upstreams={"v1": "https://old.acmeinc.com", "v2": "https://new.acmeinc.com"},
    )
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://old.acmeinc.com/users").mock(return_value=httpx.Response(200, json={"ver": "old"}))
        resp = await client.get("/gw/acme/v1/users", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    assert resp.json() == {"ver": "old"}


@pytest.mark.asyncio
async def test_deprecated_version_gets_sunset_header(client):
    tenant = await _create_tenant(
        client,
        version_upstreams={"v1": "https://old.acmeinc.com"},
        deprecated_versions=["v1"],
    )
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://old.acmeinc.com/users").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/v1/users", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    assert resp.headers.get("Sunset") == "true"
    assert resp.headers.get("X-API-Deprecated-Version") == "v1"


@pytest.mark.asyncio
async def test_unversioned_request_falls_back_to_default_upstream(client):
    tenant = await _create_tenant(client, version_upstreams={"v1": "https://old.acmeinc.com"})
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/users").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/users", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


# --------------------------------------------------------------------------
# API key path scoping
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_key_can_access_allowed_prefix(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"], allowed_path_prefix="reports")

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/reports/q1").mock(return_value=httpx.Response(200, json={"ok": True}))
        resp = await client.get("/gw/acme/reports/q1", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_scoped_key_rejected_outside_allowed_prefix(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"], allowed_path_prefix="reports")

    resp = await client.get("/gw/acme/admin/delete-everything", headers={"X-API-Key": key["plaintext_key"]})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unscoped_key_can_access_anything(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])  # no allowed_path_prefix

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://upstream.acmeinc.com/anything/goes").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        resp = await client.get("/gw/acme/anything/goes", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Multi-region failover
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_region_fails_over_to_second_replica(client, monkeypatch):
    from app.config import get_settings as _gs

    monkeypatch.setattr(_gs(), "PROXY_MAX_RETRIES", 0)

    tenant = await _create_tenant(
        client,
        upstream_replicas=[
            {"url": "https://us-east.acmeinc.com", "region": "us-east", "priority": 0},
            {"url": "https://us-west.acmeinc.com", "region": "us-west", "priority": 1},
        ],
    )
    key = await _create_key(client, tenant["id"])

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://us-east.acmeinc.com/ping").mock(side_effect=httpx.ConnectError("down"))
        mock.get("https://us-west.acmeinc.com/ping").mock(return_value=httpx.Response(200, json={"region": "west"}))
        resp = await client.get("/gw/acme/ping", headers={"X-API-Key": key["plaintext_key"]})

    assert resp.status_code == 200
    assert resp.json() == {"region": "west"}


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_webhook_returns_secret_once(client):
    tenant = await _create_tenant(client)
    resp = await client.post(
        f"/admin/tenants/{tenant['id']}/webhooks",
        json={"url": "https://example.com/hook", "event_types": ["key.revoked"]},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["secret"]
    assert body["url"] == "https://example.com/hook"


@pytest.mark.asyncio
async def test_key_revocation_triggers_webhook_delivery(client, monkeypatch):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])

    await client.post(
        f"/admin/tenants/{tenant['id']}/webhooks",
        json={"url": "https://example.com/hook", "event_types": ["key.revoked"]},
    )

    delivered = {}

    async def _fake_post(self, url, content=None, headers=None, **kwargs):
        delivered["url"] = url
        delivered["headers"] = headers
        delivered["body"] = json.loads(content)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    resp = await client.delete(f"/admin/api-keys/{key['id']}")
    assert resp.status_code == 204

    assert delivered.get("url") == "https://example.com/hook"
    assert delivered["body"]["event"] == "key.revoked"
    assert "X-Gateway-Signature" in delivered["headers"]


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_records_tenant_and_key_actions(client):
    tenant = await _create_tenant(client)
    key = await _create_key(client, tenant["id"])
    await client.delete(f"/admin/api-keys/{key['id']}")

    resp = await client.get("/admin/audit-log")
    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()]
    assert "tenant.create" in actions
    assert "api_key.create" in actions
    assert "api_key.revoke" in actions


# --------------------------------------------------------------------------
# Stripe provisioning
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stripe_provision_disabled_by_default(client):
    tenant = await _create_tenant(client)
    resp = await client.post(f"/admin/tenants/{tenant['id']}/stripe/provision")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stripe_provision_creates_customer_and_subscription(client, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_ENABLED", True)
    monkeypatch.setattr(settings, "STRIPE_API_KEY", "sk_test_fake")
    monkeypatch.setattr(settings, "STRIPE_PLAN_PRICE_IDS", {"free": "price_fake123"})

    import stripe as stripe_module

    class FakeCustomer:
        id = "cus_fake123"

    class FakeSubscription(dict):
        pass

    def fake_customer_create(**kwargs):
        return FakeCustomer()

    def fake_subscription_create(**kwargs):
        return FakeSubscription(items={"data": [{"id": "si_fake123"}]})

    monkeypatch.setattr(stripe_module.Customer, "create", fake_customer_create)
    monkeypatch.setattr(stripe_module.Subscription, "create", fake_subscription_create)

    tenant = await _create_tenant(client)
    resp = await client.post(f"/admin/tenants/{tenant['id']}/stripe/provision")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stripe_customer_id"] == "cus_fake123"
    assert body["stripe_subscription_item_id"] == "si_fake123"


# --------------------------------------------------------------------------
# GraphQL complexity limiting
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graphql_deep_query_consumes_more_budget_than_shallow(client, monkeypatch):
    from app.config import get_settings as _gs

    monkeypatch.setattr(_gs(), "GRAPHQL_COMPLEXITY_POINTS_PER_MINUTE", 20)

    tenant = await _create_tenant(client, graphql_path="graphql")
    key = await _create_key(client, tenant["id"], rate_limit_per_minute=1000)

    shallow_query = json.dumps({"query": "{ users { id } }"})
    deep_query = json.dumps({"query": "{ a { b { c { d { e { f { g { h } } } } } } } }"})

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://upstream.acmeinc.com/graphql").mock(return_value=httpx.Response(200, json={"ok": True}))

        # A few shallow queries should be fine within the budget.
        r1 = await client.post(
            "/gw/acme/graphql", headers={"X-API-Key": key["plaintext_key"]}, content=shallow_query
        )
        assert r1.status_code == 200

        # A single deep query should consume much more of the budget and
        # eventually get rejected well before 1000 plain requests would.
        r2 = await client.post("/gw/acme/graphql", headers={"X-API-Key": key["plaintext_key"]}, content=deep_query)

    assert r2.status_code == 429
    assert "complexity" in r2.json()["detail"].lower()
