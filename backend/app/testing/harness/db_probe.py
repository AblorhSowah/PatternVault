"""Database probe harness (runs in the target interpreter). Checks SQLite files and, for PatternVault-style projects,
the migration round trip: upgrade → ORM drift == 0 → integrity/FK → downgrade → upgrade."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile


def sqlite_checks(path: str) -> list[dict]:
    out = []
    uri = f"file:{path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        con.close()
    except sqlite3.Error as exc:
        return [
            {
                "name": f"{os.path.basename(path)}: opens read-only",
                "status": "error",
                "expected": "database opens",
                "actual": str(exc),
                "details": "",
            }
        ]
    out.append(
        {
            "name": f"{os.path.basename(path)}: integrity_check",
            "status": "passed" if integrity == ["ok"] else "failed",
            "expected": "ok",
            "actual": ";".join(integrity)[:200],
            "details": "",
        }
    )
    out.append(
        {
            "name": f"{os.path.basename(path)}: foreign_key_check",
            "status": "passed" if not fk else "failed",
            "expected": "no violations",
            "actual": f"{len(fk)} violation(s)",
            "details": str(fk[:5]),
        }
    )
    return out


def pv_migrations(root: str) -> list[dict]:
    sys.path[:0] = [os.path.join(root, "backend"), root]
    from app.database import engine as eng

    tmp = tempfile.mkdtemp(prefix="pv-dbprobe-")
    e = eng.create_db_engine(os.path.join(tmp, "probe.db"))
    cases = []

    def add(name, ok, expected, actual, details=""):
        cases.append(
            {
                "name": name,
                "status": "passed" if ok else "failed",
                "expected": expected,
                "actual": str(actual)[:300],
                "details": details[:500],
            }
        )

    try:
        eng.run_migrations(e)
        head = eng.head_revision()
        add(
            "migrations: upgrade to head on an empty database",
            eng.current_revision(e) == head,
            f"revision {head}",
            eng.current_revision(e),
        )
        drift = eng.schema_drift(e)
        add(
            "migrations: ORM metadata has no drift from the migrated schema",
            not drift,
            "no differences",
            f"{len(drift)} difference(s)",
            str(drift[:3]),
        )
        chk = eng.integrity_check(e)
        add(
            "database: integrity and foreign keys are clean after migration",
            chk["ok"],
            "ok",
            chk["integrity"],
            str(chk["foreign_key_violations"]),
        )
        eng.downgrade_migrations(e)
        from sqlalchemy import inspect

        left = set(inspect(e).get_table_names()) - {"alembic_version"}
        add("migrations: downgrade to base removes every table", not left, "no tables", sorted(left))
        eng.run_migrations(e)
        add(
            "migrations: re-upgrade after downgrade works",
            eng.current_revision(e) == head,
            f"revision {head}",
            eng.current_revision(e),
        )
    except Exception as exc:
        cases.append(
            {
                "name": "migrations: round trip",
                "status": "error",
                "expected": "completes",
                "actual": repr(exc)[:300],
                "details": "",
            }
        )
    finally:
        e.dispose()
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--patternvault", action="store_true")
    ap.add_argument("--sqlite", action="append", default=[])
    a = ap.parse_args()
    cases: list[dict] = []
    for p in a.sqlite:
        cases.extend(sqlite_checks(p))
    if a.patternvault:
        cases.extend(pv_migrations(a.root))
    print(json.dumps({"cases": cases}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
