import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class PlanTier(str, enum.Enum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    upstream_base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    plan: Mapped[PlanTier] = mapped_column(Enum(PlanTier), default=PlanTier.FREE, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Multi-region / multi-replica upstreams. Each entry:
    # {"region": "us-east", "url": "https://us.acme.com", "priority": 0}
    # Empty list means "just use upstream_base_url" (single-origin, the
    # common case). Lower priority number = tried first.
    upstream_replicas: Mapped[list] = mapped_column(JSON, default=list)

    # Per-API-version upstream overrides, e.g. {"v1": "https://old.acme.com",
    # "v2": "https://new.acme.com"}. A version not listed here falls back
    # to upstream_base_url / upstream_replicas.
    version_upstreams: Mapped[dict] = mapped_column(JSON, default=dict)
    deprecated_versions: Mapped[list] = mapped_column(JSON, default=list)

    # Request/response transform rules, applied in order. Supported types:
    # add_header, remove_header, rewrite_path_prefix, strip_json_field.
    # See app/transforms.py for the exact schema each type expects.
    transform_rules: Mapped[list] = mapped_column(JSON, default=list)

    # Optional: path (relative to the gateway prefix) treated as a GraphQL
    # endpoint for query-complexity-aware rate limiting instead of plain
    # per-request counting. None disables this.
    graphql_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Per-tenant CIDR allowlist, e.g. ["203.0.113.0/24", "198.51.100.7/32"].
    # Empty list = allow all (the common case; most tenants don't restrict by IP).
    ip_allowlist: Mapped[list] = mapped_column(JSON, default=list)

    # Response caching for idempotent GETs. None/0 disables caching.
    cache_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Request schema validation: {"METHOD /path": {<json schema>}}. A
    # request whose method+path matches a key is validated against that
    # schema before being forwarded; on failure the gateway returns 400
    # without ever calling the upstream.
    request_schemas: Mapped[dict] = mapped_column(JSON, default=dict)

    # Canary / percentage-based rollout: [{"version": "v2", "percentage": 10}].
    # For UNVERSIONED requests only (an explicit /v1/ or /v2/ in the path
    # always wins) — this splits the "no version specified" traffic between
    # the default upstream and a canary version's upstream.
    canary_rules: Mapped[list] = mapped_column(JSON, default=list)

    api_keys: Mapped[list["APIKey"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)  # shown to user, e.g. agw_ab12
    label: Mapped[str] = mapped_column(String(255), default="default")
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    # If set, this key may only call downstream paths starting with this
    # prefix (after leading-slash stripping), e.g. "reports/" for a
    # read-only reporting key. None = full access to the tenant's API.
    allowed_path_prefix: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")


class UsageLog(Base):
    """Aggregated per-minute usage bucket, written by the billing consumer."""

    __tablename__ = "usage_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    api_key_id: Mapped[str] = mapped_column(String(36), ForeignKey("api_keys.id"), nullable=False, index=True)
    bucket_minute: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # e.g. 2026-07-09T12:34
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    total_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class WebhookEndpoint(Base):
    """A tenant-registered URL to receive event notifications (quota
    warnings, circuit breaker trips, key revocations)."""

    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    secret: Mapped[str] = mapped_column(String(255), nullable=False)
    # Which event types this endpoint wants; empty list = all events.
    event_types: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLogEntry(Base):
    """Immutable record of admin actions, for compliance/audit purposes.
    Rows are only ever inserted, never updated or deleted."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)  # admin username
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "tenant.create"
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "tenant" | "api_key"
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
