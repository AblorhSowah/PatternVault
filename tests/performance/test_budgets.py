"""Performance budgets (generous, to avoid CI flakiness) - the numbers are printed so regressions are visible in the log.

Large-repository behaviour: analysis/capture must stream files (bounded memory), never load a project wholesale.
Run with:  pytest tests/performance -m performance -s
"""

import statistics
import time
import tracemalloc
from pathlib import Path

import numpy as np
import pytest

from app.extraction.analyzer import analyze_project
from app.extraction.extractor import FileInput, SessionInput, extract_candidates
from app.extraction.structure import extract_structure
from app.learning.blobs import BlobStore
from app.learning.workspace import compute_changes, snapshot_project
from app.retrieval.search import SearchQuery
from app.retrieval.vector_store import SqliteVectorStore
from app.security.exclusions import ExclusionPolicy
from app.security.secrets import redact

pytestmark = pytest.mark.performance


def timed(fn, *a, **k):
    t0 = time.perf_counter()
    out = fn(*a, **k)
    return out, time.perf_counter() - t0


TOPICS = [
    "authentication jwt token",
    "database migration users",
    "invoice pdf report",
    "websocket notification",
    "stripe payment webhook",
    "warehouse inventory stock",
    "email smtp queue",
    "file upload storage",
]


def test_index_and_search_one_thousand_patterns(services):
    t0 = time.perf_counter()
    for i in range(1000):
        topic = TOPICS[i % len(TOPICS)]
        code = f"def handle_{i}(request):\n    # {topic}\n    return {{'v': request.get('{topic.split()[0]}'), 'n': {i}}}\n\n\ndef helper_{i}(x):\n    return x + {i}\n"
        c = extract_candidates(
            SessionInput(
                prompts=[f"Implement {topic} #{i}"],
                files=[FileInput(f"app/f_{i}.py", "created", code)],
                project_language="python",
            )
        )
        services.patterns.add_candidate(c[0], project_id=None, session_id=None)
    build = time.perf_counter() - t0
    lat = []
    for q in range(100):
        t = time.perf_counter()
        res = services.search.search(SearchQuery(f"{TOPICS[q % len(TOPICS)]} handler"))
        lat.append((time.perf_counter() - t) * 1000)
        assert res.hits
    p50, p95 = statistics.median(lat), sorted(lat)[94]
    print(
        f"\n[perf] index 1000 patterns: {build:.1f}s ({build / 1000 * 1000:.1f} ms/pattern) · search p50 {p50:.1f} ms · p95 {p95:.1f} ms"
    )
    assert build / 1000 < 0.25 and p95 < 400
    listed, took = timed(services.patterns.list_patterns, q="authentication", limit=50)
    print(f"[perf] FTS list with filters: {took * 1000:.1f} ms")
    assert listed["total"] > 0 and took < 0.5


def test_vector_store_scales_to_twenty_thousand_vectors(tmp_path):
    vs = SqliteVectorStore(tmp_path / "v.db")
    rng = np.random.default_rng(0)
    mat = rng.normal(size=(20_000, 512)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    t0 = time.perf_counter()
    import sqlite3

    con = sqlite3.connect(tmp_path / "v.db")
    con.executemany(
        "INSERT INTO vectors(key, model, dim, vec, meta) VALUES (?,?,?,?,?)",
        [(f"p:{i}", "m", 512, mat[i].tobytes(), "{}") for i in range(20_000)],
    )
    con.commit()
    con.close()
    load = time.perf_counter() - t0
    q = mat[123]
    first, t_first = timed(vs.search, q, 10, "m")  # includes loading the matrix into the cache
    lat = []
    for _ in range(30):
        _, t = timed(vs.search, q, 10, "m")
        lat.append(t * 1000)
    print(
        f"\n[perf] 20k vectors: bulk insert {load:.1f}s · first search {t_first * 1000:.0f} ms · cached p95 {sorted(lat)[28]:.1f} ms"
    )
    assert first[0][0] == "p:123" and sorted(lat)[28] < 100 and t_first < 5


def test_large_repository_analysis_and_capture_stay_bounded(tmp_path):
    root = tmp_path / "big"
    for d in range(60):
        pkg = root / f"pkg{d}"
        pkg.mkdir(parents=True)
        for f in range(50):
            (pkg / f"mod_{f}.py").write_text(
                f"def fn_{d}_{f}(a, b):\n    return a + b + {f}\n\n\nclass C{d}_{f}:\n    def m(self):\n        return {d}\n"
            )
    (root / "requirements.txt").write_text("fastapi==0.110.0\n")
    assert sum(1 for _ in root.rglob("*.py")) == 3000
    policy = ExclusionPolicy()
    tracemalloc.start()
    analysis, t_an = timed(analyze_project, root, policy)
    _, peak_an = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    blobs = BlobStore(tmp_path / "blobs")
    base, t_snap = timed(snapshot_project, root, policy, blobs)
    for i in range(10):
        (root / "pkg0" / f"mod_{i}.py").write_text(f"def changed_{i}():\n    return {i}\n")
    changes, t_diff = timed(compute_changes, root, base, policy, blobs)
    _, peak_cap = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(
        f"\n[perf] 3000-file repo: analyze {t_an:.1f}s (peak {peak_an / 2**20:.0f} MB) · baseline {t_snap:.1f}s · diff {t_diff:.1f}s (peak {peak_cap / 2**20:.0f} MB)"
    )
    assert analysis.file_count >= 3000 and len(changes) == 10
    assert t_an < 20 and t_snap < 30 and t_diff < 30
    assert peak_an < 300 * 2**20 and peak_cap < 400 * 2**20  # never the whole project in memory at once


def test_parser_and_redactor_throughput():
    big = "\n".join(f"def f{i}(a, b):\n    return a + b + {i}\n" for i in range(2500))
    _, t_parse = timed(extract_structure, "python", big)
    text = "\n".join(f"line {i}: value = {i} and token_count = {i}" for i in range(50_000))
    _, t_red = timed(redact, text)
    print(f"\n[perf] parse 5000 lines {t_parse * 1000:.0f} ms · redact 50k lines {t_red:.2f} s")
    assert t_parse < 1.0 and t_red < 5.0


def test_api_dashboard_latency_with_data(api, auth_project):
    t_list = []
    for _ in range(30):
        _, t = timed(api.get, f"{V1}/dashboard")
        t_list.append(t * 1000)
    print(f"\n[perf] /dashboard p95 {sorted(t_list)[28]:.0f} ms (includes a local-model probe)")
    assert sorted(t_list)[28] < 1500 and Path(auth_project).exists()


V1 = "/api/v1"
