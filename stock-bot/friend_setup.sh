#!/usr/bin/env bash
# PythonTrading — first-time setup for friends (clone from GitHub, run locally)
set -e
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
  echo "Install Python 3.11+ first."
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements.txt -q
mkdir -p logs

echo ""
echo "PythonTrading is ready."
echo "  1. Register in the portal"
echo "  2. Enter Alpaca PAPER API keys"
echo "  3. Bot tab: Download market data, then Start bot"
echo ""
streamlit run portal.py
