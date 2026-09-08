from dataclasses import dataclass
import random

from redis.asyncio import Redis


@dataclass
class UpstreamCandidate:
    url: str
    region: str | None = None
    priority: int = 0


def resolve_candidates(
    upstream_base_url: str,
    upstream_replicas: list[dict],
    version_upstreams: dict[str, str],
    version: str | None,
) -> list[UpstreamCandidate]:
    """Builds the ordered list of upstream URLs to try for this request.

    Priority: an exact version-specific override (if the requested version
    matches one) takes precedence as the sole candidate — different API
    versions are assumed to be genuinely different deployments, not
    interchangeable replicas. Otherwise, multi-region replicas are used,
    ordered by (recorded latency ascending, then configured priority),
    falling back to the single upstream_base_url if no replicas are configured.
    """
    if version and version in version_upstreams:
        return [UpstreamCandidate(url=version_upstreams[version])]

    if upstream_replicas:
        return [
            UpstreamCandidate(url=r["url"], region=r.get("region"), priority=r.get("priority", 0))
            for r in sorted(upstream_replicas, key=lambda r: r.get("priority", 0))
        ]

    return [UpstreamCandidate(url=upstream_base_url)]


class LatencyTracker:
    """Tracks a rolling latency estimate per upstream URL in Redis (an
    exponential moving average), so candidates can be reordered toward
    whichever replica has been fastest recently — this is what makes
    routing latency-aware rather than purely priority-based."""

    _ALPHA = 0.3  # weight given to the newest sample

    def __init__(self, redis: Redis):
        self._redis = redis

    def _key(self, url: str) -> str:
        return f"latency_ema:{url}"

    async def record(self, url: str, latency_ms: float) -> None:
        current = await self._redis.get(self._key(url))
        if current is None:
            new_value = latency_ms
        else:
            new_value = self._ALPHA * latency_ms + (1 - self._ALPHA) * float(current)
        await self._redis.set(self._key(url), new_value, ex=3600)

    async def order_by_latency(self, candidates: list[UpstreamCandidate]) -> list[UpstreamCandidate]:
        if len(candidates) <= 1:
            return candidates

        scored = []
        for c in candidates:
            raw = await self._redis.get(self._key(c.url))
            # Unknown latency sorts as "assume average" rather than
            # "assume fastest" or "assume slowest", so a brand-new replica
            # doesn't immediately steal all traffic or get starved of it.
            latency = float(raw) if raw is not None else 100.0
            scored.append((latency, c.priority, c))

        scored.sort(key=lambda t: (t[0], t[1]))
        return [c for _, _, c in scored]


def select_canary_version(canary_rules: list[dict]) -> str | None:
    """For requests with NO explicit version in the path, rolls a weighted
    random choice across configured canary rules to decide whether this
    particular request should be diverted to a canary version instead of
    the tenant's default upstream.

    canary_rules: [{"version": "v2", "percentage": 10}, ...]. Percentages
    are independent draws against the default (not required to sum to
    100) — e.g. a single 10% rule sends ~10% of traffic to that version
    and leaves the rest on the default path. Returns the chosen version
    string, or None if no canary rule fires (falls back to default).
    """
    for rule in canary_rules:
        percentage = rule.get("percentage", 0)
        if percentage <= 0:
            continue
        if random.random() * 100 < percentage:
            return rule.get("version")
    return None
