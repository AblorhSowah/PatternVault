# Installation

## Requirements
* VS Code ≥ 1.100, Python ≥ 3.11 (only used to create the backend's private environment), network access to PyPI **once** for the backend's dependencies.
* Optional: [Ollama](https://ollama.com) + a coding model (e.g. `ollama pull qwen2.5-coder:7b`) for local generation. RAM guidance: `backend/app/generation/model_catalog.json`.

## From a VSIX (end users)
1. Build or obtain `patternvault-0.1.0.vsix` (`./scripts/package.sh` produces it in `dist/`).
2. `code --install-extension patternvault-0.1.0.vsix` (or *Extensions → … → Install from VSIX*).
3. Run **PatternVault: Set Up Backend**. It creates a private virtual environment in VS Code's global storage and installs the wheel bundled in the VSIX.
   If it cannot find Python 3.11+, set `patternvault.backend.systemPython`.
4. Open the PatternVault activity-bar view or run **PatternVault: Open Workbench**.

Data lives in `~/.patternvault` (override with `patternvault.backend.home` / `PATTERNVAULT_HOME`).

## From source (developers)
```bash
npm run setup        # uv sync (Python 3.13) + npm install
npm run check        # ruff, mypy, bandit, pytest+coverage, Playwright E2E, tsc, eslint, vitest, build
npm run check:full   # … plus extension-host tests in real VS Code
./scripts/package.sh # wheel + VSIX into dist/
```
Press **F5** in VS Code on `vscode-extension/` to debug the extension; the backend is found in the repo's `.venv` automatically.

## Verifying an installation
`cd vscode-extension && npx tsc -p tsconfig.test.json && PV_SYSTEM_PYTHON=/path/to/python3.13 node out/test/packaged/runPackaged.js`
installs the VSIX into a throw-away VS Code profile, provisions the backend and runs a capture end to end.

## Running the backend without VS Code
`pip install dist/patternvault-0.1.0-py3-none-any.whl && PATTERNVAULT_TOKEN=$(openssl rand -hex 24) patternvault-backend --port 8765`
