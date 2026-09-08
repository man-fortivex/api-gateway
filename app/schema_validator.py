import json

import jsonschema


def find_matching_schema(request_schemas: dict, method: str, path: str) -> dict | None:
    """request_schemas keys look like "POST /users" or "PUT /orders/*"
    (a single trailing "*" matches one or more path segments, not full
    regex — enough for the common "validate this endpoint's body" case
    without pulling in a path-templating library)."""
    key_exact = f"{method.upper()} /{path.lstrip('/')}"
    if key_exact in request_schemas:
        return request_schemas[key_exact]

    for key, schema in request_schemas.items():
        if not key.endswith("*"):
            continue
        method_part, _, path_part = key.partition(" ")
        if method_part.upper() != method.upper():
            continue
        prefix = path_part.lstrip("/").rstrip("*")
        if path.lstrip("/").startswith(prefix):
            return schema

    return None


def validate_body(body: bytes, schema: dict) -> list[str]:
    """Returns a list of human-readable validation error messages; empty
    list means valid. Non-JSON bodies against a schema are treated as a
    single validation error rather than raising, so a malformed request
    gets a clean 400 instead of an unhandled exception."""
    try:
        data = json.loads(body) if body else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ["Request body is not valid JSON"]

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
