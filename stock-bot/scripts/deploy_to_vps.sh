#!/usr/bin/env bash
#
# deploy_to_vps.sh — safe git-based deploy for the PythonTrading bots on Ubuntu 24.04.
#
# Run on the VPS as the deploy user (needs sudo for systemctl restart):
#   ./scripts/deploy_to_vps.sh <git-ref>
#   ./scripts/deploy_to_vps.sh v1.5.3
#   ./scripts/deploy_to_vps.sh origin/main
#
# Workflow:
#   1. Verify clean working tree (no local edits clobbered)
#   2. git fetch + checkout the requested ref (tag/branch/commit)
#   3. pip install -r requirements.txt into the shared venv
#   4. Run allocator smoke test (fast parity gate)
#   5. Restart paper-bot (always) and live-bot (only with --restart-live)
#   6. Verify both units are active and print heartbeat status
#
# Flags:
#   --restart-live     also restart live-bot.service (default: paper only)
#   --skip-tests       skip the smoke test gate (not recommended)
#   --no-restart       deploy code only, do not touch services
#
# Env overrides:
#   REPO_DIR       repo root (default: parent of this script's dir)
#   VENV_DIR       venv dir (default: $REPO_DIR/../.venv)
#   PIP_ARGS       extra pip args (default: -q)
#
set -euo pipefail

GIT_REF="${1:-}"
if [[ -z "$GIT_REF" || "$GIT_REF" == --* ]]; then
  echo "Usage: $0 <git-ref> [--restart-live] [--skip-tests] [--no-restart]" >&2
  exit 2
fi
shift || true

RESTART_LIVE=0
SKIP_TESTS=0
NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --restart-live) RESTART_LIVE=1 ;;
    --skip-tests)   SKIP_TESTS=1 ;;
    --no-restart)   NO_RESTART=1 ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
VENV_DIR="${VENV_DIR:-$(cd "$REPO_DIR/.." && pwd)/.venv}"
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
PIP_ARGS="${PIP_ARGS:--q}"
START_TS="$(date +%s)"

log() { echo "[deploy $(date -Is)] $*"; }

log "config: repo=$REPO_DIR venv=$VENV_DIR ref=$GIT_REF restart_live=$RESTART_LIVE skip_tests=$SKIP_TESTS no_restart=$NO_RESTART"

cd "$REPO_DIR"

# 1. Safety: refuse to deploy over uncommitted changes.
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree is dirty. Commit/stash local changes before deploy." >&2
  git status --short >&2
  exit 1
fi

PREV_REF="$(git rev-parse --short HEAD)"
log "current HEAD: $PREV_REF"

# 2. Fetch + checkout requested ref.
log "fetching tags + refs"
git fetch --all --tags --prune
log "checking out $GIT_REF"
git checkout --quiet "$GIT_REF"
# If it's a branch, fast-forward to the remote tip.
if git symbolic-ref -q HEAD >/dev/null; then
  git pull --ff-only --quiet || true
fi
NEW_REF="$(git rev-parse --short HEAD)"
log "now at: $NEW_REF"
if [[ "$PREV_REF" != "$NEW_REF" ]]; then
  log "changed files ($PREV_REF -> $NEW_REF):"
  git --no-pager diff --stat "$PREV_REF" "$NEW_REF" 2>/dev/null | tail -n 25 || true
else
  log "no code change (already at $NEW_REF)"
fi

rollback() {
  log "ROLLBACK -> $PREV_REF"
  git checkout --quiet "$PREV_REF" || true
}

# 3. Dependencies.
if [[ ! -x "$PY" ]]; then
  echo "ERROR: venv python not found at $PY" >&2
  exit 1
fi
log "installing requirements"
if ! "$PIP" install $PIP_ARGS -r requirements.txt; then
  echo "ERROR: pip install failed" >&2
  rollback
  exit 1
fi

# 4. Smoke-test gate: Smart Dynamic VTI allocator parity.
if (( SKIP_TESTS == 0 )); then
  log "running allocator smoke test"
  if ! "$PY" scripts/test_dynamic_vti_allocator.py; then
    echo "ERROR: smoke test failed — rolling back, services untouched." >&2
    rollback
    exit 1
  fi
else
  log "skipping tests (--skip-tests)"
fi

# 5. Restart services.
if (( NO_RESTART == 1 )); then
  log "code deployed at $NEW_REF; --no-restart set, services untouched."
  exit 0
fi

restart_unit() {
  local unit="$1"
  if ! systemctl list-unit-files | grep -q "^${unit}.service"; then
    log "WARN: ${unit}.service not installed; skipping"
    return 0
  fi
  log "restarting ${unit}"
  sudo systemctl restart "${unit}"
  sleep 5
  if systemctl is-active --quiet "${unit}"; then
    log "${unit} active"
  else
    echo "ERROR: ${unit} failed to start after deploy" >&2
    sudo systemctl status "${unit}" --no-pager -l | tail -n 30 >&2
    return 1
  fi
}

restart_unit paper-bot || { rollback; log "restart paper-bot after rollback"; sudo systemctl restart paper-bot || true; exit 1; }

if (( RESTART_LIVE == 1 )); then
  echo ""
  echo ">>> About to restart LIVE bot (REAL MONEY). Ctrl-C within 10s to abort."
  sleep 10
  restart_unit live-bot || { echo "ERROR: live-bot restart failed" >&2; exit 1; }
else
  log "live-bot NOT restarted (use --restart-live to include it)"
fi

# 6. Post-deploy health.
log "deploy complete: $PREV_REF -> $NEW_REF ($(( $(date +%s) - START_TS ))s elapsed)"
if [[ -x "$SCRIPT_DIR/cloud_healthcheck.sh" ]]; then
  "$SCRIPT_DIR/cloud_healthcheck.sh" paper || true
fi
