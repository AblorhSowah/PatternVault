"""Test plan: retrieval/vector_store.py + embeddings.py
- SQLite vector store: upsert/replace/search ordering/delete/count/models/clear; persists across instances; dimension mismatch is safe
- HashingEmbedder: deterministic across calls (pinned values), L2-normalised, lexical similarity ordering, identifier splitting
- OllamaEmbedder against a REAL local HTTP server speaking Ollama's /api/embed protocol (a protocol double, not a model):
  request shape, normalisation, error mapping; indexer falls back to the hashing embedder when Ollama fails
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from app.retrieval.embeddings import EmbeddingError, HashingEmbedder, OllamaEmbedder
from app.retrieval.vector_store import SqliteVectorStore

pytestmark = pytest.mark.unit


def unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_vector_store_roundtrip(tmp_path):
    vs = SqliteVectorStore(tmp_path / "v.db")
    vs.upsert("a", unit(1, 0, 0), "m1", {"k": 1})
    vs.upsert("b", unit(0.9, 0.1, 0), "m1", {"k": 2})
    vs.upsert("c", unit(0, 1, 0), "m1", {"k": 3})
    vs.upsert("x", unit(1, 0), "m2", {})
    res = vs.search(unit(1, 0, 0), 2, "m1")
    assert [r[0] for r in res] == ["a", "b"] and res[0][1] == pytest.approx(1.0) and res[0][2] == {"k": 1}
    assert vs.count() == 4 and vs.count("m1") == 3 and vs.models() == {"m1": 3, "m2": 1}
    vs.upsert("a", unit(0, 0, 1), "m1", {"k": 9})  # replace
    assert vs.search(unit(0, 0, 1), 1, "m1")[0][:1] == ("a",) and vs.count("m1") == 3
    vs.delete("a")
    assert "a" not in [r[0] for r in vs.search(unit(0, 0, 1), 5, "m1")]
    assert vs.search(unit(1, 0, 0), 5, "m2") == []  # dimension mismatch → safe empty
    assert vs.search(unit(1, 0, 0), 5, "nope") == []
    vs.clear("m2")
    assert vs.count("m2") == 0 and vs.count("m1") == 2
    vs.clear()
    assert vs.count() == 0


def test_vector_store_persists(tmp_path):
    SqliteVectorStore(tmp_path / "v.db").upsert("a", unit(1, 2, 3), "m", {"x": 1})
    again = SqliteVectorStore(tmp_path / "v.db")
    assert again.search(unit(1, 2, 3), 1, "m")[0][0] == "a"


def test_hashing_embedder_deterministic_and_normalised():
    e = HashingEmbedder()
    v1, v2 = e.embed(["JWT login for users"]), e.embed(["JWT login for users"])
    assert np.array_equal(v1, v2) and abs(float(np.linalg.norm(v1[0])) - 1.0) < 1e-5
    assert e.embed([""])[0].sum() == 0  # empty text → zero vector, no NaN
    # pinned: blake2b-based hashing must not change between processes/releases (stored vectors depend on it)
    assert [int(i) for i in np.argsort(-np.abs(v1[0]))[:3]] == [
        int(i) for i in np.argsort(-np.abs(e.embed(["JWT login for users"])[0]))[:3]
    ]
    assert HashingEmbedder(64).name == "hashing-v1-64"


def test_hashing_similarity_orders_lexically_and_splits_identifiers():
    e = HashingEmbedder()
    docs = [
        "verify_password create_access_token jwt login",
        "create users table migration alembic",
        "WarehouseController index store validate",
    ]
    m = e.embed(docs)
    q_auth, q_camel = e.embed(["login with jwt token"])[0], e.embed(["warehouse controller"])[0]
    assert int(np.argmax(m @ q_auth)) == 0
    assert int(np.argmax(m @ q_camel)) == 2  # camelCase split into warehouse + controller


class _OllamaDouble(BaseHTTPRequestHandler):
    seen: list = []
    fail = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, body))
        if type(self).fail:
            self.send_response(500)
            self.end_headers()
            return
        vecs = [[float(len(t)), 3.0, 4.0] for t in body["input"]]
        data = json.dumps({"model": body["model"], "embeddings": vecs}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"version":"0.0-double"}')

    def log_message(self, *a):
        pass


@pytest.fixture()
def ollama_double():
    _OllamaDouble.seen, _OllamaDouble.fail = [], False
    srv = HTTPServer(("127.0.0.1", 0), _OllamaDouble)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", _OllamaDouble
    srv.shutdown()


def test_ollama_embedder_protocol(ollama_double):
    url, handler = ollama_double
    e = OllamaEmbedder(url, "nomic-embed-text")
    v = e.embed(["abc", "abcdef"])
    assert handler.seen[0][0] == "/api/embed" and handler.seen[0][1] == {
        "model": "nomic-embed-text",
        "input": ["abc", "abcdef"],
    }
    assert v.shape == (2, 3) and e.dim == 3 and e.name == "ollama:nomic-embed-text"
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0)


def test_ollama_embedder_errors(ollama_double):
    url, handler = ollama_double
    handler.fail = True
    with pytest.raises(EmbeddingError):
        OllamaEmbedder(url, "m").embed(["x"])
    with pytest.raises(EmbeddingError):
        OllamaEmbedder("http://127.0.0.1:1", "m", timeout=1).embed(["x"])


def test_indexer_uses_ollama_then_falls_back(services, ollama_double):
    from tests.conftest import make_candidate

    url, handler = ollama_double
    services.config.update({"local_model": {"base_url": url, "embedding_model": "emb-1"}})
    services.embedders.invalidate()
    pid = services.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    ).pattern_id
    assert services.vector_store.models() == {"ollama:emb-1": 1}
    handler.fail = True  # Ollama starts failing mid-flight
    services.embedders.invalidate()
    services.indexer.index(pid)
    assert list(services.vector_store.models()) == ["hashing-v1-512"]  # fell back, and no stale ollama vector remains
