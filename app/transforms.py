"""Small rules engine for per-tenant request/response transforms.

Each rule is a dict with a "type" key. Supported types:

  {"type": "add_header", "name": "X-Foo", "value": "bar"}
      Adds/overwrites a header on the outbound request to the upstream.

  {"type": "remove_header", "name": "X-Foo"}
      Removes a header before forwarding to the upstream.

  {"type": "rewrite_path_prefix", "from": "/old", "to": "/new"}
      If the downstream path starts with "from", replaces that prefix
      with "to" before forwarding.

  {"type": "strip_json_field", "field": "internal_id"}
      If the upstream's response is JSON, removes the named top-level
      field before returning it to the caller. Silently a no-op on
      non-JSON or non-dict responses.

Rules are applied in list order. This intentionally stays simple (no
conditionals, no nested field paths) — enough to demonstrate the pattern
without building a full expression language.
"""

import json


def apply_request_header_rules(headers: dict[str, str], rules: list[dict]) -> dict[str, str]:
    result = dict(headers)
    for rule in rules:
        if rule.get("type") == "add_header":
            result[rule["name"]] = rule["value"]
        elif rule.get("type") == "remove_header":
            result.pop(rule["name"], None)
            # Header names are case-insensitive; also drop a differently-cased match.
            for key in list(result.keys()):
                if key.lower() == rule["name"].lower():
                    result.pop(key, None)
    return result


def apply_path_rewrite_rules(path: str, rules: list[dict]) -> str:
    for rule in rules:
        if rule.get("type") == "rewrite_path_prefix":
            prefix = rule["from"]
            if path.startswith(prefix.lstrip("/")):
                path = rule["to"].lstrip("/") + path[len(prefix.lstrip("/")):]
    return path


def apply_response_body_rules(body: bytes, content_type: str | None, rules: list[dict]) -> bytes:
    strip_fields = [r["field"] for r in rules if r.get("type") == "strip_json_field"]
    if not strip_fields:
        return body
    if not content_type or "application/json" not in content_type:
        return body

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    if not isinstance(data, dict):
        return body

    for field in strip_fields:
        data.pop(field, None)

    return json.dumps(data).encode("utf-8")
