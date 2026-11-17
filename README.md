# PatternVault

**Local-first AI development intelligence for VS Code.** PatternVault observes AI-assisted development work (using only supported, local
sources), distils it into reusable knowledge that is only trusted once **tests have actually passed**, and reuses it with a locally hosted
model when your cloud assistant is unavailable.

> Version 0.1.0. What works, and what has *not* been verified, is stated plainly in [PROJECT_PROGRESS.md](PROJECT_PROGRESS.md) and
> [docs/LIMITATIONS.md](docs/LIMITATIONS.md). Most importantly: **no real LLM was run during development** (see Limitations).

## What it does

* **Chat** — a chat in the VS Code side bar: ask about your stored patterns, get streamed answers that cite them, search, generate and run agent tasks with slash
  commands, or ask about the code you have selected. Works (honestly, from stored patterns) even when no model is running.
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
| `vscode-extension/` | TypeScript extension: backend supervisor, status bar, commands, **Workbench** webview (8 sections: Dashboard, Chat, AI Learning Centre, Pattern Library, Code Generation, Agent Control Centre, Testing Centre, Settings) |
| `tests/` | `unit/ integration/ security/ regression/ performance/ e2e/` (Playwright) |
| `docs/` | [Architecture](docs/ARCHITECTURE.md) · [ADRs](docs/ADR.md) · [Security](docs/SECURITY.md) · [Limitations](docs/LIMITATIONS.md) · [Install](docs/INSTALL.md) · [User guide](docs/USER_GUIDE.md) · [API](docs/API.md) · [DB schema](docs/DATABASE_SCHEMA.md) · [Testing](docs/TESTING_STRATEGY.md) · [Risks](docs/DEPENDENCIES_AND_RISKS.md) · [Roadmap](docs/ROADMAP.md) |
| `scripts/` | `check.sh` (full verification), `package.sh` (wheel + VSIX) |

## Install

### Requirements

