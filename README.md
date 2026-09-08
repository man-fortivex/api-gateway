# API Gateway SaaS

A multi-tenant API gateway that gives indie developers auth, rate limiting,
and usage-based billing for their own backend APIs — without them having to
build any of it.

A tenant registers their upstream API URL, gets an API key to hand to their
own users, and every request flows through this gateway first:

```
caller → [this gateway: auth, rate limit, log usage] → tenant's real backend
```

## Why this is sellable

Indie devs building their own SaaS constantly reinvent: API key issuance,
per-key rate limiting, and usage metering for billing. This packages all
three behind one URL prefix (`/gw/{tenant_slug}/...`) so they change one
line in their client SDK and get it for free.

## Architecture

| Component | Tech | Role |
|---|---|---|
| Gateway API | FastAPI + httpx | Auth, rate limit, reverse-proxy every request |
| Rate limiter | Redis (sorted sets) | Atomic sliding-window counter per API key |
| Usage events | Kafka (aiokafka) | Fire-and-forget event per request, decoupled from the request path |
| Billing aggregator | Standalone Kafka consumer | Rolls events into per-minute usage rows for invoicing |
| Metadata store | Postgres (SQLite for local dev) | Tenants, API keys, plans, aggregated usage |

**Key design decisions:**
- **Rate limiting is atomic** via Redis `MULTI/EXEC`, not a Lua script —
  this keeps it portable across managed Redis providers/clusters that
  restrict `EVAL`, and fully unit-testable with `fakeredis`.
- **Billing never blocks serving traffic.** If Kafka is unreachable, the
  gateway logs the usage event and keeps serving requests rather than
  failing the caller's request over a telemetry problem.
- **API keys are hashed with SHA-256**, not bcrypt — they're high-entropy
  random tokens (not user passwords), so we need fast deterministic lookup
  by hash rather than slow salted hashing.
- **The billing aggregator runs as a separate process** (`app/billing/consumer.py`),
  so a slow consumer or DB never adds latency to the request path.

## Monetization model

- **Free**: e.g. 10k requests/mo, 60 req/min cap — lead generation.
- **Starter / Pro / Enterprise**: higher `rate_limit_per_minute` per key,
  metered overage billed from the `usage_logs` table (already aggregated
  per tenant/key/minute — sum by billing period and feed into Stripe usage
  records).
- Charge per proxied request above plan quota, or flat monthly + overage —
  the `UsageLog` schema supports either without changes.

## New: auth, billing, quotas, resilience, metrics

**Admin login required.** `/admin/*` and `/billing/*` now require a bearer
token. Generate your password hash and set it before first run:

```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
# put the output in .env as ADMIN_PASSWORD_HASH, set ADMIN_USERNAME too
```

Then log in and use the token:

```bash
TOKEN=$(curl -s -X POST localhost:8000/admin/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"yourpassword"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")

curl localhost:8000/admin/tenants -H "Authorization: Bearer $TOKEN"
```

The dashboard (`/`) now shows a login screen first and stores the token in
`localStorage`.

**Self-serve signup** — public, no auth, creates a tenant + first API key
in one call. Also served as a page at `/signup.html`.

```bash
curl -X POST localhost:8000/signup -H "Content-Type: application/json" -d '{
  "name": "Acme", "slug": "acme", "email": "ops@acme.com",
  "upstream_base_url": "https://api.acme-backend.com"
}'
```

Rate-limited to 5 signups/hour per IP (`app/routers/signup.py`).

**Monthly quotas** — enforced per plan tier via a Redis counter
(`app/quota.py`), independent of the per-minute rate limiter. Configured in
`PLAN_MONTHLY_QUOTA` in `config.py` (free=10k, starter=100k, pro=1M,
enterprise=unlimited). Exceeding it returns `402 Payment Required` with
`X-Quota-Limit` / `X-Quota-Remaining` response headers.

**Circuit breaker + retry** — `app/circuit_breaker.py` (Redis-backed, so it
works correctly across multiple gateway replicas) opens after
`CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive upstream failures (default
5) and rejects with `503` for `CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default
30) without even calling the upstream. `app/proxy.py` retries connect-level
failures up to `PROXY_MAX_RETRIES` times with exponential backoff before
that counts as a failure.

**Stripe metered billing** — `app/billing/stripe_sync.py` sums the
previous day's `usage_logs` per tenant and reports it to Stripe via
`SubscriptionItem.create_usage_record`. Set a tenant's
`stripe_subscription_item_id` (currently DB-only — add an admin endpoint
for this before using in production), set `STRIPE_ENABLED=true` and
`STRIPE_API_KEY`, then run:

```bash
python -m app.billing.stripe_sync          # once
python -m app.billing.stripe_sync --loop   # daily, for docker-compose
```

