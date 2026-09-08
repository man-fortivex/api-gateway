import bcrypt
import fakeredis.aioredis
import httpx
import pytest
from asgi_lifespan import LifespanManager

from app import redis_client
from app.auth import settings as auth_settings
from app.database import engine, init_models
from app.kafka_producer import usage_producer
from app.main import app

TEST_ADMIN_USERNAME = "admin"
TEST_ADMIN_PASSWORD = "test-password-123"


@pytest.fixture(autouse=True)
def _patch_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(redis_client, "_redis", fake)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    yield


@pytest.fixture(autouse=True)
def _set_admin_credentials(monkeypatch):
    password_hash = bcrypt.hashpw(TEST_ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
    monkeypatch.setattr(auth_settings, "ADMIN_USERNAME", TEST_ADMIN_USERNAME)
    monkeypatch.setattr(auth_settings, "ADMIN_PASSWORD_HASH", password_hash)
    yield


@pytest.fixture(autouse=True)
async def _fresh_db():
    await init_models()
    yield
    async with engine.begin() as conn:
        from app.database import Base

        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
async def _disable_kafka(monkeypatch):
    # Kafka broker isn't available in the test environment; verify the
    # gateway degrades gracefully (usage events get logged, not raised).
    async def _noop_publish(*args, **kwargs):
        return None

    monkeypatch.setattr(usage_producer, "publish_usage_event", _noop_publish)
    yield


@pytest.fixture
async def client():
    """An httpx client already authenticated as the admin user."""
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            login_resp = await ac.post(
                "/admin/auth/login",
                json={"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD},
            )
            assert login_resp.status_code == 200, login_resp.text
            token = login_resp.json()["access_token"]
            ac.headers["Authorization"] = f"Bearer {token}"
            yield ac


@pytest.fixture
async def anon_client():
    """A client with no admin auth header, for testing auth rejection paths
    and public endpoints (signup, metrics, gateway proxy)."""
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
