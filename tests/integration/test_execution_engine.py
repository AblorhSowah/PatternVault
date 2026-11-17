"""Test plan: execution/recovery.py, file_ops.py, commands.py, orchestration/approvals.py
Recovery: exact bytes+mode restored, created files removed (and empty dirs pruned), outside-root refused, private permissions, expiry
FileOps: DENY aborts the whole batch untouched; approvals awaited (assisted/sensitive); mid-write failure restores everything; audited
Commands: allow-list/approval/deny per mode, timeouts kill the process group, env is sanitised (no PatternVault/secret vars), output redacted
Approvals: approve/deny/timeout/double-decide
"""

import asyncio
import os
import stat
import sys
import time

import pytest

from app.config import PermissionLevel as L
from app.execution.commands import CommandDenied
from app.execution.file_ops import ChangeDenied, ChangeSpec
from app.execution.recovery import RecoveryError
from app.models import Project
from app.orchestration.approvals import ApprovalDenied, ApprovalTimeout
from app.security.permissions import PermissionPolicy
from tests.conftest import FAKE_AWS

pytestmark = pytest.mark.integration


@pytest.fixture()
def proj(services, tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_text("a = 1\n")
    (root / "src/b.py").write_bytes(b"b = 2\r\n\x00bin")
    os.chmod(root / "src/a.py", 0o640)
    with services.session_factory() as db:
        p = Project(name="p", root_path=str(root))
        db.add(p)
        db.commit()
        return services, p.id, root


def run(coro):
    return asyncio.run(coro)


def audit_actions(svc):
    from sqlalchemy import select

    from app.models import AuditLog

    with svc.session_factory() as db:
        return {a for (a,) in db.execute(select(AuditLog.action)).all()}


# ------------------------------------------------------------------ recovery
def test_recovery_restores_exact_state(proj):
    svc, pid, root = proj
    before_b = (root / "src/b.py").read_bytes()
    point = svc.recovery.create(pid, root, ["src/a.py", "src/b.py", "src/new/c.py"], label="t")
    (root / "src/a.py").write_text("changed\n")
    (root / "src/b.py").unlink()
    (root / "src/new").mkdir()
    (root / "src/new/c.py").write_text("created later\n")
    res = svc.recovery.restore(point, root)
    assert sorted(res["restored"]) == ["src/a.py", "src/b.py"] and res["removed"] == ["src/new/c.py"]
    assert (root / "src/a.py").read_text() == "a = 1\n" and (
        root / "src/b.py"
    ).read_bytes() == before_b  # byte-exact incl. CRLF/NUL
    assert stat.S_IMODE((root / "src/a.py").stat().st_mode) == 0o640
    assert not (root / "src/new").exists()  # empty directory created by the change is pruned
    assert svc.recovery.list(pid)[0]["status"] == "restored"
    assert {"recovery.create", "recovery.restore"} <= audit_actions(svc)


def test_recovery_is_private_and_refuses_escape(proj):
    svc, pid, root = proj
    point = svc.recovery.create(pid, root, ["src/a.py"])
    d = svc.paths.recovery / str(point)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in d.iterdir())
    with pytest.raises(RecoveryError, match="outside"):
        svc.recovery.create(pid, root, ["../outside.txt"])
    with pytest.raises(RecoveryError):
        svc.recovery.restore(999, root)


def test_recovery_expiry_deletes_data(proj):
    svc, pid, root = proj
    point = svc.recovery.create(pid, root, ["src/a.py"])
    from datetime import timedelta

    from app.models import RecoveryPoint
    from app.models.base import utcnow

    with svc.session_factory() as db:
        db.get(RecoveryPoint, point).created_at = utcnow() - timedelta(days=40)
        db.commit()
    assert svc.recovery.expire(30) == 1 and not (svc.paths.recovery / str(point)).exists()
    with pytest.raises(RecoveryError, match="expired"):
        svc.recovery.restore(point, root)


# ------------------------------------------------------------------ file ops
def policy(root, level, **kw):
    return PermissionPolicy(level, root, **kw)


