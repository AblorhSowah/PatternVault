"""Test plan: hybrid retrieval, context analysis, quality, portability (real SQLite/FTS5/vector store; Services-level + API)
Retrieval
- relevant pattern ranks first for natural-language queries; identifier & camelCase queries work
- verified outranks observed at equal relevance; deprecated hidden unless asked
- language/framework/version signals: boosts, mismatch penalties and human-readable warnings
- cross-project controls (project.share_knowledge, config allow_cross_project_retrieval)
- switching embedding model flags stale vectors; reindex fixes them
Context
- intent detection (replay / adapt / new / debug); missing-context report (no knowledge, vague, framework mismatch, unverified)
Quality
- duplicates / conflicts / outdated / recommendations; relations are audited + reversible; config & permissions untouched
Portability
- export → import into a fresh instance; evidence is not transferable (imported = observed); secrets re-redacted; bad files rejected
"""

import json

import pytest

from app.config import RuntimeSettings
from app.knowledge.portability import ImportError_
from app.models import Project
from app.retrieval.context import detect_intent, request_signals
from app.retrieval.search import SearchQuery
from app.services.container import Services
from tests.conftest import FAKE_AWS, make_candidate, make_testing_session_for

pytestmark = pytest.mark.integration
V1 = "/api/v1"


def top(svc, text, **kw):
    return svc.search.search(SearchQuery(text, **kw))


# ------------------------------------------------------------------ retrieval
@pytest.mark.parametrize(
    "query,expected",
    [
        ("add login with JWT tokens and password hashing", "jwt"),
        ("session based login using flask-login", "session"),
        ("create database tables for users and orders", "migration"),
        ("inventory warehouse controller", "laravel"),
        ("verify_password", "jwt"),
        ("CreateAccessToken", "jwt"),
        ("WarehouseController validate", "laravel"),
    ],
)
def test_relevant_pattern_ranks_first(kb, query, expected):
    svc, ids = kb
    res = top(svc, query)
    assert res.hits and res.hits[0].pattern_id == ids[expected], [(h.pattern_id, h.score) for h in res.hits]
    assert res.hits[0].reasons and res.embedder.startswith("hashing")


def test_nonsense_query_returns_nothing(kb):
    assert top(kb[0], "kubernetes helm chart rollout canary").hits == []
    assert top(kb[0], "   ").hits == []


def test_trust_reranks_and_deprecated_hidden(kb):
    svc, ids = kb
    # make an equally relevant duplicate-ish "observed" session pattern vs verified jwt: verify jwt
    ts = make_testing_session_for(svc)
    svc.patterns.approve(ids["jwt"])
    svc.patterns.promote(ids["jwt"], "tested", ts)
    svc.patterns.record_use(ids["jwt"], success=True)
    svc.patterns.promote(ids["jwt"], "verified", ts)
    res = top(svc, "login authentication password")
    ranked = [h.pattern_id for h in res.hits]
    assert ranked[0] == ids["jwt"] and res.hits[0].trust == 1.0
    svc.patterns.deprecate(ids["session"])
    assert ids["session"] not in [h.pattern_id for h in top(svc, "login authentication password").hits]
    assert ids["session"] in [
        h.pattern_id for h in top(svc, "login authentication password", include_deprecated=True).hits
    ]
    assert not any("not tested or verified" in n for n in top(svc, "login authentication password").notes)


def test_untrusted_top_result_noted(kb):
    assert any("tested or verified" in n for n in top(kb[0], "warehouse controller").notes)


def test_language_framework_version_signals(kb):
    svc, ids = kb
    res = top(svc, "warehouse controller validation", language="php", framework="Laravel", framework_version="11.2")
    h = res.hits[0]
    assert h.pattern_id == ids["laravel"] and any("framework Laravel matches" in r for r in h.reasons)
    assert any("learned on Laravel 9, project uses 11" in w for w in h.warnings)
    wrong = top(svc, "warehouse controller validation", language="python", framework="FastAPI")
    laravel = next(h for h in wrong.hits if h.pattern_id == ids["laravel"])
    assert any("written for php" in w for w in laravel.warnings) and laravel.score < h.score


