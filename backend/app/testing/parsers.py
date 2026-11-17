"""Parsers turning tool output into :class:`TestCase` lists."""

from __future__ import annotations

import json
import re
from typing import Any

from defusedxml import ElementTree as SafeET  # hardened against entity-expansion/DTD attacks in hostile test output

from app.testing.report import TestCase


def parse_junit(xml_text: str) -> list[TestCase]:
    cases: list[TestCase] = []
    try:
        root = SafeET.fromstring(xml_text)
    except (SafeET.ParseError, ValueError):
        return cases
    for tc in root.iter("testcase"):
        name = f"{tc.get('classname', '')}::{tc.get('name', '')}".strip(":")
        t = int(float(tc.get("time", "0")) * 1000)
        fail = tc.find("failure")
        err = tc.find("error")
        skip = tc.find("skipped")
        if fail is not None or err is not None:
            node = fail if fail is not None else err
            assert node is not None
            msg = (node.get("message") or "")[:500]
            cases.append(
                TestCase(
                    name,
                    "failed" if fail is not None else "error",
                    description=f"{tc.get('file', '')}",
                    expected="test passes",
                    actual=msg,
                    error_details=(node.text or "")[:8000],
                    duration_ms=t,
                    file_path=tc.get("file"),
                )
            )
        elif skip is not None:
            cases.append(TestCase(name, "skipped", actual=(skip.get("message") or "")[:300], duration_ms=t))
        else:
            cases.append(TestCase(name, "passed", expected="test passes", actual="passed", duration_ms=t))
    return cases


def parse_vitest_json(text: str) -> list[TestCase]:
    try:
        data = json.loads(text[text.index("{") :])
    except (ValueError, json.JSONDecodeError):
        return []
    cases = []
    for suite in data.get("testResults", []):
        for a in suite.get("assertionResults", []):
            status = {
                "passed": "passed",
                "failed": "failed",
                "pending": "skipped",
                "skipped": "skipped",
                "todo": "skipped",
            }.get(a.get("status"), "error")
            msgs = "\n".join(a.get("failureMessages") or [])
            cases.append(
                TestCase(
                    a.get("fullName") or a.get("title", ""),
                    status,
                    description=suite.get("name", ""),
                    expected="test passes",
                    actual=status,
                    error_details=msgs[:8000],
                    duration_ms=int(a.get("duration") or 0),
                    file_path=suite.get("name"),
                )
            )
    return cases


def parse_ruff_json(text: str) -> list[TestCase]:
    try:
        items = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    by_file: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        by_file.setdefault(i["filename"], []).append(i)
    return [
        TestCase(
            f"ruff:{f}",
            "failed",
            description="lint",
            expected="no lint errors",
            actual=f"{len(v)} finding(s)",
            error_details="\n".join(f"{x['location']['row']}: {x['code']} {x['message']}" for x in v[:50]),
            file_path=f,
        )
        for f, v in sorted(by_file.items())
    ]


def parse_bandit_json(text: str) -> list[dict[str, Any]]:
    try:
        return list(json.loads(text).get("results", []))
    except (ValueError, json.JSONDecodeError):
        return []


_TSC = re.compile(r"^(?P<file>[^\s(]+)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.+)$", re.M)


def parse_tsc(text: str) -> list[TestCase]:
    return [
        TestCase(
            f"tsc:{m['file']}:{m['line']}",
            "failed",
            description="TypeScript compile",
            expected="no type errors",
            actual=f"{m['code']}: {m['msg']}"[:400],
            error_details=m.group(0),
            file_path=m["file"],
        )
        for m in _TSC.finditer(text)
    ]


def parse_eslint_json(text: str) -> list[TestCase]:
    try:
        items = json.loads(text[text.index("[") :])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for f in items:
        errs = [m for m in f.get("messages", []) if m.get("severity") == 2]
        if errs:
            out.append(
                TestCase(
                    f"eslint:{f['filePath']}",
                    "failed",
                    description="lint",
                    expected="no lint errors",
                    actual=f"{len(errs)} error(s)",
                    error_details="\n".join(
                        f"{m.get('line')}: {m.get('ruleId')} {m.get('message')}" for m in errs[:50]
                    ),
                    file_path=f["filePath"],
                )
            )
    return out


_COV = re.compile(r"^TOTAL\s+.*?(\d+(?:\.\d+)?)%\s*$", re.M)


def parse_pytest_cov(text: str) -> float | None:
    m = _COV.search(text)
    return float(m.group(1)) if m else None
