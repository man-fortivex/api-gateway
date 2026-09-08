import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WebhookEndpoint

logger = logging.getLogger("gateway.webhooks")

_DELIVERY_TIMEOUT_SECONDS = 5.0


def _sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def _deliver(endpoint: WebhookEndpoint, event_type: str, payload: dict) -> None:
    body = json.dumps({"event": event_type, "data": payload, "ts": time.time()}).encode("utf-8")
    signature = _sign_payload(endpoint.secret, body)

    try:
        async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_SECONDS) as client:
            await client.post(
                endpoint.url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Gateway-Signature": signature,
                    "X-Gateway-Event": event_type,
                },
            )
    except Exception:
        # Fire-and-forget: a tenant's webhook receiver being down must never
        # affect the gateway itself. No retry queue in this MVP — a
        # production version should push failed deliveries to a dead-letter
        # queue for later replay, and expose delivery status in the dashboard.
        logger.warning("Webhook delivery failed for endpoint %s, event %s", endpoint.id, event_type)


async def dispatch_event(db: AsyncSession, tenant_id: str, event_type: str, payload: dict) -> None:
    """Sends `event_type` to all of the tenant's active webhook endpoints
    that subscribe to it. Runs deliveries concurrently and does not raise
    on delivery failure — this must never block or fail the request path
    that triggered the event."""
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.is_active.is_(True),
        )
    )
    endpoints = result.scalars().all()

    matching = [
        e for e in endpoints
        if not e.event_types or event_type in e.event_types
    ]
    if not matching:
        return

    await asyncio.gather(*(_deliver(e, event_type, payload) for e in matching), return_exceptions=True)


async def dispatch_event_standalone(tenant_id: str, event_type: str, payload: dict) -> None:
    """Same as dispatch_event, but opens its own short-lived DB session.

    Use this from FastAPI BackgroundTasks on the hot (gateway proxy) path:
    a request-scoped `db` session is closed by FastAPI as soon as the route
    handler returns, before background tasks run, so reusing it here would
    be a use-after-close bug. This function is safe to schedule with
    `background_tasks.add_task(...)` for exactly that reason.
    """
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await dispatch_event(session, tenant_id, event_type, payload)
