# Architecture Decision Records

Format: **Context → Decision → Consequences**. Add new records at the bottom; never rewrite history —
supersede with a new record.

## ADR-001 — Python backend + TypeScript extension, split by HTTP/WebSocket
**Context.** Parsing, indexing, model calls and test execution are heavier and better served by Python's
ecosystem; VS Code APIs are only available to the extension host.
**Decision.** Two processes. The extension spawns the backend on `127.0.0.1:<free port>` with a random
bearer token passed via environment variable.
**Consequences.** Backend is testable without VS Code and usable from CLI/CI. We must manage process
lifecycle and token handling in the extension.

## ADR-002 — Python 3.13 toolchain via `uv`, `requires-python >=3.11`
**Context.** The machine's system Python is 3.9, too old for the stack. All dependencies resolved on 3.13.
**Decision.** Develop/test on uv-managed 3.13; declare `>=3.11` as the supported floor.
**Consequences.** CI should add a 3.11 job before release (listed in risks).

## ADR-003 — SQLite + SQLAlchemy 2.x + Alembic
**Context.** Local-first, zero-admin, single-user, must be easily backed up and exported.
**Decision.** SQLite (WAL, FK on) with SQLAlchemy typed ORM; schema via Alembic migrations (never
`create_all` in production paths). FTS5 virtual table for keyword search.
**Consequences.** Single-writer limits; acceptable for a local tool. Migrations run at startup.

## ADR-004 — Vector storage behind an interface; SQLite default, ChromaDB optional
**Context.** ChromaDB brings ~60 transitive dependencies (onnxruntime, grpc, opentelemetry) and a large
install; the requirement allows "ChromaDB or an equivalent local vector database".
**Decision.** `VectorStore` protocol with (a) `SqliteVectorStore` — float32 blobs in a *separate* `vectors.db`,
NumPy cosine search, good to ~10⁵ vectors — and (b) `ChromaVectorStore`, enabled by installing the
`chroma` extra. Vectors are always rebuildable from the relational DB.
**Consequences.** Lean default install. Above ~10⁵ vectors the user should switch to Chroma (documented).

## ADR-005 — Embeddings: Ollama when available, deterministic hashing fallback
**Context.** Offline mode must work without any model download.
**Decision.** `OllamaEmbedder` (configurable model) when reachable; otherwise `HashingEmbedder`
(signed feature-hashing of identifier-aware tokens + character n-grams, L2-normalised). Hybrid search also
uses SQLite FTS5 (BM25) so retrieval is useful even with the lexical fallback.
**Consequences.** The fallback is *lexical*, not neural; documented as a limitation. Vectors record the
model that produced them so a model change triggers re-indexing.

## ADR-006 — Pattern lifecycle with a hard verification gate
**Context.** Requirement: never learn every response as correct; only verified outcomes are top-confidence.
**Decision.** States `observed→generated→approved→tested→verified` + `deprecated`. `verified` requires a
persisted passing `testing_session` linked to the pattern; the transition API rejects anything else.
**Consequences.** Tests are on the critical path of learning; unverifiable projects stay at `approved`.

## ADR-007 — Provider adapters report honest capabilities
**Context.** Providers expose very different information; some expose nothing.
**Decision.** Each adapter declares a `capabilities` object (prompts / responses / tool calls / file edits).
Only documented or locally-readable log formats are parsed; everything else falls back to workspace/git
observation plus manual capture. No scraping of private internals, no reasoning extraction.
**Consequences.** Copilot/Cursor capture is limited to workspace observation + user-initiated capture
until those vendors expose supported APIs.

## ADR-008 — Webviews use plain HTML + VS Code CSS variables
**Context.** `@vscode/webview-ui-toolkit` was archived (2025); React adds build weight.
**Decision.** Framework-free webview (ES modules, one CSS file using `--vscode-*` variables), CSP with
nonce, all backend calls proxied through the extension host (the token never enters the webview).
**Consequences.** Webview logic is unit/e2e testable in a headless browser with a host shim.

## ADR-009 — No Language Server in v1
**Context.** The spec says "LSP where appropriate".
**Decision.** Not appropriate yet: PatternVault adds no language-specific diagnostics or completions.
Diagnostics come from the code-quality/testing agents through the Testing Centre.
**Consequences.** Revisit if inline pattern suggestions are added.