def test_filters(kb):
    svc, ids = kb
    only_auth = top(svc, "login users table", categories=["authentication"])
    assert {h.pattern["category"] for h in only_auth.hits} == {"authentication"}
    assert top(svc, "login", statuses=["verified"]).hits == []


def test_cross_project_controls(kb):
    svc, ids = kb
    with svc.session_factory() as db:
        a, b = Project(name="a", root_path="/a"), Project(name="b", root_path="/b", share_knowledge=False)
        db.add_all([a, b])
        db.commit()
        aid, bid = a.id, b.id
    private = svc.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="1.0", tweak="\n\ndef extra_helper():\n    return 1\n"),
        project_id=bid,
        session_id=None,
    ).pattern_id
    assert private not in [h.pattern_id for h in top(svc, "jwt login password", project_id=aid).hits]  # b doesn't share
    assert private in [
        h.pattern_id for h in top(svc, "jwt login password", project_id=bid).hits
    ]  # own project always visible
    svc.config.update({"privacy": {"allow_cross_project_retrieval": False}})
    with svc.session_factory() as db:
        db.get(Project, bid).share_knowledge = True
        db.commit()
    assert private not in [
        h.pattern_id for h in top(svc, "jwt login password", project_id=aid).hits
    ]  # global switch off
    assert private in [
        h.pattern_id for h in top(svc, "jwt login password", project_id=bid).hits
    ]  # own project still visible


def test_embedding_model_switch_flags_stale_then_reindex(kb, ollama_double=None):
    svc, ids = kb
    assert svc.indexer.stale() == []
    svc.vector_store.clear()
    assert len(svc.indexer.stale()) == 4
    # vectors that exist only under another model name (e.g. the user switched embedding models)
    import numpy as np

    svc.vector_store.upsert("p:1", np.ones(8, dtype=np.float32), "some-other-model", {"pattern_id": 1})
    assert any("different embedding model" in n for n in top(svc, "login jwt password").notes)
    out = svc.indexer.reindex_all()
    assert out["indexed"] == 4 and svc.indexer.stale() == []
    assert not any("different embedding model" in n for n in top(svc, "login jwt password").notes)


def test_vector_removed_on_delete(kb):
    svc, ids = kb
    before = svc.vector_store.count()
    svc.patterns.delete(ids["session"])
    assert svc.vector_store.count() == before - 1


# ------------------------------------------------------------------ context
@pytest.mark.parametrize(
    "text,intent",
    [
        ("Recreate the authentication implementation I previously used", "replay"),
        ("Use the inventory architecture we built earlier but extend it to support multiple warehouses", "adapt"),
        ("Add a CSV export endpoint to the reports API", "new"),
        ("Fix the failing login test, it raises KeyError", "debug"),
    ],
)
def test_intent_detection(text, intent):
    assert detect_intent(text) == intent


def test_request_signals():
    s = request_signals("Port app/auth.py from Laravel to python using FastAPI, we used it before")
    assert (
        s["languages"] == ["python"]
        and set(s["frameworks"]) == {"FastAPI", "Laravel"}
        and s["paths"] == ["app/auth.py"]
    )
    assert s["refers_to_prior_work"] is True


