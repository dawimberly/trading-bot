"""Paper-only auto-tune with severity tiers (T0–T5).

T0 Ops      — log rotate / uptime notes (no strategy). Auto.
T1 Hygiene  — disk/journal hygiene. Auto.
T2 Local    — ATR stop step (paper). Auto if tier <= AUTO_APPLY_MAX_TIER.
T3 Structure— concentration trim off, etc. Propose only (needs human).
T4 Alloc    — VTI% / Dynamic VTI / sleeves. Never auto.
T5 Live     — live book / capital. Never auto.

Never touches live or the 33/67 VTI lock.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from modules.safe_io import read_json_file, rotate_oversized_file, write_json_atomic

logger = logging.getLogger(__name__)

_STATE = Path(__file__).resolve().parents[1] / "data" / "auto_tune_state.json"
_ROOT = Path(__file__).resolve().parents[1]
_LOGS = _ROOT / "logs"

# Whitelist: key -> tier + allowed values. T4/T5 never appear here.
_ACTIONS: dict[str, dict[str, Any]] = {
    "ATR_STOP_MULTIPLIER": {
        "tier": 2,
        "values": ("2.0", "2.5", "3.0"),
        "default": "2.0",
        "when": "atr_dominates",
    },
    "CONCENTRATION_GUARD_ENABLED": {
        "tier": 3,
        "values": ("true", "false"),
        "default": "true",
        "when": "trim_steals_wins",
    },
}

_MIN_CLOSED = int(os.getenv("AUTO_TUNE_MIN_CLOSED", "40") or 40)
_COOLDOWN_DAYS = int(os.getenv("AUTO_TUNE_COOLDOWN_DAYS", "14") or 14)
_MAX_MISSED_RTH_WEEK = int(os.getenv("AUTO_TUNE_MAX_MISSED_RTH_MIN", "120") or 120)
_ATR_SHARE_MIN = float(os.getenv("AUTO_TUNE_ATR_SHARE_MIN", "0.55") or 0.55)
_DEFAULT_MAX_TIER = 2


def enabled() -> bool:
    return os.getenv("AUTO_TUNE_ENABLED", "false").lower() in ("1", "true", "yes")


def apply_enabled() -> bool:
    """Master apply switch. Tier still caps what may write."""
    return os.getenv("AUTO_TUNE_APPLY", "false").lower() in ("1", "true", "yes")


def max_apply_tier() -> int:
    """Highest tier that may write .env without human approval (default T2)."""
    raw = os.getenv("AUTO_APPLY_MAX_TIER", str(_DEFAULT_MAX_TIER))
    try:
        return max(0, min(5, int(str(raw).strip())))
    except ValueError:
        return _DEFAULT_MAX_TIER


def _load_state() -> dict[str, Any]:
    data = read_json_file(_STATE)
    return data if isinstance(data, dict) else {}


def _save_state(data: dict[str, Any]) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_STATE, data)


def _paper_env_path() -> Path:
    from modules.portal_paths import resolve_primary_paper_book_dir

    return resolve_primary_paper_book_dir() / ".env"


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _upsert_env(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    lines = text.splitlines()
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        if key_re.match(line):
            new_lines.append(f"{key}={value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(f"# auto_tune T-apply {datetime.now().isoformat(timespec='seconds')}")
        new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _lock_ok(env: dict[str, str]) -> tuple[bool, str]:
    dyn = (env.get("PAPER_DYNAMIC_VTI") or env.get("PAPER_DYNAMIC_VTI_ENABLED") or "false").lower()
    if dyn in ("1", "true", "yes"):
        return False, "Dynamic VTI is ON (lock broken)"
    try:
        vti = float(env.get("PAPER_VTI_CORE_PCT") or env.get("VTI_CORE_PCT") or "0.33")
    except ValueError:
        vti = 0.33
    if abs(vti - 0.33) > 0.02:
        return False, f"VTI pct {vti} != 0.33"
    try:
        nyse = float(env.get("PAPER_NYSE_SLEEVE_CAP_PCT") or env.get("NYSE_SLEEVE_CAP_PCT") or "0.67")
    except ValueError:
        nyse = 0.67
    if nyse < 0.65 or nyse > 0.70:
        return False, f"NYSE cap {nyse} not ~0.67"
    return True, "lock ok"


def _cooldown_ok(state: dict[str, Any], *, for_apply: bool) -> tuple[bool, str]:
    """Apply cooldown uses last_applied_at only; propose can fire nightly."""
    raw = state.get("last_applied_at") if for_apply else ""
    if not for_apply:
        return True, "propose cooldown n/a"
    if not raw:
        return True, "no prior apply"
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return True, "bad prior ts ignored"
    age = datetime.now() - ts
    if age < timedelta(days=_COOLDOWN_DAYS):
        left = _COOLDOWN_DAYS - age.days
        return False, f"apply cooldown {left}d left"
    return True, f"apply cooldown clear ({age.days}d)"


def _uptime_ok() -> tuple[bool, str, dict[str, Any]]:
    try:
        from modules.session_uptime import snapshot

        up = snapshot()
    except Exception:
        up = {}
    missed = int(up.get("missed_rth_minutes") or 0)
    open_ok = up.get("open_ok_0935")
    if missed > _MAX_MISSED_RTH_WEEK:
        return False, f"missed_rth {missed}m > {_MAX_MISSED_RTH_WEEK}", up
    if open_ok is False and missed > 30:
        return False, "open LATE with material missed RTH", up
    return True, "uptime ok", up


def run_t0_t1_hygiene() -> list[dict[str, Any]]:
    """T0/T1: rotate fat logs. Safe, no permission."""
    actions: list[dict[str, Any]] = []
    if not _LOGS.is_dir():
        return actions
    targets = (
        "insider_impact.log",
        "sector_screener.jsonl",
        "thinking_engine.log",
        "weekly_review_debug.log",
        "run_all.log",
        "run_all_paper.log",
        "run_all_live.log",
        "events.log",
    )
    for name in targets:
        path = _LOGS / name
        if not path.is_file():
            continue
        try:
            before = path.stat().st_size
            rotate_oversized_file(path, max_bytes=50 * 1024 * 1024, backup_count=3)
            after = path.stat().st_size if path.is_file() else 0
            if after < before:
                actions.append(
                    {
                        "tier": 0 if name.startswith("run_all") or name == "events.log" else 1,
                        "action": "rotate_log",
                        "file": name,
                        "before_mb": round(before / 1e6, 1),
                        "after_mb": round(after / 1e6, 1),
                    }
                )
        except Exception as exc:
            logger.debug("t0/t1 rotate failed %s: %s", name, exc)
    return actions


def _pick_action(ledger: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    by = {r["reason"]: r for r in (ledger.get("by_reason") or []) if isinstance(r, dict)}
    atr = by.get("smart_atr_stop") or {}
    trim = by.get("concentration_guard_trim") or {}
    net = float(ledger.get("net") or 0)
    atr_net = float(atr.get("net") or 0)
    trim_net = float(trim.get("net") or 0)
    gross_loss = sum(
        abs(float(r.get("net") or 0))
        for r in (ledger.get("by_reason") or [])
        if float(r.get("net") or 0) < 0
    )
    atr_share = (abs(atr_net) / gross_loss) if gross_loss > 1e-9 and atr_net < 0 else 0.0

    cur_atr = (env.get("ATR_STOP_MULTIPLIER") or "2.0").strip() or "2.0"
    cur_conc = (env.get("CONCENTRATION_GUARD_ENABLED") or "true").strip().lower() or "true"

    # T2: ATR step
    if net < 0 and atr_share >= _ATR_SHARE_MIN and atr_net < -200:
        meta = _ACTIONS["ATR_STOP_MULTIPLIER"]
        vals = meta["values"]
        try:
            idx = list(vals).index(cur_atr)
        except ValueError:
            idx = 0
        if idx + 1 < len(vals):
            nxt = vals[idx + 1]
            return {
                "key": "ATR_STOP_MULTIPLIER",
                "from": cur_atr,
                "to": nxt,
                "tier": int(meta["tier"]),
                "reason": (
                    f"ATR exits {atr_share:.0%} of loss $ "
                    f"(atr_net=${atr_net:.0f}, ledger_net=${net:.0f})"
                ),
            }

    # T3: trim-off (propose unless max tier raised)
    if (
        net < 0
        and trim_net > 0
        and trim_net < 250
        and atr_net < -500
        and cur_conc in ("1", "true", "yes")
    ):
        meta = _ACTIONS["CONCENTRATION_GUARD_ENABLED"]
        return {
            "key": "CONCENTRATION_GUARD_ENABLED",
            "from": "true",
            "to": "false",
            "tier": int(meta["tier"]),
            "reason": (
                f"concentration trims +${trim_net:.0f} while ATR ${atr_net:.0f}; "
                "stop nibbling winners"
            ),
        }
    return None


def _may_auto_apply(proposal: dict[str, Any]) -> tuple[bool, str]:
    tier = int(proposal.get("tier") or 99)
    cap = max_apply_tier()
    if tier > cap:
        return False, f"T{tier} > AUTO_APPLY_MAX_TIER={cap} (propose only)"
    if not apply_enabled():
        return False, "AUTO_TUNE_APPLY=false"
    if tier >= 4:
        return False, "T4+ never auto"
    return True, f"T{tier} <= max {cap}"


def evaluate(*, force: bool = False, nightly: bool = False) -> dict[str, Any]:
    """Run T0/T1 hygiene + gated T2/T3 strategy proposal/apply."""
    from modules.exit_ledger import summarize_exit_ledger

    now = datetime.now().isoformat(timespec="seconds")
    state = _load_state()
    ledger = summarize_exit_ledger(days=20)
    env_path = _paper_env_path()
    env = _read_env_file(env_path)
    max_tier = max_apply_tier()

    result: dict[str, Any] = {
        "ts": now,
        "enabled": enabled(),
        "apply_enabled": apply_enabled(),
        "max_apply_tier": max_tier,
        "nightly": nightly,
        "decision": "HOLD",
        "gates": {},
        "proposal": None,
        "applied": False,
        "hygiene": [],
        "message": "",
        "ledger": {
            "n": ledger.get("n"),
            "net": ledger.get("net"),
            "expectancy": ledger.get("expectancy"),
        },
        "env_path": str(env_path),
    }

    # T0/T1 always when enabled (or forced nightly)
    if enabled() or force or nightly:
        result["hygiene"] = run_t0_t1_hygiene()

    if not enabled() and not force:
        result["message"] = "AUTO_TUNE_ENABLED=false"
        result["decision"] = "OFF"
        if result["hygiene"]:
            result["message"] += f" | hygiene {len(result['hygiene'])} actions"
        _save_state({**state, "last_eval": result})
        return result

    lock_ok, lock_msg = _lock_ok(env)
    up_ok, up_msg, up = _uptime_ok()
    n = int(ledger.get("n") or 0)
    sample_ok = n >= _MIN_CLOSED
    result["gates"] = {
        "lock": lock_msg,
        "uptime": up_msg,
        "sample": f"n={n} need>={_MIN_CLOSED}",
        "max_apply_tier": max_tier,
        "uptime_snap": up,
    }

    if not lock_ok:
        result["decision"] = "BLOCKED"
        result["message"] = lock_msg
        _save_state({**state, "last_eval": result})
        return result

    proposal = _pick_action(ledger, env)
    if not proposal:
        result["decision"] = "HOLD"
        result["message"] = "no T2/T3 action from exit $"
        if result["hygiene"]:
            result["message"] += f" | T0/T1 hygiene x{len(result['hygiene'])}"
        # Still need sample/uptime notes for dashboard
        if not sample_ok:
            result["message"] += f" | sample thin ({n})"
        if not up_ok:
            result["message"] += f" | {up_msg}"
        _save_state({**state, "last_eval": result})
        return result

    result["proposal"] = proposal
    tier = int(proposal.get("tier") or 99)
    result["message"] = (
        f"T{tier} {proposal['key']}: {proposal['from']} → {proposal['to']} "
        f"({proposal['reason']})"
    )

    # Strategy gates only for T2+ apply path
    if not up_ok and not force:
        result["decision"] = "PROPOSE"
        result["message"] += f" | hold apply: {up_msg}"
        state["last_proposed_at"] = now
        state["last_proposal"] = proposal
        _save_state({**state, "last_eval": result})
        return result
    if not sample_ok and not force:
        result["decision"] = "PROPOSE"
        result["message"] += f" | hold apply: need n>={_MIN_CLOSED}"
        state["last_proposed_at"] = now
        state["last_proposal"] = proposal
        _save_state({**state, "last_eval": result})
        return result

    may_apply, may_msg = _may_auto_apply(proposal)
    cool_ok, cool_msg = _cooldown_ok(state, for_apply=True)
    result["gates"]["apply"] = may_msg
    result["gates"]["cooldown"] = cool_msg

    force_apply = force and os.getenv("AUTO_TUNE_FORCE_APPLY", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if may_apply and (cool_ok or force_apply):
        try:
            _upsert_env(env_path, proposal["key"], str(proposal["to"]))
            result["applied"] = True
            result["decision"] = "APPLIED"
            result["message"] += f" | AUTO T{tier} — wrote paper_v2 .env — restart paper"
            state["last_applied_at"] = now
            state["last_applied"] = proposal
        except Exception as exc:
            result["decision"] = "ERROR"
            result["message"] = f"apply failed: {exc}"
            logger.exception("auto_tune apply failed")
    else:
        result["decision"] = "PROPOSE"
        if not may_apply:
            result["message"] += f" | {may_msg}"
        elif not cool_ok:
            result["message"] += f" | {cool_msg}"
        state["last_proposed_at"] = now
        state["last_proposal"] = proposal

    if result["hygiene"]:
        result["message"] += f" | hygiene x{len(result['hygiene'])}"

    state["last_eval"] = result
    _save_state(state)
    try:
        from modules.trade_journal import log_ops_event

        log_ops_event(
            "auto_tune",
            notes=result["decision"],
            tier=tier,
            key=proposal.get("key"),
            to=proposal.get("to"),
        )
    except Exception:
        pass

    # Best-effort Telegram on APPLIED / T3+ PROPOSE
    try:
        if result["decision"] in ("APPLIED", "PROPOSE") and tier >= 2:
            from modules import alerts

            fn = getattr(alerts, "send_message", None) or getattr(
                alerts, "notify", None
            )
            if callable(fn):
                fn(f"Auto-tune {result['decision']}: {result['message'][:200]}")
    except Exception:
        logger.debug("auto_tune alert skip", exc_info=True)

    return result


def heartbeat_snapshot() -> dict[str, Any]:
    state = _load_state()
    last = state.get("last_eval") if isinstance(state.get("last_eval"), dict) else {}
    prop = last.get("proposal") if isinstance(last.get("proposal"), dict) else {}
    return {
        "enabled": enabled(),
        "apply_enabled": apply_enabled(),
        "max_apply_tier": max_apply_tier(),
        "decision": last.get("decision"),
        "message": (last.get("message") or "")[:180],
        "proposal": prop,
        "proposal_tier": prop.get("tier"),
        "last_applied_at": state.get("last_applied_at") or "",
        "last_proposed_at": state.get("last_proposed_at") or "",
        "hygiene_n": len(last.get("hygiene") or []),
    }
