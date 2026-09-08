import asyncio
import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import extract_api_key_header, resolve_api_key_fast, resolve_tenant_by_slug_cached
from app.auth_cache import KeyCache, TenantCache
from app.chaos import ChaosInjector
from app.circuit_breaker import CircuitBreaker
from app.config import get_settings
from app.database import get_db
from app.graphql_limiter import GraphQLComplexityLimiter
from app.ip_allowlist import is_ip_allowed
from app.kafka_producer import usage_producer
from app.metrics import CIRCUIT_OPEN_COUNT, QUOTA_EXCEEDED_COUNT, RATE_LIMITED_COUNT, record_request
from app.proxy import forward_request
from app.quota import MonthlyQuota
from app.rate_limiter import RateLimiter
from app.redis_client import get_redis
from app.region_router import LatencyTracker, resolve_candidates, select_canary_version
from app.response_cache import ResponseCache
from app.schema_validator import find_matching_schema, validate_body
from app.tracing import start_span
from app.webhooks import dispatch_event_standalone

router = APIRouter(tags=["gateway"])
settings = get_settings()

_VERSION_SEGMENT_RE = re.compile(r"^v[0-9]+$")
_QUOTA_WARNING_RATIO = 0.9


def get_rate_limiter() -> RateLimiter:
    return RateLimiter(get_redis(), window_seconds=60)


def get_monthly_quota() -> MonthlyQuota:
    return MonthlyQuota(get_redis())


def get_circuit_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        get_redis(),
        failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        cooldown_seconds=settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    )


def get_latency_tracker() -> LatencyTracker:
    return LatencyTracker(get_redis())


def get_response_cache() -> ResponseCache:
    return ResponseCache(get_redis(), settings.RESPONSE_CACHE_MAX_BODY_BYTES)


def get_chaos_injector() -> ChaosInjector:
    return ChaosInjector(get_redis())


def get_tenant_cache() -> TenantCache:
    return TenantCache(get_redis())


def get_key_cache() -> KeyCache:
    return KeyCache(get_redis())


def _split_version(downstream_path: str) -> tuple[str | None, str]:
    parts = downstream_path.lstrip("/").split("/", 1)
    if parts and _VERSION_SEGMENT_RE.match(parts[0]):
        remainder = parts[1] if len(parts) > 1 else ""
        return parts[0], remainder
    return None, downstream_path


