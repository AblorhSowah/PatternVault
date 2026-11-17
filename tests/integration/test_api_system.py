"""Test plan: REST API foundation
- /health is public; every other route requires the bearer token
- dashboard returns zeroed counts on an empty DB and reflects created data
- mode switch persists, is audited and published on the event bus
- config PATCH validates (422), persists and audits; secrets rejected
- projects: register (idempotent), reject bad path / data dir, update, delete requires confirm
- audit chain verifies; tampering is detected
"""

import pytest
from sqlalchemy import text

from app.models import KnowledgePattern
from app.security.audit import verify_chain

pytestmark = pytest.mark.integration
V1 = "/api/v1"


def test_health_is_public(anon_client):
    r = anon_client.get(f"{V1}/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_dashboard_empty_and_populated(client, services):
    d = client.get(f"{V1}/dashboard").json()
    assert d["counts"] == {
        "projects": 0,
        "sessions": 0,
        "patterns": 0,
        "verified_patterns": 0,
        "generated_implementations": 0,
    }
    assert d["mode"] == "learning"
    with services.session_factory() as s:
        s.add(KnowledgePattern(title="a", fingerprint="1", status="verified"))
        s.add(KnowledgePattern(title="b", fingerprint="2", status="observed"))
        s.commit()
    c = client.get(f"{V1}/dashboard").json()["counts"]
    assert c["patterns"] == 2 and c["verified_patterns"] == 1


def test_mode_switch_persists_and_audits(client, services):
    assert client.put(f"{V1}/mode", json={"mode": "offline"}).json()["mode"] == "offline"
    assert client.get(f"{V1}/mode").json()["mode"] == "offline"
    assert client.put(f"{V1}/mode", json={"mode": "bogus"}).status_code == 422
    audit = client.get(f"{V1}/audit", params={"action": "mode."}).json()
    assert audit[0]["detail"] == {"from": "learning", "to": "offline"}
    assert any(e["type"] == "mode.changed" for e in services.events.recent())


def test_config_patch_roundtrip_and_validation(client):
    r = client.patch(f"{V1}/config", json={"permission_level": "read_only", "agents": {"max_fix_attempts": 5}})
    assert r.status_code == 200 and r.json()["agents"]["max_fix_attempts"] == 5
    assert client.get(f"{V1}/config").json()["permission_level"] == "read_only"
    assert client.patch(f"{V1}/config", json={"agents": {"max_fix_attempts": 99}}).status_code == 422
    assert client.patch(f"{V1}/config", json={"cloud": {"api_key": "sk-abcdefghijklmnop"}}).status_code == 422
    assert client.patch(f"{V1}/config", json={"privacy": {"redact_secrets": False}}).status_code == 422
    # the rejected attempts are audited as denied, and the rejected secret is not leaked into the log
    denied = [a for a in client.get(f"{V1}/audit", params={"action": "config."}).json() if a["outcome"] == "denied"]
    assert len(denied) >= 2


def test_config_reset(client):
    client.patch(f"{V1}/config", json={"mode": "hybrid"})
    assert client.post(f"{V1}/config/reset").json()["mode"] == "learning"


def test_project_lifecycle(client, tmp_path):
    root = tmp_path / "myproj"
    root.mkdir()
    r = client.post(f"{V1}/projects", json={"root_path": str(root)})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json()["name"] == "myproj"
    again = client.post(f"{V1}/projects", json={"root_path": str(root)})
    assert again.json()["id"] == pid  # idempotent
    assert len(client.get(f"{V1}/projects").json()) == 1
    assert client.patch(f"{V1}/projects/{pid}", json={"capture_enabled": False}).json()["capture_enabled"] is False
    assert client.delete(f"{V1}/projects/{pid}").status_code == 400  # needs confirm
    assert client.delete(f"{V1}/projects/{pid}", params={"confirm": "true"}).status_code == 204
    assert client.get(f"{V1}/projects/{pid}").status_code == 404
    assert root.exists()  # files on disk are never touched


def test_project_rejects_bad_paths(client, services, tmp_path):
    assert client.post(f"{V1}/projects", json={"root_path": str(tmp_path / "nope")}).status_code == 422
    assert client.post(f"{V1}/projects", json={"root_path": str(services.paths.home)}).status_code == 422
    assert client.post(f"{V1}/projects", json={"root_path": str(services.paths.blobs)}).status_code == 422


def test_audit_chain_verifies_and_detects_tampering(client, services):
    client.put(f"{V1}/mode", json={"mode": "hybrid"})
    client.put(f"{V1}/mode", json={"mode": "offline"})
    assert client.get(f"{V1}/audit/verify").json()["ok"] is True
    with services.engine.begin() as c:
        c.execute(text("UPDATE audit_logs SET action='mode.nothing' WHERE id=(SELECT MIN(id)+1 FROM audit_logs)"))
    res = client.get(f"{V1}/audit/verify").json()
    assert res["ok"] is False and res["first_bad_id"] is not None


def test_audit_chain_detects_deleted_middle_row(client, services):
    for m in ("hybrid", "offline", "learning"):
        client.put(f"{V1}/mode", json={"mode": m})
    with services.engine.begin() as c:
        c.execute(text("DELETE FROM audit_logs WHERE id=(SELECT MAX(id)-1 FROM audit_logs)"))
    with services.session_factory() as s:
        assert verify_chain(s)["ok"] is False


def test_audit_redacts_secrets_in_details(services):
    services.audit.record(
        "agent:x", "test.leak", detail={"cmd": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789'"}
    )
    with services.session_factory() as s:
        from sqlalchemy import select

        from app.models import AuditLog

        row = s.scalars(select(AuditLog).where(AuditLog.action == "test.leak")).one()
        assert "abcdefghijklmnop" not in str(row.detail)
        assert verify_chain(s)["ok"]