| | Needed for | Notes |
|---|---|---|
| **VS Code ≥ 1.100** | the extension | |
| **Python ≥ 3.11** | the backend | Only used to create a private virtual environment; it never touches your global Python. |
| **Internet access, once** | installing the backend's Python dependencies from PyPI | After that, PatternVault works offline. |
| **[Ollama](https://ollama.com)** + a coding model | *optional*: local code generation | Not needed for capture, search, replay or testing. |
| **Node.js + npm, and [uv](https://docs.astral.sh/uv/)** | *only to build the VSIX from source* | Not needed if you are given a ready-made `.vsix`. |

### Option A — Install the extension (VSIX)

1. **Get the VSIX.** There is no marketplace listing yet, so build it from source:

   ```bash
   git clone https://github.com/AblorhSowah/PatternVault.git
   cd PatternVault
   npm run setup          # uv sync (Python 3.13) + npm install
   ./scripts/package.sh   # → dist/patternvault-0.1.0.vsix (bundles the backend wheel)
   ```

   If someone gave you `patternvault-0.1.0.vsix`, skip to step 2.

2. **Install it in VS Code.** Either run

   ```bash
   code --install-extension dist/patternvault-0.1.0.vsix
   ```

   or open the **Extensions** view → **⋯** menu → **Install from VSIX…** and pick the file.

3. **Create the backend environment (once).** Open the Command Palette (`⇧⌘P` on macOS, `Ctrl+Shift+P` elsewhere) and run
   **PatternVault: Set Up Backend (create Python environment)**. It builds a private virtual environment in VS Code's global storage and installs the wheel
   bundled in the VSIX (17–42 seconds in my clean-profile test; it downloads the dependencies from PyPI). If it says it cannot find Python 3.11+, set `patternvault.backend.systemPython` to the interpreter's path
   (for example `/usr/local/bin/python3.12`) and run the command again.

4. **Open it.** Click the **PatternVault** icon in the activity bar — the **Chat** is at the top of the side bar — or run **PatternVault: Open Workbench** for the full UI. The backend starts automatically with VS Code
   (setting `patternvault.backend.autoStart`); the status bar shows its state and the current mode.

**Upgrading** from an earlier build of the same version: install the new VSIX, then run **PatternVault: Set Up Backend** again. It rebuilds the private Python environment from the
wheel bundled in the new VSIX, so the UI and the backend always match; without this step the new UI would talk to the old backend. Your data is untouched, and new tables are added automatically
the next time the backend starts (verified on a database created by the previous build).

Your data is stored in `~/.patternvault` (change it with `patternvault.backend.home` or the `PATTERNVAULT_HOME` environment variable).

### Option B — Run from source (for development)

```bash
npm run setup          # uv sync (Python 3.13) + npm install
npm run check          # ruff, mypy, bandit, pytest+coverage, performance, Playwright E2E, tsc, eslint, vitest, build
npm run check:full     # … plus extension-host tests in real VS Code
./scripts/package.sh   # dist/patternvault-0.1.0-py3-none-any.whl + dist/patternvault-0.1.0.vsix
```

Open the `vscode-extension/` folder in VS Code and press **F5**; the extension finds the backend in the repository's `.venv` automatically.

### Optional — Set up a local model (Ollama)

Generating *new* or *adapted* code needs a local model. (Capture, search, review, testing and **recreating a stored pattern** do not.)

1. Install Ollama from [ollama.com](https://ollama.com) and make sure it is running (the desktop app, or `ollama serve`).
2. Download a coding model. Pick by RAM; `qwen2.5-coder:7b` is a good default for a 16 GB machine:

   ```bash
   ollama pull qwen2.5-coder:1.5b   # ~4 GB RAM — fast, modest quality
   ollama pull qwen2.5-coder:7b     # ~8 GB RAM — recommended
   ollama pull nomic-embed-text     # optional: better semantic search than the built-in lexical embedder
   ```

3. In the Workbench open **Settings → Local model**, choose the model, click **Use**, then **Test**. The Dashboard shows *Ollama … ready* when it works.
   PatternVault talks to Ollama at `http://127.0.0.1:11434` by default, and only to a local address unless you explicitly allow otherwise.

> **Heads-up:** the Ollama integration is tested against protocol doubles, not a real model (see [Limitations](docs/LIMITATIONS.md)).
> After step 3, run the opt-in live test once to confirm it on your machine:
> `PV_OLLAMA_URL=http://127.0.0.1:11434 PV_OLLAMA_MODEL=qwen2.5-coder:1.5b .venv/bin/python -m pytest tests/e2e/test_live_ollama.py -m live`

## Use it

PatternVault starts in **Learning** mode with **assisted** permissions (every write or command needs your approval). A typical first session:

### Chat with PatternVault

The quickest way in. The **Chat** view is at the top of the PatternVault side bar (click the PatternVault icon in the activity bar). It is also a section of the Workbench,
and **PatternVault: Open Chat** jumps to it.

* **Ask in plain language** — "how did we do password hashing?", "what do I have for pagination?". PatternVault retrieves the matching patterns from your knowledge base and
  your local model answers from them, streaming as it writes. Answers cite the patterns they used as `#12` chips you can click; a citation of a pattern that was *not*
  retrieved is flagged instead of trusted. Untick **Use my patterns** to ask a general question without retrieval.
* **No model running? It still helps.** If Ollama is off or no model is selected, the chat says so and answers with the matching stored patterns. It never pretends a model replied.
* **Ask about code you are reading** — select code in an editor, right-click → **PatternVault: Ask About Selection** (or use the Command Palette). The question is put in the chat box
  for you to read, edit and send. Files that look like they hold credentials (`.env`, keys, …) are refused.
* **Slash commands** — type `/` in the chat box:

  | Command | What it does |
  |---|---|
  | `/search <words>` | search your stored patterns; **Show** a result, or **Ask about this** |
  | `/context <request>` | how a request is understood and what knowledge is missing |
  | `/generate <request>` | propose code as a reviewable diff — **nothing is written** |
  | `/task <request>` | have the agents apply it: recovery point → review → apply → tests → auto-fix → rollback. You are asked to confirm first |
  | `/recreate <pattern id>` | replay a stored implementation into the project, with tests (confirmation first) |
  | `/status` | mode, local model and knowledge-base summary |
  | `/new`, `/help` | new conversation, list of commands |

  `/generate`, `/task` and `/recreate` work on a project: pick it in the selector at the top of the chat (a lone registered project is preselected).
* **Code blocks** have **Copy** and **Open in editor** (a new, unsaved tab). Press **Enter** to send, **Shift+Enter** for a new line, **Stop** to cut an answer short.
* **Conversations are saved locally** (`~/.patternvault`). Switch between them or **Delete** one from the header. Secrets are redacted before a message is sent to the model or stored.
* **The chat can only talk.** It has no way to change files or run commands by itself; `/task` is the only route to changes, and it always asks first and takes a recovery point.

### 1. Teach it — capture what your AI assistant does

* **Capture window (works with any assistant):** run **PatternVault: Start Capture Window**, do your work with Copilot, Cursor, Claude Code, etc., then run
  **PatternVault: End Capture Window and Learn**. PatternVault diffs the files that changed, redacts secrets, extracts patterns and stores them. The first capture
  registers the open folder as a project automatically (or use **Register Current Workspace as Project**).
* **Import past sessions:** **PatternVault: Import AI Assistant Sessions** reads supported local logs (Claude Code verified, Continue unverified).
  Copilot and Cursor expose no readable logs, so use capture windows for them — **PatternVault: Show Supported AI Assistants** lists what each can do.

### 2. Review what it learned — **AI Learning Centre**

New patterns start as **observed**. Approve the good ones and reject or deprecate the rest. `tested` and `verified` status can only be earned by a *passing test run*;
clicking *Approve* never grants it.

### 3. Find knowledge — **Pattern Library**

Search by meaning, file, symbol or route (for example "jwt refresh token middleware"). Results are ranked by relevance × trust; select one to see its code,
dependencies, framework/version, evidence and version history. Export your knowledge or create an encrypted backup from **Settings**.

### 4. Generate or recreate code — **Code Generation**

1. Pick the project and describe what you want.
2. **Analyze context** shows how the request was understood and what knowledge is missing.
3. Choose what to do:
   * **Generate** / **Plan recreation** — *propose* changes (diffs plus automatic checks). Nothing is written to disk.
   * **Run end-to-end task** — recovery point → review → apply → run tests → (if they fail) debugging agent fixes → independent retest → roll back if still failing →
     learn only from verified results.
   * **Recreate and verify…** — replays a stored implementation into a project **without any model**.

### 5. Test and supervise — **Testing Centre** and **Agent Control Centre**

Ten testing agents (unit, integration, e2e, API, database, extension, security, performance, regression, code quality) report expected vs actual, error details, the
correction applied and the retest result. Anything that could not run is shown as **skipped**, never "passed". The Agent Control Centre is where approvals, running
tasks, recovery points (**Restore** undoes a change) and agent activity live.

### Modes and permissions

| Setting | Options | Change it with |
|---|---|---|
| **Mode** | *Learning* (observe and learn) · *Offline* (local knowledge + local model only, never any network) · *Hybrid* (local model plus a cloud endpoint you consented to) | status bar, or **PatternVault: Switch Operating Mode** |
| **Permission level** | `read_only` (proposals only) · `assisted` (approve each write/command) · `approved_execution` (approve the plan once) · `restricted_autonomous` (safe in-project edits and allow-listed test commands run on their own) | **PatternVault: Set Agent Permission Level** |

Destructive commands, writes outside the project and `.git` writes are **always** blocked, at every level. API keys for cloud endpoints go to VS Code SecretStorage
(**PatternVault: Store Provider API Key**), never to a file. More detail: [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

### Commands you may need

| Command | What it does |
|---|---|
| **PatternVault: Open Chat** | focus the chat in the side bar |
| **PatternVault: Ask About Selection** | put the selected code in the chat box as a question (also in the editor's right-click menu) |
| **PatternVault: Show Backend Log** | first stop for any problem |
| **PatternVault: Restart Backend** / **Stop Backend** / **Start Backend** | control the local server |
| **PatternVault: Analyze Current Project** | shows the detected language, frameworks and file count |
| **PatternVault: Refresh Status** | re-read backend state |

### Troubleshooting

| Symptom | Fix |
|---|---|
| *Set Up Backend* cannot find Python | Install Python 3.11+ and set `patternvault.backend.systemPython` to its full path. |
| Status bar says the backend is stopped | Run **Show Backend Log**, then **Restart Backend**. If you skipped step 3, run **Set Up Backend**. |
| Startup times out on a slow machine | Raise `patternvault.backend.startupTimeoutMs` (default 30000). |
| Dashboard says *local model server not running* | Start Ollama, confirm with `ollama list`, then **Settings → Local model**. |
| Chat says *No local model is ready* | It still answers from your stored patterns. For written answers, start Ollama and pick a model in **Settings → Local model**. |
| Chat does not answer or shows *could not reach the backend* | Run **Show Backend Log** then **Restart Backend**; the chat retries on its own. |
| An answer or generation says it was *cut off at the output limit* | The model hit **Settings → Local model → Max answer length** (default 3072 tokens, never more than half the context window). Raise it, or ask for fewer files at a time. |
| Generation says the model is not ready | Pull a model (`ollama pull qwen2.5-coder:7b`), select it in Settings and click **Test**. |
| Search finds little | Capture or import more sessions; for better semantic recall pull `nomic-embed-text`, select it, and click **Re-index**. |
| Start over | Stop the backend, remove `~/.patternvault`. (**Back up first**: Settings → Backup and restore.) |

### Run the backend without VS Code

```bash
pip install dist/patternvault-0.1.0-py3-none-any.whl
PATTERNVAULT_TOKEN=$(openssl rand -hex 24) patternvault-backend --port 8765
```

The API is loopback-only and requires that bearer token. See [docs/API.md](docs/API.md).

### Uninstall

Uninstall the extension from the Extensions view. Two things are left behind on purpose, and you can delete them if you want a clean removal: your knowledge base in
`~/.patternvault` (back it up first if you may want it again) and the managed Python environment, which is a `venv` folder inside the extension's global-storage
directory in VS Code's user data.

Full install notes: [docs/INSTALL.md](docs/INSTALL.md).

## Security posture (summary)

Loopback-only API with per-launch bearer token · secrets redacted before anything is stored/embedded/prompted · `.env`/keys never read ·
API keys only in VS Code SecretStorage · untrusted text is data, never instructions · recovery point before every agent change ·
verification only through real passing tests. Details and residual risks: [docs/SECURITY.md](docs/SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
