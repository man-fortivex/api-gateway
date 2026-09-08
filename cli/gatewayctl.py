#!/usr/bin/env python3
"""gatewayctl — command-line client for the API Gateway admin API.

Install:
    pip install click httpx

Usage:
    gatewayctl login --url http://localhost:8000 --username admin
    gatewayctl tenant create --name "Acme" --slug acme --email ops@acme.com --upstream https://api.acme.com
    gatewayctl tenant list
    gatewayctl key issue --tenant-id <id> --label prod --rate-limit 100
    gatewayctl key revoke --key-id <id>
    gatewayctl key rotate --key-id <id>
    gatewayctl webhook add --tenant-id <id> --url https://example.com/hook --event key.revoked
    gatewayctl audit-log

Credentials (base URL + bearer token) are stored in ~/.gatewayctl/credentials.json
after `login`, so subsequent commands don't need to re-authenticate.
"""

import json
import sys
from pathlib import Path

import click
import httpx

CONFIG_DIR = Path.home() / ".gatewayctl"
CONFIG_FILE = CONFIG_DIR / "credentials.json"


def _load_credentials() -> dict:
    if not CONFIG_FILE.exists():
        click.echo("Not logged in. Run `gatewayctl login` first.", err=True)
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text())


def _save_credentials(base_url: str, token: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"base_url": base_url, "token": token}))
    CONFIG_FILE.chmod(0o600)  # contains a bearer token; don't leave it world-readable


def _client() -> httpx.Client:
    creds = _load_credentials()
    return httpx.Client(base_url=creds["base_url"], headers={"Authorization": f"Bearer {creds['token']}"})


def _print_response(resp: httpx.Response) -> None:
    try:
        data = resp.json()
        click.echo(json.dumps(data, indent=2))
    except json.JSONDecodeError:
        click.echo(resp.text)
    if resp.is_error:
        sys.exit(1)


@click.group()
def cli() -> None:
    """gatewayctl — manage your API Gateway from the command line."""


@cli.command()
@click.option("--url", required=True, help="Gateway base URL, e.g. http://localhost:8000")
@click.option("--username", required=True)
@click.option("--password", prompt=True, hide_input=True)
def login(url: str, username: str, password: str) -> None:
    """Authenticate and store the admin bearer token locally."""
    resp = httpx.post(f"{url}/admin/auth/login", json={"username": username, "password": password})
    if resp.is_error:
        click.echo(f"Login failed: {resp.text}", err=True)
        sys.exit(1)
    token = resp.json()["access_token"]
    _save_credentials(url, token)
    click.echo(f"Logged in. Credentials stored at {CONFIG_FILE}")


@cli.group()
def tenant() -> None:
    """Manage tenants."""


@tenant.command("create")
@click.option("--name", required=True)
@click.option("--slug", required=True)
@click.option("--email", required=True)
@click.option("--upstream", required=True, help="Upstream base URL")
@click.option("--plan", default="free", type=click.Choice(["free", "starter", "pro", "enterprise"]))
def tenant_create(name: str, slug: str, email: str, upstream: str, plan: str) -> None:
    with _client() as client:
        resp = client.post(
            "/admin/tenants",
            json={"name": name, "slug": slug, "email": email, "upstream_base_url": upstream, "plan": plan},
        )
    _print_response(resp)


@tenant.command("list")
def tenant_list() -> None:
    with _client() as client:
        resp = client.get("/admin/tenants")
    _print_response(resp)


@tenant.command("get")
@click.argument("tenant_id")
def tenant_get(tenant_id: str) -> None:
    with _client() as client:
        resp = client.get(f"/admin/tenants/{tenant_id}")
    _print_response(resp)


@cli.group()
def key() -> None:
    """Manage API keys."""