def test_context_report_missing_items(kb):
    svc, ids = kb
    with svc.session_factory() as db:
        p = Project(
            name="shop",
            root_path="/s",
            frameworks=[{"name": "FastAPI", "version": "0.110.0", "ecosystem": "pypi", "dev": False}],
            conventions={"primary_language": "python", "analyzed_at": "x", "test_locations": ["tests"]},
        )
        db.add(p)
        db.commit()
        pid = p.id
    r = svc.context.analyze("Recreate the JWT authentication login I previously used", pid)
    d = r.to_dict()
    assert d["intent"] == "replay" and d["project"]["framework"] == "FastAPI"
    assert d["hits"][0]["pattern_id"] == ids["jwt"]
    kinds = {m["kind"] for m in d["missing"]}
    assert "unverified-knowledge" in kinds and "no-knowledge" not in kinds
    bad = svc.context.analyze("Recreate the previous kubernetes operator I wrote", pid).to_dict()
    nk = next(m for m in bad["missing"] if m["kind"] == "no-knowledge")
    assert nk["severity"] == "blocker"
    vague = svc.context.analyze("fix it", None).to_dict()
    assert {"project", "vague-request"} <= {m["kind"] for m in vague["missing"]}
    mismatch = svc.context.analyze("Add a Laravel controller for warehouses with validation", pid).to_dict()
    assert any(m["kind"] == "framework-mismatch" for m in mismatch["missing"])


# ------------------------------------------------------------------ quality
def test_quality_duplicates_conflicts_outdated(kb):
    svc, ids = kb
    near = svc.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110.0", tweak="\n\ndef helper():\n    return 1\n"),
        project_id=None,
        session_id=None,
    )
    assert near.action == "created" and near.pattern_id != ids["jwt"]  # structure differs → separate pattern
    with svc.session_factory() as db:
        db.add(
            Project(
                name="p",
                root_path="/p",
                frameworks=[{"name": "Laravel", "version": "11.0", "ecosystem": "composer", "dev": False}],
            )
        )
        db.commit()
    cfg_before = svc.config.config.model_dump()
    rep = svc.quality.analyze()
    assert {"pattern_id": near.pattern_id, "duplicate_of": ids["jwt"]} == {
        k: rep["duplicates"][0][k] for k in ("pattern_id", "duplicate_of")
    }
    conflict_pairs = {frozenset((c["a"], c["b"])) for c in rep["conflicts"]}
    assert (
        frozenset((ids["jwt"], ids["session"])) not in conflict_pairs
    )  # different frameworks → not competing in the same stack
    assert any(o["pattern_id"] == ids["laravel"] and "now use 11" in o["reasons"][0] for o in rep["outdated"])
    kinds = {r["kind"] for r in rep["recommendations"]}
    assert {"merge", "refresh"} <= kinds
    assert svc.config.config.model_dump() == cfg_before  # quality analysis never touches configuration/permissions
    with svc.session_factory() as db:
        from sqlalchemy import select

        from app.models import PatternRelationship

        rels = db.scalars(select(PatternRelationship)).all()
        assert any(r.relation == "duplicate_of" and r.source_id == near.pattern_id for r in rels)
        rid = rels[0].id
    assert svc.quality.remove_relation(rid) and not svc.quality.remove_relation(rid)  # reversible


def test_quality_conflicting_approaches_same_stack(services):
    a = services.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    ).pattern_id
    sess = make_candidate("session", fw="FastAPI", fw_ver="0.110")  # session login in the same framework
    b = services.patterns.add_candidate(sess, project_id=None, session_id=None).pattern_id
    rep = services.quality.analyze(apply_relations=False)
    assert any({c["a"], c["b"]} == {a, b} and "flask-login" in c["reason"].lower() for c in rep["conflicts"]), rep[
        "conflicts"
    ]


def test_quality_recommends_verification_after_reuse(kb):
    svc, ids = kb
    svc.patterns.record_use(ids["jwt"], success=True)
    svc.patterns.record_use(ids["jwt"], success=True)
    rep = svc.quality.analyze(apply_relations=False)
    assert any(r["kind"] == "verify" and r["pattern_id"] == ids["jwt"] for r in rep["recommendations"])