**Metrics** — Prometheus format at `/metrics`: `gateway_requests_total`,
`gateway_request_latency_ms`, `gateway_rate_limited_total`,
`gateway_quota_exceeded_total`, `gateway_circuit_open_rejections_total`, all
labeled by `tenant_slug`.

**Horizontal scaling** — the gateway process itself is stateless (all
shared state lives in Redis/Postgres/Kafka), so it can run as N replicas
behind a load balancer. Test with:

```bash
docker compose up --build --scale app=3
```

## Dashboard UI

Open `http://localhost:8000/` in a browser. It's a single-page console
(vanilla HTML/CSS/JS, no build step, no external dependencies) that lets you:

- Create tenants
- Issue and revoke API keys per tenant
- See a live rate-limit gauge per key (green → amber → red as usage
  approaches the per-minute cap)

It's a thin client over the same `/admin/*` and `/billing/*` endpoints
shown in the curl walkthrough below — nothing it does can't also be done
via the API directly.

**Known gap:** `/admin/*` has no login yet, so don't expose this publicly
without adding auth first (see "What's intentionally left as next steps").

## Running locally

```bash
cp .env.example .env
docker compose up --build
```

This starts Postgres, Redis, Kafka, the gateway (`:8000`), and the billing
worker. Or run without Docker:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# in a second terminal:
python -m app.billing.consumer
```

By default `DATABASE_URL` falls back to SQLite and `KAFKA_ENABLED` can be
set to `false` for a dependency-free local run (usage events are just
logged instead of published).

## Usage walkthrough

```bash
# 1. Register a tenant
curl -X POST localhost:8000/admin/tenants -H "Content-Type: application/json" -d '{
  "name": "Acme Inc", "slug": "acme", "email": "ops@acme.com",
  "upstream_base_url": "https://api.acme-backend.com"
}'
# -> {"id": "...", ...}

# 2. Issue an API key for that tenant
curl -X POST localhost:8000/admin/tenants/{tenant_id}/api-keys -H "Content-Type: application/json" -d '{
  "label": "prod", "rate_limit_per_minute": 100
}'
# -> {"plaintext_key": "agw_...", ...}   <- shown ONLY this once

# 3. Callers now hit the gateway instead of Acme's backend directly
curl localhost:8000/gw/acme/v1/whatever -H "X-API-Key: agw_..."
# gateway authenticates, rate-limits, forwards to
# https://api.acme-backend.com/v1/whatever, logs usage, returns the response

# 4. Check aggregated usage for billing
curl localhost:8000/billing/tenants/{tenant_id}/usage
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

12 tests covering: rate-limiter correctness/isolation (fakeredis), tenant
CRUD, full proxy round-trip through a mocked upstream (respx), auth
rejection paths (missing/invalid/revoked/wrong-tenant key), end-to-end
429 rate-limit enforcement, and 502 handling when the upstream is down.

## Round 3: schema validation, key rotation, IP allowlist, caching, CLI, CI, canary, chaos

**1. Request schema validation** — per-tenant `request_schemas`:
`{"POST /users": {<JSON Schema>}}`. Invalid bodies get `400` with a list
of errors, before ever reaching the upstream. `app/schema_validator.py`.

**2. API key TTL + rotation** — `expires_in_seconds` on key creation sets
`expires_at`; expired keys are rejected at auth time (`app/auth.py`).
`POST /admin/api-keys/{id}/rotate` issues a new key and gives the old one
a `KEY_ROTATION_GRACE_SECONDS` grace period instead of an immediate cutover.

**3. IP allowlist** — per-tenant `ip_allowlist` (CIDR blocks). Checked
*before* API key validation even runs, via a public tenant-by-slug lookup —
a disallowed IP gets a flat 403 without learning anything about whether
its key would've been valid. `app/ip_allowlist.py`.

**4. Response caching** — per-tenant `cache_ttl_seconds` caches
successful GET responses in Redis. `X-Cache: HIT`/`MISS` header on every
response. `POST /admin/tenants/{id}/cache/purge` to force a refresh.
`app/response_cache.py`.

**5. `gatewayctl` CLI** — `cli/gatewayctl.py`, installable via
`pip install -e .` (adds a `gatewayctl` command). Wraps the whole admin
API: `gatewayctl login`, `tenant create/list`, `key issue/revoke/rotate`,
`webhook add/list`, `audit-log`, `chaos enable/disable`, `cache-purge`.
Credentials cached at `~/.gatewayctl/credentials.json` (mode 600).

**6. CI** — `.github/workflows/ci.yml`: runs all tests against a real
Redis service container on every push/PR, verifies the CLI installs
cleanly, and builds (not pushes) the Docker image on `main`.