@key.command("issue")
@click.option("--tenant-id", required=True)
@click.option("--label", default="default")
@click.option("--rate-limit", default=60, type=int)
@click.option("--path-prefix", default=None, help="Restrict this key to paths under this prefix")
@click.option("--expires-in", default=None, type=int, help="Seconds until this key expires")
def key_issue(tenant_id: str, label: str, rate_limit: int, path_prefix: str | None, expires_in: int | None) -> None:
    with _client() as client:
        resp = client.post(
            f"/admin/tenants/{tenant_id}/api-keys",
            json={
                "label": label,
                "rate_limit_per_minute": rate_limit,
                "allowed_path_prefix": path_prefix,
                "expires_in_seconds": expires_in,
            },
        )
    _print_response(resp)


@key.command("list")
@click.option("--tenant-id", required=True)
def key_list(tenant_id: str) -> None:
    with _client() as client:
        resp = client.get(f"/admin/tenants/{tenant_id}/api-keys")
    _print_response(resp)


@key.command("revoke")
@click.option("--key-id", required=True)
def key_revoke(key_id: str) -> None:
    with _client() as client:
        resp = client.delete(f"/admin/api-keys/{key_id}")
    if resp.status_code == 204:
        click.echo("Revoked.")
    else:
        _print_response(resp)


@key.command("rotate")
@click.option("--key-id", required=True)
def key_rotate(key_id: str) -> None:
    with _client() as client:
        resp = client.post(f"/admin/api-keys/{key_id}/rotate")
    _print_response(resp)


@cli.group()
def webhook() -> None:
    """Manage webhook endpoints."""


@webhook.command("add")
@click.option("--tenant-id", required=True)
@click.option("--url", required=True)
@click.option("--event", "events", multiple=True, help="Repeatable; omit to receive all event types")
def webhook_add(tenant_id: str, url: str, events: tuple[str, ...]) -> None:
    with _client() as client:
        resp = client.post(
            f"/admin/tenants/{tenant_id}/webhooks", json={"url": url, "event_types": list(events)}
        )
    _print_response(resp)


@webhook.command("list")
@click.option("--tenant-id", required=True)
def webhook_list(tenant_id: str) -> None:
    with _client() as client:
        resp = client.get(f"/admin/tenants/{tenant_id}/webhooks")
    _print_response(resp)


@cli.command("audit-log")
@click.option("--limit", default=50, type=int)
def audit_log(limit: int) -> None:
    with _client() as client:
        resp = client.get("/admin/audit-log", params={"limit": limit})
    _print_response(resp)


@cli.group()
def chaos() -> None:
    """Fault injection for testing resilience (circuit breaker, retries)."""


@chaos.command("enable")
@click.option("--tenant-id", required=True)
@click.option("--mode", required=True, type=click.Choice(["fail", "latency"]))
@click.option("--duration", default=60, type=int, help="Seconds this chaos mode stays active")
@click.option("--extra-ms", default=0, type=int, help="Extra latency to inject (mode=latency only)")
def chaos_enable(tenant_id: str, mode: str, duration: int, extra_ms: int) -> None:
    with _client() as client:
        resp = client.post(
            f"/admin/tenants/{tenant_id}/chaos",
            json={"mode": mode, "duration_seconds": duration, "extra_ms": extra_ms},
        )
    if resp.status_code == 204:
        click.echo(f"Chaos mode '{mode}' enabled for {duration}s.")
    else:
        _print_response(resp)


@chaos.command("disable")
@click.option("--tenant-id", required=True)
def chaos_disable(tenant_id: str) -> None:
    with _client() as client:
        resp = client.delete(f"/admin/tenants/{tenant_id}/chaos")
    if resp.status_code == 204:
        click.echo("Chaos mode disabled.")
    else:
        _print_response(resp)


@cli.command("cache-purge")
@click.option("--tenant-id", required=True)
def cache_purge(tenant_id: str) -> None:
    with _client() as client:
        resp = client.post(f"/admin/tenants/{tenant_id}/cache/purge")
    _print_response(resp)


if __name__ == "__main__":
    cli()
