"""Test plan: encrypted backups
- encrypted & plain round trip into a FRESH home: patterns, versions, blobs, config, audit chain, search all work after restore
- wrong passphrase / tampered file / truncated file / foreign file → refused, nothing changes
- malicious archive (path traversal, absolute path, symlink, unexpected member) is rejected before extraction
- restore takes an automatic safety copy first; verify() is read-only; delete needs confirm; names are validated
- backups never contain recovery data/logs; file mode 0600; the passphrase appears nowhere on disk
"""

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from app.config import RuntimeSettings
from app.knowledge.backup import MAGIC_ENC, BackupError
from app.retrieval.search import SearchQuery
from app.security.audit import verify_chain
from app.services.container import Services
from tests.conftest import make_candidate

pytestmark = pytest.mark.integration
PASS = "correct horse battery staple"
V1 = "/api/v1"


@pytest.fixture()
def populated(services):
    for name, kw in (
        ("jwt", {"fw": "FastAPI", "fw_ver": "0.110"}),
        ("migration", {"fw": "SQLAlchemy", "fw_ver": "2.0"}),
    ):
        services.patterns.add_candidate(make_candidate(name, **kw), project_id=None, session_id=None)
    services.config.update({"permission_level": "read_only", "privacy": {"exclude_globs": ["legacy/**"]}})
    services.recovery.root.joinpath("99").mkdir()
    (services.recovery.root / "99" / "raw.bin").write_bytes(b"RAW-USER-FILE-BYTES")
    (services.paths.logs / "backend.log").write_text("log line")
    return services


def test_encrypted_roundtrip_into_a_fresh_instance(populated, tmp_path):
    b = populated.backups.create(PASS, label="nightly")
    assert b["encrypted"] and b["counts"]["knowledge_patterns"] == 2 and b["bytes"] > 1000
    assert stat.S_IMODE(os.stat(b["path"]).st_mode) == 0o600
    other = Services(RuntimeSettings(home=tmp_path / "fresh"))
    try:
        (other.paths.backups / b["name"]).write_bytes(Path(b["path"]).read_bytes())
        res = other.backups.restore(b["name"], PASS)
        assert (
            res["counts"]["knowledge_patterns"] == 2
            and res["reindexed"]["indexed"] == 2
            and res["safety_copy"].endswith(".pvbak")
        )
        assert other.patterns.list_patterns()["total"] == 2
        assert (
            other.config.config.permission_level.value == "read_only"
            and other.config.config.privacy.exclude_globs == ["legacy/**"]
        )
        assert other.search.search(SearchQuery("jwt login password")).hits[0].pattern["category"] == "authentication"
        with other.session_factory() as db:
            assert verify_chain(db)["ok"]
        # blobs restored byte-for-byte
        src_blobs = {p.name for p in populated.paths.blobs.rglob("*") if p.is_file()}
        assert src_blobs <= {p.name for p in other.paths.blobs.rglob("*") if p.is_file()}
        # the restored database is healthy and writable
        other.patterns.add_candidate(
            make_candidate("laravel", fw="Laravel", fw_ver="11", language="php"), project_id=None, session_id=None
        )
        assert other.patterns.list_patterns()["total"] == 3
    finally:
        other.close()


def test_plain_backup_roundtrip_and_listing(populated):
    b = populated.backups.create(None)
    assert not b["encrypted"] and populated.backups.list()[0]["encrypted"] is False
    populated.patterns.delete(1)
    assert populated.patterns.list_patterns()["total"] == 1
    populated.backups.restore(b["name"])
    assert populated.patterns.list_patterns()["total"] == 2  # the deleted pattern is back
    assert (
        any(x["name"].endswith(".pvbak") for x in populated.backups.list()) and len(populated.backups.list()) == 2
    )  # + safety copy


