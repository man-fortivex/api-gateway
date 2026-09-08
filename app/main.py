import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
from app.database import init_models
from app.kafka_producer import usage_producer
from app.proxy import close_http_client
from app.redis_client import close_redis, get_redis
from app.routers import admin, admin_auth, billing, gateway, signup
from app.tracing import setup_tracing

logging.basicConfig(level=logging.INFO)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    await usage_producer.start()
    try:
        await get_redis().ping()
        logging.info("Redis connection OK")
    except Exception:
        logging.warning("Redis not reachable at startup — rate limiting will fail until it is.")
    yield
    await usage_producer.stop()
    await close_http_client()
    await close_redis()


app = FastAPI(
    title=settings.APP_NAME,
    description="Multi-tenant API gateway: auth, rate limiting, and usage billing as a service.",
    version="1.0.0",
    lifespan=lifespan,
)

setup_tracing(app)

app.include_router(admin_auth.router)
app.include_router(admin.router)
app.include_router(billing.router)
app.include_router(signup.router)
app.include_router(gateway.router)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(str(_STATIC_DIR / "index.html"))


@app.get("/signup.html", include_in_schema=False)
async def signup_page() -> FileResponse:
    return FileResponse(str(_STATIC_DIR / "signup.html"))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
