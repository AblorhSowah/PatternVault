# PatternVault — Technical Architecture

PatternVault is a **local-first development-intelligence layer** for VS Code. It observes AI-assisted
development work, distils it into verified, reusable knowledge, and later uses that knowledge (plus a
local model) to recreate or adapt implementations when cloud AI is unavailable.

> Status legend used across the docs: ✅ implemented and verified by tests · 🟡 partially implemented ·
> ⬜ planned. The authoritative status is [`PROJECT_PROGRESS.md`](../PROJECT_PROGRESS.md).

## 1. System context

```
┌──────────────────────────── VS Code ────────────────────────────┐
│  PatternVault extension (TypeScript)                            │
│   ├─ commands / status bar / tree views                         │
│   ├─ Workbench webview (7 sections)  ◄─ postMessage ─► host     │
│   ├─ WorkspaceObserver (FileSystemWatcher, git, tasks)          │
│   ├─ Assistant detector (Copilot, Claude Code, Continue, …)     │
│   ├─ SecretStorage (API keys; never written to disk)            │
│   └─ BackendProcess + BackendClient (HTTP + WebSocket)          │
└────────────────────────────┬────────────────────────────────────┘
                             │ 127.0.0.1 only, bearer token
┌────────────────────────────▼────────────────────────────────────┐
│  PatternVault backend (Python 3.11+, FastAPI)                   │
│   api/  REST + WebSocket (live task events)                     │
│   orchestration/  Orchestrator, task DAG, file locks, events    │
│   agents/  Learning · Extraction · Knowledge · Retrieval ·      │
│            CodeGen · Replay · Debug · Review/Security · Docs    │
│   testing/ 10 dedicated testing agents + runner + reports       │
│   learning/ provider adapters, session capture, workspace diff  │
│   extraction/ language detect, tree-sitter structure, patterns  │
│   retrieval/ embeddings, vector store, hybrid search, context   │
│   generation/ LLM clients (Ollama, OpenAI-compatible), RAG      │
│   replay/ reconstruct stored implementations                    │
│   execution/ permissioned file ops, commands, recovery points   │
│   security/ secrets, exclusions, permissions, audit, crypto     │
│   knowledge/ store, lifecycle, quality, export/import, backup   │
│   database/ SQLAlchemy + Alembic (SQLite)                       │
└───────┬───────────────────┬──────────────────────┬──────────────┘
        │                   │                      │
   SQLite (relational)  Vector store           Ollama (local LLM,
   FTS5 (keyword)       (SQLite default,       embeddings) — optional
   snapshot blobs       ChromaDB optional)     cloud only with consent
```

### Why a Python backend plus a TypeScript extension

The extension owns everything that needs the VS Code API (watchers, SecretStorage, webviews). The backend
owns everything heavy (parsing, indexing, retrieval, model calls, test execution). The boundary is a
narrow, versioned HTTP/WebSocket API so each side can be tested alone, and the backend remains usable
from a CLI or CI without VS Code.

## 2. Data directory

All state is under `PATTERNVAULT_HOME` (default `~/.patternvault`):

| Path                | Content                                                       |
| ------------------- | ------------------------------------------------------------- |
| `patternvault.db` | relational knowledge DB (SQLite, WAL)                         |
| `vectors.db`      | embedding vectors (separate file, rebuildable from the DB)    |
| `blobs/`          | content-addressed file snapshots (sha256)                     |
| `recovery/`       | recovery-point manifests                                      |
| `backups/`        | (encrypted) backups                                           |
| `config.json`     | user configuration (validated; contains **no secrets**) |
| `logs/`           | backend logs                                                  |

## 3. Component breakdown

| Component             | Responsibility                                                                                                              | Phase |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----- |
| `app.config`        | typed settings, data-dir resolution, user config load/save                                                                  | 1     |
| `app.database`      | engine, session, WAL pragmas, Alembic runner                                                                                | 1     |
| `app.models`        | ORM entities (see §4)                                                                                                      | 1     |
| `app.security.*`    | secret detection/redaction, path exclusions, permission modes, audit hash-chain, encrypted backups, untrusted-content guard | 1–2  |
| `app.api`           | FastAPI app, bearer-token auth, routers, WebSocket hub                                                                      | 1     |
| `app.learning`      | provider adapters, session capture, workspace change tracking                                                               | 2     |
| `app.extraction`    | language/framework detection, tree-sitter structure, pattern extraction, dedupe                                             | 2     |
| `app.knowledge`     | pattern store, lifecycle (observed→verified), relationships, quality, export/import                                        | 2–3  |
| `app.retrieval`     | embedders, vector store, hybrid ranking, project context analysis                                                           | 3     |
| `app.generation`    | LLM clients, model discovery, RAG prompt building, output parsing, adaptation                                               | 4     |
| `app.replay`        | reconstruct stored implementations for the current project                                                                  | 4     |
| `app.agents`        | agent interface/registry + the nine development agents                                                                      | 5     |
| `app.testing`       | runner, report model, the ten testing agents, regression baselines                                                          | 5     |
| `app.orchestration` | orchestrator, DAG executor, file locks, event bus                                                                           | 5     |
| `app.execution`     | permission-gated file ops, command runner, recovery points, fix loop                                                        | 6     |
| `vscode-extension`  | UI, observers, backend lifecycle, SecretStorage                                                                             | 1–7  |

## 4. Database design

SQLite, accessed with SQLAlchemy 2.x, schema managed by Alembic. Foreign keys are enforced
(`PRAGMA foreign_keys=ON`), WAL mode is on, and every foreign key / lookup column is indexed.