**7. Canary rollout** — per-tenant `canary_rules`:
`[{"version": "v2", "percentage": 10}]` sends ~10% of *unversioned*
requests to `v2`'s upstream instead of the default. An explicit
`/v1/...` or `/v2/...` in the request path always wins over canary
selection. `app/region_router.py::select_canary_version`.

**8. Chaos injection** — `POST /admin/tenants/{id}/chaos` with
`{"mode": "fail"}` makes every request to that tenant fail at the proxy
stage (driving the real circuit breaker/retry logic) for a time-boxed
duration; `{"mode": "latency", "extra_ms": 500}` injects delay instead.
Auto-expires via Redis TTL so a forgotten chaos test can't cause a lasting
outage. `app/chaos.py`.

## Performance: what changed and what it actually measured

Four changes, aimed at the two things that dominate latency under load —
per-request database round trips and per-request Redis round trips —
plus multi-process scaling for real deployments.

**1. Auth caching (`app/auth_cache.py`)** — the biggest single win.
Every proxied request used to do 3 sequential DB queries (tenant-by-slug
for the IP check, API-key-by-hash, tenant-by-id again inside the old auth
resolver). Tenants and keys are effectively immutable after creation
except for two explicit actions — revoke and rotate — so both are now
cached in Redis (tenant: 30s TTL, key: 10s TTL) with **explicit
invalidation on revoke/rotate**, not just TTL expiry. A cache hit does
zero DB queries. This is the one change worth understanding if you only
read one part of this: see `tests/test_auth_cache.py` for the correctness
tests that matter here — specifically that a revoked key is rejected on
the *very next* request, not after the cache naturally expires.

**2. Combined Redis round trips** — the circuit-breaker-open check and
the chaos-injection check were two sequential `GET`s on every request;
now one `MGET`. Small on its own, but it's on every single request.

