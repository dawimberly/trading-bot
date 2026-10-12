#!/usr/bin/env bash
# Cloud Agent install: prepare the active stock-bot project (Python venv + deps).
# Idempotent: safe to re-run against a cached or partially prepared workspace.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/stock-bot"

# System packages the default image lacks: venv support + build headers for
# any source builds. Non-interactive; skipped quickly when already installed.
if command -v sudo >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-venv python3-dev build-essential
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip -q
pip install -r requirements.txt

mkdir -p logs
echo "stock-bot environment ready."
