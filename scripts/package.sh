#!/usr/bin/env bash
# Build the backend wheel and the VSIX (which bundles that wheel). Output: dist/*.whl and dist/*.vsix
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf dist vscode-extension/backend/*.whl
uv build --wheel --out-dir dist
cp dist/patternvault-*.whl vscode-extension/backend/
npm --prefix vscode-extension run build
(cd vscode-extension && npx vsce package --no-dependencies --skip-license --out ../dist/)
ls -lh dist
