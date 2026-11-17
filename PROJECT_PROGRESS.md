# PatternVault — Project Progress (final report)

_Last updated: 2026-11-17. Every number below was copied from an actual run of `scripts/check.sh --host --package` (the final run
passed all stages) or from the named command. Nothing is estimated._

## Verdict

PatternVault 0.1.0 is a working, tested, packaged system. **One acceptance area is not fully demonstrated: generation with a real
local model.** No Ollama was installed; I downloaded the official release (SHA-256 verified) but the sandbox denied executing it, and I did not
work around that. Everything else listed under "Acceptance criteria" was exercised for real. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Phase status

| Phase | Status |
|---|---|
| 1 Foundation | ✅ |
| 2 Knowledge capture | ✅ |
| 3 Knowledge intelligence | ✅ |
| 4 Local AI | 🟡 complete and tested against Ollama *protocol doubles*; **real-model run not performed** |
| 5 Multi-agent engine | ✅ |
| 6 Autonomous execution | ✅ (built together with 5: the orchestrator depends on it) |
| 7 Hardening & release | ✅ |

## Final verification run (`scripts/check.sh --host --package`, exit 0)

| Stage | Result |
|---|---|
| ruff lint + format check | clean |
| mypy (105 source files) | clean |
| bandit | clean (0 issues; B101/B404/B603 skipped by documented policy) |
| pytest — unit, integration, security, regression | **584 passed**, 0 failed — coverage **87.3 %** of `backend/app` |
| performance budgets (`-m performance`) | **5 passed** (numbers below) |
| Playwright E2E (real Chromium + real backend process) | **25 passed** (1 live-Ollama test deselected/skipped) |
| tsc / ESLint / esbuild | clean |
| Vitest (unit + real-backend integration) | **120 passed** |
| VS Code extension-host tests (real VS Code 1.129.1) | **5 passed** |
| Wheel + VSIX build | `patternvault-0.1.0-py3-none-any.whl` (≈198 KB), `patternvault-0.1.0.vsix` (≈250 KB, bundles the wheel) |
| **Clean-profile install of the VSIX in real VS Code** | **5 passed**: fresh user-data/extensions dirs → VSIX installed → *Set Up Backend* built a private venv from the bundled wheel (17–42 s) → backend served requests, reported 0.1.0 → capture window produced a pattern |

Total executed: 744 automated checks (+1 skipped live test). Code size: backend 14.9 k lines (105 files), backend tests 7.5 k lines, extension 1.6 k lines TS +
1.0 k lines webview, extension tests 1.1 k lines.

Other checks run during the security review: `pip-audit` — no known vulnerabilities; `npm audit --omit=dev` — 0 (4 *moderate* advisories exist only in dev tooling,
`@vscode/vsce` transitives, not shipped); Security Testing Agent on this repository — the high/critical findings were all reviewed (SQL built from fixed identifiers, `rmtree` of
our own temp dirs) and then rewritten or justified; the Trojan-Source scan is a test.

Performance (macOS arm64, from `pytest tests/performance -s`): index 1000 patterns 2.8 s; hybrid search p50 2.7 ms / p95 4.3 ms; FTS list 5.8 ms; 20 000-vector search
93 ms first / 1.6 ms cached; 3000-file repository analysed in 1.2 s (peak 4 MB), baseline 1.8 s, diff 1.4 s (peak 5 MB); parse 5000 lines 32 ms; redact 50 000 lines 0.39 s.

## Acceptance criteria (spec §19)

| | Criterion | Evidence |
|---|---|---|
| A | Learning: capture a session, identify modified files, extract patterns, store locally, separate verified/unverified | `test_capture_flow`, `test_import_and_lifecycle`, extension↔backend integration; lifecycle gate tests ✅ |
| B | Retrieval: search, natural language, across projects (with permissions), framework/version differences | `test_retrieval` (ranking, cross-project switches, version warnings) ✅ |
| C | Independent generation: local model, no cloud, stored knowledge, recreate, adapt | recreate ✅ (model-free, verified end to end); adapt/new ✅ **against protocol doubles**; **real local model ⚠ not run** |
| D | Agent collaboration: assign, coordinate, detect failures, request corrections, verify corrections | `test_orchestrator` (correction loop, independent retest, learned-solution reuse, rollback, locks, approvals) ✅ |
| E | Automated testing: unit/integration/e2e/security checks, reports, regression detection | `test_testing_agents` — ten agents on real projects and on PatternVault itself ✅ |
| F | Offline: starts without internet, local repository, local model, generate+test without cloud | starts/operates offline ✅ (`offline` mode never calls cloud; hashing embedder); local-model part ⚠ as in C |
| G | Security: protect sensitive files, credentials, approvals, audit, recover from failed modifications | `tests/security`, `test_execution_engine`, rollback tests, backup tests ✅ |

