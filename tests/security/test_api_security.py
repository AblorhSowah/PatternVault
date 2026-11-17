"""Test plan: API security posture
- every non-health route returns 401 without/with a wrong token (enumerated from the OpenAPI schema)
- token comparison tolerant of case in scheme only, never of token content
- non-loopback Host headers are rejected (DNS-rebinding defence)
- API never echoes secrets from config; error bodies do not leak internals
"""

import pytest

from tests.conftest import TOKEN

pytestmark = pytest.mark.security


def _routes(app):
    """Enumerate (method, path) from the OpenAPI schema (robust to FastAPI's nested router objects)."""
    out = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api/v1") and path != "/api/v1/health":
            out.extend((m.upper(), path) for m in ops)
    return out


def test_all_routes_require_auth(anon_client):
    routes = _routes(anon_client.app)
    assert len(routes) >= 8
    for method, path in routes:
        concrete = path.replace("{project_id}", "1")
        resp = anon_client.request(method, concrete)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
        resp = anon_client.request(method, concrete, headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401, f"{method} {path} (bad token) -> {resp.status_code}"


@pytest.mark.parametrize("header", ["", "Basic abc", TOKEN, "Bearer", f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}"])
def test_malformed_auth_headers_rejected(anon_client, header):
    r = anon_client.get("/api/v1/config", headers={"Authorization": header} if header else {})
    assert r.status_code == 401


def test_bearer_scheme_case_insensitive_token_exact(anon_client):
    assert anon_client.get("/api/v1/config", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_rejects_foreign_host_header(anon_client):
    r = anon_client.get("/api/v1/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_schema_and_docs_endpoints_not_exposed(anon_client, path):
    assert anon_client.get(path).status_code == 404


def test_validation_errors_do_not_leak_server_paths(client):
    r = client.post("/api/v1/projects", json={"root_path": ""})
    assert r.status_code == 422
    assert "/Users/" not in r.text and "Traceback" not in r.text
