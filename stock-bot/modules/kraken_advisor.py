"""Kraken Pro playbook: macro context + holdings → short phone-friendly actions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from modules.macro_signals import ensure_macro_daily, evaluate, load_daily_matrix, regime_from_daily

# Leveraged / high-risk symbols to flag on Kraken stocks tab
LEVERAGED_TICKERS = frozenset(
    {
        "GUSH",
        "DUST",
        "NUGT",
        "JNUG",
        "TQQQ",
        "SQQQ",
        "SPXL",
        "SPXS",
        "LABU",
        "LABD",
        "FNGU",
        "FNGD",
    }
)

DUPLICATE_US_CORE = frozenset({"QQQ", "VOO", "VTI", "SPY", "IVV"})

CASH_ASSETS = frozenset({"USD", "ZUSD", "USDT", "USDC", "DAI"})

# Kraken asset code → display ticker (extend as needed)
ASSET_ALIASES = {
    "ZUSD": "USD",
    "XXBT": "BTC",
    "XBT": "BTC",
    "XETH": "ETH",
    "ETH": "ETH",
    "SOL": "SOL",
    "RENDER": "RENDER",
    "XRENDER": "RENDER",
}


def _display_asset(code: str) -> str:
    c = (code or "").upper()
    return ASSET_ALIASES.get(c, c)


def fetch_kraken_balances() -> dict[str, Any]:
    """Return {ok, balances: [{asset, amount, display}], error}."""
    key, secret = config.get_kraken_credentials()
    if not key or not secret:
        return {"ok": False, "error": "Kraken API keys missing in .env", "balances": []}

    try:
        from kraken.spot import SpotClient

        client = SpotClient(key=key, secret=secret)
        raw = client.request("POST", "/0/private/Balance") or {}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "balances": []}

    rows: list[dict] = []
    for asset, amount in raw.items():
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        display = _display_asset(asset)
        rows.append({"asset": asset, "display": display, "amount": amt})

    rows.sort(key=lambda r: -r["amount"])
    return {"ok": True, "balances": rows, "error": None}


def _daily_ok(daily) -> bool:
    return daily is not None and not daily.empty and len(daily) >= 50


def _macro_snapshot() -> dict:
    ensure_macro_daily(refresh=False)
    daily = load_daily_matrix(days=450)
    regime, vol = regime_from_daily(daily) if _daily_ok(daily) else ("unknown", "unknown")
    sig = evaluate(daily, regime) if _daily_ok(daily) else {}
    return {
        "regime": regime,
        "vol": vol,
        "stress": bool(sig.get("stress")),
        "yield_gate": bool(sig.get("yield_gate")),
        "spy_below_ma200": bool(sig.get("spy_below_ma200")),
        "ok": bool(sig.get("ok")),
    }


def load_manual_positions() -> list[dict]:
    """Optional user file when Kraken stock balances are not in REST Balance."""
    path = Path(__file__).resolve().parents[1] / "reference" / "kraken_positions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("positions", [])
    except (json.JSONDecodeError, OSError):
        return []


def _advice_from_holdings(
    balances: list[dict],
    manual: list[dict],
    macro: dict,
    *,
    slot: str,
) -> list[str]:
    """Short action lines for phone."""
    lines: list[str] = []
    displays = {b["display"].upper() for b in balances}
    manual_tickers = {p.get("ticker", "").upper() for p in manual if p.get("ticker")}

    # Crypto cash
    usd_amt = sum(b["amount"] for b in balances if b["display"].upper() in CASH_ASSETS)
    crypto_non_cash = [b for b in balances if b["display"].upper() not in CASH_ASSETS]

    all_tickers = displays | manual_tickers

    if slot == "evening":
        lines.append("Evening check - review only; avoid big new trades after midnight.")
    else:
        lines.append("Morning check - max 1-2 actions today, then close the app.")

    # Macro (from same logic as Alpaca game plan)
    if macro.get("stress"):
        lines.append("Macro: STRESS - favor cash; do not add speculative size.")
    else:
        lines.append("Macro: calm - okay to rebalance small, not chase headlines.")

    if macro.get("yield_gate"):
        lines.append("Bond/yield gate ON - bot would not add broad US index (SPY) now.")

    if usd_amt > 0 and len(balances) > 0:
        total_est = usd_amt + sum(
            b["amount"] for b in crypto_non_cash
        )  # rough if stocks only in manual
        if total_est > 0 and usd_amt / total_est >= 0.5:
            lines.append(f"Crypto tab: high USD cash (~{usd_amt:.0f}) - good; no rush to deploy.")

    lev = [t for t in all_tickers if t in LEVERAGED_TICKERS]
    if lev:
        lines.append(f"SELL or trim leverage first: {', '.join(sorted(lev))}.")

    core_dupes = [t for t in all_tickers if t in DUPLICATE_US_CORE]
    if len(core_dupes) >= 2:
        lines.append(
            f"Pick ONE US core (keep {core_dupes[0]} OR {core_dupes[1]}), trim the other - overlap risk."
        )

    n_positions = len(manual) or len([b for b in balances if b["display"].upper() not in CASH_ASSETS])
    if n_positions >= 8:
        lines.append(
            f"You have ~{n_positions} positions - sell smallest $10 slices until 5 or fewer names."
        )

    if "RENDER" in all_tickers:
        lines.append("RENDER: speculative; trim part if up big and size >10% of crypto.")

    if "NASA" in all_tickers or "ASTS" in all_tickers or "SPCX" in all_tickers:
        lines.append("Space names: one theme only; bot watches SPCX - do not double with many space bets.")

    if not macro.get("stress"):
        lines.append("Metals (GLD/SLV): bot adds on Alpaca stress only - not required on Kraken today.")

    if slot == "morning":
        lines.append("DO TODAY: (1) remove leverage if any (2) one simplify sell (3) then stop scrolling.")
    else:
        lines.append("TONIGHT: note prices only; execute trades tomorrow after 11am if still wanted.")

    return lines


def build_playbook(*, slot: str) -> tuple[str, str]:
    """Return (subject, body) for morning or evening."""
    slot = slot.lower()
    if slot not in ("morning", "evening"):
        slot = "morning"

    macro = _macro_snapshot()
    bal = fetch_kraken_balances()
    manual = load_manual_positions()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"Kraken playbook ({slot}) - {stamp}"

    body_lines = [
        "KRAKEN PRO - what to do",
        "========================",
        f"Slot: {slot} (bot logic + your holdings)",
        "",
        "--- Macro (same engine as paper bot) ---",
        f"Regime: {macro.get('regime', '?')[:40]}",
        f"Vol: {macro.get('vol', '?')}",
        f"Stress: {'yes' if macro.get('stress') else 'no'} | Yield gate: {'on' if macro.get('yield_gate') else 'off'}",
        "",
        "--- Holdings ---",
    ]

    if bal.get("ok") and bal.get("balances"):
        for b in bal["balances"][:12]:
            body_lines.append(f"  {b['display']}: {b['amount']:.6g}")
        if len(bal["balances"]) > 12:
            body_lines.append(f"  ... +{len(bal['balances']) - 12} more")
    else:
        body_lines.append(f"  (Kraken API: {bal.get('error', 'unknown')})")
        body_lines.append("  Tip: add keys to .env or edit reference/kraken_positions.json")

    if manual:
        body_lines.append("  Manual stocks:")
        for p in manual:
            body_lines.append(
                f"    {p.get('ticker', '?')}: ~{p.get('pct', '?')}% "
                f"(${p.get('usd', '?')})"
            )

    body_lines.append("")
    body_lines.append("--- Actions ---")
    body_lines.extend(_advice_from_holdings(bal.get("balances") or [], manual, macro, slot=slot))
    body_lines.append("")
    body_lines.append("Not financial advice. Alpaca bot still runs on paper only.")

    return subject, "\n".join(body_lines)
