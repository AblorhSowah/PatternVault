# Implementation Roadmap & Checklist

Check a box only when the item is implemented **and** its tests have been run and passed.
Run history lives in [`PROJECT_PROGRESS.md`](../PROJECT_PROGRESS.md).

## Phase 1 — Foundation
- [x] Repository layout, `pyproject.toml`, root `package.json`, tooling config (ruff, mypy, pytest, bandit)
- [x] Configuration management (typed settings + validated `config.json`, env overrides)
- [x] SQLite engine (WAL, FK) + full ORM schema + Alembic migration 0001
- [x] Security core: secret redaction, path exclusions, permission modes, hash-chained audit log
- [x] FastAPI app: bearer-token auth, health/config/projects/audit endpoints, WebSocket event hub
- [x] VS Code extension skeleton: backend process manager, typed client, status bar, commands, SecretStorage
- [x] Initial test suites (backend unit/integration/security; extension Vitest)

## Phase 2 — Knowledge capture
- [x] Project registry + language/framework/dependency detection
- [x] Provider adapter interface + adapters (Claude Code logs, Continue sessions, manual/workspace)
- [x] Session capture service (prompts, responses, file changes via git/FS diff, snapshots)
- [x] Pattern extraction (tree-sitter structure, categories, dependencies, dedupe)
- [x] Pattern store with lifecycle, versions, provenance, relationships
- [x] Extension WorkspaceObserver + assistant detector

## Phase 3 — Knowledge intelligence
- [x] Embedders (hashing + Ollama), vector stores (SQLite + Chroma optional), FTS5
- [x] Hybrid retrieval ranked by relevance × verification × version match
- [x] Project context analysis + missing-context detection
- [x] Workbench webview: Dashboard, Learning Centre, Pattern Library

## Phase 4 — Local AI
- [x] Ollama client: discovery, selection, streaming, resource options (verified against a protocol double only — see PROJECT_PROGRESS)
- [x] RAG prompt builder + structured output parsing
- [x] Local generation (new) and adaptation of existing patterns
- [x] Pattern replay (reconstruct, dependency + environment checks)
- [x] Generation Workspace UI

## Phase 5 — Multi-agent engine
- [x] Agent interface, registry, persisted definitions
- [x] Orchestrator (planner, DAG executor, history, conflict detection, file locks, events)
- [x] Review/Security, Debugging, Documentation, Learning, Extraction, Knowledge, Retrieval, CodeGen, Replay agents
- [x] Ten testing agents + regression baselines + reports
- [x] Agent Control Centre + Testing Centre UI

## Phase 6 — Autonomous execution
- [x] Permission-gated file ops + command runner
- [x] Recovery points + rollback
- [x] Generate → apply → test → debug → retest loop with verification gate
- [x] Approvals flow (backend + UI)

## Phase 7 — Hardening and release
- [x] Full test pass, coverage report
- [x] Security review + dependency audit
- [x] Performance budgets verified
- [x] Backup/restore/export/import verified
- [x] User guide, API docs, install guide, changelog
- [x] VSIX packaging + clean-profile install verification (see PROJECT_PROGRESS for the run)
