"""Standalone worker: reports aggregated usage to Stripe metered billing.

    python -m app.billing.stripe_sync          # run once
    python -m app.billing.stripe_sync --loop   # run every 24h

For each active tenant with a `stripe_subscription_item_id` set, sums the
UsageLog rows for the previous UTC day and reports them to Stripe as a
usage record on that subscription item. Run this daily (via docker-compose,
a cron entry, or a scheduler) — it does not run inside the request path,
so a slow or down Stripe API never affects gateway latency.
"""

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import stripe
from sqlalchemy import select

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import Tenant, UsageLog

logger = logging.getLogger("gateway.billing.stripe_sync")


def _yesterday_bucket_prefix() -> str:
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


async def _sum_usage_for_tenant(tenant_id: str, day_prefix: str) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UsageLog).where(
                UsageLog.tenant_id == tenant_id,
                UsageLog.bucket_minute.like(f"{day_prefix}%"),
            )
        )
        rows = result.scalars().all()
        return sum(row.request_count for row in rows)


async def _report_to_stripe(subscription_item_id: str, quantity: int) -> None:
    if quantity <= 0:
        return
    # Stripe's SDK is synchronous; run it off the event loop so it doesn't
    # block other work if this is ever imported into a long-running process.
    await asyncio.to_thread(
        stripe.SubscriptionItem.create_usage_record,
        subscription_item_id,
        quantity=quantity,
        timestamp=int(datetime.now(timezone.utc).timestamp()),
        action="increment",
    )


async def sync_once() -> None:
    settings = get_settings()
    if not settings.STRIPE_ENABLED:
        logger.info("Stripe billing disabled via config; skipping sync.")
        return
    if not settings.STRIPE_API_KEY:
        logger.warning("STRIPE_ENABLED is true but STRIPE_API_KEY is empty; skipping sync.")
        return

    stripe.api_key = settings.STRIPE_API_KEY
    day_prefix = _yesterday_bucket_prefix()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(
                Tenant.is_active.is_(True),
                Tenant.stripe_subscription_item_id.is_not(None),
            )
        )
        tenants = result.scalars().all()

    logger.info("Syncing usage for %d tenant(s), day=%s", len(tenants), day_prefix)

    for tenant in tenants:
        try:
            quantity = await _sum_usage_for_tenant(tenant.id, day_prefix)
            await _report_to_stripe(tenant.stripe_subscription_item_id, quantity)
            logger.info("Reported %d requests for tenant %s (%s)", quantity, tenant.slug, tenant.id)
        except Exception:
            logger.exception("Failed to sync usage for tenant %s (%s)", tenant.slug, tenant.id)


async def run_loop(interval_seconds: int = 24 * 60 * 60) -> None:
    while True:
        await sync_once()
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run continuously, once per day")
    args = parser.parse_args()

    if args.loop:
        asyncio.run(run_loop())
    else:
        asyncio.run(sync_once())
