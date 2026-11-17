"""Source hygiene: no invisible/bidirectional Unicode characters in first-party source (Trojan Source, CVE-2021-42574).

The character class is built from numeric code points so this file contains none of those characters itself.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.security
ROOT = Path(__file__).resolve().parents[2]
_RANGES = ((0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0xFEFF, 0xFEFF), (0xE0000, 0xE007F))
BAD = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _RANGES) + "]")
SOURCES = (("backend", "py"), ("tests", "py"), ("vscode-extension/src", "ts"), ("vscode-extension/test", "ts"))


def test_no_invisible_or_bidi_characters_in_sources():
    offenders = []
    for base, ext in SOURCES:
        for p in (ROOT / base).rglob(f"*.{ext}"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            if BAD.search(text):
                offenders.append(str(p.relative_to(ROOT)))
    assert offenders == []


def test_detector_itself_works():
    assert BAD.search("a" + chr(0x202E) + "b") and BAD.search(chr(0x200B)) and not BAD.search("plain ascii text")
