"""Chat: conversations stored locally, answers grounded in the user's knowledge base, streamed from the local model.

The chat can only *talk*. It never writes files, runs commands or starts agent tasks: those stay behind the existing
host-confirmed routes (/generate, /tasks). Anything sent to a model is secret-redacted and retrieved knowledge is framed as
untrusted data. With no usable model the chat degrades to an honest, model-free answer made from stored patterns.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select

from app.chat.prompts import ChatRef, build_chat_messages
from app.config import Mode
from app.generation.llm import OpenAICompatibleLLM
from app.generation.ollama import OllamaClient, OllamaError, build_options
from app.models import ChatConversation, ChatMessage, Project
from app.models.base import utcnow
from app.retrieval.search import SearchQuery
from app.security.secrets import redact
from app.services.modes import ModeViolation, NoBackendAvailable

MAX_MESSAGE_CHARS = 8000
HISTORY_MESSAGES = 20
MAX_RUNNING_TURNS = 4
KEEP_FINISHED_TURNS = 100
FLUSH_SECONDS = 0.04
FLUSH_CHARS = 64
DEFAULT_TITLE = "New chat"


class ChatError(RuntimeError):
    """The chat request itself is invalid."""


class ChatBusy(ChatError):
    """Another answer is still being written for this conversation (or too many are running)."""


@dataclass
class Turn:
    id: int
    conversation_id: int
    status: str = "running"  # running | done | error | cancelled
    text: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    message_id: int | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "text": self.text,
            "sources": self.sources,
            "meta": self.meta,
            "error": self.error,
            "message_id": self.message_id,
        }


@dataclass
class TurnRequest:
    message: str
    backend: str = "auto"  # auto | local | cloud
    model: str | None = None
    use_knowledge: bool = True
    cloud_key: str | None = field(default=None, repr=False)  # memory only; never stored or logged
    top_k: int = 4


def _msg_dict(m: ChatMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "role": m.role,
        "content": m.content,
        "sources": m.sources or [],
        "meta": m.meta or {},
        "created_at": m.created_at.isoformat(),
    }


def _title_from(message: str) -> str:
    first = " ".join(message.split())[:60].strip()
    return first or DEFAULT_TITLE


class ChatService:
    def __init__(self, svc: Any) -> None:
        self.svc = svc  # app.services.container.Services (duck-typed, like CodeGenerator)
        self._turns: dict[int, Turn] = {}
        self._seq = 0

    # ------------------------------------------------------------------ conversations
    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.svc.session_factory() as db:
            counts = {
                int(cid): int(n)
                for cid, n in db.execute(
                    select(ChatMessage.conversation_id, func.count(ChatMessage.id)).group_by(
                        ChatMessage.conversation_id
                    )
                ).all()
            }
            rows = (
                db.execute(
                    select(ChatConversation)
                    .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "project_id": c.project_id,
                    "message_count": int(counts.get(c.id, 0)),
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                }
                for c in rows
            ]

    def create_conversation(self, title: str | None = None, project_id: int | None = None) -> dict[str, Any]:
        with self.svc.session_factory() as db:
            if project_id is not None and db.get(Project, project_id) is None:
                raise LookupError("project not found")
            c = ChatConversation(title=redact((title or DEFAULT_TITLE)[:200]).text, project_id=project_id)
            db.add(c)
            db.commit()
            return {
                "id": c.id,
                "title": c.title,
                "project_id": c.project_id,
                "message_count": 0,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }

    def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        with self.svc.session_factory() as db:
            c = db.get(ChatConversation, conversation_id)
            if c is None:
                raise LookupError("conversation not found")
            msgs = (
                db.execute(select(ChatMessage).where(ChatMessage.conversation_id == c.id).order_by(ChatMessage.id))
                .scalars()
                .all()
            )
            running = next(
                (t.id for t in self._turns.values() if t.conversation_id == c.id and t.status == "running"), None
            )
            return {
                "id": c.id,
                "title": c.title,
                "project_id": c.project_id,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
                "running_turn": running,
                "messages": [_msg_dict(m) for m in msgs],
            }

    def set_project(self, conversation_id: int, project_id: int | None) -> None:
        with self.svc.session_factory() as db:
            c = db.get(ChatConversation, conversation_id)
            if c is None:
                raise LookupError("conversation not found")
            if project_id is not None and db.get(Project, project_id) is None:
                raise LookupError("project not found")
            c.project_id = project_id
            db.commit()

    def delete_conversation(self, conversation_id: int) -> None:
        for t in self._turns.values():
            if t.conversation_id == conversation_id and t.status == "running" and t.task:
                t.task.cancel()
        with self.svc.session_factory() as db:
            c = db.get(ChatConversation, conversation_id)
            if c is None:
                raise LookupError("conversation not found")
            db.execute(delete(ChatMessage).where(ChatMessage.conversation_id == conversation_id))
            db.delete(c)
            db.commit()
        self.svc.audit.record("user", "chat.delete", resource_type="chat_conversation", resource_id=conversation_id)

    def delete_all(self) -> int:
        for t in self._turns.values():
            if t.status == "running" and t.task:
                t.task.cancel()
        with self.svc.session_factory() as db:
            n = int(db.execute(select(func.count(ChatConversation.id))).scalar_one())
            db.execute(delete(ChatMessage))
            db.execute(delete(ChatConversation))
            db.commit()
        self.svc.audit.record("user", "chat.delete_all", resource_type="chat_conversation", detail={"conversations": n})
        return n

    # ------------------------------------------------------------------ turns
    async def start_turn(self, conversation_id: int, req: TurnRequest) -> dict[str, Any]:
        message = req.message.strip()
        if not message:
            raise ChatError("message is empty")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ChatError(f"message is longer than {MAX_MESSAGE_CHARS} characters")
        if req.backend not in ("auto", "local", "cloud"):
            raise ChatError(f"unknown backend preference '{req.backend}'")
        if req.backend == "cloud" and self.svc.config.config.mode != Mode.HYBRID:
            raise ModeViolation(f"cloud answers are disabled in {self.svc.config.config.mode.value} mode")
        if any(t.conversation_id == conversation_id and t.status == "running" for t in self._turns.values()):
            raise ChatBusy("an answer is still being written for this conversation")
        if sum(1 for t in self._turns.values() if t.status == "running") >= MAX_RUNNING_TURNS:
            raise ChatBusy("too many answers are being written at once; wait for one to finish")

        redacted = redact(message)
        with self.svc.session_factory() as db:
            conv = db.get(ChatConversation, conversation_id)
            if conv is None:
                raise LookupError("conversation not found")
            history = self._history(db, conversation_id)
            first = not history and conv.title == DEFAULT_TITLE
            if first:
                conv.title = _title_from(redacted.text)
            user_msg = ChatMessage(
                conversation_id=conversation_id,
                role="user",
                content=redacted.text,
                sources=[],
                meta={"redactions": redacted.count} if redacted.count else {},
            )
            db.add(user_msg)
            conv.updated_at = utcnow()
            db.commit()
            project_id = conv.project_id
            user_message_id = user_msg.id

        self._seq += 1
        turn = Turn(self._seq, conversation_id)
        turn.meta["redactions_in_question"] = redacted.count
        self._turns[turn.id] = turn
        self._prune()
        turn.task = asyncio.create_task(self._run(turn, req, redacted.text, history, project_id))
        self.svc.events.publish("chat.turn.started", {"turn_id": turn.id, "conversation_id": conversation_id})
        return {"turn_id": turn.id, "user_message_id": user_message_id, "conversation_id": conversation_id}

    def get_turn(self, turn_id: int) -> dict[str, Any]:
        t = self._turns.get(turn_id)
        if t is None:
            raise LookupError("turn not found")
        return t.to_dict()

    def cancel_turn(self, turn_id: int) -> bool:
        t = self._turns.get(turn_id)
        if t is None:
            raise LookupError("turn not found")
        if t.status != "running" or t.task is None:
            return False
        t.task.cancel()
        return True

    # ------------------------------------------------------------------ internals
    def _prune(self) -> None:
        finished = [t for t in self._turns.values() if t.status != "running"]
        for t in finished[: max(0, len(finished) - KEEP_FINISHED_TURNS)]:
            self._turns.pop(t.id, None)

    @staticmethod
    def _history(db: Any, conversation_id: int) -> list[dict[str, str]]:
        rows = (
            db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id.desc())
                .limit(HISTORY_MESSAGES)
            )
            .scalars()
            .all()
        )
        out = [
            {"role": m.role, "content": m.content}
            for m in reversed(rows)
            if m.content.strip() and not (m.meta or {}).get("error") and not (m.meta or {}).get("no_model")
        ]
        return out

    def _project(self, project_id: int | None) -> dict[str, Any] | None:
        if project_id is None:
            return None
        with self.svc.session_factory() as db:
            p = db.get(Project, project_id)
            if p is None:
                return None
            fw = next((f for f in p.frameworks or [] if not f.get("dev")), None)
            return {
                "name": p.name,
                "language": (p.conventions or {}).get("primary_language"),
                "framework": fw["name"] if fw else None,
                "framework_version": fw.get("version") if fw else None,
            }

    def _retrieve(self, question: str, project_id: int | None, top_k: int) -> list[ChatRef]:
        res = self.svc.search.search(SearchQuery(question[:500], project_id, top_k=top_k + 2))
        refs: list[ChatRef] = []
        for hit in res.hits:
            if hit.pattern.get("status") == "deprecated":
                continue
            p = self.svc.patterns.get(hit.pattern_id)
            c = p.get("content", {})
            refs.append(
                ChatRef(
                    p["id"],
                    p["title"],
                    p["status"],
                    p["confidence"],
                    p["language"],
                    p["framework"],
                    p["framework_version"],
                    p.get("summary") or "",
                    c.get("steps", []),
                    c.get("dependencies", []),
                    c.get("files", []),
                    list(hit.warnings),
                    hit.score,
                )
            )
            if len(refs) >= top_k:
                break
        return refs

    @staticmethod
    def _knowledge_only_answer(reason: str, refs: list[ChatRef], knowledge_on: bool) -> str:
        head = f"I can't reach a language model right now ({reason})."
        if not knowledge_on:
            return (
                head
                + " Knowledge lookup is switched off, so I have nothing to show. Start your local model or turn it on."
            )
        if not refs:
            return (
                head
                + " I also found nothing relevant in your knowledge base for this question.\n\n"
                + "Try `/search <words>`, or start Ollama and pick a model in **Settings → Local model** to get written answers."
            )
        lines = [head + " Here is what your knowledge base has on this:", ""]
        for i, r in enumerate(refs, 1):
            stack = " / ".join(x for x in (r.language, r.framework) if x)
            ver = f" {r.framework_version}" if r.framework_version else ""
            lines.append(
                f"{i}. **{r.title}** [#{r.id}] — {r.status}{(' · ' + stack + ver) if stack else ''} · relevance {r.score:.2f}"
            )
            if r.summary:
                lines.append(f"   {r.summary.strip()[:220]}")
            lines.extend(f"   ⚠ {w}" for w in r.warnings[:2])
        lines += [
            "",
            "Start Ollama and pick a model in **Settings → Local model** to get written answers; `/generate` and `/task` also need one.",
        ]
        return "\n".join(lines)

    async def _stream(
        self, backend: str, req: TurnRequest, messages: list[dict[str, str]]
    ) -> AsyncIterator[dict[str, Any]]:
        cfg = self.svc.config.config
        if backend == "local":
            model = req.model or cfg.local_model.model
            if not model:
                raise OllamaError("no local model selected: choose one in Settings")
            client = OllamaClient.from_config(cfg.local_model)
            yield {"model": model}
            async for chunk in client.chat_stream(model, messages, build_options(cfg.local_model)):
                yield chunk
            return
        if not (req.cloud_key and cfg.cloud.openai_compatible_base_url and cfg.cloud.openai_compatible_model):
            raise OllamaError("cloud backend is not configured")
        llm = OpenAICompatibleLLM(
            cfg.cloud.openai_compatible_base_url, cfg.cloud.openai_compatible_model, req.cloud_key
        )
        yield {"model": llm.model}
        res = await llm.chat(messages)
        yield {"delta": res.content}
        yield {
            "done": True,
            "prompt_tokens": res.prompt_tokens,
            "completion_tokens": res.completion_tokens,
            "done_reason": res.done_reason,
        }

    async def _run(
        self, turn: Turn, req: TurnRequest, question: str, history: list[dict[str, str]], project_id: int | None
    ) -> None:
        svc = self.svc
        t0 = time.monotonic()
        notes: list[str] = []
        pending = ""
        last_flush = time.monotonic()
        backend: str | None = None

        def emit(text: str) -> None:
            # ``offset`` lets a client that also polls apply each delta exactly once, in order.
            offset = len(turn.text)
            turn.text += text
            svc.events.publish(
                "chat.turn.delta",
                {"turn_id": turn.id, "conversation_id": turn.conversation_id, "delta": text, "offset": offset},
                ephemeral=True,
            )

        try:
            refs: list[ChatRef] = []
            if req.use_knowledge:
                refs = self._retrieve(question, project_id, req.top_k)
            turn.sources = [r.source() for r in refs]
            try:
                resolution = await svc.modes.resolve(req.backend, bool(req.cloud_key), req.model)
                backend = resolution.backends[0]
                notes.extend(resolution.warnings)
            except (ModeViolation, NoBackendAvailable) as exc:
                reason = str(exc).removeprefix("local model unavailable: ")
                turn.meta.update(model_used=False, no_model=True, reason=reason)
                emit(self._knowledge_only_answer(reason, refs, req.use_knowledge))
            if backend is not None:
                messages, stats = build_chat_messages(
                    question=question,
                    history=history,
                    project=self._project(project_id),
                    refs=refs,
                    num_ctx=svc.config.config.local_model.num_ctx,
                )
                turn.meta.update(
                    backend=backend, model_used=True, **{k: stats[k] for k in ("patterns", "history_messages")}
                )
                if stats["redactions"] > turn.meta.get("redactions_in_question", 0):
                    notes.append(
                        "Secret-like values in earlier messages or retrieved patterns were redacted before the model saw them."
                    )
                async for chunk in self._stream(backend, req, messages):
                    if "model" in chunk:
                        turn.meta["model"] = chunk["model"]
                    elif chunk.get("done"):
                        turn.meta["prompt_tokens"] = chunk.get("prompt_tokens", 0)
                        turn.meta["completion_tokens"] = chunk.get("completion_tokens", 0)
                        if chunk.get("done_reason") == "length":
                            turn.meta["truncated"] = True
                            limit = build_options(svc.config.config.local_model)["num_predict"]
                            notes.append(
                                f"The answer was cut off at the output limit ({limit} tokens). Ask me to continue, "
                                "or raise “Max answer length” in Settings → Local model."
                            )
                    elif chunk.get("delta"):
                        pending += chunk["delta"]
                        now = time.monotonic()
                        if len(pending) >= FLUSH_CHARS or now - last_flush >= FLUSH_SECONDS:
                            emit(pending)
                            pending, last_flush = "", now
                if pending:
                    emit(pending)
                    pending = ""
            turn.status = "done"
        except asyncio.CancelledError:
            if pending:
                turn.text += pending
            turn.status = "cancelled"
            turn.meta["cancelled"] = True
        except OllamaError as exc:
            if pending:
                turn.text += pending
            turn.status = "error"
            turn.error = str(exc)
        except Exception as exc:  # a chat turn must always finish with a visible, honest state
            if pending:
                turn.text += pending
            turn.status = "error"
            turn.error = f"{type(exc).__name__}: {exc}"
        finally:
            turn.meta["duration_ms"] = int((time.monotonic() - t0) * 1000)
            if notes:
                turn.meta["notes"] = notes
            if turn.error:
                turn.meta["error"] = turn.error
            self._finish(turn, backend)

    def _finish(self, turn: Turn, backend: str | None) -> None:
        svc = self.svc
        content = redact(turn.text).text
        try:
            with svc.session_factory() as db:
                conv = db.get(ChatConversation, turn.conversation_id)
                if conv is not None and (content.strip() or turn.error or turn.status == "cancelled"):
                    msg = ChatMessage(
                        conversation_id=conv.id,
                        role="assistant",
                        content=content if content.strip() else ("" if turn.error else "(stopped)"),
                        sources=turn.sources,
                        meta=turn.meta,
                    )
                    db.add(msg)
                    conv.updated_at = utcnow()
                    db.commit()
                    turn.message_id = msg.id
        except Exception as exc:  # conversation deleted mid-answer, disk full, ...
            turn.status = "error"
            turn.error = turn.error or f"could not save the answer: {exc}"
        with contextlib.suppress(Exception):
            svc.audit.record(
                "user",
                "chat.message",
                resource_type="chat_conversation",
                resource_id=turn.conversation_id,
                outcome="allowed" if turn.status == "done" else turn.status,
                detail={
                    "backend": backend or "knowledge_only",
                    "model": turn.meta.get("model"),
                    "sources": [s["id"] for s in turn.sources],
                    "redactions": turn.meta.get("redactions_in_question", 0),
                    "duration_ms": turn.meta.get("duration_ms"),
                },
            )
        svc.events.publish(
            "chat.turn.done",
            {
                "turn_id": turn.id,
                "conversation_id": turn.conversation_id,
                "status": turn.status,
                "message_id": turn.message_id,
            },
        )
