import asyncio
import time

import httpx
from fastapi import HTTPException, Request, Response, status

from app.config import get_settings
from app.region_router import UpstreamCandidate
from app.transforms import apply_path_rewrite_rules, apply_request_header_rules, apply_response_body_rules

settings = get_settings()

# A single shared client (connection pooling) reused across requests.
_client: httpx.AsyncClient | None = None

# Headers that must not be blindly forwarded upstream or back to the caller,
# since they are connection-scoped or would leak internal gateway details.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# Headers that belong to the gateway's own auth layer and must never leak
# to the tenant's upstream service — it has no business seeing the
# caller's gateway API key.
_GATEWAY_ONLY_HEADERS = {
    "x-api-key",
}


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Defaults (100 max connections, 20 keepalive) are conservative
        # for a gateway fronting many concurrent tenants — raise both, and
        # keep connections alive longer so repeat calls to the same
        # upstream reuse a warm connection instead of re-handshaking.
        # HTTP/2 lets multiple in-flight requests share one connection to
        # upstreams that support it (falls back to HTTP/1.1 otherwise).
        _client = httpx.AsyncClient(
            timeout=settings.PROXY_TIMEOUT_SECONDS,
            http2=True,
            limits=httpx.Limits(
                max_connections=settings.PROXY_MAX_CONNECTIONS,
                max_keepalive_connections=settings.PROXY_MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=settings.PROXY_KEEPALIVE_EXPIRY_SECONDS,
            ),
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _filter_headers(headers: httpx.Headers, extra_strip: set[str] = frozenset()) -> dict[str, str]:
    strip = _HOP_BY_HOP_HEADERS | extra_strip
    return {k: v for k, v in headers.items() if k.lower() not in strip}


async def _try_candidate(
    client: httpx.AsyncClient,
    method: str,
    candidate: UpstreamCandidate,
    downstream_path: str,
    params,
    headers: dict[str, str],
    body: bytes,
) -> httpx.Response | None:
    """Attempts one upstream candidate with retry-with-backoff on
    connect-level failures. Returns None (never raises) if every retry on
    THIS candidate failed, so the caller can fail over to the next one."""
    target_url = f"{candidate.url.rstrip('/')}/{downstream_path.lstrip('/')}"

    for attempt in range(settings.PROXY_MAX_RETRIES + 1):
        try:
            return await client.request(method=method, url=target_url, params=params, headers=headers, content=body)
        except (httpx.ConnectTimeout, httpx.ConnectError):
            if attempt < settings.PROXY_MAX_RETRIES:
                await asyncio.sleep(settings.PROXY_RETRY_BACKOFF_BASE_SECONDS * (2**attempt))
    return None


async def forward_request(
    request: Request,
    candidates: list[UpstreamCandidate],
    downstream_path: str,
    transform_rules: list[dict] | None = None,
) -> tuple[Response, int, str]:
    """Forwards an incoming request to the tenant's upstream, trying each
    candidate in order (multi-region failover) and applying any configured
    transform rules.

    Returns (fastapi_response, latency_ms, upstream_url_used) — the third
    value lets the caller record per-replica latency for future routing
    decisions and know which region actually served the request.
    """
    transform_rules = transform_rules or []
    body = await request.body()
    if len(body) > settings.MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Request body too large")

    rewritten_path = apply_path_rewrite_rules(downstream_path.lstrip("/"), transform_rules)
    forward_headers = _filter_headers(request.headers, extra_strip=_GATEWAY_ONLY_HEADERS)
    forward_headers = apply_request_header_rules(forward_headers, transform_rules)

    client = get_http_client()
    start = time.perf_counter()

    upstream_response = None
    used_candidate = None
    for candidate in candidates:
        upstream_response = await _try_candidate(
            client, request.method, candidate, rewritten_path, request.query_params, forward_headers, body
        )
        if upstream_response is not None:
            used_candidate = candidate
            break
        # This candidate exhausted its retries; fail over to the next one
        # (different region/replica) rather than giving up immediately.

    if upstream_response is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="All upstream candidates unreachable")

    latency_ms = int((time.perf_counter() - start) * 1000)

    response_body = apply_response_body_rules(
        upstream_response.content, upstream_response.headers.get("content-type"), transform_rules
    )

    response = Response(
        content=response_body,
        status_code=upstream_response.status_code,
        headers=_filter_headers(upstream_response.headers),
        media_type=upstream_response.headers.get("content-type"),
    )
    return response, latency_ms, used_candidate.url
