"""OPT-IN live test against a REAL Ollama + real model. Enabled only when PV_OLLAMA_URL is set, e.g.

    PV_OLLAMA_URL=http://127.0.0.1:11434 PV_OLLAMA_MODEL=qwen2.5-coder:1.5b pytest tests/e2e/test_live_ollama.py -m live

Skipped (visibly) otherwise: PatternVault never claims real-model verification it has not performed.
"""

import os

import httpx
import pytest
from tests.conftest import run_git

pytestmark = pytest.mark.live
URL = os.environ.get("PV_OLLAMA_URL")
MODEL = os.environ.get("PV_OLLAMA_MODEL", "")


@pytest.mark.skipif(not URL, reason="set PV_OLLAMA_URL to run against a real Ollama")
def test_real_model_generates_parsable_code(client, services, tmp_path):
    tags = httpx.get(f"{URL}/api/tags", timeout=10).json()["models"]
    model = MODEL or (tags[0]["name"] if tags else "")
    assert model, "no model installed in the target Ollama"
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app/main.py").write_text("def add(a, b):\n    return a + b\n")
    run_git(root, "init", "-q", "-b", "main")
    pid = client.post("/api/v1/projects", json={"root_path": str(root)}).json()["id"]
    client.post(f"/api/v1/projects/{pid}/analyze")
    services.config.update(
        {"mode": "offline", "local_model": {"base_url": URL, "model": model, "request_timeout_s": 900}}
    )
    assert client.post("/api/v1/models/test").json()["ok"] is True
    r = client.post(
        "/api/v1/generate",
        json={
            "request": "Add a function multiply(a, b) to app/main.py and a pytest test for add and multiply in tests/test_main.py",
            "project_id": pid,
        },
    )
    assert r.status_code == 200, r.text
    files = r.json()["result"]["files"]
    assert files, "the real model produced no parsable files"
    assert not [i for f in files for i in f["issues"] if i["level"] == "error" and "syntax" in i["message"]]
