import json
import logging
import time
from typing import Any

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaConnectionError

from app.config import get_settings

logger = logging.getLogger("gateway.kafka")


class UsageEventProducer:
    """Publishes billing/usage events to Kafka.

    Design note: if Kafka is unreachable, the gateway must NOT go down just
    because billing telemetry can't be shipped. We log and drop rather than
    raise, since losing a few usage events is far cheaper than an outage.
    A production deployment should back this with a local durable queue
    (e.g. Redis stream) as a retry buffer if zero data loss is required.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._producer: AIOKafkaProducer | None = None
        self._healthy = False

    async def start(self) -> None:
        if not self._settings.KAFKA_ENABLED:
            logger.info("Kafka disabled via config; usage events will be logged only.")
            return
        try:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._settings.KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                enable_idempotence=True,
            )
            await self._producer.start()
            self._healthy = True
        except KafkaConnectionError:
            logger.warning("Could not connect to Kafka at startup; usage events will be logged only.")
            self._producer = None
            self._healthy = False

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()

    async def publish_usage_event(
        self,
        tenant_id: str,
        api_key_id: str,
        path: str,
        status_code: int,
        latency_ms: int,
    ) -> None:
        event: dict[str, Any] = {
            "tenant_id": tenant_id,
            "api_key_id": api_key_id,
            "path": path,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "ts": time.time(),
        }

        if self._producer is not None and self._healthy:
            try:
                await self._producer.send_and_wait(self._settings.KAFKA_USAGE_TOPIC, event)
                return
            except Exception:
                logger.exception("Failed to publish usage event to Kafka; falling back to log.")

        logger.info("usage_event=%s", event)


usage_producer = UsageEventProducer()