**3. Connection pool tuning** — httpx's defaults (100 max / 20 keepalive
connections) are conservative for a gateway serving many tenants
concurrently; raised via `PROXY_MAX_CONNECTIONS` /
`PROXY_MAX_KEEPALIVE_CONNECTIONS`, plus HTTP/2 enabled for upstreams that
support it. SQLAlchemy's pool is similarly tuned via `DB_POOL_SIZE` /
`DB_MAX_OVERFLOW` (Postgres only — SQLite doesn't support real pooling).

**4. Multi-process scaling** — the Dockerfile now runs gunicorn with
`UvicornWorker`s (`WEB_CONCURRENCY`, default 4) instead of a single
uvicorn process. Safe because every piece of shared state (rate limits,
quotas, circuit breaker, auth cache) already lives in Redis/Postgres, not
in-process — verified by actually booting 2 workers and confirming both
serve traffic independently before shipping this.

**Measured, before/after, using `scripts/benchmark.py`** (same
methodology as the earlier benchmark section: in-process, mocked
upstream, isolates gateway-only overhead — see that section for the
important caveats about what this does and doesn't represent):

| | Before | After | Change |
|---|---|---|---|
| Throughput | 206 req/s | 336 req/s | **+63%** |
| Latency p50 | 116 ms | 68 ms | **-41%** |
| Latency p95 | 159 ms | 109 ms | -32% |
| Latency p99 | 174 ms | 155 ms | -11% |

**Honest caveat on these numbers**: the benchmark hits the same
tenant/key repeatedly, so after the first request every subsequent one is
an auth-cache hit — representative of a real repeat caller (the common
case), but it means these numbers show the *best case* for the caching
change. A workload hitting many distinct tenants/keys with low repeat
rate would see a smaller improvement from item 1, though items 2-4 help
regardless of cache hit rate. Run `scripts/benchmark.py` yourself against
your own traffic shape rather than trusting these numbers blindly.

## Performance: what's intentionally NOT done, and why

- **Response streaming for large bodies** — the proxy currently buffers
  the full request and response body (needed for transform rules like
  `strip_json_field` and for response caching, both of which need the
  whole body in memory to work). Streaming would help large-payload
  latency-to-first-byte, but only for tenants using neither feature — the
  conditional logic to safely fall back is real complexity for a
  narrower win than the four changes above. Worth doing before this
  handles genuinely large media/file payloads at scale, not before that.
- **Read replicas / sharding** — not remotely necessary at this scale;
  Postgres connection pooling (item 3) is the right amount of DB scaling
  work for a single-region gateway.

## Advanced: multi-region, transforms, versioning, webhooks, tracing, scoping, audit, GraphQL, benchmarks

**1. Multi-region routing + failover** — set `upstream_replicas` on a
tenant instead of (or in addition to) `upstream_base_url`:
```json
"upstream_replicas": [
  {"url": "https://us-east.acme.com", "region": "us-east", "priority": 0},
  {"url": "https://eu-west.acme.com", "region": "eu-west", "priority": 1}
]
```
`app/region_router.py` orders candidates by a Redis-tracked latency EMA
(falling back to `priority`), and `app/proxy.py` fails over to the next
candidate on connect failure. See `LatencyTracker`.

**2. Request/response transforms** — per-tenant `transform_rules` list:
`add_header`, `remove_header`, `rewrite_path_prefix`, `strip_json_field`.
Applied in `app/transforms.py`, wired into `app/proxy.py`.

**3. API versioning** — call `/gw/{slug}/v1/...` vs `/v2/...`; set
`version_upstreams` (`{"v1": "https://old...", "v2": "https://new..."}`)
and `deprecated_versions` on the tenant. Deprecated versions get a
`Sunset: true` response header. Logic in `app/routers/gateway.py::_split_version`.

**4. Webhooks** — tenants register endpoints via
`POST /admin/tenants/{id}/webhooks` (returns a signing secret once).
Events fire on `quota.warning` (≥90% of monthly quota), `circuit.opened`,
and `key.revoked`. Delivered as `POST` with an `X-Gateway-Signature`
(HMAC-SHA256) header — see `app/webhooks.py`. Fire-and-forget via FastAPI
`BackgroundTasks`, so a slow/down receiver never adds latency to the
gateway request that triggered it.

**5. Distributed tracing** — `OTEL_ENABLED=true` turns on FastAPI +
httpx auto-instrumentation (`app/tracing.py`). Spans print to stdout by
default, or ship to `OTEL_EXPORTER_OTLP_ENDPOINT` if set. The httpx
instrumentation automatically propagates the `traceparent` header to the
tenant's upstream, so their own OTel-instrumented backend can be
correlated with the gateway's span for the same request.

**6. API key scoping** — set `allowed_path_prefix` when issuing a key
(e.g. `"reports"`) to restrict it to that path prefix only; unset means
full access. Enforced in `app/routers/gateway.py` before any upstream call.

**7. Audit log** — every tenant/key/webhook create/revoke writes an
immutable row (actor, action, target, detail, timestamp). View via
`GET /admin/audit-log`. See `app/audit.py`, `app/models.py::AuditLogEntry`.

**8. GraphQL vs gRPC passthrough** — GraphQL: set a tenant's
`graphql_path` (e.g. `"graphql"`) to switch that path from flat
per-request rate limiting to a points-based query-complexity budget
(`app/graphql_limiter.py`, brace-depth heuristic, not a full AST parser).
**gRPC is intentionally not implemented** — gRPC is HTTP/2 + protobuf
framing with bidirectional streaming; correctly proxying it needs a
gRPC-aware server (`grpc.aio`) terminating and re-issuing RPCs, not an
httpx-based REST proxy forwarding request/response pairs. Bolting on a
"gRPC passthrough" here would either silently mangle streaming calls or
just tunnel raw bytes without the framing awareness that makes it
useful — shipping that would be worse than not having it. A real
implementation is a separate proxy component, not an extension of this one.

**9. Stripe auto-provisioning** — set `STRIPE_PLAN_PRICE_IDS` (plan tier
→ Stripe Price ID, for prices you've already created in Stripe), then
`POST /admin/tenants/{id}/stripe/provision` creates the Stripe customer +
subscription and stores the IDs on the tenant, ready for `stripe_sync.py`
to report usage against.

**10. Load testing** — `locustfile.py` for a real network-level load
test against a running instance. `scripts/benchmark.py` measures the
gateway's own added overhead in-process (mocked upstream, no network) —
run it yourself with `PYTHONPATH=. python scripts/benchmark.py`. Reference
numbers from this dev sandbox (SQLite + local Redis, 1000 requests,
concurrency 25) — expect different numbers with Postgres/Redis over a real
network, this isolates gateway-only overhead:

```
Throughput:      195 req/s
Latency p50:     121 ms
Latency p95:     168 ms
Latency p99:     222 ms
```

## What's intentionally left as next steps

- Alembic migrations (currently `create_all` — fine for MVP, not for
  schema changes in production).
- Multi-admin support (currently a single username/password from `.env`,
  not a user table).
- Webhook delivery retry / dead-letter queue (currently fire-and-forget,
  one attempt).
- Full GraphQL AST-based complexity scoring (currently a brace-depth
  heuristic — see the gRPC note above for why gRPC is out of scope entirely).
- A UI for round 2 and round 3 features (transforms, versioning, webhooks,
  audit log, scoping, schema validation, IP allowlist, caching, canary,
  chaos) — the dashboard still only covers tenant/key CRUD; everything
  else is API/CLI-only for now.
- Response cache doesn't vary by request headers (e.g. `Accept-Language`)
  — fine for simple APIs, would need a `Vary`-aware cache key for others.
- CI builds the Docker image but doesn't push/deploy anywhere — wire a
  registry login + deploy step once you have a real target (Fly.io,
  Railway, a VPS via SSH, etc.).
## Setup notes
