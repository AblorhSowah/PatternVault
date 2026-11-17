"""Test plan: storage accounting, enforced limits, garbage collection
- usage reports every store; limits come from config; over-limit blocks NEW capture (HTTP 507) but never reads
- gc removes only unreferenced blobs (keeps snapshot + baseline blobs), reports freed bytes, is audited
"""

import pytest

from tests.conftest import apply_auth_feature

pytestmark = pytest.mark.integration
V1 = "/api/v1"


@pytest.fixture()
def project(api, auth_project):
    return api.post(f"{V1}/projects", json={"root_path": str(auth_project)}).json()["id"]


def test_usage_and_limits(api, project):
    u = api.get(f"{V1}/storage").json()
    assert (
        u["db_bytes"] > 0
        and u["limits"] == {"max_db_mb": 4096, "max_blobs_mb": 2048}
        and not u["over_blob_limit"]
        and not u["over_db_limit"]
    )
    assert {"blob_bytes", "vector_bytes", "backup_bytes", "recovery_bytes"} <= set(u)


def test_over_limit_blocks_capture_but_not_reads(api, project, auth_project):
    api.post(f"{V1}/sessions", json={"project_id": project, "title": "ok"})  # fills blobs with a baseline
    svc = api.app.state.services
    # the smallest legal limit is 16 MB; write a large blob to exceed it
    svc.config.update({"storage": {"max_blobs_mb": 16}})
    svc.blobs.put(b"x" * (17 * 1024 * 1024))
    assert api.get(f"{V1}/storage").json()["over_blob_limit"] is True
    r = api.post(f"{V1}/sessions", json={"project_id": project})
    assert r.status_code == 507 and "over its limit" in r.json()["detail"]
    assert (
        api.get(f"{V1}/patterns").status_code == 200 and api.get(f"{V1}/dashboard").status_code == 200
    )  # reads still work
    svc.config.update({"storage": {"max_blobs_mb": 4096}})
    assert api.post(f"{V1}/sessions", json={"project_id": project}).status_code == 201


def test_gc_removes_only_unreferenced_blobs(api, project, auth_project):
    sid = api.post(f"{V1}/sessions", json={"project_id": project, "title": "s"}).json()["id"]
    apply_auth_feature(auth_project)
    api.post(f"{V1}/sessions/{sid}/end", json={"extract": False})
    svc = api.app.state.services
    orphan = svc.blobs.put(b"orphan blob nobody references " * 100)
    before = {p.name for p in svc.paths.blobs.rglob("*") if p.is_file()}
    res = api.post(f"{V1}/storage/gc").json()
    after = {p.name for p in svc.paths.blobs.rglob("*") if p.is_file()}
    assert orphan in before and orphan not in after and res["removed"] >= 1 and res["freed_bytes"] > 2000
    assert (
        orphan not in after and after < before
    )  # unreferenced blobs go (incl. the finished session's unchanged-file baseline copies)
    # sessions still readable (their snapshot blobs survived)
    detail = api.get(f"{V1}/sessions/{sid}").json()
    assert detail["changes"] and any("def hash_password" in c["diff"] for c in detail["changes"])
    api.delete(f"{V1}/sessions/{sid}", params={"confirm": "true"})
    assert api.post(f"{V1}/storage/gc").json()["removed"] > 0  # now the session's blobs are garbage
    assert any(a["action"] == "storage.gc" for a in api.get(f"{V1}/audit").json())


def test_gc_never_breaks_an_active_capture_window(api, project, auth_project):
    sid = api.post(f"{V1}/sessions", json={"project_id": project, "title": "open"}).json()["id"]
    svc = api.app.state.services
    api.post(f"{V1}/storage/gc")  # runs while the window is open: the baseline's "before" blobs must survive
    (auth_project / "app/main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/health')\ndef health():\n    return {'ok': False}\n"
    )
    end = api.post(f"{V1}/sessions/{sid}/end", json={"extract": False}).json()
    detail = api.get(f"{V1}/sessions/{sid}").json()
    change = next(c for c in detail["changes"] if c["path"] == "app/main.py")
    assert (
        end["changes"] == 1 and change["lines_added"] >= 1 and "-    return {'ok': True}" in change["diff"]
    )  # before-content was still there
    assert svc.blobs is not None
