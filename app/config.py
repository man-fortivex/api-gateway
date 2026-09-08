from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration. All values overridable via environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "API Gateway SaaS"
    ENV: str = "development"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./gateway.db"
    # Only applied for non-SQLite databases (see app/database.py)
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 30
    DB_POOL_RECYCLE_SECONDS: int = 1800

    # Redis (rate limiting + key cache)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Kafka (usage event streaming for billing)
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_USAGE_TOPIC: str = "gateway.usage.events"
    KAFKA_ENABLED: bool = True

    # Proxy behaviour
    PROXY_TIMEOUT_SECONDS: float = 15.0
    MAX_REQUEST_BODY_BYTES: int = 5 * 1024 * 1024  # 5 MB

    # Upstream connection pooling — tuned for heavy concurrent load rather
    # than httpx's conservative defaults (100 max / 20 keepalive).
    PROXY_MAX_CONNECTIONS: int = 500
    PROXY_MAX_KEEPALIVE_CONNECTIONS: int = 100
    PROXY_KEEPALIVE_EXPIRY_SECONDS: float = 30.0

    # Security
    API_KEY_PREFIX: str = "agw_"
    ADMIN_JWT_SECRET: str = "change-me-to-a-random-32-plus-character-secret-in-production"
    ADMIN_JWT_EXPIRE_MINUTES: int = 60 * 12

    # Admin login (single operator account for MVP; swap for a real user
    # table + multi-admin support before this is a multi-person team).
    ADMIN_USERNAME: str = "admin"
    # bcrypt hash of the admin password. Generate with:
    #   python -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
    ADMIN_PASSWORD_HASH: str = ""

    # Monthly request quotas per plan tier (None = unlimited). Enforced via
    # a Redis counter, independent of the per-minute rate limiter.
    PLAN_MONTHLY_QUOTA: dict[str, int | None] = {
        "free": 10_000,
        "starter": 100_000,
        "pro": 1_000_000,
        "enterprise": None,
    }

    # Stripe metered billing
    STRIPE_API_KEY: str = ""
    STRIPE_ENABLED: bool = False
    # Maps plan tier -> Stripe Price ID, for the auto-provisioning endpoint.
    # These must already exist in your Stripe account (this doesn't create
    # products/prices, only subscribes a tenant to one that exists).
    STRIPE_PLAN_PRICE_IDS: dict[str, str] = {}

    # Upstream retry / circuit breaker
    PROXY_MAX_RETRIES: int = 2
    PROXY_RETRY_BACKOFF_BASE_SECONDS: float = 0.2
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 30

    # Distributed tracing
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "api-gateway"
    # If unset, spans are printed to stdout (console exporter) — fine for
    # local dev. Set to a real collector URL (e.g. an OTLP-HTTP endpoint)
    # in production.
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""

    # GraphQL query-complexity limiting (applies only on a tenant's
    # configured graphql_path)
    GRAPHQL_COMPLEXITY_POINTS_PER_MINUTE: int = 500

    # API key rotation: how long the OLD key stays valid after rotation,
    # so callers have time to switch over without a hard cutover.
    KEY_ROTATION_GRACE_SECONDS: int = 24 * 60 * 60

    # Response cache (Redis-backed, per tenant via Tenant.cache_ttl_seconds)
    RESPONSE_CACHE_MAX_BODY_BYTES: int = 512 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