def test_apply_writes_atomically_with_recovery_and_audit(proj):
    svc, pid, root = proj
    p = policy(root, L.RESTRICTED_AUTONOMOUS)
    res = run(
        svc.file_ops.apply(
            pid,
            p,
            [ChangeSpec("src/a.py", "a = 99\n", "modify"), ChangeSpec("src/new.py", "n = 1\n", "create")],
            task_id=None,
            label="x",
        )
    )
    assert (root / "src/a.py").read_text() == "a = 99\n" and (root / "src/new.py").exists()
    assert (
        res.created == ["src/new.py"]
        and "src/new.py" in p.created_paths
        and stat.S_IMODE((root / "src/a.py").stat().st_mode) == 0o640
    )
    svc.recovery.restore(res.recovery_point_id, root)
    assert (root / "src/a.py").read_text() == "a = 1\n" and not (root / "src/new.py").exists()
    assert {"file.write", "recovery.create", "recovery.restore"} <= audit_actions(svc)


def test_denied_change_aborts_whole_batch(proj):
    svc, pid, root = proj
    p = policy(root, L.RESTRICTED_AUTONOMOUS)
    with pytest.raises(ChangeDenied) as ei:
        run(
            svc.file_ops.apply(
                pid,
                p,
                [
                    ChangeSpec("src/ok.py", "x = 1\n", "create"),
                    ChangeSpec("../evil.py", "x", "create"),
                    ChangeSpec(".git/hooks/x", "x", "create"),
                ],
            )
        )
    assert len(ei.value.denied) == 2
    assert not (root / "src/ok.py").exists()  # nothing was written
    assert svc.recovery.list(pid) == []  # and no recovery point was even needed


def test_read_only_mode_denies_writes(proj):
    svc, pid, root = proj
    with pytest.raises(ChangeDenied, match="read-only"):
        run(svc.file_ops.apply(pid, policy(root, L.READ_ONLY), [ChangeSpec("src/ok.py", "x", "create")]))


def test_sensitive_file_waits_for_approval_even_when_autonomous(proj):
    svc, pid, root = proj

    async def scenario():
        p = policy(root, L.RESTRICTED_AUTONOMOUS)
        t = asyncio.create_task(
            svc.file_ops.apply(
                pid, p, [ChangeSpec("package.json", "{}\n", "create"), ChangeSpec("src/x.py", "x = 1\n", "create")]
            )
        )
        for _ in range(100):
            await asyncio.sleep(0.05)
            if svc.approvals.pending():
                break
        pend = svc.approvals.pending()
        assert (
            len(pend) == 1 and pend[0]["detail"]["path"] == "package.json" and not (root / "src/x.py").exists()
        )  # nothing written while waiting
        svc.approvals.decide(pend[0]["id"], True, "ok")
        return await t

    res = run(scenario())
    assert (root / "package.json").exists() and (root / "src/x.py").exists() and len(res.approvals) == 1


def test_denied_approval_leaves_files_untouched(proj):
    svc, pid, root = proj

    async def scenario():
        t = asyncio.create_task(
            svc.file_ops.apply(pid, policy(root, L.ASSISTED), [ChangeSpec("src/x.py", "x = 1\n", "create")])
        )
        for _ in range(100):
            await asyncio.sleep(0.05)
            if svc.approvals.pending():
                break
        svc.approvals.decide(svc.approvals.pending()[0]["id"], False, "no")
        with pytest.raises(ApprovalDenied):
            await t

    run(scenario())
    assert not (root / "src/x.py").exists()


def test_failure_mid_write_restores_everything(proj):
    svc, pid, root = proj
    (root / "blocked").write_text(
        "i am a file"
    )  # a later change needs 'blocked' to be a directory → fails after the first writes
    p = policy(root, L.RESTRICTED_AUTONOMOUS)
    with pytest.raises(OSError):
        run(
            svc.file_ops.apply(
                pid,
                p,
                [ChangeSpec("src/a.py", "a = 99\n", "modify"), ChangeSpec("blocked/inner.py", "x = 1\n", "create")],
            )
        )
    assert (root / "src/a.py").read_text() == "a = 1\n"  # first change rolled back automatically


