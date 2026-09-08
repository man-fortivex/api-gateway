from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.auth import generate_api_key
from app.database import get_db
from app.models import APIKey, PlanTier, Tenant
from app.rate_limiter import RateLimiter
from app.redis_client import get_redis

router = APIRouter(prefix="/signup", tags=["signup"])

# Signup is public and unauthenticated, so it's the one endpoint most
# exposed to abuse (bot-created tenants). Rate limit by caller IP rather
# than by API key, since there's no key yet at this point.
_SIGNUP_LIMIT_PER_HOUR = 5


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    email: EmailStr
    upstream_base_url: str


class SignupResponse(BaseModel):
    tenant_id: str
    slug: str
    plaintext_key: str
    rate_limit_per_minute: int
    monthly_quota: int | None


@router.post("", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, request: Request, db: AsyncSession = Depends(get_db)) -> SignupResponse:
    client_ip = request.client.host if request.client else "unknown"
    limiter = RateLimiter(get_redis(), window_seconds=3600)
    allowed, _ = await limiter.check(f"signup:{client_ip}", limit_per_window=_SIGNUP_LIMIT_PER_HOUR)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many signups from this address — try again later",
        )

    existing = await db.execute(select(Tenant).where(Tenant.slug == payload.slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That slug is already taken")

    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        email=payload.email,
        upstream_base_url=payload.upstream_base_url,
        plan=PlanTier.FREE,
    )
    db.add(tenant)
    await db.flush()

    plaintext_key, key_hash, key_prefix = generate_api_key()
    api_key = APIKey(
        tenant_id=tenant.id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        label="default",
        rate_limit_per_minute=60,
    )
    db.add(api_key)
    await db.commit()

    from app.config import get_settings

    settings = get_settings()
    return SignupResponse(
        tenant_id=tenant.id,
        slug=tenant.slug,
        plaintext_key=plaintext_key,
        rate_limit_per_minute=api_key.rate_limit_per_minute,
        monthly_quota=settings.PLAN_MONTHLY_QUOTA.get(PlanTier.FREE.value),
    )