## What was built (by phase)

1. **Foundation** — config manager, SQLite (25 tables, 3 reversible Alembic migrations), security core (redaction, exclusions, 4-level permissions, hash-chained audit),
   FastAPI with token auth/WebSocket, extension skeleton (supervisor, client, SecretStorage).
2. **Capture** — project analysis, provider adapters (Claude Code verified on a real transcript; Continue unverified; Copilot/Cursor honest no-capture), capture windows, import,
   tree-sitter structure (10 grammars, offline), categoriser, extractor, pattern store with lifecycle/versions/FTS5.
3. **Intelligence** — embedders, vector stores, hybrid ranked retrieval, context analysis, quality analysis, export/import, Workbench webview.
4. **Local AI** — Ollama client, mode controller + fallback, RAG generation (parser, validator, repair loop), adaptation, model-free replay, cloud client (consent-gated).
5. **Agents** — 20 agents, orchestrator (planner, DAG, locks, verification gate, correction loop), 10 testing agents + harnesses, Agent Control Centre and Testing Centre.
6. **Execution** — permission-gated atomic file ops, recovery points/rollback, command runner (sanitised env, process-group kill), approvals.
7. **Hardening** — encrypted backup/restore, storage limits + GC, regression + performance suites, Playwright E2E, packaging with managed backend provisioning, docs.

## Defects found by the tests and fixed

Security-relevant: placeholder filter swallowed real secrets · secret detector flagged identifiers like `passed`/`tokenize` (and would have mangled captured code) ·
pattern store trusted callers to redact · literal bidi characters in source · `tree-sitter-language-pack` downloaded parsers at runtime (breaks offline) ·
restore rewound the audit trail · GC deleted baselines of open capture windows · stdlib XML parser on hostile test output (→ `defusedxml`) ·
scanner flagged its own rule patterns.
Correctness: audit/transaction self-deadlock · FK cycle · `requirements.txt` not a manifest · approving a deprecated pattern · `promote()` accepted demotions ·
hash-noise made nonsense queries match · stale vectors undetected · non-object JSON line crashed discovery · missing timestamps crashed discovery ·
**`verify` ran after `apply` was skipped** · `learn` skipped on failure so failures were never recorded · double-counted pattern uses · read-only mode made proposals errors ·
disabled/skipped agents blocked downstream steps · review override not honoured · error banner leaked an internal step key.
UI: success messages vanished on re-render · mobile overflow · unstyled outside VS Code · `null` rendered as text · views stale without pushed events (now poll).
Test-harness: pytest collecting a helper, a flaky `b"jwt" not in <random bytes>` assertion, inherited `ELECTRON_RUN_AS_NODE`, and several of my own wrong expectations.

Each has a named guard in `tests/regression/test_known_bugs.py` or the feature's own tests.

## Outstanding issues / honest limitations

* **No real LLM run** (see Verdict). Local model quality, small-model format compliance, and compatibility with a specific Ollama version are unmeasured.
  To verify: install Ollama, `ollama pull qwen2.5-coder:1.5b`, run `PV_OLLAMA_URL=http://127.0.0.1:11434 PV_OLLAMA_MODEL=qwen2.5-coder:1.5b pytest tests/e2e/test_live_ollama.py -m live`.
* Continue adapter unverified against a real install; Copilot/Cursor cannot be captured automatically; ChromaDB backend untested.
* Tested only on macOS/arm64 · Python 3.13 · VS Code 1.129 (Windows/Linux and Python 3.11/3.12 untested; POSIX process-group handling).
* Installing needs Python ≥ 3.11 and PyPI access once (*Set Up Backend*); a platform-independent offline install is not possible with compiled dependencies.
* The audit chain is tamper-evident, not tamper-proof; recovery points hold unencrypted raw copies of files being changed.
* Extraction/categorisation are heuristics; the default embedder is lexical.
* Dev-tooling `npm audit` shows 4 moderate advisories (`@vscode/vsce` transitives, not shipped).
* **Nothing has been committed**: all work is in the working tree. `dist/` holds the built wheel and VSIX.

## Where to look

[README](README.md) · [Install](docs/INSTALL.md) · [User guide](docs/USER_GUIDE.md) · [Architecture](docs/ARCHITECTURE.md) · [ADRs](docs/ADR.md) ·
[Security](docs/SECURITY.md) · [Limitations](docs/LIMITATIONS.md) · [API](docs/API.md) · [DB schema](docs/DATABASE_SCHEMA.md) · [Testing](docs/TESTING_STRATEGY.md) · [Roadmap](docs/ROADMAP.md)
