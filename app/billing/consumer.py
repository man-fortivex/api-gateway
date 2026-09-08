"""Standalone worker process: run separately from the API server.

    python -m app.billing.consumer

Consumes usage events from Kafka and aggregates them into per-tenant,
per-api-key, per-minute UsageLog rows for billing. Runs as its own process
so a slow or down billing pipeline never blocks the request-serving gateway.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer
from sqlalchemy import select

from app.config import get_settings
from app.database import AsyncSessionLocal, init_models
from app.models import UsageLog

logger = logging.getLogger("gateway.billing.consumer")


def _bucket_for(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M")


async def _upsert_usage(event: dict) -> None:
    bucket = _bucket_for(event["ts"])
    is_error = event["status_code"] >= 400

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UsageLog).where(
                UsageLog.tenant_id == event["tenant_id"],
                UsageLog.api_key_id == event["api_key_id"],
                UsageLog.bucket_minute == bucket,
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            row = UsageLog(
                tenant_id=event["tenant_id"],
                api_key_id=event["api_key_id"],
                bucket_minute=bucket,
                request_count=0,
                error_count=0,
                total_latency_ms=0,
            )
            session.add(row)

        row.request_count += 1
        row.error_count += 1 if is_error else 0
        row.total_latency_ms += event["latency_ms"]

        await session.commit()


async def run_consumer() -> None:
    settings = get_settings()
    await init_models()

    consumer = AIOKafkaConsumer(
        settings.KAFKA_USAGE_TOPIC,
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id="billing-aggregator",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=True,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    logger.info("Billing consumer started, listening on %s", settings.KAFKA_USAGE_TOPIC)
    try:
        async for msg in consumer:
            try:
                await _upsert_usage(msg.value)
            except Exception:
                logger.exception("Failed to process usage event: %s", msg.value)
    finally:
        await consumer.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_consumer())
