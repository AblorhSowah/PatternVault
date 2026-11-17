"""Embedders. ``HashingEmbedder`` works fully offline (lexical); ``OllamaEmbedder`` gives neural embeddings."""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections import Counter
from typing import Protocol

import httpx
import numpy as np

from app.knowledge.fts import split_identifier

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    name: str
    dim: int
    min_similarity: float  # cosine below this is noise for this embedder and is ignored by search

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (n, dim) float32 matrix of L2-normalised vectors."""


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in _WORD.findall(text):
        parts = split_identifier(tok)
        low = tok.lower()
        out.append(low)
        if len(parts) > 1:
            out.extend(parts)
    return out


class HashingEmbedder:
    """Signed feature hashing of identifier-aware tokens, adjacent-token bigrams and character trigrams.

    Deterministic across processes (blake2b, not Python's randomised ``hash``). It captures *lexical* similarity,
    not meaning - documented limitation (ADR-005).
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = f"hashing-v1-{dim}"
        self.min_similarity = 0.15  # hash-collision noise sits around 0.0-0.05; true lexical overlap is well above

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        return h % self.dim, 1.0 if (h >> 63) & 1 else -1.0

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            toks = _tokens(text[:100_000])
            feats: Counter[str] = Counter(toks)
            for a, b in itertools.pairwise(toks):
                feats[f"{a} {b}"] += 0.5  # type: ignore[assignment]
            for t in set(toks):
                if len(t) >= 5:
                    for j in range(len(t) - 2):
                        feats[f"#{t[j : j + 3]}"] += 0.15  # type: ignore[assignment]
            for f, c in feats.items():
                idx, sign = self._bucket(f)
                out[i, idx] += sign * (1.0 + math.log(c)) if c >= 1 else sign * c
            n = float(np.linalg.norm(out[i]))
            if n > 0:
                out[i] /= n
        return out


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.timeout = timeout
        self.dim = 0  # learned on first call
        self.min_similarity = 0.30

    def embed(self, texts: list[str]) -> np.ndarray:
        try:
            r = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": [t[:8000] for t in texts]},
                timeout=self.timeout,
            )
            r.raise_for_status()
            vecs = np.array(r.json()["embeddings"], dtype=np.float32)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise EmbeddingError(f"ollama embedding failed: {exc}") from exc
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self.dim = int(vecs.shape[1])
        out: np.ndarray = vecs / norms
        return out
