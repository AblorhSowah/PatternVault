# PatternVault — Project Progress (final report)

_Last updated: 2026-11-17. Every number below was copied from an actual run of `scripts/check.sh --host --package` (the final run
passed all stages) or from the named command. Nothing is estimated._

## Verdict

PatternVault 0.1.0 is a working, tested, packaged system. **Generation and chat with a real local model have been verified only narrowly.**
Ollama 0.40.16 was installed later (2026-11-17) and one model, `qwen2.5-coder:7b`, was run for real. **The first real run failed** — the protocol doubles had hidden
five defects (see "Real-model findings" below) — and after the fixes 6 of 6 identical probe requests returned two files that passed their own tests. That is one model and one
small task; other models, larger tasks, adapt-with-stored-patterns, the `/task` agent loop and non-Python languages are still unverified. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Real-model findings (2026-11-17, `qwen2.5-coder:7b`, Ollama 0.40.16, macOS arm64, 16 GB, ~15 tokens/s)

The opt-in live test failed on its first run. Probing with every raw answer saved showed (6 identical generation requests, code before the fixes → after):

| | Before the fixes | After |
|---|---|---|
| Clean result (both files, 1 attempt, no issue) | **0 of 6** | **6 of 6** |
| Wrote correct files, then repeated one sentence until the output cap | 4 of 6 (~93 s each) | 0 (stopped early, warning shown) |
| No usable files (markdown fences with a bare `# path` line; 3 repairs got the same) | 2 of 6 | 0 |
| Time per request | 53–95 s | 16–31 s |
| Generated code run with pytest | n/a | 2 passed in each of the 6 runs |

Defects fixed: no output cap (a model that never stops ran to the HTTP timeout — new `Max answer length` setting, `num_predict`) · looping after the files (streaming early stop) ·
parser did not accept a bare path comment, and an unterminated explanation · repair prompt did not show the literal format · the chat prompt's example `[#12]` was copied verbatim (the model
cited a pattern that does not exist). A test-isolation bug surfaced at the same time: one E2E test silently assumed nothing was listening on Ollama's default port; the shared fixtures now pin a closed port.
Pinned by `tests/regression/test_real_model_output.py` (real recorded answers; the old parser is confirmed to fail them). Real chat samples: a greeting (3.8 s), a grounded question (46 s for 711 tokens), a deliberately capped answer.

## Addendum — Chat (added after the first release candidate)

A chat in the VS Code side bar (and as a Workbench section) was added at the user's request: knowledge-grounded answers streamed from the local model with clickable source citations, local
conversation history, slash commands (`/search /context /generate /task /recreate /status /new /help`), *Ask About Selection*, code-block Copy / Open in editor, and *Settings → Delete all chats…*.
It is covered by `tests/integration/test_chat.py` (17 tests), `tests/e2e/test_chat_ui.py` (17 tests in real Chromium), `vscode-extension/test/unit/chat.test.ts` and a real-VS-Code host test. Like generation, it was
exercised against Ollama protocol doubles and then a handful of real-model answers (see "Real-model findings"); with no model it answers from stored patterns and says so. See the *Chat* sections of the README, user guide, security model and limitations.

## Phase status

| Phase | Status |
|---|---|
| 1 Foundation | ✅ |
| 2 Knowledge capture | ✅ |
| 3 Knowledge intelligence | ✅ |
| 4 Local AI | 🟡 tested against protocol doubles **and one real model on a small task** (found and fixed 5 defects); other models/tasks unverified |
| 5 Multi-agent engine | ✅ |
| 6 Autonomous execution | ✅ (built together with 5: the orchestrator depends on it) |
| 7 Hardening & release | ✅ |
| + Chat | 🟡 tested against protocol doubles and a handful of real-model answers; quality not measured |

## Final verification run (`scripts/check.sh --host --package`, exit 0)

| Stage | Result |
|---|---|
| ruff lint + format check | clean |
| mypy (111 source files) | clean |
| bandit | clean (0 issues; B101/B404/B603 skipped by documented policy) |
| pytest — unit, integration, security, regression | **614 passed**, 0 failed — coverage **87.5 %** of `backend/app` |
| performance budgets (`-m performance`) | **5 passed** (numbers below) |
| Playwright E2E (real Chromium + real backend process) | **42 passed** (the 1 live-Ollama test is deselected here; it was run separately against the real model and passed) |
| tsc / ESLint / esbuild | clean |
| Vitest (unit + real-backend integration) | **141 passed** |
| VS Code extension-host tests (real VS Code 1.129.1) | **6 passed** |
| Wheel + VSIX build | `patternvault-0.1.0-py3-none-any.whl` (≈219 KB), `patternvault-0.1.0.vsix` (≈283 KB, bundles the wheel) |
| **Clean-profile install of the VSIX in real VS Code** | **5 passed**: fresh user-data/extensions dirs → VSIX installed → *Set Up Backend* built a private venv from the bundled wheel (17–42 s) → backend served requests, reported 0.1.0 → capture window produced a pattern |

