import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import bcrypt
import jwt
from fastapi import Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import APIKey, Tenant

settings = get_settings()


def generate_api_key() -> tuple[str, str, str]:
    """Returns (plaintext_key, key_hash, key_prefix)."""
    raw = secrets.token_urlsafe(32)
    plaintext_key = f"{settings.API_KEY_PREFIX}{raw}"
    key_hash = hash_api_key(plaintext_key)
    key_prefix = plaintext_key[: len(settings.API_KEY_PREFIX) + 6]
    return plaintext_key, key_hash, key_prefix


def hash_api_key(plaintext_key: str) -> str:
    # SHA-256 is appropriate here (not bcrypt): API keys are high-entropy
    # random tokens, not low-entropy passwords, so we don't need salted
    # slow-hashing — we need fast, deterministic lookup by hash.
    return hashlib.sha256(plaintext_key.encode("utf-8")).hexdigest()


@dataclass
class AuthContext:
    tenant: Tenant
    api_key: APIKey


async def resolve_api_key(db: AsyncSession, plaintext_key: str) -> AuthContext:
    if not plaintext_key or not plaintext_key.startswith(settings.API_KEY_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key format")

    key_hash = hash_api_key(plaintext_key)
    result = await db.execute(select(APIKey).where(APIKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()

    if api_key is None or not api_key.is_active or api_key.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")

    if api_key.expires_at is not None:
        if datetime.now(timezone.utc) > api_key.expires_at.replace(tzinfo=timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key has expired")

    tenant_result = await db.execute(select(Tenant).where(Tenant.id == api_key.tenant_id))
    tenant = tenant_result.scalar_one_or_none()

    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant account is inactive")

    return AuthContext(tenant=tenant, api_key=api_key)


async def resolve_tenant_by_slug_cached(db: AsyncSession, tenant_cache, slug: str):
    """Returns a CachedTenant (from cache) or Tenant (on cache miss, DB
    fallback, populating the cache for next time) for the given slug, or
    None if no such active tenant exists. This is the lookup used for the
    pre-auth IP allowlist check, and is reused as the tenant object for
    the rest of the request on a cache hit — avoiding a second Tenant
    fetch that resolve_api_key would otherwise do."""
    cached = await tenant_cache.get(slug)
    if cached is not None:
        return cached

    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = result.scalar_one_or_none()
    if tenant is not None:
        await tenant_cache.set(tenant)
    return tenant


async def resolve_api_key_fast(db: AsyncSession, key_cache, plaintext_key: str, known_tenant=None) -> AuthContext:
    """Cache-first API key resolution. On a cache hit, this does ZERO
    database queries. On a miss, falls back to a single DB query (joined
    load) and populates the cache for subsequent requests.

    `known_tenant` lets the caller pass in a tenant it already resolved
    (e.g. via resolve_tenant_by_slug_cached for the IP check), so this
    function never needs to fetch Tenant a second time — it only
    validates that the key actually belongs to that tenant.
    """
    if not plaintext_key or not plaintext_key.startswith(settings.API_KEY_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key format")

    key_hash = hash_api_key(plaintext_key)
    cached_key = await key_cache.get(key_hash)

    if cached_key is not None:
        api_key = cached_key
    else:
        result = await db.execute(select(APIKey).where(APIKey.key_hash == key_hash))
        db_key = result.scalar_one_or_none()
        if db_key is None or not db_key.is_active or db_key.revoked_at is not None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")
        await key_cache.set(db_key)
        api_key = db_key

    if not api_key.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")

    if api_key.expires_at is not None:
        expires_at = api_key.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key has expired")

    if known_tenant is not None:
        if api_key.tenant_id != known_tenant.id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key")
        tenant = known_tenant
    else:
        tenant_result = await db.execute(select(Tenant).where(Tenant.id == api_key.tenant_id))
        tenant = tenant_result.scalar_one_or_none()
        if tenant is None or not tenant.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant account is inactive")

    return AuthContext(tenant=tenant, api_key=api_key)


def extract_api_key_header(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")
    return x_api_key


# --------------------------------------------------------------------------
# Admin authentication (JWT bearer token, single operator account for MVP)
# --------------------------------------------------------------------------

_JWT_ALGORITHM = "HS256"


def verify_admin_credentials(username: str, password: str) -> bool:
    if not settings.ADMIN_PASSWORD_HASH:
        # No password configured — refuse rather than silently allowing in.
        return False
    if username != settings.ADMIN_USERNAME:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), settings.ADMIN_PASSWORD_HASH.encode("utf-8"))


def create_admin_token(username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + settings.ADMIN_JWT_EXPIRE_MINUTES * 60,
        "scope": "admin",
    }
    return jwt.encode(payload, settings.ADMIN_JWT_SECRET, algorithm=_JWT_ALGORITHM)


def _decode_admin_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.ADMIN_JWT_SECRET, algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token") from exc

    if payload.get("scope") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token does not grant admin access")
    return payload


async def require_admin(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: validates the `Authorization: Bearer <token>` header.

    Returns the admin username on success; raises 401/403 otherwise.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing admin bearer token")

    token = authorization.removeprefix("Bearer ").strip()
    payload = _decode_admin_token(token)
    return payload["sub"]
