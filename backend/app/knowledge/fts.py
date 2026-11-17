"""SQLite FTS5 keyword index for patterns (BM25)."""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")
STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "from",
        "by",
        "is",
        "are",
        "be",
        "this",
        "that",
        "it",
        "as",
        "at",
        "we",
        "you",
        "i",
        "my",
        "our",
        "your",
        "add",
        "make",
        "create",
        "use",
        "using",
        "new",
        "please",
        "can",
        "could",
        "should",
        "would",
        "want",
        "need",
        "implement",
        "build",
        "write",
    ]
)


def split_identifier(token: str) -> list[str]:
    """camelCase / snake_case → words, so ``AuthController`` is findable by "auth" and "controller"."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token).replace("_", " ")
    return [w.lower() for w in spaced.split() if len(w) >= 2]


def tokenize_query(query: str) -> list[str]:
    words: list[str] = []
    for tok in _TOKEN.findall(query):
        for w in [tok.lower(), *split_identifier(tok)]:
            if w not in STOPWORDS and w not in words:
                words.append(w)
    return words[:24]


def build_match(query: str) -> str | None:
    words = tokenize_query(query)
    if not words:
        return None
    return " OR ".join(f'"{w}"*' if len(w) >= 3 else f'"{w}"' for w in words)


def index_pattern(db: Session, pattern_id: int, title: str, summary: str, tags: str, body: str) -> None:
    db.execute(text("DELETE FROM pattern_fts WHERE pattern_id = :id"), {"id": pattern_id})
    expanded = " ".join(w for tok in _TOKEN.findall(body) for w in split_identifier(tok))
    db.execute(
        text("INSERT INTO pattern_fts(pattern_id, title, summary, tags, body) VALUES (:id, :t, :s, :g, :b)"),
        {"id": pattern_id, "t": title, "s": summary, "g": tags, "b": body[:200_000] + "\n" + expanded[:50_000]},
    )


def remove_pattern(db: Session, pattern_id: int) -> None:
    db.execute(text("DELETE FROM pattern_fts WHERE pattern_id = :id"), {"id": pattern_id})


def search(db: Session, query: str, limit: int = 50) -> list[tuple[int, float]]:
    """Return ``(pattern_id, bm25)`` best-first. bm25 is negative-better in SQLite; we flip the sign."""
    match = build_match(query)
    if match is None:
        return []
    rows = db.execute(
        text(
            "SELECT pattern_id, bm25(pattern_fts, 0, 6.0, 3.0, 2.0, 1.0) AS s FROM pattern_fts "
            "WHERE pattern_fts MATCH :m ORDER BY s LIMIT :n"
        ),
        {"m": match, "n": limit},
    ).fetchall()
    return [(int(r[0]), -float(r[1])) for r in rows]
