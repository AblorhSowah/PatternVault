"""Test plan: engine REST API (tasks, approvals, agents, testing sessions, recovery points) with real files/pytest runs.
- POST /tasks → 202; poll to terminal; report markdown; history lists; cancel; validation errors
- approvals visible while a task waits, decided through the API; recovery point restore needs confirmation and reverts files
- agent list (20 incl. orchestrator) + enable/disable persisted + audited; orchestrator cannot be disabled
- testing run/summary/sessions expose every field the Testing Centre needs
- a cloud API key supplied with a task is never persisted anywhere
"""

import json
import time

import pytest

from tests.helpers.fake_ollama import FakeOllama
from tests.integration.test_orchestrator import CALC, GOOD_CALC, blocks, mkcalc

pytestmark = pytest.mark.integration
V1 = "/api/v1"


def wait_task(client, tid, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        t = client.get(f"{V1}/tasks/{tid}").json()
        if t["status"] in ("verified", "completed", "failed", "cancelled"):
            return t
        time.sleep(0.2)
    raise AssertionError(f"task {tid} did not finish: {client.get(f'{V1}/tasks/{tid}').json()['status']}")


def setup(client, services, tmp_path, fake, level="restricted_autonomous"):
    root, pid = mkcalc(services, tmp_path)
    services.config.update(
        {
            "mode": "offline",
            "permission_level": level,
            "local_model": {"base_url": fake.url, "model": "qwen2.5-coder:7b"},
        }
    )
    return root, pid


def test_task_lifecycle_through_the_api(client, services, tmp_path):
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        root, pid = setup(client, services, tmp_path, fake)
        r = client.post(
            f"{V1}/tasks",
            json={
                "kind": "generate",
                "request": "Add a multiply function to calc.py",
                "project_id": pid,
                "params": {"agents": ["unit-testing"]},
            },
        )
        assert r.status_code == 202
        t = wait_task(client, r.json()["id"])
    assert t["status"] == "verified" and [p["status"] for p in t["plan"]] == ["success"] * 6
    assert any(e["agent"] == "code-generation" and e["status"] == "success" for e in t["executions"])
    md = client.get(f"{V1}/tasks/{t['id']}/report")
    assert (
        md.status_code == 200 and md.headers["content-type"].startswith("text/plain") and "Verified:** yes" in md.text
    )
    assert client.get(f"{V1}/tasks").json()[0]["id"] == t["id"]
    dash = client.get(f"{V1}/dashboard").json()
    assert dash["recent_tasks"][0]["verified"] is True and dash["counts"]["patterns"] == 1
    # testing sessions created by the run are queryable
    sessions = client.get(f"{V1}/testing/sessions", params={"project_id": pid}).json()
    assert (
        sessions[0]["agent_key"] == "unit-testing"
        and sessions[0]["status"] == "passed"
        and sessions[0]["task_id"] == t["id"]
    )
    detail = client.get(f"{V1}/testing/sessions/{sessions[0]['id']}").json()
    assert detail["results"] and {
        "name",
        "expected",
        "actual",
        "status",
        "error_details",
        "correction_applied",
        "retest_status",
    } <= set(detail["results"][0])
    summ = client.get(f"{V1}/testing/summary", params={"project_id": pid}).json()
    assert (
        len(summ["agents"]) == 10
        and next(a for a in summ["agents"] if a["agent"] == "unit-testing")["last"]["status"] == "passed"
    )


def test_task_validation_and_errors(client, services, tmp_path):
    assert client.post(f"{V1}/tasks", json={"kind": "nonsense", "request": "x"}).status_code == 422
    assert client.post(f"{V1}/tasks", json={"kind": "generate", "request": "x"}).status_code == 422  # needs a project
    assert client.post(f"{V1}/tasks", json={"kind": "generate", "request": "x", "project_id": 999}).status_code == 404
    assert client.get(f"{V1}/tasks/999").status_code == 404 and client.get(f"{V1}/tasks/999/report").status_code == 404
    assert client.post(f"{V1}/testing/run", json={"project_id": 999}).status_code == 404
    root, pid = mkcalc(services, tmp_path)
    assert client.post(f"{V1}/testing/run", json={"project_id": pid, "agents": ["not-an-agent"]}).status_code == 422
    assert client.post(f"{V1}/tasks/999/cancel").json() == {"cancelled": False}


def test_approvals_through_the_api(client, services, tmp_path):
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        root, pid = setup(client, services, tmp_path, fake, level="assisted")
        tid = client.post(
            f"{V1}/tasks",
            json={
                "kind": "generate",
                "request": "Add multiply to calc.py",
                "project_id": pid,
                "params": {"agents": ["unit-testing"]},
            },
        ).json()["id"]
        decided = 0
        end = time.time() + 180
        while time.time() < end and client.get(f"{V1}/tasks/{tid}").json()["status"] in (
            "pending",
            "running",
            "awaiting_approval",
        ):
            for a in client.get(f"{V1}/approvals", params={"pending_only": True}).json():
                assert a["status"] == "pending" and a["summary"] and a["action"] in ("write_file", "run_command")
                r = client.post(f"{V1}/approvals/{a['id']}/decide", json={"approve": True, "note": "ok"})
                assert r.status_code == 200 and r.json()["status"] == "approved"
                assert (
                    client.post(f"{V1}/approvals/{a['id']}/decide", json={"approve": False}).status_code == 409
                )  # already decided
                decided += 1
            time.sleep(0.1)
        t = client.get(f"{V1}/tasks/{tid}").json()
    assert t["status"] == "verified" and decided >= 3
    assert client.post(f"{V1}/approvals/9999/decide", json={"approve": True}).status_code == 404
    audit = {a["action"] for a in client.get(f"{V1}/audit", params={"limit": 500}).json()}
    assert {
        "approval.requested",
        "approval.approved",
        "task.submit",
        "task.finish",
        "file.write",
        "command.run",
    } <= audit


def test_recovery_points_restore_with_confirmation(client, services, tmp_path):
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        root, pid = setup(client, services, tmp_path, fake)
        t = wait_task(
            client,
            client.post(
                f"{V1}/tasks",
                json={
                    "kind": "generate",
                    "request": "Add multiply to calc.py",
                    "project_id": pid,
                    "params": {"agents": ["unit-testing"]},
                },
            ).json()["id"],
        )
    assert "multiply" in (root / "calc.py").read_text()
    pts = client.get(f"{V1}/recovery-points", params={"project_id": pid}).json()
    assert pts and pts[0]["task_id"] == t["id"] and "calc.py" in pts[0]["files"]
    assert (
        client.post(f"{V1}/recovery-points/{pts[0]['id']}/restore").status_code == 400
    )  # destructive → confirm required
    assert client.post(f"{V1}/recovery-points/999/restore", params={"confirm": True}).status_code == 404
    res = client.post(f"{V1}/recovery-points/{pts[0]['id']}/restore", params={"confirm": True}).json()
    assert (
        "calc.py" in res["restored"]
        and (root / "calc.py").read_text() == CALC
        and not (root / "tests/unit/test_multiply.py").exists()
    )
    assert (
        client.post(f"{V1}/recovery-points/{pts[0]['id']}/restore", params={"confirm": True}).status_code == 200
    )  # idempotent restore


def test_agents_api_enable_disable_and_listing(client, services):
    agents = client.get(f"{V1}/agents").json()
    assert len(agents) == 20 and {a["category"] for a in agents} == {"development", "testing", "orchestration"}
    assert sum(a["category"] == "testing" for a in agents) == 10 and all(a["enabled"] for a in agents)
    r = client.patch(f"{V1}/agents/code-review-security", json={"enabled": False}).json()
    assert r["enabled"] is False and "code-review-security" in client.get(f"{V1}/config").json()["agents"]["disabled"]
    assert client.patch(f"{V1}/agents/code-review-security", json={"enabled": True}).json()["enabled"] is True
    assert client.patch(f"{V1}/agents/orchestrator", json={"enabled": False}).status_code == 409
    assert client.patch(f"{V1}/agents/nope", json={"enabled": False}).status_code == 404
    assert any(a["action"] == "agent.update" for a in client.get(f"{V1}/audit").json())


def test_testing_run_endpoint_runs_selected_agents(client, services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    services.config.update({"permission_level": "restricted_autonomous"})
    tid = client.post(f"{V1}/testing/run", json={"project_id": pid, "agents": ["unit-testing", "e2e-testing"]}).json()[
        "id"
    ]
    t = wait_task(client, tid)
    assert t["kind"] == "test" and t["status"] == "verified"
    assert t["result"]["gate"]["passed"] == ["unit-testing"] and t["result"]["gate"]["skipped"] == ["e2e-testing"]
    (root / "tests/unit/test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n"
    )
    t2 = wait_task(
        client, client.post(f"{V1}/testing/run", json={"project_id": pid, "agents": ["unit-testing"]}).json()["id"]
    )
    assert t2["status"] == "failed" and t2["result"]["gate"]["failed"] == ["unit-testing"]
    failing = [
        s
        for s in client.get(f"{V1}/testing/sessions", params={"agent": "unit-testing"}).json()
        if s["status"] == "failed"
    ]
    d = client.get(f"{V1}/testing/sessions/{failing[0]['id']}").json()
    bad = next(r for r in d["results"] if r["status"] == "failed")
    assert "assert" in bad["error_details"] and bad["expected"] and bad["actual"]
    assert client.get(f"{V1}/testing/summary").json()["failing_results_total"] >= 1


def test_cloud_key_with_a_task_is_never_persisted(client, services, tmp_path):
    key = "sk-cloud-" + "k" * 30
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        root, pid = setup(client, services, tmp_path, fake)
        t = wait_task(
            client,
            client.post(
                f"{V1}/tasks",
                json={
                    "kind": "generate",
                    "request": "Add multiply to calc.py",
                    "project_id": pid,
                    "params": {"agents": ["unit-testing"]},
                },
                headers={"X-PV-Cloud-Key": key},
            ).json()["id"],
        )
    services.engine.dispose()
    blob = (
        json.dumps(t)
        + json.dumps(client.get(f"{V1}/audit", params={"limit": 500}).json())
        + services.paths.config.read_text()
    )
    blob += services.paths.db.read_bytes().decode("latin-1")
    assert key not in blob
