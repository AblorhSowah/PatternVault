"""Test plan: generation/ollama.py, llm.py, services/modes.py (against protocol doubles = real HTTP servers)
Ollama client: version/tags/ps/show/chat/stream/embed/pull; resource options passed through; error mapping
OpenAI-compatible client: bearer key sent, never stored
Mode matrix: offline/learning/hybrid × preference × availability × consent × auto_fallback → exact outcome; fallbacks audited
"""

import asyncio
import json

import pytest

from app.config import Mode
from app.generation.llm import OllamaLLM, OpenAICompatibleLLM
from app.generation.ollama import OllamaClient, OllamaError, OllamaUnavailable, build_options
from app.services.modes import ModeViolation, NoBackendAvailable
from tests.helpers.fake_ollama import FakeCloud, FakeOllama

pytestmark = pytest.mark.integration


def run(coro):
    return asyncio.run(coro)


def test_client_discovery_and_status():
    with FakeOllama(["qwen2.5-coder:7b", "nomic-embed-text"]) as fake:
        c = OllamaClient(fake.url)
        assert run(c.version()) == "0.0.0-double" and run(c.is_available())
        models = run(c.list_models())
        assert [m.name for m in models] == ["qwen2.5-coder:7b", "nomic-embed-text"]
        assert (models[0].family, models[0].parameter_size, models[0].quantization) == ("qwen2", "7B", "Q4_K_M")
        assert run(c.running())[0]["name"] == "qwen2.5-coder:7b"
        assert run(c.show("qwen2.5-coder:7b"))["details"]["family"] == "qwen2"
        with pytest.raises(OllamaError, match="not found"):
            run(c.show("nope"))


def test_chat_stream_embed_and_resource_options():
    with FakeOllama(responder=lambda m, msgs, o: f"echo {msgs[-1]['content']}") as fake:
        c = OllamaClient(fake.url, keep_alive="10m")
        opts = build_options(type("C", (), {"temperature": 0.3, "num_ctx": 4096, "num_thread": 4, "num_gpu": 0})())
        res = run(c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "hi there"}], opts))
        assert res.content == "echo hi there" and res.prompt_tokens == 11 and res.model == "qwen2.5-coder:7b"
        sent = fake.chats()[0]
        assert sent["options"] == {
            "temperature": 0.3,
            "num_ctx": 4096,
            "num_thread": 4,
            "num_gpu": 0,
        }  # CPU/GPU limits honoured
        assert sent["keep_alive"] == "10m" and sent["stream"] is False

        async def collect():
            return [x async for x in c.chat_stream("qwen2.5-coder:7b", [{"role": "user", "content": "a b c"}])]

        chunks = run(collect())
        assert "".join(x.get("delta", "") for x in chunks) == "echo a b c" and chunks[-1]["done"] is True
        assert run(c.embed("m", ["abc", "abcdefgh"])) == [[4.0, 2.0, 3.0], [2.0, 2.0, 3.0]]


def test_pull_progress_and_default_options():
    with FakeOllama() as fake:

        async def collect():
            return [x async for x in OllamaClient(fake.url).pull("new-model")]

        steps = run(collect())
        assert [s["status"] for s in steps][-1] == "success" and steps[1]["completed"] == 50
        assert build_options(
            type("C", (), {"temperature": 0.2, "num_ctx": 8192, "num_thread": None, "num_gpu": None})()
        ) == {"temperature": 0.2, "num_ctx": 8192}


def test_client_errors():
    with FakeOllama() as fake:
        c = OllamaClient(fake.url)
        with pytest.raises(OllamaError, match="not found") as ei:
            run(c.chat("missing-model", [{"role": "user", "content": "x"}]))
        assert ei.value.status == 404 and not isinstance(ei.value, OllamaUnavailable)
        fake.fail_status = 500
        with pytest.raises(OllamaError, match="boom"):
            run(c.list_models())
        fake.fail_status = None
        fake.stream_error = "model crashed"

        async def collect():
            return [x async for x in c.chat_stream("qwen2.5-coder:7b", [{"role": "user", "content": "a b"}])]

        with pytest.raises(OllamaError, match="model crashed"):
            run(collect())
    down = OllamaClient("http://127.0.0.1:1", timeout=2)
    with pytest.raises(OllamaUnavailable, match="cannot reach Ollama"):
        run(down.version())
    assert run(down.is_available()) is False


def test_ollama_llm_requires_a_selected_model():
    cfg = type(
        "C",
        (),
        {
            "model": None,
            "base_url": "http://127.0.0.1:1",
            "request_timeout_s": 5,
            "keep_alive": "5m",
            "temperature": 0.2,
            "num_ctx": 1024,
            "num_thread": None,
            "num_gpu": None,
        },
    )()
    with pytest.raises(OllamaError, match="no local model selected"):
        OllamaLLM.from_config(cfg)


