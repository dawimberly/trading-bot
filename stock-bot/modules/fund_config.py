"""Live + paper fund pair and dynamic fund allocation helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import config
from modules.portal_paths import PORTAL_ROOT, PROJECT_ROOT, has_alpaca_config, read_user_env_prefs

_SCALED_CAP_KEYS = ("spy", "crypto", "nyse", "vti_core")
_VOL_HIGH = float(os.getenv("DYNAMIC_SLEEVE_VOL_HIGH", "0.025"))
_VOL_ELEVATED = float(os.getenv("DYNAMIC_SLEEVE_VOL_ELEVATED", "0.018"))
_VOL_CALM = float(os.getenv("DYNAMIC_VTI_VOL_CALM", "0.015"))
_SCALE_HIGH = float(os.getenv("DYNAMIC_SLEEVE_SCALE_HIGH", "0.75"))
_SCALE_ELEVATED = float(os.getenv("DYNAMIC_SLEEVE_SCALE_ELEVATED", "0.90"))
_VTI_STRESS = float(os.getenv("DYNAMIC_VTI_STRESS_PCT", "0.75"))
_VTI_DEFAULT_AGGRESSIVE = float(os.getenv("DYNAMIC_VTI_DEFAULT_PCT", "0.60"))
_VTI_CALM = float(os.getenv("DYNAMIC_VTI_CALM_PCT", "0.45"))
_VTI_VOL_STRESS = float(os.getenv("DYNAMIC_VTI_VOL_STRESS", "0.025"))
_VTI_VOL_CALM = float(os.getenv("DYNAMIC_VTI_VOL_CALM", "0.015"))
_PAPER_SMALL_VTI = float(os.getenv("PAPER_SMALL_ACCOUNT_VTI_PCT", "0.90"))


def _resolve_vol_score(
    vol_score: float | None,
    volatility: str | None,
) -> float | None:
    """Map get_volatility() label to a numeric score when vol_score omitted."""
    if vol_score is not None:
        return vol_score
    if volatility == "High":
        return 0.02
    if volatility == "Low":
        return 0.01
    return None


def get_vti_core_pct(
    equity: float,
    vol_score: float | None = None,
    macro_stress: bool = False,
    is_paper_aggressive: bool = False,
    volatility: str | None = None,
    *,
    regime: str | None = None,
    data=None,
    bubble_score_100: float | None = None,
    insider_state: dict | None = None,
) -> float:
    """Dynamic VTI for paper aggressive; live and locked paths stay conservative.

    Paper aggressive tiers (when PAPER_DYNAMIC_VTI enabled):
      stress / vol > 2.5%  -> 75% VTI (safety floor)
      calm vol < 1.5%      -> 45% VTI (widen active sleeves)
      default              -> 60% VTI
    Small account (<$500) on paper aggressive -> 90% VTI.
    Live / non-aggressive -> static VTI_CORE_PCT.
    """
    del regime, data, bubble_score_100, insider_state  # reserved; simple tier path only

    paper_agg = bool(is_paper_aggressive) or (
        config.PAPER_TRADING and config.paper_aggressive_context()
    )

    if equity < config.SMALL_ACCOUNT_EQUITY_THRESHOLD:
        if paper_agg:
            return _PAPER_SMALL_VTI
        if config.live_conservative_profile_active():
            return config.LIVE_VTI_CORE_PCT
        return config.SMALL_ACCOUNT_VTI_CORE_PCT

    if paper_agg:
        if not config.PAPER_DYNAMIC_VTI_ENABLED:
            return config.PAPER_VTI_CORE_PCT

        vol = _resolve_vol_score(vol_score, volatility)
        if macro_stress or (vol is not None and vol > _VTI_VOL_STRESS):
            return _VTI_STRESS
        if vol is not None and vol < _VTI_VOL_CALM:
            return _VTI_CALM
        return _VTI_DEFAULT_AGGRESSIVE

    return config.VTI_CORE_PCT


def get_dynamic_risk_per_trade(
    equity: float,
    vol_score: float,
    regime: str,
    macro_stress: bool,
) -> float:
    """Dynamic risk % — paper aggressive / paper chase only; live stays fixed."""
    if equity < config.SMALL_ACCOUNT_EQUITY_THRESHOLD:
        return config.SMALL_ACCOUNT_RISK_PER_TRADE
    if not config.PAPER_TRADING:
        return config.RISK_PER_TRADE

    paper_active = config.paper_aggressive_context() or config.paper_chase_mode_enabled()
    if not (config.PAPER_AGGRESSIVE_ENABLED and paper_active):
        return config.RISK_PER_TRADE

    if not config.PAPER_DYNAMIC_RISK_ENABLED:
        return config.RISK_PER_TRADE

    regime_l = (regime or "").lower()
    if vol_score < 0.015 and "bull" in regime_l and not macro_stress:
        risk = config.effective_paper_risk_calm_pct()
    elif vol_score < 0.02 and not macro_stress:
        risk = config.PAPER_RISK_MODERATE_PCT
    else:
        risk = config.PAPER_RISK_STRESS_PCT
    return min(risk, config.effective_paper_risk_calm_pct())


def get_dynamic_sleeve_caps(vol_score: float, equity: float) -> dict[str, float]:
    """Volatility-scaled sleeve caps from cross-asset vol (market_context)."""
    if equity < config.SMALL_ACCOUNT_EQUITY_THRESHOLD:
        return config.fund_allocation_pct()

    base = config.fund_allocation_pct()
    scale = 1.0
    if vol_score > _VOL_HIGH:
        scale = _SCALE_HIGH
    elif vol_score > _VOL_ELEVATED:
        scale = _SCALE_ELEVATED

    dynamic = dict(base)
    for key in _SCALED_CAP_KEYS:
        if key in dynamic:
            dynamic[key] = round(dynamic[key] * scale, 6)
    metal = dynamic.get("metal", 0.0)
    dynamic["cash_buffer"] = round(
        1.0 - metal - sum(dynamic[k] for k in _SCALED_CAP_KEYS if k in dynamic),
        6,
    )
    if dynamic["cash_buffer"] < 0:
        raise ValueError(
            f"Dynamic sleeve caps over-allocated after vol scale {scale}: {dynamic}"
        )
    return dynamic

ROOT_ENV_SLOT = "@root"


def is_root_slot(name: str) -> bool:
    return (name or "").strip().lower() in (ROOT_ENV_SLOT, ".env", "root", "@.env")


def root_env_path() -> Path:
    return PROJECT_ROOT / ".env"


def read_root_env_prefs() -> dict[str, bool]:
    path = root_env_path()
    paper, allow_live = True, False
    if not path.is_file():
        return {"paper": paper, "allow_live": allow_live}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("PAPER_TRADING="):
            paper = line.split("=", 1)[1].strip().lower() in ("1", "true", "yes")
        elif line.startswith("ALLOW_LIVE_TRADING="):
            allow_live = line.split("=", 1)[1].strip().lower() in ("1", "true", "yes")
    return {"paper": paper, "allow_live": allow_live}

FUND_PAIR_FILE = PORTAL_ROOT / "fund_pair.json"
EXAMPLE_FILE = PORTAL_ROOT / "fund_pair.json.example"


def _from_env() -> tuple[str, str]:
    live = os.getenv("FUND_LIVE_USER", "").strip().lower()
    paper = os.getenv("FUND_PAPER_USER", "").strip().lower()
    return live, paper


def load_fund_pair() -> tuple[str, str]:
    """Return (live_username, paper_username)."""
    live, paper = _from_env()
    if live and paper:
        return live, paper
    if FUND_PAIR_FILE.is_file():
        data = json.loads(FUND_PAIR_FILE.read_text(encoding="utf-8"))
        live = str(data.get("live_user") or "").strip().lower()
        paper = str(data.get("paper_user") or "").strip().lower()
        if live and paper:
            return live, paper
    return "", ""


def save_fund_pair(live_user: str, paper_user: str) -> None:
    PORTAL_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "live_user": live_user.strip().lower(),
        "paper_user": paper_user.strip().lower(),
    }
    FUND_PAIR_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def validate_fund_pair(live_user: str, paper_user: str) -> list[str]:
    """Return list of blocking errors (empty = OK)."""
    errors: list[str] = []
    if live_user == paper_user:
        errors.append("live_user and paper_user must be different.")
    if live_user and paper_user and is_root_slot(live_user) and is_root_slot(paper_user):
        errors.append("Only one side can use @root — use a portal user for the other book.")

    for label, name in (("live", live_user), ("paper", paper_user)):
        if not name:
            errors.append(f"Missing {label} username.")
            continue
        if is_root_slot(name):
            path = root_env_path()
            if not path.is_file():
                errors.append(f"{label} @root: no .env at {path}")
                continue
            text = path.read_text(encoding="utf-8")
            if "APCA_API_KEY_ID=" not in text or "APCA_API_SECRET_KEY=" not in text:
                errors.append(f"{label} @root: .env missing APCA_* keys")
            continue
        if not has_alpaca_config(name):
            errors.append(
                f"{label} user '{name}' has no Alpaca keys — "
                f"save keys in portal or data/portal/users/{name}/.env"
            )
    if live_user and not is_root_slot(live_user) and has_alpaca_config(live_user):
        if read_user_env_prefs(live_user).get("paper"):
            errors.append(
                f"live user '{live_user}' is set to paper trading — use live keys there."
            )
    if live_user and is_root_slot(live_user) and root_env_path().is_file():
        if read_root_env_prefs().get("paper"):
            errors.append("live @root: .env has PAPER_TRADING=true — use live keys for live bot.")
    if paper_user and not is_root_slot(paper_user) and has_alpaca_config(paper_user):
        if not read_user_env_prefs(paper_user).get("paper"):
            errors.append(
                f"paper user '{paper_user}' must use paper keys (Paper trading checked)."
            )
    if paper_user and is_root_slot(paper_user) and root_env_path().is_file():
        if not read_root_env_prefs().get("paper"):
            errors.append(
                "paper @root: .env must have PAPER_TRADING=true for paper Sharpe chase."
            )
    return errors


def ensure_example_file() -> None:
    if EXAMPLE_FILE.is_file():
        return
    PORTAL_ROOT.mkdir(parents=True, exist_ok=True)
    EXAMPLE_FILE.write_text(
        json.dumps(
            {
                "live_user": "yourname-live",
                "paper_user": "yourname-paper",
                "_comment": "Register both in portal.py, save keys, then copy to fund_pair.json",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
