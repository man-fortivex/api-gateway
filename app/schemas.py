from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models import PlanTier


class UpstreamReplica(BaseModel):
    url: str
    region: str | None = None
    priority: int = 0


class TransformRule(BaseModel):
    """See app/transforms.py for the exact meaning of each type's fields.
    Left loosely typed (extra fields allowed) since different rule types
    use different keys."""

    model_config = {"extra": "allow"}
    type: str


class CanaryRule(BaseModel):
    version: str
    percentage: float = Field(ge=0, le=100)


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    email: EmailStr
    upstream_base_url: str
    plan: PlanTier = PlanTier.FREE
    upstream_replicas: list[UpstreamReplica] = Field(default_factory=list)
    version_upstreams: dict[str, str] = Field(default_factory=dict)
    deprecated_versions: list[str] = Field(default_factory=list)
    transform_rules: list[TransformRule] = Field(default_factory=list)
    graphql_path: str | None = None
    ip_allowlist: list[str] = Field(default_factory=list)
    cache_ttl_seconds: int | None = None
    request_schemas: dict[str, dict] = Field(default_factory=dict)
    canary_rules: list[CanaryRule] = Field(default_factory=list)


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    email: str
    upstream_base_url: str
    plan: PlanTier
    is_active: bool
    created_at: datetime
    upstream_replicas: list[dict] = Field(default_factory=list)
    version_upstreams: dict[str, str] = Field(default_factory=dict)
    deprecated_versions: list[str] = Field(default_factory=list)
    transform_rules: list[dict] = Field(default_factory=list)
    graphql_path: str | None = None
    ip_allowlist: list[str] = Field(default_factory=list)
    cache_ttl_seconds: int | None = None
    request_schemas: dict = Field(default_factory=dict)
    canary_rules: list[dict] = Field(default_factory=list)
    stripe_customer_id: str | None = None
    stripe_subscription_item_id: str | None = None

    model_config = {"from_attributes": True}


class APIKeyCreate(BaseModel):
    label: str = "default"
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100_000)
    allowed_path_prefix: str | None = None
    expires_in_seconds: int | None = Field(default=None, ge=60)


class APIKeyOut(BaseModel):
    id: str
    key_prefix: str
    label: str
    rate_limit_per_minute: int
    allowed_path_prefix: str | None = None
    expires_at: datetime | None = None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class APIKeyCreatedOut(APIKeyOut):
    """Returned only once, at creation time — the plaintext key is never stored."""

    plaintext_key: str


class KeyRotateResponse(BaseModel):
    new_key: APIKeyCreatedOut
    old_key_id: str
    old_key_expires_at: datetime


class ChaosRequest(BaseModel):
    mode: str = Field(pattern="^(fail|latency)$")
    duration_seconds: int = Field(default=60, ge=1, le=3600)
    extra_ms: int = Field(default=0, ge=0, le=30_000)


class CachePurgeResponse(BaseModel):
    purged_count: int


class UsageSummaryOut(BaseModel):
    tenant_id: str
    api_key_id: str
    bucket_minute: str
    request_count: int
    error_count: int
    avg_latency_ms: float


class WebhookEndpointCreate(BaseModel):
    url: str
    event_types: list[str] = Field(default_factory=list)


class WebhookEndpointOut(BaseModel):
    id: str
    tenant_id: str
    url: str
    event_types: list[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookEndpointCreatedOut(WebhookEndpointOut):
    """Returned only once — the signing secret is used to verify delivery
    authenticity (HMAC-SHA256 over the payload, in X-Gateway-Signature)."""

    secret: str


class AuditLogOut(BaseModel):
    id: str
    actor: str
    action: str
    target_type: str
    target_id: str
    detail: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class StripeProvisionResponse(BaseModel):
    stripe_customer_id: str
    stripe_subscription_item_id: str