@router.api_route(
    "/gw/{tenant_slug}/{downstream_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_endpoint(
    tenant_slug: str,
    downstream_path: str,
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(extract_api_key_header),
    db: AsyncSession = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
    quota: MonthlyQuota = Depends(get_monthly_quota),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    latency_tracker: LatencyTracker = Depends(get_latency_tracker),
    response_cache: ResponseCache = Depends(get_response_cache),
    chaos_injector: ChaosInjector = Depends(get_chaos_injector),
    tenant_cache: TenantCache = Depends(get_tenant_cache),
    key_cache: KeyCache = Depends(get_key_cache),
) -> Response:
    # --- Tenant lookup (cached) + IP allowlist, BEFORE key validation ----
    # so a disallowed IP never learns anything about whether its key
    # would otherwise have been valid. On a cache hit this is a single
    # Redis GET and zero DB queries.
    tenant_for_ip_check = await resolve_tenant_by_slug_cached(db, tenant_cache, tenant_slug)
    if tenant_for_ip_check is not None and tenant_for_ip_check.ip_allowlist:
        client_ip = request.client.host if request.client else ""
        if not is_ip_allowed(client_ip, tenant_for_ip_check.ip_allowlist):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Source IP not permitted")

    auth_ctx = await resolve_api_key_fast(db, key_cache, api_key, known_tenant=tenant_for_ip_check)
    tenant = auth_ctx.tenant

    if tenant.slug != tenant_slug:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")

    normalized_path = downstream_path.lstrip("/")

    # --- API key path scoping ---------------------------------------
    if auth_ctx.api_key.allowed_path_prefix:
        if not normalized_path.startswith(auth_ctx.api_key.allowed_path_prefix.lstrip("/")):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key is not permitted to access this path",
            )

    # --- Per-minute rate limit ---------------------------------------
    allowed, current_count = await limiter.check(
        identity_key=auth_ctx.api_key.id,
        limit_per_window=auth_ctx.api_key.rate_limit_per_minute,
    )
    if not allowed:
        RATE_LIMITED_COUNT.labels(tenant_slug=tenant_slug).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({auth_ctx.api_key.rate_limit_per_minute} req/min)",
            headers={"Retry-After": "60"},
        )

    # --- GraphQL query-complexity limiting (in addition to the above) ---
    if tenant.graphql_path and normalized_path == tenant.graphql_path.lstrip("/"):
        gql_limiter = GraphQLComplexityLimiter(limiter, settings.GRAPHQL_COMPLEXITY_POINTS_PER_MINUTE)
        body_preview = await request.body()
        gql_allowed, _ = await gql_limiter.check(auth_ctx.api_key.id, body_preview)
        if not gql_allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="GraphQL query complexity budget exceeded for this minute",
                headers={"Retry-After": "60"},
            )

    # --- Request schema validation ---------------------------------------
    matching_schema = find_matching_schema(tenant.request_schemas, request.method, normalized_path)
    if matching_schema is not None:
        body_preview = await request.body()
        errors = validate_body(body_preview, matching_schema)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "Request failed schema validation", "errors": errors},
            )

    # --- Monthly quota -------------------------------------------------
    monthly_limit = settings.PLAN_MONTHLY_QUOTA.get(tenant.plan.value)
    quota_ok, quota_count = await quota.check_and_increment(tenant.id, monthly_limit)
    if not quota_ok:
        QUOTA_EXCEEDED_COUNT.labels(tenant_slug=tenant_slug).inc()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Monthly quota exceeded ({monthly_limit} requests on the {tenant.plan.value} plan)",
        )
    if monthly_limit is not None and quota_count / monthly_limit >= _QUOTA_WARNING_RATIO:
        background_tasks.add_task(
            dispatch_event_standalone,
            tenant.id,
            "quota.warning",
            {"tenant_slug": tenant_slug, "used": quota_count, "limit": monthly_limit},
        )

    # --- Response cache (GET only) ---------------------------------------
    if tenant.cache_ttl_seconds:
        cached = await response_cache.get(tenant.id, request.method, normalized_path, str(request.query_params))
        if cached is not None:
            record_request(tenant_slug, cached["status_code"], 0)
            response = Response(
                content=cached["body"].encode("latin-1"),
                status_code=cached["status_code"],
                headers=cached["headers"],
            )
            response.headers["X-Cache"] = "HIT"
            return response

    # --- Circuit breaker + chaos state, combined into ONE Redis round
    # trip via MGET rather than two sequential GETs — this is on every
    # single request, so halving its round trips matters under load. ---
    redis = get_redis()
    is_open_raw, chaos_raw = await redis.mget(breaker.open_key(tenant.id), chaos_injector.redis_key(tenant.id))

    if is_open_raw is not None:
        CIRCUIT_OPEN_COUNT.labels(tenant_slug=tenant_slug).inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upstream is currently failing repeatedly; temporarily rejecting requests",
            headers={"Retry-After": str(settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS)},
        )

    chaos_state = None
    if chaos_raw is not None:
        try:
            chaos_state = json.loads(chaos_raw)
        except (json.JSONDecodeError, TypeError):
            chaos_state = None

    # --- Version routing, canary rollout, multi-region candidates ---------
    version, remaining_path = _split_version(downstream_path)
    if version is None and tenant.canary_rules:
        canary_version = select_canary_version(tenant.canary_rules)
        if canary_version and canary_version in tenant.version_upstreams:
            version = canary_version

    candidates = resolve_candidates(tenant.upstream_base_url, tenant.upstream_replicas, tenant.version_upstreams, version)
    candidates = await latency_tracker.order_by_latency(candidates)
    forward_path = remaining_path if (version and version in tenant.version_upstreams) else downstream_path

    with start_span("gateway.proxy_request", {"tenant.slug": tenant_slug, "http.method": request.method}):
        try:
            if chaos_state and chaos_state.get("mode") == "fail":
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail="Chaos: simulated upstream failure"
                )
            if chaos_state and chaos_state.get("mode") == "latency" and chaos_state.get("extra_ms"):
                await asyncio.sleep(chaos_state["extra_ms"] / 1000)

            response, latency_ms, used_upstream_url = await forward_request(
                request, candidates, forward_path, tenant.transform_rules
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_502_BAD_GATEWAY:
                opened_now = await breaker.record_failure(tenant.id)
                if opened_now:
                    background_tasks.add_task(
                        dispatch_event_standalone,
                        tenant.id,
                        "circuit.opened",
                        {"tenant_slug": tenant_slug},
                    )
            raise

    await breaker.record_success(tenant.id)
    if chaos_state is None:
        # Don't let simulated chaos pollute real latency-based routing data.
        await latency_tracker.record(used_upstream_url, latency_ms)
    record_request(tenant_slug, response.status_code, latency_ms)

    if tenant.cache_ttl_seconds:
        await response_cache.set(
            tenant.id,
            request.method,
            normalized_path,
            str(request.query_params),
            response.status_code,
            dict(response.headers),
            response.body,
            tenant.cache_ttl_seconds,
        )
        response.headers["X-Cache"] = "MISS"

    await usage_producer.publish_usage_event(
        tenant_id=tenant.id,
        api_key_id=auth_ctx.api_key.id,
        path=downstream_path,
        status_code=response.status_code,
        latency_ms=latency_ms,
    )

    if version and version in tenant.deprecated_versions:
        response.headers["Sunset"] = "true"
        response.headers["X-API-Deprecated-Version"] = version

    response.headers["X-RateLimit-Limit"] = str(auth_ctx.api_key.rate_limit_per_minute)
    response.headers["X-RateLimit-Remaining"] = str(
        max(0, auth_ctx.api_key.rate_limit_per_minute - current_count)
    )
    if monthly_limit is not None:
        response.headers["X-Quota-Limit"] = str(monthly_limit)
        response.headers["X-Quota-Remaining"] = str(max(0, monthly_limit - quota_count))
    return response
