from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter(
    "gateway_requests_total",
    "Total proxied requests",
    ["tenant_slug", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "gateway_request_latency_ms",
    "Latency of proxied requests in milliseconds",
    ["tenant_slug"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)

RATE_LIMITED_COUNT = Counter(
    "gateway_rate_limited_total",
    "Requests rejected for exceeding the per-minute rate limit",
    ["tenant_slug"],
)

QUOTA_EXCEEDED_COUNT = Counter(
    "gateway_quota_exceeded_total",
    "Requests rejected for exceeding the monthly plan quota",
    ["tenant_slug"],
)

CIRCUIT_OPEN_COUNT = Counter(
    "gateway_circuit_open_rejections_total",
    "Requests rejected because the tenant's upstream circuit breaker is open",
    ["tenant_slug"],
)


def record_request(tenant_slug: str, status_code: int, latency_ms: int) -> None:
    REQUEST_COUNT.labels(tenant_slug=tenant_slug, status_code=str(status_code)).inc()
    REQUEST_LATENCY.labels(tenant_slug=tenant_slug).observe(latency_ms)
