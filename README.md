# PatternVault

**Local-first AI development intelligence for VS Code.** PatternVault observes AI-assisted development work (using only supported, local
sources), distils it into reusable knowledge that is only trusted once **tests have actually passed**, and reuses it with a locally hosted
model when your cloud assistant is unavailable.

> Version 0.1.0. What works, and what has *not* been verified, is stated plainly in [PROJECT_PROGRESS.md](PROJECT_PROGRESS.md) and
> [docs/LIMITATIONS.md](docs/LIMITATIONS.md). Most importantly: **no real LLM was run during development** (see Limitations).

## What it does

* **Learn** — capture windows and assistant-log import (Claude Code verified; Continue unverified; Copilot/Cursor have no supported capture API),
  secret redaction, tree-sitter structure extraction, explainable categorisation, deduplicated/versioned patterns with a strict
  `observed → generated → approved → tested → verified` lifecycle.
* **Retrieve** — hybrid FTS5 + vector search ranked by relevance × trust, framework/version-aware, with a missing-context report.
* **Generate & recreate** — RAG over stored patterns with a local Ollama model (new code, adaptation of existing patterns) and a model-free replay
  of stored implementations; outputs are validated, repaired and reviewed before they are proposed.
* **Agents** — 20 agents coordinated by an orchestrator: a permission-gated apply → test → debug → retest → rollback cycle with recovery points,
  approvals, file locks and a verification gate; ten dedicated testing agents.
* **Operate safely** — 4 permission levels, destructive commands always blocked, audit hash-chain, encrypted backups, host-enforced webview policy.

## Layout

| Path | Purpose |
|---|---|
| `backend/app/` | FastAPI backend (api, agents, orchestration, learning, extraction, retrieval, generation, replay, testing, execution, security, knowledge, database) |
| `vscode-extension/` | TypeScript extension: backend supervisor, status bar, commands, **Workbench** webview (8 sections) |
| `tests/` | `unit/ integration/ security/ regression/ performance/ e2e/` (Playwright) |
| `docs/` | [Architecture](docs/ARCHITECTURE.md) · [ADRs](docs/ADR.md) · [Security](docs/SECURITY.md) · [Limitations](docs/LIMITATIONS.md) · [Install](docs/INSTALL.md) · [User guide](docs/USER_GUIDE.md) · [API](docs/API.md) · [DB schema](docs/DATABASE_SCHEMA.md) · [Testing](docs/TESTING_STRATEGY.md) · [Risks](docs/DEPENDENCIES_AND_RISKS.md) · [Roadmap](docs/ROADMAP.md) |
| `scripts/` | `check.sh` (full verification), `package.sh` (wheel + VSIX) |

## Quick start (developers)

```bash
npm run setup          # uv sync (Python 3.13) + npm install
npm run check          # ruff, mypy, bandit, pytest+coverage, performance, Playwright E2E, tsc, eslint, vitest, build
npm run check:full     # … plus extension-host tests in real VS Code
./scripts/package.sh   # dist/patternvault-0.1.0-py3-none-any.whl + dist/patternvault-0.1.0.vsix
```

Install the VSIX, run **PatternVault: Set Up Backend** once, then **PatternVault: Open Workbench**. See [docs/INSTALL.md](docs/INSTALL.md).

## Security posture (summary)

Loopback-only API with per-launch bearer token · secrets redacted before anything is stored/embedded/prompted · `.env`/keys never read ·
API keys only in VS Code SecretStorage · untrusted text is data, never instructions · recovery point before every agent change ·
verification only through real passing tests. Details and residual risks: [docs/SECURITY.md](docs/SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