```
projects ─┬─< dev_sessions >─ ai_providers
          │        ├─< user_prompts ─< captured_responses
          │        ├─< file_changes >─ file_snapshots
          │        └─< errors
          ├─< dependencies
          ├─< tasks ─< agent_executions >─ agent_definitions
          │       └─< recovery_points, approvals
          └─< testing_sessions ─< test_results

knowledge_patterns ─< pattern_versions
        ├─< pattern_relationships (src, dst, relation)
        ├─< code_implementations
        ├─< dependencies (pattern-required)
        └─< user_feedback (polymorphic target)
errors ─< debugging_solutions
audit_logs  (append-only, hash-chained)
```

Pattern lifecycle: `observed → generated → approved → tested → verified`, with `deprecated` as a terminal
side-state. **`verified` can only be reached through a recorded passing test session**; the lifecycle
module refuses other transitions. Confidence is computed from lifecycle state, test evidence, user feedback
and reuse outcomes (see `knowledge/lifecycle.py`).

## 5. Multi-agent execution architecture

```
Request ─► Orchestrator.plan() ─► task DAG
  context ─► (replay | generate) ─► review/security ─► apply ─► test ─► [debug ─► apply ─► retest]* ─► document ─► learn
```

* Every agent implements `Agent.run(ctx, payload) -> AgentResult` and is registered in an
  `AgentRegistry` (definitions are persisted in `agent_definitions`).
* The orchestrator executes steps whose dependencies are satisfied; independent steps run concurrently.
* **File safety:** steps that write declare the paths they touch; `FileLockManager` serialises writers per
  path (no uncontrolled parallel modification). A recovery point is taken before the first write.
* **Verification gate:** a task becomes `verified` only if ≥1 relevant testing agent ran and **all** relevant
  agents passed. "Generation completed" never implies success.
* **Correction loop:** a failing testing agent returns structured failures → orchestrator routes them to the
  Debugging agent → patch applied (under permissions) → the *same* testing agent re-runs; max N attempts;
  rollback to the recovery point if exhausted.
* **Audit & history:** every step writes an `agent_executions` row and an audit entry; live progress is
  published on the event bus and streamed over WebSocket.

## 6. Operating modes

| Mode         | Behaviour                                                                             |
| ------------ | ------------------------------------------------------------------------------------- |
| `learning` | Observe/capture from cloud assistants; no local generation required                   |
| `offline`  | Local knowledge + local model only; no network egress at all                          |
| `hybrid`   | Local and cloud both permitted per policy (cloud needs explicit consent per provider) |

Automatic fallback: when `auto_fallback` is on and the preferred backend (cloud) is unreachable, the mode
router transitions to `offline` and records the switch in the audit log.

## 7. Permission model

`read_only` < `assisted` < `approved_execution` < `restricted_autonomous`. Enforced centrally in
`security/permissions.py`; all file operations and commands go through it. Destructive commands
(`rm -rf`, `git reset --hard`, `DROP`, …), writes to sensitive files (`.env`, CI config, lockfiles,
`package.json` scripts, …) and writes outside the project root are denied or require explicit approval,
regardless of mode.

## 8. Trust boundaries

* The backend only listens on `127.0.0.1` and requires a per-launch bearer token.
* Captured text, retrieved knowledge and model output are **untrusted data**. They are never executed or
  treated as instructions; `security/untrusted.py` flags injection-like content and agents pass it to
  models only inside delimited data blocks.
* Secrets are redacted *before* anything is persisted or embedded. `.env`, private keys and credential
  files are excluded from capture entirely.


## 9. As built (deviations from the original plan)

* **Phases 5 and 6 were built together**: the orchestrator needs permission-gated writes, recovery points and approvals to run its apply → test → fix cycle,
  so `execution/` (file ops, commands, recovery) and `orchestration/approvals.py` landed before the orchestrator.
* **Agents** (20 definitions, persisted in `agent_definitions`): Learning & Observation, Pattern Extraction, Knowledge Management, Context & Retrieval,
  Code Generation, Pattern Replay, Debugging, Code Review & Security, Documentation, the ten testing agents, and the Orchestrator.
* **Orchestrator** (`orchestration/orchestrator.py`): plans per task kind (`generate | replay | debug | review | test | document`), runs ready steps concurrently,
  serialises writers with per-path locks and a per-project mutation lock, propagates failures/skips to a fixpoint (a skipped `apply` blocks `verify`),
  records every execution, and ends each task through the **verification gate**. The `learn` step always runs so failed outcomes are recorded too.
* **Correction loop**: failing testing agents → Debugging agent (known verified solution → deterministic fix → local model) → review → apply (new recovery point) →
  the *same* failing agents re-run independently → `correction_applied`/`retest_status` written on the original failing results → verified solution stored.
  Exhausted attempts restore the first recovery point.
* **Testing agents** run in the target project's interpreter; probe harnesses (`testing/harness/`) are trusted scripts shipped in the wheel.
* **Vector store**: SQLite default; `ChromaVectorStore` exists but is untested (optional extra).
* **Model catalog** ships inside the wheel (`generation/model_catalog.json`); the top-level `local-models/` directory from the plan was not needed.
* **Distribution**: the VSIX bundles the backend wheel; *Set Up Backend* provisions a managed venv in the extension's global storage (ADR-016).
* **Not built**: a Language Server (ADR-009); cloud providers other than an OpenAI-compatible endpoint; multi-user features.
