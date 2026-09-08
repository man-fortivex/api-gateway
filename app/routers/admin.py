import asyncio
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import generate_api_key, require_admin
from app.auth_cache import KeyCache
from app.chaos import ChaosInjector
from app.config import get_settings
from app.database import get_db
from app.models import APIKey, AuditLogEntry, Tenant, WebhookEndpoint
from app.redis_client import get_redis
from app.response_cache import ResponseCache
from app.schemas import (
    APIKeyCreate,
    APIKeyCreatedOut,
    APIKeyOut,
    AuditLogOut,
    CachePurgeResponse,
    ChaosRequest,
    KeyRotateResponse,
    StripeProvisionResponse,
    TenantCreate,
    TenantOut,
    WebhookEndpointCreate,
    WebhookEndpointCreatedOut,
    WebhookEndpointOut,
)
from app.webhooks import dispatch_event_standalone

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
settings = get_settings()


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreate, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> Tenant:
    existing = await db.execute(select(Tenant).where(Tenant.slug == payload.slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant slug already in use")

    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        email=payload.email,
        upstream_base_url=payload.upstream_base_url,
        plan=payload.plan,
        upstream_replicas=[r.model_dump() for r in payload.upstream_replicas],
        version_upstreams=payload.version_upstreams,
        deprecated_versions=payload.deprecated_versions,
        transform_rules=[r.model_dump() for r in payload.transform_rules],
        graphql_path=payload.graphql_path,
        ip_allowlist=payload.ip_allowlist,
        cache_ttl_seconds=payload.cache_ttl_seconds,
        request_schemas=payload.request_schemas,
        canary_rules=[r.model_dump() for r in payload.canary_rules],
    )
    db.add(tenant)
    await db.flush()
    await record_audit(db, actor, "tenant.create", "tenant", tenant.id, {"slug": tenant.slug})
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(db: AsyncSession = Depends(get_db)) -> list[Tenant]:
    result = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    return list(result.scalars().all())


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: str, db: AsyncSession = Depends(get_db)) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


