"""Lightweight GraphQL query-complexity limiting.

A plain per-request rate limit is the wrong tool for GraphQL: a single
request can ask for a shallow list of IDs, or a deeply nested query that
costs the upstream 100x more work. This estimates a "complexity score"
from the query's field/selection-set depth and count, and enforces a
per-minute points budget instead of a request count.

This is a heuristic (brace-depth counting), not a real GraphQL parser —
good enough to stop obviously-expensive queries without adding a full
graphql-core dependency. A production version should parse the AST
properly and weight mutations/introspection differently.
"""

import json

from app.rate_limiter import RateLimiter


def estimate_complexity(request_body: bytes) -> int:
    """Returns an integer complexity score for a GraphQL POST body.
    Falls back to a conservative default if the body isn't parseable
    GraphQL JSON, so malformed input doesn't get a free pass."""
    try:
        payload = json.loads(request_body)
        query = payload.get("query", "") if isinstance(payload, dict) else ""
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 10

    if not query:
        return 10

    depth = 0
    max_depth = 0
    for ch in query:
        if ch == "{":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == "}":
            depth = max(0, depth - 1)

    field_count = query.count("{")

    return max(1, max_depth * 5 + field_count)


class GraphQLComplexityLimiter:
    """Points-based limiter: each API key gets a per-minute complexity
    budget instead of a flat request count, reusing the same sliding
    window primitive as the regular rate limiter."""

    def __init__(self, limiter: RateLimiter, points_per_minute: int):
        self._limiter = limiter
        self._points_per_minute = points_per_minute

    async def check(self, api_key_id: str, request_body: bytes) -> tuple[bool, int]:
        cost = estimate_complexity(request_body)
        allowed = True
        last_count = 0
        # Spend `cost` points against the shared sliding-window budget by
        # checking `cost` times in one call (capped to avoid a pathological
        # loop on a maliciously huge query). Each check() adds one entry
        # to the same Redis sorted set, so this really does consume `cost`
        # units of the per-minute budget, not just check it once.
        for _ in range(min(cost, 200)):
            allowed, last_count = await self._limiter.check(
                identity_key=f"graphql:{api_key_id}",
                limit_per_window=self._points_per_minute,
            )
            if not allowed:
                break
        return allowed, last_count
