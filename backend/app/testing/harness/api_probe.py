"""API probe harness. Runs INSIDE the target project's interpreter (needs only fastapi/starlette there).

Loads a FastAPI app, walks its OpenAPI schema and runs safe negative/shape checks through the in-process TestClient.
Prints one JSON document to stdout. Never sends valid mutating requests.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tempfile


def sample(schema: dict, spec: dict) -> object:
    t = schema.get("type")
    if "$ref" in schema:
        return sample(spec["components"]["schemas"][schema["$ref"].split("/")[-1]], spec)
    return {"integer": 1, "number": 1, "boolean": True, "string": "x"}.get(str(t), "x")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--app", required=True, help="module:attr (attr may be an app or a zero-arg factory)")
    ap.add_argument("--token", default="")
    ap.add_argument("--expect-auth", action="store_true")
    ap.add_argument("--public", default="/api/v1/health")
    ap.add_argument("--max-ops", type=int, default=200)
    a = ap.parse_args()
    sys.path[:0] = [a.root, os.path.join(a.root, "backend")]
    os.environ.setdefault("PATTERNVAULT_HOME", tempfile.mkdtemp(prefix="pv-probe-"))
    module, _, attr = a.app.partition(":")
    obj = getattr(importlib.import_module(module), attr)
    app = obj() if callable(obj) and not hasattr(obj, "openapi") else obj
    from fastapi.testclient import TestClient

    cases: list[dict] = []
    client = TestClient(app, raise_server_exceptions=False)
    try:
        spec = app.openapi()
    except Exception as exc:
        print(json.dumps({"error": f"cannot build OpenAPI schema: {exc}", "cases": []}))
        return 0
    public = {p.strip() for p in a.public.split(",") if p.strip()}
    n = 0
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in sorted(ops.items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete"} or n >= a.max_ops:
                continue
            n += 1
            url = path
            for p in op.get("parameters", []):
                if p["in"] == "path":
                    url = url.replace("{" + p["name"] + "}", str(sample(p.get("schema", {}), spec)))
            url = re.sub(r"\{[^}]+\}", "1", url)
            name = f"{method.upper()} {path}"
            body_req = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
            required = []
            if body_req:
                s = body_req
                if "$ref" in s:
                    s = spec["components"]["schemas"][s["$ref"].split("/")[-1]]
                required = s.get("required", [])
            # 1. unauthenticated / invalid input never produces a server error
            has_body = method.lower() in {"post", "put", "patch"}
            r = client.request(method.upper(), url, json={} if has_body else None)
            cases.append(
                {
                    "name": f"{name}: no server error on bare request",
                    "status": "passed" if r.status_code < 500 else "failed",
                    "expected": "status < 500",
                    "actual": str(r.status_code),
                    "details": r.text[:300] if r.status_code >= 500 else "",
                }
            )
            # 2. auth enforced (only when the project says it expects it)
            if a.expect_auth and path not in public:
                ok = r.status_code in (401, 403)
                cases.append(
                    {
                        "name": f"{name}: rejects unauthenticated requests",
                        "status": "passed" if ok else "failed",
                        "expected": "401/403 without credentials",
                        "actual": str(r.status_code),
                        "details": "",
                    }
                )
            # 3. validation: required fields must be enforced (authenticated, empty body)
            if required and method.lower() in {"post", "put", "patch"}:
                h = {"Authorization": f"Bearer {a.token}"} if a.token else {}
                rr = client.request(method.upper(), url, json={}, headers=h)
                cases.append(
                    {
                        "name": f"{name}: validates required fields {required}",
                        "status": "passed" if 400 <= rr.status_code < 500 else "failed",
                        "expected": "4xx for an empty body",
                        "actual": str(rr.status_code),
                        "details": rr.text[:200],
                    }
                )
            # 4. authenticated GET shape (only parameterless GETs, read-only)
            if method.lower() == "get" and a.token and "{" not in path:
                rr = client.get(url, headers={"Authorization": f"Bearer {a.token}"})
                declared = (op.get("responses", {}).get("200", {}).get("content", {}) or {}).get("application/json")
                if rr.status_code == 200 and declared is not None:
                    try:
                        data = rr.json()
                        want = declared.get("schema", {}).get("type")
                        expected_type = {"object": dict, "array": list}.get(str(want)) if want else None
                        good = expected_type is None or isinstance(data, expected_type)
                    except ValueError:
                        good = False
                    cases.append(
                        {
                            "name": f"{name}: response matches declared JSON shape",
                            "status": "passed" if good else "failed",
                            "expected": "declared JSON type",
                            "actual": rr.headers.get("content-type", ""),
                            "details": "",
                        }
                    )
    print(json.dumps({"cases": cases, "operations": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
