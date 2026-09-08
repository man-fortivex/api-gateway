import json

import httpx
import pytest
import respx
from click.testing import CliRunner

from cli.gatewayctl import CONFIG_FILE, cli


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    """Point the CLI's credential storage at a temp file so tests never
    touch the real ~/.gatewayctl/credentials.json."""
    fake_config = tmp_path / "credentials.json"
    import cli.gatewayctl as gatewayctl_module

    monkeypatch.setattr(gatewayctl_module, "CONFIG_FILE", fake_config)
    monkeypatch.setattr(gatewayctl_module, "CONFIG_DIR", tmp_path)
    yield


@pytest.fixture
def runner():
    return CliRunner()


def test_login_stores_credentials(runner, tmp_path):
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-gateway.test/admin/auth/login").mock(
            return_value=httpx.Response(200, json={"access_token": "fake-token-123", "token_type": "bearer"})
        )
        result = runner.invoke(
            cli,
            ["login", "--url", "http://fake-gateway.test", "--username", "admin"],
            input="secretpass\n",
        )

    assert result.exit_code == 0, result.output
    assert "Logged in" in result.output

    import cli.gatewayctl as gatewayctl_module

    stored = json.loads(gatewayctl_module.CONFIG_FILE.read_text())
    assert stored["token"] == "fake-token-123"
    assert stored["base_url"] == "http://fake-gateway.test"


def test_login_failure_exits_nonzero(runner):
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://fake-gateway.test/admin/auth/login").mock(
            return_value=httpx.Response(401, json={"detail": "Invalid credentials"})
        )
        result = runner.invoke(
            cli,
            ["login", "--url", "http://fake-gateway.test", "--username", "admin"],
            input="wrongpass\n",
        )

    assert result.exit_code != 0


def test_tenant_create_requires_login_first(runner):
    result = runner.invoke(
        cli,
        ["tenant", "create", "--name", "Acme", "--slug", "acme", "--email", "a@b.com", "--upstream", "https://x.com"],
    )
    assert result.exit_code != 0
    assert "Not logged in" in result.output


def _write_fake_credentials(tmp_path):
    import cli.gatewayctl as gatewayctl_module

    gatewayctl_module.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    gatewayctl_module.CONFIG_FILE.write_text(
        json.dumps({"base_url": "http://fake-gateway.test", "token": "fake-token-123"})
    )


def test_tenant_create_sends_expected_request(runner, tmp_path):
    _write_fake_credentials(tmp_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://fake-gateway.test/admin/tenants").mock(
            return_value=httpx.Response(201, json={"id": "t-123", "slug": "acme"})
        )
        result = runner.invoke(
            cli,
            [
                "tenant", "create",
                "--name", "Acme", "--slug", "acme",
                "--email", "ops@acme.com", "--upstream", "https://api.acme.com",
            ],
        )

    assert result.exit_code == 0, result.output
    assert route.called
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["slug"] == "acme"
    assert sent_body["upstream_base_url"] == "https://api.acme.com"
    assert '"id": "t-123"' in result.output


def test_key_issue_sends_expected_fields(runner, tmp_path):
    _write_fake_credentials(tmp_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://fake-gateway.test/admin/tenants/t-123/api-keys").mock(
            return_value=httpx.Response(201, json={"plaintext_key": "agw_fake"})
        )
        result = runner.invoke(
            cli,
            [
                "key", "issue",
                "--tenant-id", "t-123", "--label", "prod",
                "--rate-limit", "100", "--path-prefix", "reports",
            ],
        )

    assert result.exit_code == 0, result.output
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body["label"] == "prod"
    assert sent_body["rate_limit_per_minute"] == 100
    assert sent_body["allowed_path_prefix"] == "reports"


def test_key_revoke_reports_success(runner, tmp_path):
    _write_fake_credentials(tmp_path)

    with respx.mock(assert_all_called=True) as mock:
        mock.delete("http://fake-gateway.test/admin/api-keys/key-1").mock(return_value=httpx.Response(204))
        result = runner.invoke(cli, ["key", "revoke", "--key-id", "key-1"])

    assert result.exit_code == 0
    assert "Revoked" in result.output


def test_chaos_enable_sends_correct_payload(runner, tmp_path):
    _write_fake_credentials(tmp_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://fake-gateway.test/admin/tenants/t-123/chaos").mock(
            return_value=httpx.Response(204)
        )
        result = runner.invoke(
            cli,
            ["chaos", "enable", "--tenant-id", "t-123", "--mode", "latency", "--duration", "30", "--extra-ms", "500"],
        )

    assert result.exit_code == 0
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body == {"mode": "latency", "duration_seconds": 30, "extra_ms": 500}


def test_webhook_add_supports_repeated_event_flags(runner, tmp_path):
    _write_fake_credentials(tmp_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://fake-gateway.test/admin/tenants/t-123/webhooks").mock(
            return_value=httpx.Response(201, json={"secret": "shh"})
        )
        result = runner.invoke(
            cli,
            [
                "webhook", "add",
                "--tenant-id", "t-123", "--url", "https://example.com/hook",
                "--event", "key.revoked", "--event", "circuit.opened",
            ],
        )

    assert result.exit_code == 0
    sent_body = json.loads(route.calls[0].request.content)
    assert sorted(sent_body["event_types"]) == ["circuit.opened", "key.revoked"]