## ADR-010 — Audit log is hash-chained
**Context.** Agents act on user files; the log must be trustworthy.
**Decision.** Each `audit_logs` row stores `prev_hash` and `hash=sha256(prev_hash‖canonical_json)`.
`verify_chain()` detects edits/deletions.
**Consequences.** Tamper-*evident*, not tamper-proof (a local attacker can rewrite the whole chain).

## ADR-011 — Encrypted backups: scrypt + Fernet (cryptography)
**Decision.** Backup = tar.gz of DB + blobs + config, encrypted with a key derived by scrypt from a
user passphrase (random salt, stored in the header). No key recovery by design.
**Consequences.** Lost passphrase ⇒ lost backup; surfaced in the UI.

## ADR-012 — Individual tree-sitter grammar wheels instead of `tree-sitter-language-pack`
**Context.** `tree-sitter-language-pack` 1.25 downloads grammars from the network on first use (cached under the
user cache dir). That violates "starts offline from a clean install".
**Decision.** Depend on `tree-sitter` plus one wheel per grammar (python, javascript, typescript/tsx, php, go, java,
ruby, rust, c-sharp). Parsers are compiled into the wheels. Adding a language = adding a wheel + a node-type table.
**Consequences.** Slightly more dependencies; zero runtime downloads. Verified: all 10 parsers load offline.

## ADR-013 — Assistant "thinking" blocks are never captured
**Context.** Claude Code's local transcripts contain `thinking` blocks. The requirements forbid pretending to access
hidden reasoning, and such text is the most sensitive part of a transcript.
**Decision.** Adapters discard reasoning blocks at parse time (only a count is kept in `meta.skipped`). Pinned by tests
that search the DB file, WAL and blob store for a marker string.

## ADR-014 — Audit and request transactions must not overlap (SQLite single writer)
**Context.** The audit logger uses its own connection so entries survive rolled-back requests; calling it while the
request transaction holds the write lock self-deadlocked (found by tests).
**Decision.** Handlers/services commit first, then audit/publish events. `AuditLogger` documents the rule.

## ADR-015 — Raw bytes are never stored by capture; redacted content only (and recovery data is separate)
**Context.** Capture must never persist secrets, but rollback (Phase 6) needs exact original bytes.
**Decision.** Capture/knowledge blobs are redacted and content-addressed. Recovery points (Phase 6) are a separate
store under `recovery/` (0700), hold raw bytes only for files an agent is about to change, and expire.

## ADR-016 — The extension provisions its own Python environment from a bundled wheel
**Context.** A VSIX cannot contain a portable Python runtime with compiled dependencies (numpy, cryptography, tree-sitter grammars are platform-specific).
**Decision.** The VSIX bundles the backend wheel. *PatternVault: Set Up Backend* finds Python ≥ 3.11, creates a venv under `globalStorage`, and `pip install`s the wheel
(network access to PyPI once). Interpreter precedence: `patternvault.backend.pythonPath` → managed venv → workspace `.venv` → `python3`.
**Consequences.** One-time network requirement and a Python prerequisite (documented in LIMITATIONS). Verified by `test:packaged` in a clean VS Code profile.

## ADR-017 — Verification is a gate in the orchestrator, not an opinion of any agent
**Decision.** `Orchestrator.evaluate_gate`: verified ⇔ ≥ 1 relevant testing agent executed and passed, none failed, security did not fail. `skipped` neither helps nor blocks.
Evidence for promotion must come from a *suite* agent (unit/integration/e2e/api/extension) — lint passing is not test evidence. Step skips propagate to a fixpoint so
`verify` can never run (and "pass") on code that was never applied.

## ADR-018 — Recovery points hold raw bytes; capture/knowledge never does
**Decision.** Rollback needs the user's exact bytes, so recovery data is a separate private store under `recovery/`; everything else is redacted. Each apply is preceded by a recovery point;
failed correction loops restore the first one. (Refines ADR-015.)

## ADR-019 — Restores never rewind the audit trail; backups are encrypted by default in the UI
**Decision.** Restore carries the live `audit_logs` over the restored database (the backup keeps its own copy inside the archive) and takes an automatic safety copy first.
Format: scrypt-derived Fernet; unencrypted backups exist only if the user leaves the passphrase empty.

## ADR-020 — Host-side policy for the webview, because pattern text is attacker-influenced
**Decision.** The webview may call only allow-listed routes through the extension host; deletes, permission escalation, external-transmission consent, task start, test runs,
approvals and restores require a host-side modal confirmation. The same list is contract-tested against every route the UI source uses.
