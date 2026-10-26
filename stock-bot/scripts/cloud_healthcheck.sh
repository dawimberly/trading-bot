#!/usr/bin/env bash
#
# cloud_healthcheck.sh — stale-heartbeat + service-down detector with Telegram alerting.
#
# Designed to run from cron every 5 minutes on the VPS, once per bot user:
#   */5 * * * * trader-paper /opt/PythonTrading/stock-bot/scripts/cloud_healthcheck.sh paper
#   */5 * * * * trader-live  /opt/PythonTrading/stock-bot/scripts/cloud_healthcheck.sh live
#
# It checks:
#   1. systemd unit is active (best-effort; skipped if systemctl unavailable)
#   2. heartbeat JSON exists and is fresh (age <= MAX_AGE_SEC)
#   3. last_cycle_error is null
# On failure it sends a Telegram alert via modules/alerts.send_telegram and debounces
# repeat alerts using a state file so cron does not spam every 5 minutes.
#
# Env overrides:
#   REPO_DIR         repo root (default: parent of this script's dir)
#   VENV_PYTHON      python to use (default: $REPO_DIR/../.venv/bin/python then python3)
#   MAX_AGE_SEC      stale threshold in seconds (default: 600)
#   ALERT_DEBOUNCE_SEC  min seconds between repeat alerts (default: 1800)
#   HEARTBEAT_FILE   explicit heartbeat path (overrides the bot-name default)
#
set -euo pipefail

BOT="${1:-paper}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
MAX_AGE_SEC="${MAX_AGE_SEC:-600}"
ALERT_DEBOUNCE_SEC="${ALERT_DEBOUNCE_SEC:-1800}"

# Resolve python: prefer the deploy venv, fall back to python3 on PATH.
if [[ -n "${VENV_PYTHON:-}" ]]; then
  PY="$VENV_PYTHON"
elif [[ -x "$REPO_DIR/../.venv/bin/python" ]]; then
  PY="$REPO_DIR/../.venv/bin/python"
elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
  PY="$REPO_DIR/.venv/bin/python"
else
  PY="python3"
fi

# Default heartbeat + systemd unit per bot.
case "$BOT" in
  paper)
    UNIT="paper-bot"
    DEFAULT_HB="$REPO_DIR/paper_chase_heartbeat.json"
    ;;
  live)
    UNIT="live-bot"
    DEFAULT_HB="$REPO_DIR/bot_heartbeat.json"
    ;;
  cloud)
    UNIT="cloud-bot"
    DEFAULT_HB="$REPO_DIR/cloud_bot/data/cloud_bot_heartbeat.json"
    ;;
  *)
    echo "Unknown bot '$BOT' (expected: paper | live | cloud)" >&2
    exit 2
    ;;
esac

HB="${HEARTBEAT_FILE:-$DEFAULT_HB}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.cache}/pythontrading-health"
mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/${BOT}_last_alert"

now_epoch="$(date +%s)"

# Startup diagnostics on stderr (never on stdout — the health parser below keys off
# an empty stdout for the "healthy" path, so keep this stream separate).
echo "[$(date -Is)] healthcheck start: bot=${BOT} unit=${UNIT} hb=${HB} py=${PY} max_age=${MAX_AGE_SEC}s debounce=${ALERT_DEBOUNCE_SEC}s" >&2

# --- Build the problem string (empty = healthy) ---
problem=""

# 1. systemd active check (best-effort).
if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl is-active --quiet "$UNIT"; then
    problem="service ${UNIT} is not active"
  fi
fi

# 2 + 3. Heartbeat freshness + last_cycle_error, parsed by python (handles ISO time).
if [[ -z "$problem" ]]; then
  hb_result="$(
    HB_PATH="$HB" MAX_AGE_SEC="$MAX_AGE_SEC" "$PY" - <<'PYEOF'
import datetime
import json
import os
import sys

path = os.environ["HB_PATH"]
max_age = int(os.environ["MAX_AGE_SEC"])

if not os.path.exists(path):
    print(f"heartbeat file missing: {path}")
    sys.exit(0)

try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:  # noqa: BLE001
    print(f"heartbeat unreadable: {exc}")
    sys.exit(0)

ts = data.get("timestamp")
age = None
if ts:
    try:
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        age = None
if age is None:
    # Fall back to file mtime.
    age = datetime.datetime.now().timestamp() - os.path.getmtime(path)

if age > max_age:
    print(f"heartbeat stale: {int(age)}s old (max {max_age}s)")
    sys.exit(0)

err = data.get("last_cycle_error")
if err:
    print(f"last_cycle_error: {str(err)[:200]}")
    sys.exit(0)

print("")  # healthy
PYEOF
  )"
  problem="$(printf '%s' "$hb_result" | head -n1)"
fi

# --- Healthy: clear state, exit 0 ---
if [[ -z "$problem" ]]; then
  rm -f "$STATE_FILE"
  echo "[$(date -Is)] ${BOT} OK (${HB})"
  exit 0
fi

# --- Unhealthy: debounce then alert ---
send_alert=1
if [[ -f "$STATE_FILE" ]]; then
  last_alert="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  if (( now_epoch - last_alert < ALERT_DEBOUNCE_SEC )); then
    send_alert=0
  fi
fi

echo "[$(date -Is)] ${BOT} UNHEALTHY: ${problem}" >&2

if (( send_alert == 1 )); then
  host="$(hostname 2>/dev/null || echo vps)"
  msg="[PythonTrading/${BOT}] ALERT on ${host}: ${problem}"
  cd "$REPO_DIR"
  ALERT_MSG="$msg" "$PY" - <<'PYEOF' || true
import os
import sys

try:
    from modules.alerts import send_telegram
except Exception as exc:  # noqa: BLE001
    print(f"could not import alerts: {exc}", file=sys.stderr)
    sys.exit(0)

ok = send_telegram(os.environ["ALERT_MSG"])
print("telegram sent" if ok else "telegram not configured / failed")
PYEOF
  echo "$now_epoch" > "$STATE_FILE"
fi

exit 1