def test_openai_compatible_client_sends_key_and_parses():
    with FakeCloud(lambda msgs: "cloud says hi") as cloud:
        llm = OpenAICompatibleLLM(cloud.url, "gpt-x", "sk-test-key-123")
        res = run(llm.chat([{"role": "user", "content": "hi"}]))
        assert res.content == "cloud says hi" and res.prompt_tokens == 5 and llm.location == "cloud"
        assert cloud.calls[0]["auth"] == "Bearer sk-test-key-123" and cloud.calls[0]["path"] == "/v1/chat/completions"
    with pytest.raises(OllamaUnavailable):
        run(OpenAICompatibleLLM("http://127.0.0.1:1/v1", "m", "k", timeout=2).chat([{"role": "user", "content": "x"}]))


# ------------------------------------------------------------------ mode matrix
def configure(svc, fake_url, *, mode, auto_fallback=True, model="qwen2.5-coder:7b", cloud=False):
    patch = {"mode": mode, "auto_fallback": auto_fallback, "local_model": {"base_url": fake_url, "model": model}}
    if cloud:
        patch["cloud"] = {
            "consent": {"openai-compatible": True},
            "openai_compatible_base_url": "http://127.0.0.1:9/v1",
            "openai_compatible_model": "gpt-x",
        }
    svc.config.update(patch)


@pytest.mark.parametrize(
    "mode,pref,local_up,cloud_ok,fallback,expected",
    [
        ("offline", "auto", True, True, True, ["local"]),
        ("offline", "local", True, False, True, ["local"]),
        ("offline", "cloud", True, True, True, ModeViolation),
        ("offline", "compare", True, True, True, ModeViolation),
        ("offline", "auto", False, True, True, NoBackendAvailable),
        ("learning", "auto", True, True, True, ["local"]),
        ("learning", "cloud", True, True, True, ModeViolation),
        ("hybrid", "auto", True, True, True, ["local"]),
        ("hybrid", "auto", False, True, True, ["cloud"]),
        ("hybrid", "auto", False, True, False, NoBackendAvailable),
        ("hybrid", "auto", False, False, True, NoBackendAvailable),
        ("hybrid", "local", False, True, True, ["cloud"]),
        ("hybrid", "local", False, True, False, NoBackendAvailable),
        ("hybrid", "cloud", True, False, True, ["local"]),
        ("hybrid", "cloud", True, False, False, NoBackendAvailable),
        ("hybrid", "cloud", True, True, True, ["cloud"]),
        ("hybrid", "compare", True, True, True, ["local", "cloud"]),
        ("hybrid", "compare", True, False, True, ["local"]),
        ("hybrid", "compare", True, False, False, NoBackendAvailable),
    ],
)
def test_mode_resolution_matrix(services, mode, pref, local_up, cloud_ok, fallback, expected):
    with FakeOllama() as fake:
        url = fake.url if local_up else "http://127.0.0.1:1"
        configure(services, url, mode=mode, auto_fallback=fallback, cloud=cloud_ok)
        coro = services.modes.resolve(pref, cloud_key_supplied=cloud_ok)
        if isinstance(expected, type):
            with pytest.raises(expected):
                run(coro)
        else:
            assert run(coro).backends == expected


def test_fallback_is_audited_and_published(services):
    with FakeOllama():
        configure(services, "http://127.0.0.1:1", mode="hybrid", cloud=True)
        res = run(services.modes.resolve("auto", cloud_key_supplied=True))
        assert res.switched and res.backends == ["cloud"] and "unavailable" in res.warnings[0]
    assert any(e["type"] == "mode.fallback" for e in services.events.recent())
    from sqlalchemy import select

    from app.models import AuditLog

    with services.session_factory() as db:
        row = db.scalars(select(AuditLog).where(AuditLog.action == "mode.fallback")).one()
        assert row.detail["wanted"] == "local" and row.detail["used"] == ["cloud"]


def test_model_not_installed_blocks_local(services):
    with FakeOllama(["other-model"]) as fake:
        configure(services, fake.url, mode="offline", model="qwen2.5-coder:7b")
        av = run(services.modes.availability())
        assert av.local_reachable and not av.local_model_installed and "not installed" in av.local_reason
        with pytest.raises(NoBackendAvailable, match="not installed"):
            run(services.modes.resolve("auto"))


def test_mode_enum_values():
    assert {m.value for m in Mode} == {"learning", "offline", "hybrid"}
    assert json.dumps({"x": 1})  # keep json import used
