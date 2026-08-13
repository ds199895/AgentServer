#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then PYTHON_BIN="$ROOT_DIR/.venv/bin/python"; fi

"$PYTHON_BIN" -m unittest discover -s tests -v
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix e2e ci
bash -n scripts/*.sh
git diff --check
