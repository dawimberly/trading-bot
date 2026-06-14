#!/usr/bin/env bash
# UFC Betting Bot — first-time setup for friends (clone from GitHub)
set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$BOT_DIR/.." && pwd)"
PRED="$REPO/ufc-predictor"

cd "$BOT_DIR"

if ! command -v python3 &>/dev/null; then
  echo "Install Python 3.11+ first."
  exit 1
fi

if [ ! -f "$PRED/config.py" ]; then
  echo "ufc-predictor not found. Clone the full trading-bot repo."
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH="$REPO"

pip install -r requirements.txt -q
pip install -r "$PRED/requirements.txt" -q

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — add THE_ODDS_API_KEY for live odds (optional)."
fi

if [ ! -f "$PRED/models/ensemble_winner.joblib" ]; then
  echo ""
  echo "First run: download data + train model (~15-30 min)..."
  echo ""
  cd "$PRED"
  python3 main.py --refresh-data --train
  cd "$BOT_DIR"
fi

echo ""
echo "UFC Betting Bot is ready — http://localhost:8502"
echo ""
streamlit run dashboard/app.py --server.port 8502
