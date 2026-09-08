"""Measures the gateway's OWN added latency: auth lookup, rate-limit check,
quota check, circuit breaker check, and response assembly — with the
upstream call itself mocked to return instantly.

This is deliberately NOT an end-to-end network benchmark (see locustfile.py
for that, against a real deployed instance). Running in-process via ASGI
transport with a mocked upstream isolates exactly one number: how much
latency the gateway adds on top of whatever the tenant's real backend
takes. That's the number that matters when evaluating "is this proxy layer
cheap enough to put in front of my API."

Run:
    python scripts/benchmark.py [--requests 2000] [--concurrency 50]
"""

import argparse
import asyncio
import statistics
import time

import httpx
import respx
from asgi_lifespan import LifespanManager


async def _setup_tenant(client: httpx.AsyncClient) -> str:
    login = await client.post(
        "/admin/auth/login", json={"username": "bench-admin", "password": "bench-password-123"}
    )
    token = login.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"

    tenant = await client.post(
        "/admin/tenants",
        json={
            "name": "Bench Co",
            "slug": "bench",
            "email": "bench@example.com",
            "upstream_base_url": "https://upstream.bench.internal",
        },
    )
    tenant_id = tenant.json()["id"]

    key_resp = await client.post(
        f"/admin/tenants/{tenant_id}/api-keys",
        json={"label": "bench", "rate_limit_per_minute": 100_000},
    )
    del client.headers["Authorization"]  # gateway calls use X-API-Key, not admin bearer
    return key_resp.json()["plaintext_key"]


async def run_benchmark(num_requests: int, concurrency: int) -> None:
    import bcrypt

    from app.auth import settings as auth_settings
    from app.main import app

    auth_settings.ADMIN_USERNAME = "bench-admin"
    auth_settings.ADMIN_PASSWORD_HASH = bcrypt.hashpw(b"bench-password-123", bcrypt.gensalt()).decode()

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
            api_key = await _setup_tenant(client)

            latencies_ms: list[float] = []
            semaphore = asyncio.Semaphore(concurrency)

            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://upstream.bench.internal/ping").mock(
                    return_value=httpx.Response(200, json={"ok": True})
                )

                async def _one_request() -> None:
                    async with semaphore:
                        start = time.perf_counter()
                        resp = await client.get("/gw/bench/ping", headers={"X-API-Key": api_key})
                        latencies_ms.append((time.perf_counter() - start) * 1000)
                        assert resp.status_code == 200, resp.text

                overall_start = time.perf_counter()
                await asyncio.gather(*(_one_request() for _ in range(num_requests)))
                total_seconds = time.perf_counter() - overall_start

    latencies_ms.sort()
    n = len(latencies_ms)
    p50 = latencies_ms[int(n * 0.50)]
    p95 = latencies_ms[int(n * 0.95)]
    p99 = latencies_ms[min(n - 1, int(n * 0.99))]
    mean = statistics.mean(latencies_ms)

    print(f"\nRequests:        {n}")
    print(f"Concurrency:     {concurrency}")
    print(f"Total time:      {total_seconds:.2f}s")
    print(f"Throughput:      {n / total_seconds:.0f} req/s")
    print(f"Latency mean:    {mean:.2f} ms")
    print(f"Latency p50:     {p50:.2f} ms")
    print(f"Latency p95:     {p95:.2f} ms")
    print(f"Latency p99:     {p99:.2f} ms")
    print(
        "\nNote: upstream call is mocked to return instantly, and this runs "
        "in-process (no real network/TLS). These numbers isolate the "
        "gateway's own overhead — auth lookup, rate limit, quota, circuit "
        "breaker, transform rules — not real-world total latency, which "
        "will also include actual network time to your upstream and back."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(run_benchmark(args.requests, args.concurrency))