Total executed: 813 automated checks (614 + 5 + 42 + 141 + 6 + 5), plus the live test run separately against the real model. Code size: backend 16.4 k lines (111 files), Python tests 8.2 k lines, extension 1.8 k lines TS +
1.3 k lines webview JS/CSS, extension tests 1.6 k lines.

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
| C | Independent generation: local model, no cloud, stored knowledge, recreate, adapt | recreate ✅ (model-free, verified end to end); new code ✅ with a **real local 7B model** on a small task (6/6 after fixes); adapt ✅ against protocol doubles only — **adapt with a real model ⚠ not run** |
| D | Agent collaboration: assign, coordinate, detect failures, request corrections, verify corrections | `test_orchestrator` (correction loop, independent retest, learned-solution reuse, rollback, locks, approvals) ✅ |
| E | Automated testing: unit/integration/e2e/security checks, reports, regression detection | `test_testing_agents` — ten agents on real projects and on PatternVault itself ✅ |
| F | Offline: starts without internet, local repository, local model, generate+test without cloud | starts/operates offline ✅ (`offline` mode never calls cloud; hashing embedder); local-model generation + tests without cloud ✅ for the small task above; ⚠ larger tasks as in C |
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
Test-harness: **the E2E static file server (default listen backlog of 5) reset connections when Chromium fetched the webview's modules in a burst — blank page, `ERR_CONNECTION_RESET`, ~50 % of full runs once the chat added three more modules; reproduced in isolation (70 of 900 connections failed at backlog 5, 0 at 128), fixed in the harness, and failures now leave a screenshot + network log in `test-results/`** · the E2E shim hung instead of reporting 503 when the backend was down · stylesheet typo that dropped input borders · pytest collecting a helper, a flaky `b"jwt" not in <random bytes>` assertion, inherited `ELECTRON_RUN_AS_NODE`, and several of my own wrong expectations.

Each has a named guard in `tests/regression/test_known_bugs.py` or the feature's own tests.

## Outstanding issues / honest limitations

* **Real-model coverage is narrow** (see Verdict): one 7B model, one small Python task, Ollama 0.40.16. Not measured: other/smaller models (`PV_OLLAMA_MODEL=qwen2.5-coder:1.5b pytest tests/e2e/test_live_ollama.py -m live`),
  multi-file tasks, adapt-with-stored-patterns, `/task`, other languages. At ~15 tokens/s a 700-token chat answer takes ~46 s. The 7B model sometimes writes "Pattern #1" instead of `[#1]` (the source chips under each answer stay correct).
* Continue adapter unverified against a real install; Copilot/Cursor cannot be captured automatically; ChromaDB backend untested.
* Tested only on macOS/arm64 · Python 3.13 · VS Code 1.129 (Windows/Linux and Python 3.11/3.12 untested; POSIX process-group handling).
* Installing needs Python ≥ 3.11 and PyPI access once (*Set Up Backend*); a platform-independent offline install is not possible with compiled dependencies.
* The audit chain is tamper-evident, not tamper-proof; recovery points hold unencrypted raw copies of files being changed.
* Extraction/categorisation are heuristics; the default embedder is lexical.
* Dev-tooling `npm audit` shows 4 moderate advisories (`@vscode/vsce` transitives, not shipped).
* The work is committed and pushed to `origin/main`. `dist/` (built wheel and VSIX) is git-ignored; rebuild it with `./scripts/package.sh`.

## Where to look

[README](README.md) · [Install](docs/INSTALL.md) · [User guide](docs/USER_GUIDE.md) · [Architecture](docs/ARCHITECTURE.md) · [ADRs](docs/ADR.md) ·
[Security](docs/SECURITY.md) · [Limitations](docs/LIMITATIONS.md) · [API](docs/API.md) · [DB schema](docs/DATABASE_SCHEMA.md) · [Testing](docs/TESTING_STRATEGY.md) · [Roadmap](docs/ROADMAP.md)