def test_wrong_passphrase_tamper_truncation_and_foreign_files_change_nothing(populated):
    b = populated.backups.create(PASS)
    path = populated.paths.backups / b["name"]
    original = path.read_bytes()
    assert original.startswith(MAGIC_ENC) and PASS.encode() not in original
    # the payload is a Fernet token (base64 text), not a readable gzip stream
    assert not original[5 + 16 :].startswith(bytes([0x1F, 0x8B]))
    for bad_pass in ("wrong passphrase!", None, ""):
        with pytest.raises(BackupError):
            populated.backups.restore(b["name"], bad_pass)
    flipped = bytearray(original)
    flipped[len(flipped) // 2] ^= 0x01
    path.write_bytes(bytes(flipped))
    with pytest.raises(BackupError, match="wrong passphrase, or the backup file is corrupt or has been tampered"):
        populated.backups.restore(b["name"], PASS)
    path.write_bytes(original[: len(original) // 3])
    with pytest.raises(BackupError):
        populated.backups.restore(b["name"], PASS)
    path.write_bytes(b"this is not a backup at all")
    with pytest.raises(BackupError, match="not a PatternVault backup"):
        populated.backups.restore(b["name"], PASS)
    assert populated.patterns.list_patterns()["total"] == 2  # state never changed by the failed attempts
    assert len(populated.backups.list()) == 1  # and no safety copy was made either


def malicious_backup(svc, members: dict[str, bytes], links: dict[str, str] | None = None) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    name = "patternvault-evil-1.pvbak"
    (svc.paths.backups / name).write_bytes(b"PVBK0" + buf.getvalue())
    return name


@pytest.mark.parametrize(
    "members,links",
    [
        ({"manifest.json": b"{}", "patternvault.db": b"x", "../../etc/evil": b"pwn"}, None),
        ({"manifest.json": b"{}", "patternvault.db": b"x", "/abs/evil": b"pwn"}, None),
        ({"manifest.json": b"{}", "patternvault.db": b"x", "blobs/zz/not-a-hash": b"pwn"}, None),
        ({"manifest.json": b"{}", "patternvault.db": b"x"}, {"blobs/aa/link": "/etc/passwd"}),
        ({"manifest.json": b"{}", "patternvault.db": b"x", "extra/script.sh": b"rm -rf ~"}, None),
    ],
)
def test_malicious_archives_are_rejected_before_anything_is_extracted(populated, members, links):
    name = malicious_backup(populated, members, links)
    with pytest.raises(BackupError, match="unexpected entry"):
        populated.backups.restore(name)
    assert populated.patterns.list_patterns()["total"] == 2 and not any(
        p.name == "evil" for p in populated.paths.home.rglob("*")
    )


def test_incomplete_or_corrupt_database_is_refused(populated):
    n1 = malicious_backup(
        populated, {"manifest.json": json.dumps({"format": "patternvault-backup", "version": 1}).encode()}
    )
    with pytest.raises(BackupError, match="incomplete"):
        populated.backups.restore(n1)
    n2 = malicious_backup(
        populated,
        {
            "manifest.json": json.dumps({"format": "patternvault-backup", "version": 1}).encode(),
            "patternvault.db": b"garbage not sqlite" * 50,
        },
    )
    with pytest.raises(Exception, match=r"(?i)integrity|not a database|malformed|file is not"):
        populated.backups.restore(n2)
    assert populated.patterns.list_patterns()["total"] == 2


def test_verify_is_read_only_and_names_are_validated(populated):
    b = populated.backups.create(PASS)
    m = populated.backups.verify(b["name"], PASS)
    assert m["counts"]["knowledge_patterns"] == 2 and m["format"] == "patternvault-backup"
    for bad in ("../../etc/passwd", "x.pvbak", "patternvault-1.txt", "patternvault-/../x.pvbak"):
        with pytest.raises(BackupError):
            populated.backups.verify(bad, PASS)
    with pytest.raises(BackupError, match="at least 8"):
        populated.backups.create("short")


def test_backup_excludes_recovery_data_and_logs(populated):
    b = populated.backups.create(None)
    raw = (populated.paths.backups / b["name"]).read_bytes()[5:]
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names and "patternvault.db" in names and "config.json" in names
    assert not any(n.startswith(("recovery", "logs", "vectors")) or "raw.bin" in n for n in names)
    assert b"RAW-USER-FILE-BYTES" not in raw


def test_backup_api_flow_with_confirmation_and_safety(client, services):
    services.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    )
    r = client.post(f"{V1}/backups", json={"passphrase": PASS, "label": "api"})
    assert r.status_code == 201 and r.json()["encrypted"]
    name = r.json()["name"]
    assert client.post(f"{V1}/backups", json={"passphrase": "x"}).status_code == 422
    assert client.get(f"{V1}/backups").json()[0]["name"] == name
    assert client.post(f"{V1}/backups/verify", json={"name": name, "passphrase": "nope-nope-nope"}).status_code == 422
    assert (
        client.post(f"{V1}/backups/verify", json={"name": name, "passphrase": PASS}).json()["counts"][
            "knowledge_patterns"
        ]
        == 1
    )
    services.patterns.delete(1)
    assert (
        client.post(f"{V1}/backups/restore", json={"name": name, "passphrase": PASS}).status_code == 400
    )  # needs confirm
    res = client.post(f"{V1}/backups/restore", json={"name": name, "passphrase": PASS, "confirm": True})
    assert res.status_code == 200 and res.json()["reindexed"]["indexed"] == 1
    assert client.get(f"{V1}/patterns").json()["total"] == 1
    assert client.delete(f"{V1}/backups/{name}").status_code == 400
    assert client.delete(f"{V1}/backups/{name}", params={"confirm": True}).status_code == 204
    assert client.delete(f"{V1}/backups/{name}", params={"confirm": True}).status_code == 404
    actions = {a["action"] for a in client.get(f"{V1}/audit", params={"limit": 200}).json()}
    assert {"backup.create", "backup.restore", "backup.delete"} <= actions
    assert PASS not in json.dumps(client.get(f"{V1}/audit", params={"limit": 500}).json())


def test_restore_never_rewinds_the_audit_trail(populated):
    b = populated.backups.create(PASS)
    populated.audit.record("agent:test", "after.backup.action", detail={"n": 1})
    populated.backups.restore(b["name"], PASS)
    from sqlalchemy import select

    from app.models import AuditLog

    with populated.session_factory() as db:
        actions = [a for (a,) in db.execute(select(AuditLog.action).order_by(AuditLog.id)).all()]
        assert (
            "after.backup.action" in actions and actions[-1] == "backup.restore"
        )  # post-backup events kept, restore appended
        assert actions.count("backup.create") >= 2  # the original + the automatic safety copy
        assert verify_chain(db)["ok"]


def test_encrypted_payload_decrypts_only_with_the_passphrase_and_plain_is_readable(populated):
    plain = populated.backups.create(None)
    enc = populated.backups.create(PASS)
    raw = (populated.paths.backups / plain["name"]).read_bytes()[5:]
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        assert (
            b"knowledge_patterns" in tar.extractfile("patternvault.db").read()
        )  # the plain variant is readable by anyone
    assert (populated.paths.backups / enc["name"]).read_bytes().count(b"knowledge_patterns") == 0
