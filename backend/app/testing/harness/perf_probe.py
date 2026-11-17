"""Performance probe for PatternVault itself: indexing, hybrid search, parsing, extraction. Prints JSON metrics."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--patterns", type=int, default=200)
    a = ap.parse_args()
    sys.path[:0] = [os.path.join(a.root, "backend"), a.root]
    from pathlib import Path

    from app.config import RuntimeSettings
    from app.extraction.extractor import FileInput, SessionInput, extract_candidates
    from app.extraction.structure import extract_structure
    from app.retrieval.search import SearchQuery
    from app.services.container import Services

    svc = Services(RuntimeSettings(home=Path(tempfile.mkdtemp(prefix="pv-perf-"))))
    topics = [
        "authentication jwt token login",
        "database migration users table",
        "invoice pdf export report",
        "websocket notifications push",
        "payment stripe webhook",
        "inventory warehouse stock",
        "email smtp sending queue",
        "file upload s3 storage",
        "rate limit middleware",
        "cache redis ttl",
    ]
    t0 = time.perf_counter()
    for i in range(a.patterns):
        topic = topics[i % len(topics)]
        code = f"def handle_{i}(request):\n    # {topic}\n    value = request.get('{topic.split()[0]}')\n    return {{'id': {i}, 'v': value}}\n\n\ndef helper_{i}(x):\n    return x + {i}\n"
        cand = extract_candidates(
            SessionInput(
                prompts=[f"Implement {topic} number {i}"],
                files=[FileInput(f"app/feature_{i}.py", "created", code)],
                project_language="python",
            )
        )
        if cand:
            svc.patterns.add_candidate(cand[0], project_id=None, session_id=None)
    index_total = time.perf_counter() - t0
    lat = []
    for q in range(60):
        s = time.perf_counter()
        svc.search.search(SearchQuery(f"{topics[q % len(topics)]} handler"))
        lat.append((time.perf_counter() - s) * 1000)
    big = "\n".join(f"def f{i}(a, b):\n    return a + b + {i}\n" for i in range(1000))
    s = time.perf_counter()
    extract_structure("python", big)
    parse_ms = (time.perf_counter() - s) * 1000
    lat.sort()
    print(
        json.dumps(
            {
                "patterns": a.patterns,
                "index_per_pattern_ms": index_total / a.patterns * 1000,
                "search_p50_ms": statistics.median(lat),
                "search_p95_ms": lat[int(len(lat) * 0.95) - 1],
                "parse_2000_lines_ms": parse_ms,
            }
        )
    )
    svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