# ------------------------------------------------------------------ commands
def test_command_policy_matrix(proj):
    svc, pid, root = proj
    p = policy(root, L.RESTRICTED_AUTONOMOUS)
    ok = run(svc.commands.run([sys.executable, "-m", "pytest", "--version"], root, p))
    assert ok.ok and "pytest" in (ok.stdout + ok.stderr).lower()
    with pytest.raises(CommandDenied, match="destructive"):
        run(svc.commands.run(["rm", "-rf", "/"], root, p))
    with pytest.raises(CommandDenied, match="working directory"):
        run(svc.commands.run([sys.executable, "-m", "pytest", "--version"], root.parent, p))
    with pytest.raises(CommandDenied, match="read-only"):
        run(svc.commands.run([sys.executable, "-m", "pytest", "--version"], root, policy(root, L.READ_ONLY)))
    from sqlalchemy import select

    from app.models import AuditLog

    with svc.session_factory() as db:
        denied = [
            a for a in db.scalars(select(AuditLog).where(AuditLog.action == "command.run")) if a.outcome == "denied"
        ]
        assert len(denied) >= 2 and any("rm -rf" in str(a.detail) for a in denied)


def test_unknown_command_needs_approval_then_runs(proj):
    svc, pid, root = proj

    async def scenario():
        t = asyncio.create_task(
            svc.commands.run(
                [sys.executable, "-c", "print('approved run')"], root, policy(root, L.RESTRICTED_AUTONOMOUS)
            )
        )
        for _ in range(100):
            await asyncio.sleep(0.05)
            if svc.approvals.pending():
                break
        assert svc.approvals.pending()[0]["action"] == "run_command"
        svc.approvals.decide(svc.approvals.pending()[0]["id"], True)
        return await t

    assert "approved run" in run(scenario()).stdout


def test_timeout_kills_process_group(proj):
    svc, pid, root = proj
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
    t0 = time.monotonic()
    res = run(
        svc.commands.run(
            [sys.executable, "-c", code], root, policy(root, L.RESTRICTED_AUTONOMOUS), timeout_s=1.5, pre_approved=True
        )
    )
    assert res.timed_out and not res.ok and time.monotonic() - t0 < 10


def test_child_environment_is_sanitised_and_output_redacted(proj, monkeypatch):
    svc, pid, root = proj
    monkeypatch.setenv("PATTERNVAULT_TOKEN", "super-secret-backend-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shh-aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_notforchildren")
    code = f"import os; print(sorted(k for k in os.environ if 'TOKEN' in k or 'SECRET' in k or 'PATTERNVAULT' in k)); print('{FAKE_AWS}'); print(os.environ.get('CI'))"
    res = run(
        svc.commands.run([sys.executable, "-c", code], root, policy(root, L.RESTRICTED_AUTONOMOUS), pre_approved=True)
    )
    assert (
        "[]" in res.stdout and "super-secret" not in res.stdout and "ghp_" not in res.stdout
    )  # secrets never reach children
    assert (
        FAKE_AWS not in res.stdout and "[REDACTED:aws-access-key-id]" in res.stdout
    )  # output is redacted before storage
    assert res.stdout.strip().endswith("1")  # CI=1 set


def test_cancellation_kills_child(proj):
    svc, pid, root = proj

    async def scenario():
        t = asyncio.create_task(
            svc.commands.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                root,
                policy(root, L.RESTRICTED_AUTONOMOUS),
                pre_approved=True,
            )
        )
        await asyncio.sleep(0.5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    t0 = time.monotonic()
    run(scenario())
    assert time.monotonic() - t0 < 5


# ------------------------------------------------------------------ approvals
def test_approval_lifecycle(services):
    a = services.approvals.request(None, "write_file", "x", {"path": "p"})
    assert services.approvals.pending()[0]["id"] == a
    services.approvals.decide(a, True, "fine")
    with pytest.raises(ValueError, match="already approved"):
        services.approvals.decide(a, False)
    with pytest.raises(LookupError):
        services.approvals.decide(999, True)
    run(services.approvals.wait(a, 1))  # already approved → returns at once
    b = services.approvals.request(None, "run_command", "y", {})
    with pytest.raises(ApprovalTimeout):
        run(services.approvals.wait(b, 0.3))
    assert services.approvals.list()[0]["decision_note"] == "timed out"
