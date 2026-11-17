# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/), versioning: SemVer.

## [Unreleased]

### Added — Chat
- A chat in the VS Code side bar (and a Workbench section): knowledge-grounded answers streamed from the local model, clickable source citations, local conversation history,
  slash commands (`/search /context /generate /task /recreate /status /new /help`), code blocks with Copy / Open in editor, and *Ask About Selection* from the editor.
- Backend: `app.chat`, `/chat/*` routes, `chat_conversations` / `chat_messages` tables (migration 0004), `chat.turn.*` events with delta offsets, model-free fallback answer.
- Security: messages and replies redacted before storage, metadata-only audit entries, text-only rendering, no write path from chat, credential-looking files refused for *Ask About Selection*.
- Commands: *PatternVault: Open Chat*, *PatternVault: Ask About Selection*.
- Fixed: input borders were not drawn (stray `)` in the stylesheet); the E2E host shim now reports a stopped backend as 503 like the real host.

### Added — Phases 3–7
- Retrieval: hashing + Ollama embedders, SQLite vector store (Chroma optional/untested), hybrid FTS5+vector search with trust/version ranking,
  context analysis, knowledge quality analysis, export/import.
- Local AI: Ollama client, mode controller (offline/learning/hybrid + fallback), RAG generation with parser/validator/repair loop, pattern adaptation,
  model-free pattern replay, OpenAI-compatible cloud client (consent-gated).
- Agents & orchestration: 20 agents, orchestrator with planner, locks, approvals, verification gate, correction loop and rollback; execution engine with
  recovery points, permission-gated file ops and command runner; ten testing agents with probe harnesses.
- Workbench webview: Dashboard, AI Learning Centre, Pattern Library, Code Generation, Agent Control Centre, Testing Centre, Settings; host-side policy layer.
- Backup/restore (scrypt+Fernet), storage limits and GC, regression/performance suites, Playwright E2E, VSIX packaging with managed backend provisioning
  and a clean-profile install check.

### Fixed (found by the test suites)
- Secret detector false positives/negatives; pattern store trusting callers to redact; verify running after a skipped apply; GC deleting open-window
  baselines; restore rewinding the audit log; double-counted pattern uses; learn step skipped on failure; read-only mode treated proposals as errors;
  `null` rendered as text in the UI; missing timestamps crashing session discovery.

### Added — Phase 2 (Knowledge capture)
- Project analysis (languages, frameworks + versions, dependencies, naming/indent conventions) for Python, JS/TS, PHP, Rust, Go, Ruby manifests.
- Provider adapter architecture; Claude Code transcript adapter (verified against a real transcript), Continue adapter
  (documented format, unverified), honest no-capture entries for Copilot/Cursor, manual/workspace capture.
- Capture windows (baseline/diff with rename detection), transcript import, redaction + exclusion on every path,
  prompt-injection flagging; `thinking` blocks never stored.
- Tree-sitter structure extraction (10 languages, offline), explainable categorisation, pattern extraction with
  dependency diffing, fingerprint/content-hash dedupe and versioning.
- Pattern service: lifecycle with test-evidence gate for `tested`/`verified`, confidence model, FTS5 search, feedback,
  approve/deprecate/restore/delete, relationships. Migration 0002.
- Extension: capture window UI (start/end, live file counter), session import, assistant detection, project analysis.

### Added — Phase 1 (Foundation)
- Python backend (FastAPI): bearer-token auth, loopback-only binding, health/dashboard/mode/config/projects/audit
  endpoints, authenticated WebSocket event stream.
- SQLite schema (24 tables) with SQLAlchemy 2 models and Alembic migration `0001`; WAL + foreign keys enforced.
- Typed configuration manager with validation, atomic writes, secret rejection, mandatory redaction.
- Security core: secret detection/redaction, path exclusions, 4-level permission gate with destructive-command
  denylist, hash-chained audit log.
- VS Code extension: backend process supervisor, typed backend client + auto-reconnecting event stream,
  status bar, status tree, SecretStorage-backed provider key commands, mode/permission pickers.
- Test suites: backend unit/integration/security; extension Vitest (incl. a real-backend integration test);
  extension-host tests in real VS Code.
- Docs: architecture, ADRs, roadmap, testing strategy, dependency/risk register.