# ------------------------------------------------------------------ portability
def test_export_import_roundtrip_demotes_status(kb, tmp_path):
    svc, ids = kb
    ts = make_testing_session_for(svc)
    svc.patterns.approve(ids["jwt"])
    svc.patterns.promote(ids["jwt"], "tested", ts)
    svc.patterns.relate(ids["jwt"], ids["session"], "related_to", 0.4)
    doc = svc.portability.export()
    assert doc["format"] == "patternvault-export" and len(doc["patterns"]) == 4
    assert (
        next(p for p in doc["patterns"] if p["uid"] == svc.patterns.get(ids["jwt"], False)["uid"])["status"] == "tested"
    )
    other = Services(RuntimeSettings(home=tmp_path / "other"))
    try:
        res = other.portability.import_(json.dumps(doc))
        assert res == {"created": 4, "skipped": 0, "total": 4}
        listed = other.patterns.list_patterns()
        assert listed["total"] == 4 and {p["status"] for p in listed["items"]} == {
            "observed"
        }  # evidence is not transferable
        assert all(p["provenance"].get("imported") for p in listed["items"])
        assert other.search.search(SearchQuery("jwt login password")).hits  # searchable immediately
        assert other.portability.import_(doc) == {"created": 0, "skipped": 4, "total": 4}  # idempotent
        assert other.patterns.get(1)["provenance"]["imported_status"] in {"tested", "observed"}
    finally:
        other.close()


def test_export_redacts_secrets_defensively(kb):
    svc, ids = kb
    with svc.session_factory() as db:
        from app.models import PatternVersion

        v = db.query(PatternVersion).first()
        v.content = {**v.content, "request": f"use key {FAKE_AWS}"}  # simulate a secret that slipped in
        db.commit()
    text = json.dumps(svc.portability.export())
    assert FAKE_AWS not in text and "[REDACTED:aws-access-key-id]" in text


@pytest.mark.parametrize(
    "payload,msg",
    [
        ("{nope", "not valid JSON"),
        ({"format": "other", "version": 1, "exported_at": "x", "patterns": []}, "unsupported format"),
        ({"format": "patternvault-export", "version": 99, "exported_at": "x", "patterns": []}, "unsupported format"),
        (
            {"format": "patternvault-export", "version": 1, "exported_at": "x", "patterns": [{"uid": "short"}]},
            "invalid export file",
        ),
        ({"nothing": 1}, "invalid export file"),
    ],
)
def test_import_rejects_bad_files(services, payload, msg):
    with pytest.raises(ImportError_, match=msg):
        services.portability.import_(payload)


# ------------------------------------------------------------------ API
def test_api_search_context_and_knowledge(client, services):
    for name, kw in (
        ("jwt", {"fw": "FastAPI", "fw_ver": "0.110"}),
        ("migration", {"fw": "SQLAlchemy", "fw_ver": "2.0"}),
    ):
        services.patterns.add_candidate(make_candidate(name, **kw), project_id=None, session_id=None)
    r = client.post(f"{V1}/search", json={"query": "jwt login", "top_k": 3}).json()
    assert r["hits"][0]["pattern"]["category"] == "authentication" and r["hits"][0]["reasons"]
    assert client.post(f"{V1}/search", json={"query": "x" * 3000}).status_code == 422
    ctx = client.post(f"{V1}/context", json={"request": "Recreate the JWT login I used before"}).json()
    assert ctx["intent"] == "replay" and ctx["hits"]
    st = client.get(f"{V1}/knowledge/stats").json()
    assert (
        st["facets"]["statuses"] == {"observed": 2}
        and st["stale_vectors"] == 0
        and st["embedder"].startswith("hashing")
    )
    assert client.post(f"{V1}/knowledge/reindex").json()["indexed"] == 2
    q = client.post(f"{V1}/knowledge/quality").json()
    assert q["patterns_analyzed"] == 2
    exp = client.get(f"{V1}/knowledge/export").json()
    assert len(exp["patterns"]) == 2
    assert client.post(f"{V1}/knowledge/import", json=exp).json()["skipped"] == 2
    assert client.post(f"{V1}/knowledge/import", json={"bad": 1}).status_code == 422
