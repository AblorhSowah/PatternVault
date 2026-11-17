#!/usr/bin/env bash
# Full verification pipeline. Exits non-zero on the first failing stage.
#   scripts/check.sh --package  additionally builds the wheel+VSIX and verifies a clean-profile install (needs network + PV_SYSTEM_PYTHON)
#   scripts/check.sh          backend + extension unit/integration checks
#   scripts/check.sh --host   additionally runs the VS Code extension-host tests (opens a VS Code window)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
stage() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

stage "ruff (lint)";            .venv/bin/ruff check backend tests
stage "ruff (format check)";    .venv/bin/ruff format --check backend tests
stage "mypy";                   .venv/bin/mypy
stage "bandit (security lint)"; .venv/bin/bandit -q -r backend/app -c pyproject.toml
stage "pytest + coverage";      $PY -m pytest tests -q --cov=backend/app --cov-report=term --cov-report=json:coverage.json -m "not e2e and not performance and not live"
stage "performance budgets";    $PY -m pytest tests/performance -q -m performance
stage "e2e (Playwright + real backend)"; $PY -m pytest tests/e2e -q -m e2e
stage "extension: tsc";         npm --prefix vscode-extension run typecheck
stage "extension: eslint";      npm --prefix vscode-extension run lint
stage "extension: vitest";      npm --prefix vscode-extension test
stage "extension: build";       npm --prefix vscode-extension run build
if [[ "${1:-}" == "--host" ]]; then
  stage "extension host tests (real VS Code)"
  npm --prefix vscode-extension run compile:tests
  env -u ELECTRON_RUN_AS_NODE npm --prefix vscode-extension run test:host
fi
if [[ "${1:-}" == "--package" || "${2:-}" == "--package" ]]; then
  stage "package (wheel + VSIX)"; ./scripts/package.sh
  stage "clean-profile install of the VSIX in real VS Code"
  npm --prefix vscode-extension run compile:tests
  (cd vscode-extension && env -u ELECTRON_RUN_AS_NODE node out/test/packaged/runPackaged.js)
fi
printf '\n\033[32mAll stages passed.\033[0m\n'
