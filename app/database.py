from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# Auth caching (app/auth_cache.py) already eliminates most DB round trips
# on the hot path, but the connection pool still needs to be sized for
# real concurrent load — the aiosqlite driver used in local dev doesn't
# support pool_size/max_overflow (SQLite is single-writer regardless), so
# these only apply when DATABASE_URL points at a real server (Postgres).
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

_engine_kwargs = {"echo": False, "future": True}
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,  # detects a dropped connection before using it, not after
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
    )

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_models() -> None:
    """Create tablee. In production, replace with Alembic migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