@router.post(
    "/tenants/{tenant_id}/api-keys",
    response_model=APIKeyCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    tenant_id: str,
    payload: APIKeyCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_admin),
) -> APIKeyCreatedOut:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    plaintext_key, key_hash, key_prefix = generate_api_key()
    expires_at = None
    if payload.expires_in_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=payload.expires_in_seconds)

    api_key = APIKey(
        tenant_id=tenant.id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        label=payload.label,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        allowed_path_prefix=payload.allowed_path_prefix,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.flush()
    await record_audit(
        db, actor, "api_key.create", "api_key", api_key.id, {"tenant_id": tenant.id, "label": api_key.label}
    )
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreatedOut(
        id=api_key.id,
        key_prefix=api_key.key_prefix,
        label=api_key.label,
        rate_limit_per_minute=api_key.rate_limit_per_minute,
        allowed_path_prefix=api_key.allowed_path_prefix,
        expires_at=api_key.expires_at,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
        plaintext_key=plaintext_key,
    )


@router.get("/tenants/{tenant_id}/api-keys", response_model=list[APIKeyOut])
async def list_api_keys(tenant_id: str, db: AsyncSession = Depends(get_db)) -> list[APIKey]:
    result = await db.execute(select(APIKey).where(APIKey.tenant_id == tenant_id))
    return list(result.scalars().all())


@router.delete("/api-keys/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    api_key_id: str, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> None:
    api_key = await db.get(APIKey, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    await record_audit(db, actor, "api_key.revoke", "api_key", api_key.id, {"tenant_id": api_key.tenant_id})
    await db.commit()

    await KeyCache(get_redis()).invalidate(api_key.key_hash)

    # Best-effort notification; must not block or fail the revoke itself.
    await dispatch_event_standalone(
        api_key.tenant_id, "key.revoked", {"api_key_id": api_key.id, "label": api_key.label}
    )


@router.post("/api-keys/{api_key_id}/rotate", response_model=KeyRotateResponse)
async def rotate_api_key(
    api_key_id: str, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> KeyRotateResponse:
    """Issues a new key with the same settings as the old one, and sets
    the OLD key to expire after KEY_ROTATION_GRACE_SECONDS rather than
    revoking it immediately — callers using the old key keep working
    during the grace period while they switch over to the new one."""
    old_key = await db.get(APIKey, api_key_id)
    if old_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if not old_key.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot rotate a revoked key")

    plaintext_key, key_hash, key_prefix = generate_api_key()
    new_key = APIKey(
        tenant_id=old_key.tenant_id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        label=f"{old_key.label} (rotated)",
        rate_limit_per_minute=old_key.rate_limit_per_minute,
        allowed_path_prefix=old_key.allowed_path_prefix,
    )
    db.add(new_key)

    grace_expiry = datetime.now(timezone.utc) + timedelta(seconds=settings.KEY_ROTATION_GRACE_SECONDS)
    # Only shorten the old key's lifetime — never extend it, in case it
    # already had a sooner expiry set for some other reason.
    if old_key.expires_at is None or old_key.expires_at.replace(tzinfo=timezone.utc) > grace_expiry:
        old_key.expires_at = grace_expiry

    await db.flush()
    await record_audit(
        db, actor, "api_key.rotate", "api_key", new_key.id,
        {"tenant_id": old_key.tenant_id, "old_key_id": old_key.id, "grace_expiry": grace_expiry.isoformat()},
    )
    await db.commit()
    await db.refresh(new_key)
    await db.refresh(old_key)

    await KeyCache(get_redis()).invalidate(old_key.key_hash)

    return KeyRotateResponse(
        new_key=APIKeyCreatedOut(
            id=new_key.id,
            key_prefix=new_key.key_prefix,
            label=new_key.label,
            rate_limit_per_minute=new_key.rate_limit_per_minute,
            allowed_path_prefix=new_key.allowed_path_prefix,
            expires_at=new_key.expires_at,
            is_active=new_key.is_active,
            created_at=new_key.created_at,
            plaintext_key=plaintext_key,
        ),
        old_key_id=old_key.id,
        old_key_expires_at=old_key.expires_at,
    )


# --------------------------------------------------------------------------
# Webhook endpoints
# --------------------------------------------------------------------------


@router.post(
    "/tenants/{tenant_id}/webhooks",
    response_model=WebhookEndpointCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook(
    tenant_id: str,
    payload: WebhookEndpointCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_admin),
) -> WebhookEndpointCreatedOut:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    secret = secrets.token_urlsafe(32)
    endpoint = WebhookEndpoint(
        tenant_id=tenant.id,
        url=payload.url,
        secret=secret,
        event_types=payload.event_types,
    )
    db.add(endpoint)
    await db.flush()
    await record_audit(db, actor, "webhook.create", "webhook", endpoint.id, {"tenant_id": tenant.id})
    await db.commit()
    await db.refresh(endpoint)

    return WebhookEndpointCreatedOut(
        id=endpoint.id,
        tenant_id=endpoint.tenant_id,
        url=endpoint.url,
        event_types=endpoint.event_types,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
        secret=secret,
    )


@router.get("/tenants/{tenant_id}/webhooks", response_model=list[WebhookEndpointOut])
async def list_webhooks(tenant_id: str, db: AsyncSession = Depends(get_db)) -> list[WebhookEndpoint]:
    result = await db.execute(select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == tenant_id))
    return list(result.scalars().all())


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: str, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> None:
    endpoint = await db.get(WebhookEndpoint, webhook_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    endpoint.is_active = False
    await record_audit(db, actor, "webhook.delete", "webhook", endpoint.id, {"tenant_id": endpoint.tenant_id})
    await db.commit()


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------


@router.get("/audit-log", response_model=list[AuditLogOut])
async def list_audit_log(db: AsyncSession = Depends(get_db), limit: int = 100) -> list[AuditLogEntry]:
    result = await db.execute(select(AuditLogEntry).order_by(AuditLogEntry.created_at.desc()).limit(limit))
    return list(result.scalars().all())


# --------------------------------------------------------------------------
# Stripe provisioning
# --------------------------------------------------------------------------


@router.post("/tenants/{tenant_id}/stripe/provision", response_model=StripeProvisionResponse)
async def provision_stripe(
    tenant_id: str, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> StripeProvisionResponse:
    """Creates a Stripe customer + metered subscription item for this
    tenant based on its current plan, and stores the resulting IDs.
    Requires STRIPE_ENABLED and STRIPE_API_KEY to be configured. The Stripe
    Price ID for each plan must already exist in your Stripe account —
    this does not create products/prices, only the customer + subscription
    linking a tenant to a price you've already set up."""
    if not settings.STRIPE_ENABLED or not settings.STRIPE_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe billing is not enabled (set STRIPE_ENABLED and STRIPE_API_KEY)",
        )

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    price_id = settings.STRIPE_PLAN_PRICE_IDS.get(tenant.plan.value)
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No Stripe price configured for plan '{tenant.plan.value}' (STRIPE_PLAN_PRICE_IDS)",
        )

    import stripe

    stripe.api_key = settings.STRIPE_API_KEY

    customer = await asyncio.to_thread(stripe.Customer.create, email=tenant.email, name=tenant.name)
    subscription = await asyncio.to_thread(
        stripe.Subscription.create,
        customer=customer.id,
        items=[{"price": price_id}],
    )
    item_id = subscription["items"]["data"][0]["id"]

    tenant.stripe_customer_id = customer.id
    tenant.stripe_subscription_item_id = item_id
    await record_audit(
        db, actor, "tenant.stripe_provision", "tenant", tenant.id,
        {"stripe_customer_id": customer.id, "stripe_subscription_item_id": item_id},
    )
    await db.commit()

    return StripeProvisionResponse(stripe_customer_id=customer.id, stripe_subscription_item_id=item_id)


# --------------------------------------------------------------------------
# Chaos / fault injection (demonstrates retry + circuit breaker behavior)
# --------------------------------------------------------------------------


@router.post("/tenants/{tenant_id}/chaos", status_code=status.HTTP_204_NO_CONTENT)
async def enable_chaos(
    tenant_id: str, payload: ChaosRequest, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> None:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    injector = ChaosInjector(get_redis())
    await injector.enable(tenant_id, payload.mode, payload.duration_seconds, payload.extra_ms)
    await record_audit(
        db, actor, "chaos.enable", "tenant", tenant_id,
        {"mode": payload.mode, "duration_seconds": payload.duration_seconds},
    )
    await db.commit()


@router.delete("/tenants/{tenant_id}/chaos", status_code=status.HTTP_204_NO_CONTENT)
async def disable_chaos(
    tenant_id: str, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> None:
    injector = ChaosInjector(get_redis())
    await injector.disable(tenant_id)
    await record_audit(db, actor, "chaos.disable", "tenant", tenant_id, {})
    await db.commit()


# --------------------------------------------------------------------------
# Response cache management
# --------------------------------------------------------------------------


@router.post("/tenants/{tenant_id}/cache/purge", response_model=CachePurgeResponse)
async def purge_cache(
    tenant_id: str, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> CachePurgeResponse:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    cache = ResponseCache(get_redis(), settings.RESPONSE_CACHE_MAX_BODY_BYTES)
    purged = await cache.purge_tenant(tenant_id)
    await record_audit(db, actor, "cache.purge", "tenant", tenant_id, {"purged_count": purged})
    await db.commit()
    return CachePurgeResponse(purged_count=purged)
