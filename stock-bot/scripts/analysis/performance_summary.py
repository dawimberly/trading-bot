"""Collect sleeve/regime/allocation performance for paper + live books."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from modules.csv_utils import read_csv_file  # noqa: E402

BOOKS = {
    "Paper": ROOT / "data/portal/users/dawimberly/books/alpaca_paper",
    "Live": ROOT / "data/portal/users/dawimberly/books/alpaca_live",
}


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_journal(book_dir: Path) -> pd.DataFrame:
  paths = [
      book_dir / "paper_journal.csv",
      ROOT / "paper_journal.csv",
      ROOT / "paper_chase_journal.csv",
  ]
  frames = []
  for p in paths:
      if p.is_file():
          try:
              df = read_csv_file(p)
              if not df.empty:
                  df["_source"] = str(p.name)
                  frames.append(df)
          except Exception:
              pass
  if not frames:
      return pd.DataFrame()
  out = pd.concat(frames, ignore_index=True)
  out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
  return out.dropna(subset=["timestamp"]).sort_values("timestamp")


def _equity_curve(journal: pd.DataFrame) -> pd.Series:
    if journal.empty or "equity" not in journal.columns:
        return pd.Series(dtype=float)
    eq = journal.copy()
    eq["equity"] = pd.to_numeric(eq["equity"], errors="coerce")
    eq = eq.dropna(subset=["equity"])
    if eq.empty:
        return pd.Series(dtype=float)
    daily = eq.groupby(eq["timestamp"].dt.date)["equity"].last()
    return daily.astype(float)


def _period_return(curve: pd.Series, days: int) -> float | None:
    if curve.empty or len(curve) < 2:
        return None
    end = curve.index[-1]
    start_cut = end - timedelta(days=days)
    sub = curve[curve.index >= start_cut]
    if len(sub) < 2:
        sub = curve.tail(min(len(curve), max(2, days // 3 + 1)))
    if len(sub) < 2:
        return None
    return (float(sub.iloc[-1]) / float(sub.iloc[0]) - 1.0) * 100.0


def _sleeve_pnl_from_notes(journal: pd.DataFrame) -> dict[str, str]:
    """Latest sleeve_pnl snippet from cycle notes."""
    if journal.empty or "notes" not in journal.columns:
        return {}
    cycles = journal[journal["event"].astype(str).str.lower() == "cycle"]
    if cycles.empty:
        return {}
    notes = str(cycles.iloc[-1].get("notes") or "")
    out: dict[str, str] = {}
    m = re.search(r"sleeve_pnl=([^;]+)", notes)
    if m:
        for part in m.group(1).split("|"):
            part = part.strip()
            if part:
                out[part.split()[0].upper()] = part
    return out


def _sleeve_trade_pnl(journal: pd.DataFrame, sleeve: str, days: int | None = None) -> dict:
    if journal.empty:
        return {"realized_pnl": 0.0, "trades": 0, "wins": 0}
    df = journal.copy()
    if days:
        cutoff = df["timestamp"].max() - timedelta(days=days)
        df = df[df["timestamp"] >= cutoff]
    sl = df[df["sleeve"].astype(str).str.lower() == sleeve.lower()]
    exits = sl[sl["event"].astype(str).str.lower().isin({"exit", "sell", "close", "fill"})]
    pnl = 0.0
    trades = 0
    wins = 0
    if "notional" in exits.columns and "qty" in exits.columns and "price" in exits.columns:
        # approximate from exit rows when pnl column missing
        pass
    if "notes" in exits.columns:
        for note in exits["notes"].dropna().astype(str):
            pm = re.search(r"pnl[=:]?\s*([+-]?\d+\.?\d*)", note, re.I)
            if pm:
                v = float(pm.group(1))
                pnl += v
                trades += 1
                if v > 0:
                    wins += 1
    return {"realized_pnl": pnl, "trades": trades, "wins": wins}


def _regime_history(journal: pd.DataFrame, days: int = 30) -> pd.DataFrame:
    if journal.empty or "regime" not in journal.columns:
        return pd.DataFrame()
    df = journal[journal["event"].astype(str).str.lower() == "cycle"].copy()
    if df.empty:
        return pd.DataFrame()
    cutoff = df["timestamp"].max() - timedelta(days=days)
    df = df[df["timestamp"] >= cutoff]
    df["regime_short"] = df["regime"].astype(str).str.split(":").str[0]
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    rows = []
    for regime, grp in df.groupby("regime_short"):
        eq = grp["equity"].dropna()
        ret = None
        if len(eq) >= 2:
            ret = (float(eq.iloc[-1]) / float(eq.iloc[0]) - 1.0) * 100.0
        rows.append(
            {
                "regime": regime,
                "days": int(grp["timestamp"].dt.date.nunique()),
                "cycles": len(grp),
                "equity_start": float(eq.iloc[0]) if len(eq) else None,
                "equity_end": float(eq.iloc[-1]) if len(eq) else None,
                "return_pct": ret,
            }
        )
    return pd.DataFrame(rows).sort_values("days", ascending=False)


def _allocation_from_heartbeat(hb: dict | None) -> dict:
    if not hb:
        return {}
    caps = hb.get("sleeve_caps") or {}
    exp = hb.get("sleeve_exposure") or {}
    equity = float(hb.get("equity") or exp.get("equity") or 0)
    out = {"equity": equity, "regime": hb.get("regime"), "timestamp": hb.get("timestamp")}
    if equity > 0:
        for key in ("vti_core", "spy", "crypto", "nyse", "metal"):
            val = float(exp.get(f"{key}_value") or 0)
            out[f"{key}_pct"] = round(val / equity * 100, 2)
            out[f"{key}_cap_pct"] = round(float(caps.get(key, 0) or 0) * 100, 2)
    else:
        for key in ("vti_core", "spy", "crypto", "nyse", "metal"):
            out[f"{key}_cap_pct"] = round(float(caps.get(key, 0) or 0) * 100, 2)
    return out


def _backtest_snippets() -> list[dict]:
    snippets = []
    files = [
        ("365d overnight", ROOT / "backtest_v15_thinking_enriched_365.txt"),
        ("1000d overnight", ROOT / "backtest_thorough_overnight_1000.txt"),
        ("365d dynamic VTI", ROOT / "backtest_dynamic_vti_365.txt"),
        ("1000d dynamic VTI", ROOT / "backtest_dynamic_vti_1000.txt"),
        ("100d stat arb v1.5.2", ROOT / "backtest_v152_statarb_final2.txt"),
    ]
    for label, path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        row = {"label": label, "file": path.name}
        for pat, key in [
            (r"Total Return:\s*([+-]?[\d.]+)%", "return_pct"),
            (r"Sharpe Ratio:\s*([+-]?[\d.]+)", "sharpe"),
            (r"Max Drawdown:\s*([+-]?[\d.]+)%", "max_dd_pct"),
        ]:
            m = re.search(pat, text)
            if m:
                row[key] = float(m.group(1))
        m = re.search(r"Stat arb pairs.*?\$\s*([+-]?[\d.]+)", text)
        if m:
            row["stat_arb_pnl"] = float(m.group(1))
        snippets.append(row)
    return snippets


def _strategy_db_summary() -> pd.DataFrame:
    db = ROOT / "data" / "strategy_metrics.db"
    if not db.is_file():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(db)
        tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", conn)
        if tables.empty:
            conn.close()
            return pd.DataFrame()
        tname = tables["name"].iloc[0]
        df = pd.read_sql(f"SELECT * FROM {tname} ORDER BY rowid DESC LIMIT 20", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def main() -> int:
    print("=" * 72)
    print("PYTHONTRADING — PERFORMANCE SUMMARY")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    # Core allocator
    alloc_state = _load_json(ROOT / "core_allocator_state.json")
    print("\n## Dynamic Core Allocator")
    if alloc_state:
        print(
            f"  Choice: {alloc_state.get('choice', '?').upper()} @ "
            f"{float(alloc_state.get('vti_pct', 0)):.1%}"
        )
        print(f"  Updated: {alloc_state.get('updated_at', '—')}")
        metrics = alloc_state.get("metrics") or {}
        for sym in ("vti", "spy"):
            m = metrics.get(sym) or {}
            if m:
                print(
                    f"  {sym.upper()} 63d: Sharpe {m.get('sharpe', 0):.2f} | "
                    f"ann ret {float(m.get('ann_return', 0)):.2%} | "
                    f"max DD {float(m.get('max_dd', 0)):.2%}"
                )
    else:
        print("  (no core_allocator_state.json)")

    # Books
    for book_name, book_dir in BOOKS.items():
        print(f"\n## {book_name} Book")
        hb = _load_json(book_dir / "bot_heartbeat.json")
        if hb:
            print(f"  Heartbeat: {hb.get('timestamp')} | status={hb.get('status', '—')}")
            if hb.get("last_cycle_error"):
                print(f"  WARN Last error: {hb['last_cycle_error']} @ {hb.get('last_cycle_error_at')}")
        alloc = _allocation_from_heartbeat(hb)
        if alloc:
            print(f"  Equity: ${alloc.get('equity', 0):,.2f} | Regime: {alloc.get('regime', '—')}")
            print("  Target caps %:", end=" ")
            print(
                ", ".join(
                    f"{k.replace('_cap_pct','').upper()} {alloc.get(f'{k.replace('_cap_pct','')}_cap_pct', alloc.get(k, 0))}%"
                    for k in ("vti_core", "spy", "crypto", "nyse", "metal")
                    if f"{k}_cap_pct" in alloc or f"{k}_pct" in alloc
                )
            )
            if alloc.get("vti_core_pct") is not None:
                print(
                    f"  Actual exposure %: VTI {alloc.get('vti_core_pct')}% | "
                    f"SPY {alloc.get('spy_pct')}% | NYSE {alloc.get('nyse_pct')}% | "
                    f"Crypto {alloc.get('crypto_pct')}% | Metal {alloc.get('metal_pct')}%"
                )
        journal = _load_journal(book_dir)
        if journal.empty:
            print("  (no journal data)")
            continue
        curve = _equity_curve(journal)
        print(f"  Journal rows: {len(journal):,} | range {journal['timestamp'].min()} → {journal['timestamp'].max()}")
        for label, days in (("30d", 30), ("90d", 90), ("Since start", 9999)):
            ret = _period_return(curve, days if days < 9999 else 3650)
            if ret is not None:
                print(f"  Portfolio {label}: {ret:+.2f}%")

        pnl_notes = _sleeve_pnl_from_notes(journal)
        if pnl_notes:
            print("  Latest sleeve unrealized (from cycle notes):")
            for k, v in pnl_notes.items():
                print(f"    {v}")

        if hb and hb.get("sleeve_pnl"):
            print("  Sleeve unrealized P&L (heartbeat):")
            for sl, data in hb["sleeve_pnl"].items():
                if sl == "vti_core":
                    continue
                print(
                    f"    {sl.upper()}: ${data.get('unrealized_pnl', 0):+,.2f} "
                    f"({data.get('unrealized_pnl_pct', 0):+.2%}) | "
                    f"{data.get('positions', 0)} pos"
                )

        print("\n  Regime performance (last 30d):")
        rh = _regime_history(journal, days=30)
        if not rh.empty:
            print(rh.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        else:
            print("    (no cycle regime data)")

    # Sleeve fills from journal
    print("\n## Sleeve Activity (Paper journal — all sources)")
    pj = _load_journal(BOOKS["Paper"])
    if not pj.empty and "sleeve" in pj.columns:
        sl = pj[pj["sleeve"].notna() & (pj["sleeve"].astype(str).str.len() > 0)]
        counts = sl.groupby(sl["sleeve"].str.lower())["event"].count()
        print("  Event counts by sleeve:")
        for sleeve, n in counts.sort_values(ascending=False).head(15).items():
            print(f"    {sleeve}: {n}")

    # Backtests
    print("\n## Recent Backtest Results")
    bt = _backtest_snippets()
    if bt:
        df = pd.DataFrame(bt)
        print(df.to_string(index=False, float_format=lambda x: f"{x:.2f}" if pd.notna(x) else ""))
    else:
        print("  (no backtest output files found)")

    # Parse dynamic VTI compare if present
    dvt = ROOT / "backtest_dynamic_vti_365.txt"
    if dvt.is_file():
        print("\n## Dynamic VTI A/B (365d)")
        for line in dvt.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("Fixed") or line.strip().startswith("Dynamic"):
                print(f"  {line.strip()}")

    dvt1 = ROOT / "backtest_dynamic_vti_1000.txt"
    if dvt1.is_file():
        print("\n## Dynamic VTI A/B (1000d)")
        for line in dvt1.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("Fixed") or line.strip().startswith("Dynamic"):
                print(f"  {line.strip()}")

    # Benchmark comparison note from 1000d backtest
    b1000 = ROOT / "backtest_thorough_overnight_1000.txt"
    if not b1000.is_file():
        b1000 = ROOT / "backtest_v15_1000day_full_news_final.txt"
    if b1000.is_file():
        text = b1000.read_text(encoding="utf-8", errors="replace")
        print("\n## VTI vs SPY vs Active (backtest reference)")
        for pat in [
            r"Total Return:\s*([+-]?[\d.]+)%",
            r"VTI Buy & Hold:\s*([+-]?[\d.]+)%",
            r"Sharpe Ratio:\s*([+-]?[\d.]+)",
            r"NYSE signals:\s*(\d+)",
            r"SPY signals:\s*(\d+)",
        ]:
            m = re.search(pat, text)
            if m:
                print(f"  {pat.split('\\\\')[0]}: {m.group(1)}")

        # sleeve attribution block
        if "SLEEVE ATTRIBUTION" in text:
            idx = text.index("--- SLEEVE ATTRIBUTION ---")
            block = text[idx : idx + 1200].split("---")[1]
            print("\n  Sleeve attribution (backtest):")
            for line in block.splitlines()[1:8]:
                if line.strip():
                    print(f"    {line.strip()}")

    strat = _strategy_db_summary()
    if not strat.empty:
        print("\n## Strategy metrics DB (latest rows)")
        print(strat.head(10).to_string(index=False))

    print("\n" + "=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
