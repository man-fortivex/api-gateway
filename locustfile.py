"""Load test for the gateway's hot path (auth + rate-limit + proxy).

Setup:
    pip install locust
    # Point a tenant's upstream_base_url at something fast and stable
    # (e.g. a local echo server) before running this against it.

Run:
    locust -f locustfile.py --host http://localhost:8000

Then open http://localhost:8089 to configure users/spawn-rate and start.
For a headless run with published numbers:

    locust -f locustfile.py --host http://localhost:8000 \
        --users 50 --spawn-rate 10 --run-time 1m --headless \
        --csv=benchmark_results

Set GATEWAY_API_KEY and GATEWAY_TENANT_SLUG below (or via env vars) to
match a real tenant/key you've created via /signup or the dashboard first.
"""

import os

from locust import HttpUser, between, task

API_KEY = os.environ.get("GATEWAY_API_KEY", "agw_replace_me")
TENANT_SLUG = os.environ.get("GATEWAY_TENANT_SLUG", "loadtest")


class GatewayUser(HttpUser):
    wait_time = between(0.05, 0.2)

    @task
    def proxied_get(self):
        self.client.get(
            f"/gw/{TENANT_SLUG}/ping",
            headers={"X-API-Key": API_KEY},
            name="/gw/[tenant]/ping",
        )
