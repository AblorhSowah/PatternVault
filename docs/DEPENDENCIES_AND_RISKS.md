# Dependencies and Risks

## Selected dependencies (resolved 2026-11-16 on Python 3.13, macOS arm64)

| Area | Package | Version at selection | Why |
|---|---|---|---|
| Web | fastapi, starlette, uvicorn | latest stable | async REST + WebSocket |
| Validation | pydantic v2, pydantic-settings | 2.14 / 2.16 | typed config & API schemas |
| DB | SQLAlchemy 2.x, Alembic | 2.1 / latest | typed ORM, migrations |
| HTTP client | httpx | latest | async Ollama / OpenAI-compatible clients |
| Parsing | tree-sitter, tree-sitter-language-pack | 0.26 / 1.25 | multi-language structure without per-grammar installs |
| Vectors | numpy (default store), chromadb (optional) | 2.5 / extra | see ADR-004 |
| Files | watchfiles, pathspec | 1.3 / 1.1 | change observation, gitignore-style exclusions |
| Crypto | cryptography | latest | scrypt + Fernet backups |
| Quality | pytest, pytest-asyncio, pytest-cov, ruff, mypy, bandit, pip-audit | latest | see TESTING_STRATEGY |
| Extension | TypeScript, esbuild, vitest, eslint, @vscode/test-electron, @vscode/vsce | latest | build/test/package |
| UI tests | Playwright | latest | webview E2E |

Exact resolved versions are locked in `uv.lock` and `vscode-extension/package-lock.json`.

## Risks and mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Ollama not installed on dev machine | Cannot verify local generation end-to-end | Use a project-local Ollama binary + tiny model for verification; if impossible, document as unverified (no simulated success) |
| R2 | Providers expose little capture data | Learning incomplete | Honest capability flags; workspace/git observation; manual capture (ADR-007) |
| R3 | Small local models write weak code | Poor generation quality | RAG with verified patterns first; replay path needs no model; test-gated acceptance; hybrid mode |
| R4 | Hashing embeddings are lexical | Weaker semantic recall offline | FTS5 BM25 hybrid; use Ollama embeddings when available (ADR-005) |
| R5 | Secret leakage into knowledge base | Privacy breach | Exclusion rules before reading, redaction before persisting/embedding, secret-scan corpus tests |
| R6 | Agent executes destructive commands | Data loss | Central permission gate, denylist, recovery points, approvals, audit |
| R7 | Prompt injection via captured/retrieved content | Agent hijack | Untrusted-data framing, injection heuristics, no auto-exec of content |
| R8 | SQLite single-writer contention | Slow under heavy background indexing | WAL, short transactions, background queue with batching |
| R9 | Backend packaging inside VSIX | Install friction | Setup command creates a venv from the bundled wheel; documented limitation (needs network once for deps) |
| R10 | VS Code API drift | Extension breakage | Pin `engines.vscode`, extension-host tests in CI |
| R11 | Only tested on Python 3.13/macOS | Hidden portability bugs | Add 3.11/3.12 and Linux/Windows CI jobs before release |
| R12 | SQLAlchemy 2.1 is recent | Ecosystem lag | Constrain `<2.2`, covered by migration + ORM tests |


## Status at release (2026-11-17)

| # | Outcome |
|---|---|
| R1 | **Open** — real Ollama never executed (sandbox denied running the downloaded binary); opt-in live test provided |
| R2 | Mitigated — adapters report honest capabilities; Claude Code verified, Continue unverified, Copilot/Cursor workspace/manual only |
| R3 | Open — local-model quality unmeasured; mitigated by RAG, replay without a model, test-gated acceptance |
| R4 | Mitigated — similarity floor + FTS hybrid; semantic quality depends on an Ollama embedding model |
| R5 | Mitigated — exclusions + redaction at every boundary (incl. the store); residual regex false negatives documented |
| R6 | Mitigated — permission gate, denylist, recovery points, approvals, audit (tests) |
| R7 | Mitigated — untrusted-data framing, path validation, review agent; model obedience cannot be proven |
| R8 | Mitigated — WAL, short transactions; no contention seen at 1000 patterns |
| R9 | Resolved — *Set Up Backend* provisions a managed venv; verified in a clean profile (needs Python ≥ 3.11 + PyPI once) |
| R10 | Mitigated — extension-host tests on VS Code 1.129.1 only |
| R11 | **Open** — Windows/Linux and Python 3.11/3.12 untested |
| R12 | Mitigated — SQLAlchemy 2.1 passed migration/ORM/API suites |
| R13 | New — TypeScript pinned to 6.0.x (typescript-eslint does not yet support 7) |
| R14 | New — `tree-sitter-language-pack` rejected (runtime downloads); one grammar wheel per language instead |

Added runtime dependencies since the plan: `psutil` (resource monitoring), `defusedxml` (hardened junit parsing).
