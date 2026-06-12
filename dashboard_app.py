"""Desktop monitor for PythonTrading — CustomTkinter dark theme.

Run:
    python dashboard_app.py
    python dashboard_app.py --launch-bot
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk


def _app_root() -> Path:
    """Project root — walk up from dist/ exe to folder with run_all.py when frozen."""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent
        for _ in range(6):
            if (candidate / "run_all.py").is_file():
                return candidate
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_ROOT = _app_root()
os.environ["PYTHONTRADING_ROOT"] = str(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from modules.ssl_certs import configure_ssl_certificates

    configure_ssl_certificates()
except ImportError:
    pass

from modules.portal_paths import (  # noqa: E402
    bind_project_root,
    book_heartbeat_path,
    book_journal_path,
    book_scorecard_path,
    ensure_book_journal,
    env_flags_for_book,
    get_last_book_id,
    get_last_username,
    has_alpaca_config,
    legacy_journal_path,
    legacy_scorecard_path,
    migrate_user_to_books,
    read_desktop_prefs,
    read_user_env_key_hint,
    read_user_env_prefs,
    save_last_book_id,
    save_last_username,
    user_dir,
    write_user_env,
)
from modules.wisdom_evaluator import filter_paper_journal  # noqa: E402
from modules.trading_books import (  # noqa: E402
    book_dropdown_entries,
    book_enabled,
    book_id_for_dropdown_label,
    book_label,
    dropdown_label_for_book,
)

bind_project_root(PROJECT_ROOT)

import customtkinter as ctk  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import tkinter as tk  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

import config  # noqa: E402
from modules.alpaca_executor import get_trading_client  # noqa: E402
from modules.portal_auth import authenticate, init_db, register_user  # noqa: E402
from modules.portal_bot import bot_running, read_bot_log_tail, start_bot, stop_bot  # noqa: E402

REFRESH_SECONDS = 60
CRYPTO_VOL_HEARTBEAT_FILE = "crypto_vol_heartbeat.json"
TRADES_LIMIT = 50
TRADE_EVENTS = frozenset({"signal", "exit", "fill"})
EQUITY_EVENTS = frozenset({"cycle", "startup"})
CHART_DAYS = 21
CHART_DPI = 72
SPARKLINE_POINTS = 48
ENABLE_SPARKLINE = True  # lightweight; disable if CPU is very slow

ENV_PATH = PROJECT_ROOT / ".env"
ICON_PATH = PROJECT_ROOT / "assets" / "dashboard.ico"

try:
    import pystray
    from PIL import Image, ImageDraw

    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

COLORS = {
    "bg": "#0a0e17",
    "surface": "#111827",
    "surface2": "#1a2332",
    "card": "#152238",
    "card_hover": "#1c2d4a",
    "border": "#243049",
    "muted": "#8b9cb8",
    "text": "#e8eef7",
    "text_dim": "#c5d0e0",
    "green": "#34d399",
    "green_dim": "#065f46",
    "red": "#f87171",
    "red_dim": "#7f1d1d",
    "amber": "#fbbf24",
    "amber_dim": "#78350f",
    "blue": "#60a5fa",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "live": "#991b1b",
    "live_bg": "#450a0a",
    "small": "#b45309",
    "small_bg": "#451a03",
    "paper_ok": "#065f46",
    "paper_ok_bg": "#064e3b",
    "chart_grid": "#243049",
}

FONTS = {
    "hero": ("Segoe UI", 28, "bold"),
    "hero_sub": ("Segoe UI", 22, "bold"),
    "title": ("Segoe UI", 20, "bold"),
    "heading": ("Segoe UI", 14, "bold"),
    "body": ("Segoe UI", 12),
    "body_sm": ("Segoe UI", 11),
    "caption": ("Segoe UI", 10),
    "metric": ("Segoe UI", 16, "bold"),
    "metric_sm": ("Segoe UI", 13, "bold"),
}


def _ctk_font(key: str) -> ctk.CTkFont:
    family, size, *rest = FONTS[key]
    weight = rest[0] if rest else "normal"
    return ctk.CTkFont(family=family, size=size, weight=weight)

plt.ioff()

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# --- Data layer --------------------------------------------------------------


def _resolve_path(relative: str) -> Path:
    return PROJECT_ROOT / relative


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _infer_sleeve(symbol: str) -> str:
    sym = config.normalize_symbol(symbol or "")
    if sym == config.SPY_BOT_SYMBOL:
        return "SPY"
    if config.is_crypto(sym):
        return "Crypto"
    if config.is_metal_symbol(sym):
        return "Metal"
    if sym:
        return "NYSE"
    return ""


def _path_for_resolve(path: Path) -> str:
    try:
        return os.path.relpath(path, PROJECT_ROOT)
    except ValueError:
        return str(path)


def _apply_user_paths(username: str, book_id: str) -> None:
    """Load book API keys and point dashboard at isolated bot files."""
    migrate_user_to_books(username)
    from modules.portal_paths import book_env_path

    env_file = book_env_path(username, book_id)
    config.reload_from_env(str(env_file))
    bd = env_file.parent
    hb = book_heartbeat_path(username, book_id)
    journal = ensure_book_journal(username, book_id)
    config.HEARTBEAT_FILE = _path_for_resolve(hb)
    config.PAPER_JOURNAL_CSV = _path_for_resolve(journal)
    config.WISDOM_SCORECARD_FILE = _path_for_resolve(bd / "wisdom_scorecard.json")
    config.WISDOM_JOURNAL_FILE = _path_for_resolve(bd / "wisdom_journal.csv")
    os.environ["PYTHONTRADING_ENV_FILE"] = str(env_file)
    os.environ["HEARTBEAT_FILE"] = str(hb)
    os.environ["PAPER_JOURNAL_CSV"] = str(journal)


def _needs_setup(username: str, book_id: str) -> bool:
    if not book_enabled(book_id):
        return False
    if not has_alpaca_config(username, book_id):
        return True
    _apply_user_paths(username, book_id)
    try:
        config.get_alpaca_credentials()
    except ValueError:
        return True
    return False


def _reset_equity_cache() -> None:
    """Drop backtest/default equity so Alpaca live balance drives the UI."""
    config.set_backtest_small_account_context(False)
    if getattr(config, "_account_equity", None) is not None:
        config._account_equity = None  # type: ignore[attr-defined]
    if getattr(config, "_small_account_mode", False):
        config._small_account_mode = False  # type: ignore[attr-defined]


def _fetch_account_summary(*, retries: int = 2) -> tuple[float | None, float | None, str | None]:
    """Fresh Alpaca account read; retries briefly on transient failures."""
    last_err: str | None = None
    for attempt in range(max(1, retries)):
        try:
            client = get_trading_client()
            acct = client.get_account()
            equity = float(acct.equity)
            cash = float(acct.cash)
            if equity > 0:
                return equity, cash, None
            last_err = "Account equity is zero"
        except ValueError as exc:
            last_err = str(exc)
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        if attempt + 1 < retries:
            time.sleep(0.35)
    return None, None, last_err


def _resolve_equity_cash(
    acct_eq: float | None,
    acct_cash: float | None,
    acct_err: str | None,
    heartbeat: dict | None,
) -> tuple[float, float, str | None]:
    """Prefer live Alpaca equity; ignore stale heartbeat when API keys are configured."""
    hb_eq = float((heartbeat or {}).get("equity") or 0)
    hb_cash = float((heartbeat or {}).get("cash") or 0)

    if acct_eq is not None and acct_eq > 0:
        cash = acct_cash if acct_cash is not None else hb_cash
        return acct_eq, cash, acct_err

    try:
        config.get_alpaca_credentials()
        has_keys = True
    except ValueError:
        has_keys = False

    if has_keys:
        # Keys present but API failed — do not show old bot heartbeat (often $100 backtest).
        return 0.0, hb_cash, acct_err or "Could not fetch live account equity"

    if hb_eq > 0:
        return hb_eq, hb_cash, acct_err

    return 0.0, hb_cash, acct_err


def _fetch_positions() -> tuple[pd.DataFrame | None, str | None]:
    try:
        client = get_trading_client()
        positions = client.get_all_positions()
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    cols = ["Ticker", "Qty", "Entry", "Current", "P&L $", "P&L %"]
    if not positions:
        return pd.DataFrame(columns=cols), None

    rows = []
    for pos in positions:
        sym = config.normalize_symbol(pos.symbol)
        rows.append(
            {
                "Ticker": sym,
                "Sleeve": _infer_sleeve(sym),
                "Qty": float(pos.qty),
                "Entry": float(getattr(pos, "avg_entry_price", 0) or 0),
                "Current": float(getattr(pos, "current_price", 0) or 0),
                "P&L $": float(getattr(pos, "unrealized_pl", 0) or 0),
                "P&L %": float(getattr(pos, "unrealized_plpc", 0) or 0) * 100,
                "_mv": float(pos.qty) * float(getattr(pos, "current_price", 0) or 0),
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("_mv", ascending=False).drop(columns=["_mv"])
    return df, None


def _journal_search_paths(username: str, book_id: str) -> list[Path]:
    paths: list[Path] = []
    for path in (
        book_journal_path(username, book_id),
        _resolve_path(config.PAPER_JOURNAL_CSV),
        legacy_journal_path(),
    ):
        if path not in paths:
            paths.append(path)
    return paths


def _segment_matches_book(scorecard: dict, book_id: str) -> bool:
    seg = scorecard.get("journal_segment") or (scorecard.get("live") or {}).get(
        "journal_segment"
    ) or {}
    book_type = str(seg.get("book_type") or "").lower()
    live_only = seg.get("live_only")
    if book_id == "alpaca_live":
        return live_only is True or book_type == "live"
    if book_id == "alpaca_paper":
        return live_only is False or book_type == "paper"
    return True


def _load_scorecard(username: str, book_id: str) -> tuple[dict | None, str]:
    """Per-book scorecard, then project root when segment matches this book."""
    candidates: list[tuple[Path, str]] = [
        (book_scorecard_path(username, book_id), "book"),
    ]
    if book_id == "alpaca_live":
        candidates.append((legacy_scorecard_path(), "project"))
    for path, source in candidates:
        data = _load_json(path)
        if data and _segment_matches_book(data, book_id):
            return data, source
    return None, ""


def _read_trade_journal_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()


def _filter_journal_for_book(path: Path, df: pd.DataFrame, book_id: str) -> pd.DataFrame:
    if df.empty:
        return df
    legacy = legacy_journal_path()
    # Mixed legacy root journal: keep only this book's segment.
    if path.resolve() == legacy.resolve():
        try:
            filtered, _meta = filter_paper_journal(df, live_only=book_id == "alpaca_live")
            if not filtered.empty:
                df = filtered
        except Exception:
            pass
    if "event" not in df.columns:
        return pd.DataFrame()
    return df.loc[df["event"].astype(str).isin(TRADE_EVENTS)].copy()


def _fetch_alpaca_fills(limit: int = TRADES_LIMIT) -> pd.DataFrame:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        client = get_trading_client()
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit, nested=True)
        orders = list(client.get_orders(filter=req))
        rows = []
        for order in orders:
            qty = float(getattr(order, "filled_qty", None) or 0)
            avg = getattr(order, "filled_avg_price", None)
            if qty <= 0 or avg is None:
                continue
            filled_at = getattr(order, "filled_at", None) or getattr(
                order, "submitted_at", None
            )
            sym = config.normalize_symbol(order.symbol)
            rows.append(
                {
                    "timestamp": str(filled_at)[:19].replace("T", " "),
                    "event": "fill",
                    "symbol": sym,
                    "side": str(getattr(order, "side", "")).split(".")[-1].lower(),
                    "notional": round(qty * float(avg), 2),
                    "sleeve": _infer_sleeve(sym),
                }
            )
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


def _load_trade_history(
    username: str,
    book_id: str,
    *,
    limit: int = TRADES_LIMIT,
) -> pd.DataFrame | None:
    """Journal signals/exits for this book, plus Alpaca fills when journal is thin."""
    journal_parts: list[pd.DataFrame] = []
    for path in _journal_search_paths(username, book_id):
        part = _filter_journal_for_book(path, _read_trade_journal_csv(path), book_id)
        if not part.empty:
            journal_parts.append(part)

    if journal_parts:
        journal_df = pd.concat(journal_parts, ignore_index=True)
        journal_df = journal_df.drop_duplicates(
            subset=[c for c in ("timestamp", "event", "symbol", "side", "notional") if c in journal_df.columns]
        )
    else:
        journal_df = pd.DataFrame()

    fills_df = _fetch_alpaca_fills(limit=limit)
    if not fills_df.empty and not journal_df.empty and "timestamp" in journal_df.columns:
        # Prefer journal signals; add fills not already represented.
        journal_df = pd.concat([journal_df, fills_df], ignore_index=True)
        journal_df = journal_df.drop_duplicates(
            subset=["timestamp", "symbol", "side"],
            keep="first",
        )
    elif journal_df.empty:
        journal_df = fills_df

    if journal_df.empty:
        return None

    journal_df["sleeve"] = journal_df["symbol"].fillna("").astype(str).map(_infer_sleeve)
    keep = ["timestamp", "event", "symbol", "side", "notional", "sleeve"]
    cols = [c for c in keep if c in journal_df.columns]
    out = journal_df[cols].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp", ascending=False)
    return out.head(limit).reset_index(drop=True)


def _filter_equity_journal(path: Path, df: pd.DataFrame, book_id: str) -> pd.DataFrame:
    if df.empty:
        return df
    if path.resolve() == legacy_journal_path().resolve():
        try:
            filtered, _meta = filter_paper_journal(df, live_only=book_id == "alpaca_live")
            if not filtered.empty:
                df = filtered
        except Exception:
            pass
    if "equity" not in df.columns:
        return pd.DataFrame()
    if "event" in df.columns:
        df = df.loc[df["event"].astype(str).isin(EQUITY_EVENTS)].copy()
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    return df.dropna(subset=["equity"])


def _load_equity_sparkline(
    username: str,
    book_id: str,
    *,
    max_points: int = SPARKLINE_POINTS,
) -> pd.DataFrame | None:
    parts: list[pd.DataFrame] = []
    for path in _journal_search_paths(username, book_id):
        raw = _read_trade_journal_csv(path)
        part = _filter_equity_journal(path, raw, book_id)
        if not part.empty:
            parts.append(part[["timestamp", "equity"]])

    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp"], keep="last")
    # Focus on small live window when present (exclude old paper $100k history).
    small = df[df["equity"] <= config.SMALL_ACCOUNT_EQUITY_THRESHOLD * 2]
    if len(small) >= 5:
        df = small
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts")
    if len(df) > max_points:
        step = max(1, len(df) // max_points)
        df = df.iloc[::step].tail(max_points)
    return df[["ts", "equity"]].reset_index(drop=True)


def _load_daily_closes(symbol: str, days: int = CHART_DAYS) -> pd.DataFrame | None:
    table = f"{config.normalize_symbol(symbol)}_daily"
    db_path = _resolve_path(config.DB_PATH)
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            return None
        df = pd.read_sql(
            f'SELECT Date, Close FROM "{table}" ORDER BY Date DESC LIMIT ?',
            conn,
            params=(days,),
        )
    finally:
        conn.close()
    if df.empty:
        return None
    df = df.sort_values("Date").reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    return df.dropna(subset=["Date", "Close"])


def _market_open_countdown(heartbeat: dict | None) -> str:
    scan = (heartbeat or {}).get("scan_schedule") or {}
    if scan.get("market_open"):
        return "Open"
    session_open = scan.get("session_open") or scan.get("orders_start")
    if not session_open:
        return "—"
    try:
        open_dt = pd.Timestamp(session_open)
        now = pd.Timestamp.now(tz=open_dt.tz)
        secs = (open_dt - now).total_seconds()
        if secs <= 0:
            return "Soon"
        hours = int(secs // 3600)
        mins = int((secs % 3600) // 60)
        if hours >= 24:
            return f"{hours // 24}d {hours % 24}h"
        return f"{hours}h {mins}m"
    except Exception:
        return str(session_open)[:16]


def _regime_color(regime: str) -> str:
    r = (regime or "").upper()
    if "PANIC" in r or "BEAR" in r:
        return COLORS["red"]
    if "BULL" in r or "RISK_ON" in r:
        return COLORS["green"]
    return COLORS["amber"]


def _expected_actions(heartbeat: dict | None) -> list[str]:
    if not heartbeat:
        return ["Start Bot to begin trading cycles (first heartbeat ~60s)."]
    lines: list[str] = []
    if heartbeat.get("halted"):
        lines.append("Risk halt — no new entries.")
    wisdom = heartbeat.get("wisdom") or {}
    if wisdom.get("paused"):
        lines.append("Wisdom paused — entries blocked.")
    gp = heartbeat.get("game_plan_state") or {}
    if (gp.get("signals") or {}).get("yield_gate"):
        lines.append("Yield gate blocking SPY entries.")
    exposure = heartbeat.get("sleeve_exposure") or {}
    vti_val = float(exposure.get("vti_core_value") or 0)
    vti_cap = float(exposure.get("vti_core_cap") or 0)
    vti_tgt = float((heartbeat.get("sleeve_caps") or {}).get("vti_core") or 0)
    scan = heartbeat.get("scan_schedule") or {}
    if vti_tgt > 0 and vti_cap > 0 and vti_val < vti_cap * 0.95:
        if scan.get("market_open"):
            lines.append(f"VTI under target — rebalance toward {vti_tgt:.0%} likely.")
        else:
            lines.append(f"Will buy VTI toward {vti_tgt:.0%} on next market open.")
    elif vti_tgt > 0:
        lines.append(f"VTI core on target (~{vti_tgt:.0%}).")
    if scan.get("market_open"):
        lines.append("Equity session open.")
    else:
        lines.append(f"Session closed — next open in {_market_open_countdown(heartbeat)}.")
    return lines or ["Monitoring — no immediate actions."]


def _find_run_all_pids() -> list[int]:
    pids: list[int] = []
    try:
        if sys.platform == "win32":
            cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*run_all.py*' } | "
                "Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            pids = [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
        else:
            out = subprocess.check_output(["pgrep", "-f", "run_all.py"], text=True)
            pids = [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        pass
    return pids


def _bot_python() -> str:
    """Interpreter for run_all.py (venv python when dashboard is a frozen .exe)."""
    if getattr(sys, "frozen", False):
        venv_py = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        if venv_py.is_file():
            return str(venv_py)
        import shutil

        for name in ("python", "python3", "python.exe"):
            found = shutil.which(name)
            if found:
                return found
    return sys.executable


def _launch_bot() -> tuple[bool, str]:
    if _find_run_all_pids():
        return False, "run_all.py is already running."
    run_all = PROJECT_ROOT / "run_all.py"
    if not run_all.is_file():
        return False, f"run_all.py not found at {run_all}"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    subprocess.Popen(
        [_bot_python(), str(run_all)],
        cwd=str(PROJECT_ROOT),
        creationflags=flags,
    )
    return True, "Started run_all.py in background."


def _tray_image() -> "Image.Image":
    img = Image.new("RGBA", (64, 64), (20, 20, 20, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([8, 8, 56, 56], radius=10, fill=(30, 58, 95, 255))
    draw.text((18, 20), "PT", fill=(241, 245, 249, 255))
    return img


def _stop_bot_processes() -> tuple[int, str]:
    pids = _find_run_all_pids()
    if not pids:
        return 0, "No run_all.py process found."
    stopped = 0
    errors: list[str] = []
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    check=True,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                subprocess.run(["kill", "-TERM", str(pid)], check=True, capture_output=True)
            stopped += 1
        except subprocess.CalledProcessError as exc:
            errors.append(f"PID {pid}: {exc}")
    msg = f"Stopped {stopped} bot process(es)."
    if errors:
        msg += " " + "; ".join(errors)
    return stopped, msg


def _light_line_chart(
    df: pd.DataFrame,
    *,
    title: str,
    color: str,
    height: float = 1.35,
    show_axis: bool = False,
) -> Figure:
    fig = Figure(figsize=(4.0, height), dpi=CHART_DPI, facecolor=COLORS["surface"])
    ax = fig.add_subplot(111)
    ax.set_facecolor(COLORS["surface"])
    y = df["Close"].values
    x = range(len(y))
    ax.plot(x, y, color=color, linewidth=1.8, antialiased=True)
    ax.fill_between(x, y, min(y) * 0.998 if len(y) else 0, color=color, alpha=0.12)
    if not show_axis:
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
    else:
        ax.tick_params(colors=COLORS["muted"], labelsize=7)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(COLORS["chart_grid"])
        ax.spines["left"].set_color(COLORS["chart_grid"])
    ax.set_title(title, color=COLORS["text"], fontsize=10, pad=6, fontweight="bold")
    fig.subplots_adjust(left=0.08, right=0.98, top=0.82, bottom=0.12)
    return fig


# --- UI widgets --------------------------------------------------------------


class MetricCard(ctk.CTkFrame):
    """Metric tile with optional hero sizing and subtle hover highlight."""

    def __init__(
        self,
        master,
        title: str,
        *,
        hero: bool = False,
        **kwargs,
    ):
        super().__init__(
            master,
            corner_radius=14,
            fg_color=COLORS["card"],
            border_width=1,
            border_color=COLORS["border"],
            **kwargs,
        )
        self._hero = hero
        pad_x = 14 if hero else 10
        pad_y = 12 if hero else 8
        self._title = ctk.CTkLabel(
            self,
            text=title.upper(),
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._title.pack(anchor="w", padx=pad_x, pady=(pad_y, 2))
        value_size = 32 if hero else 16
        self._value = ctk.CTkLabel(
            self,
            text="—",
            font=ctk.CTkFont(family="Segoe UI", size=value_size, weight="bold"),
            text_color=COLORS["text"],
        )
        self._value.pack(anchor="w", padx=pad_x, pady=(0, pad_y))
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        for w in (self._title, self._value):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)

    def _on_enter(self, _event=None) -> None:
        self.configure(fg_color=COLORS["card_hover"], border_color=COLORS["accent"])

    def _on_leave(self, _event=None) -> None:
        self.configure(fg_color=COLORS["card"], border_color=COLORS["border"])

    def set(self, text: str, color: str | None = None) -> None:
        self._value.configure(text=text, text_color=color or COLORS["text"])


class DataTable(ctk.CTkFrame):
    """Lightweight dark table via ttk.Treeview."""

    def __init__(self, master, columns: list[str], *, height: int = 8, large: bool = False):
        super().__init__(master, fg_color="transparent")
        style = ttk.Style()
        style.theme_use("clam")
        style_name = "Dash.Treeview.Large" if large else "Dash.Treeview"
        row_h = 34 if large else 26
        font = ("Segoe UI", 12) if large else ("Segoe UI", 10)
        head_font = ("Segoe UI", 11, "bold") if large else ("Segoe UI", 10, "bold")
        style.configure(
            style_name,
            background=COLORS["surface"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["surface"],
            borderwidth=0,
            rowheight=row_h,
            font=font,
        )
        style.configure(
            f"{style_name}.Heading",
            background=COLORS["surface2"],
            foreground=COLORS["muted"] if not large else COLORS["text_dim"],
            font=head_font,
        )
        style.map(style_name, background=[("selected", COLORS["accent"])])

        self._columns = columns
        self._tree = ttk.Treeview(
            self,
            columns=columns,
            show="headings",
            height=height,
            style=style_name,
        )
        for col in columns:
            self._tree.heading(col, text=col)
            if large:
                widths = {
                    "Ticker": 96,
                    "symbol": 96,
                    "Sleeve": 88,
                    "sleeve": 88,
                    "Qty": 80,
                    "Current": 92,
                    "P&L $": 88,
                    "P&L %": 72,
                    "Time": 118,
                    "Side": 56,
                    "Notional": 88,
                }
                width = widths.get(col, 80)
            else:
                width = 88 if col in ("Ticker", "symbol", "event", "sleeve") else 72
            anchor = "w" if col in ("Ticker", "symbol", "Time", "timestamp") else "center"
            self._tree.column(col, width=width, anchor=anchor, stretch=col in ("Ticker", "symbol"))
        self._tree.tag_configure("profit", foreground=COLORS["green"])
        self._tree.tag_configure("loss", foreground=COLORS["red"])
        scroll = ctk.CTkScrollbar(self, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def clear(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

    def set_rows(self, rows: list[dict], *, pnl_col: str | None = None) -> None:
        self.clear()
        for row in rows:
            values = [row.get(c, "") for c in self._columns]
            tag = ""
            if pnl_col and pnl_col in row:
                try:
                    tag = "profit" if float(row[pnl_col]) >= 0 else "loss"
                except (TypeError, ValueError):
                    tag = ""
            self._tree.insert("", "end", values=values, tags=(tag,) if tag else ())


class LoginApp(ctk.CTk):
    """Sign in — same accounts as the web portal."""

    def __init__(self, on_success) -> None:
        super().__init__()
        self._on_success = on_success
        self.title("PythonTrading — Sign in")
        self.geometry("420x520")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg"])
        if ICON_PATH.is_file():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

        ctk.CTkLabel(
            self,
            text="PythonTrading",
            font=_ctk_font("title"),
        ).pack(pady=(28, 4))
        ctk.CTkLabel(
            self,
            text="Sign in to connect Alpaca and open your dashboard.",
            font=_ctk_font("body_sm"),
            text_color=COLORS["muted"],
        ).pack(pady=(0, 18))

        card = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=16,
            border_width=1,
            border_color=COLORS["border"],
        )
        card.pack(padx=28, pady=8, fill="both", expand=True)
        self._tabs = ctk.CTkTabview(
            card,
            width=340,
            height=340,
            fg_color=COLORS["surface"],
            segmented_button_fg_color=COLORS["surface2"],
            segmented_button_selected_color=COLORS["accent"],
            segmented_button_unselected_color=COLORS["surface2"],
        )
        self._tabs.pack(padx=12, pady=12)
        tab_login = self._tabs.add("Log in")
        tab_register = self._tabs.add("Register")

        self._login_user = self._form_field(tab_login, "Username")
        self._login_pwd = self._form_field(tab_login, "Password", show="*")
        last_user = get_last_username()
        if last_user:
            self._login_user.insert(0, last_user)
        prefs = read_desktop_prefs()
        remember_default = prefs.get("remember_username", True) is not False
        self._remember_var = ctk.BooleanVar(value=remember_default)
        ctk.CTkCheckBox(
            tab_login,
            text="Remember username",
            variable=self._remember_var,
            font=_ctk_font("body_sm"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
        ).pack(anchor="w", padx=8, pady=(4, 0))
        ctk.CTkButton(
            tab_login,
            text="Log in",
            command=self._do_login,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            corner_radius=10,
            height=36,
        ).pack(pady=16)
        self._login_user.bind("<Return>", lambda _e: self._login_pwd.focus_set())
        self._login_pwd.bind("<Return>", lambda _e: self._do_login())
        if last_user:
            self.after(100, self._login_pwd.focus_set)

        self._reg_user = self._form_field(tab_register, "Choose username")
        self._reg_pwd = self._form_field(tab_register, "Password", show="*")
        self._reg_pwd2 = self._form_field(tab_register, "Confirm password", show="*")
        invite_required = bool(os.getenv("PORTAL_INVITE_CODE", "").strip())
        self._reg_invite = self._form_field(
            tab_register,
            "Invite code" + (" (required)" if invite_required else " (optional)"),
            show="*",
            required=False,
        )
        ctk.CTkButton(
            tab_register,
            text="Create account",
            command=self._do_register,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            corner_radius=10,
            height=36,
        ).pack(pady=16)
        self._reg_pwd2.bind("<Return>", lambda _e: self._do_register())

        init_db()

    def _form_field(
        self, parent, label: str, *, show: str | None = None, required: bool = True
    ) -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label, anchor="w").pack(fill="x", padx=8, pady=(8, 2))
        entry = ctk.CTkEntry(parent, show=show)
        entry.pack(fill="x", padx=8)
        entry._required = required  # type: ignore[attr-defined]
        return entry

    def _do_login(self) -> None:
        ok, name = authenticate(self._login_user.get(), self._login_pwd.get())
        if ok and name:
            self._finish(name)
            return
        messagebox.showerror("Log in", "Invalid username or password.")

    def _do_register(self) -> None:
        pwd = self._reg_pwd.get()
        if pwd != self._reg_pwd2.get():
            messagebox.showerror("Register", "Passwords do not match.")
            return
        username = self._reg_user.get().strip().lower()
        ok, msg = register_user(
            username,
            pwd,
            invite_code=self._reg_invite.get(),
        )
        if ok:
            save_last_username(username, remember=self._remember_var.get())
            logged_in, name = authenticate(username, pwd)
            if logged_in and name:
                self._finish(name)
                return
            messagebox.showinfo("Register", msg + " Log in with your new account.")
            self._tabs.set("Log in")
            self._login_user.delete(0, "end")
            self._login_user.insert(0, username)
            return
        messagebox.showerror("Register", msg)

    def _finish(self, username: str) -> None:
        save_last_username(username, remember=self._remember_var.get())
        self.withdraw()
        self._on_success(username)
        self.destroy()


def _book_dropdown_values() -> list[str]:
    return [label for label, _bid, _ok in book_dropdown_entries()]


def _selectable_book_ids() -> set[str]:
    return {bid for _label, bid, ok in book_dropdown_entries() if ok}


class BookMenu(ctk.CTkToplevel):
    """Hamburger menu — switch books and account actions."""

    def __init__(
        self,
        master,
        username: str,
        current_book: str,
        *,
        on_switch_book,
        on_edit_keys,
        on_logout,
    ) -> None:
        super().__init__(master)
        self._on_switch_book = on_switch_book
        self.title("Menu")
        self.geometry("300x280")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["card"])
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(
            self,
            text="Account",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            self,
            text=f"Signed in as {username}",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11),
            wraplength=260,
        ).pack(anchor="w", padx=16, pady=(0, 10))

        ctk.CTkLabel(
            self,
            text="Trading account",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(0, 4))
        self._book_var = ctk.StringVar(value=dropdown_label_for_book(current_book))
        ctk.CTkOptionMenu(
            self,
            variable=self._book_var,
            values=_book_dropdown_values(),
            command=self._on_book_selected,
            width=260,
            fg_color="#1e3a5f",
            button_color="#334155",
            button_hover_color="#475569",
        ).pack(anchor="w", padx=16, pady=(0, 12))

        ctk.CTkButton(
            self,
            text="Edit API keys for current account",
            fg_color="#374151",
            hover_color="#4b5563",
            command=lambda: self._action(on_edit_keys),
        ).pack(fill="x", padx=16, pady=(4, 4))
        ctk.CTkButton(
            self,
            text="Log out",
            fg_color="#7f1d1d",
            hover_color="#991b1b",
            command=lambda: self._action(on_logout),
        ).pack(fill="x", padx=16, pady=(4, 16))

    def _on_book_selected(self, menu_label: str) -> None:
        self.grab_release()
        self.destroy()
        self._on_switch_book(menu_label)

    def _action(self, callback) -> None:
        self.grab_release()
        self.destroy()
        callback()


class AlpacaKeysDialog(ctk.CTkToplevel):
    """First-run setup or edit saved Alpaca credentials."""

    def __init__(
        self,
        master,
        username: str,
        book_id: str,
        on_complete,
        *,
        edit_mode: bool = False,
    ):
        super().__init__(master)
        self._username = username
        self._book_id = book_id
        self._edit_mode = edit_mode
        self._on_complete = on_complete
        self._prefs = env_flags_for_book(book_id)
        self._selectable = _selectable_book_ids()
        self._book_var = ctk.StringVar(value=dropdown_label_for_book(book_id))
        self._hint_label: ctk.CTkLabel | None = None
        self._apply_book_title()

        heading = "Alpaca API keys" if edit_mode else "First-run setup"
        ctk.CTkLabel(
            self,
            text=heading,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self,
            text="Each account uses its own API key pair from Alpaca.",
            text_color=COLORS["muted"],
            wraplength=400,
        ).pack(pady=(0, 10))

        account_row = ctk.CTkFrame(self, fg_color="transparent")
        account_row.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(
            account_row,
            text="Trading account",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkOptionMenu(
            account_row,
            variable=self._book_var,
            values=_book_dropdown_values(),
            command=self._on_book_selected,
            width=320,
        ).pack(anchor="w")

        self._mode_label = ctk.CTkLabel(
            self,
            text="",
            wraplength=400,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._mode_label.pack(padx=20, pady=(10, 4))
        self._refresh_book_ui()

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=20)

        self._api_key = self._field(form, "API Key ID")
        self._api_secret = self._field(form, "API Secret", show="*")
        ctk.CTkLabel(form, text="Telegram (optional)", text_color=COLORS["muted"]).pack(
            anchor="w", pady=(10, 4)
        )
        self._tg_token = self._field(form, "Bot token", required=False)
        self._tg_chat = self._field(form, "Chat ID", required=False)

        btn_label = "Save keys" if edit_mode else "Save & Continue"
        ctk.CTkButton(self, text=btn_label, command=self._save).pack(pady=16)

    def _apply_book_title(self) -> None:
        book_name = book_label(self._book_id)
        suffix = "Edit keys" if self._edit_mode else "Setup"
        self.title(f"{suffix} — {book_name}")

    def _on_book_selected(self, menu_label: str) -> None:
        book_id = book_id_for_dropdown_label(menu_label)
        if not book_id:
            return
        if book_id not in self._selectable:
            messagebox.showinfo(
                "Coming soon",
                f"{menu_label} is not available yet.",
            )
            self._book_var.set(dropdown_label_for_book(self._book_id))
            return
        if book_id == self._book_id:
            return
        self._book_id = book_id
        self._prefs = env_flags_for_book(book_id)
        self._apply_book_title()
        self._refresh_book_ui()

    def _refresh_book_ui(self) -> None:
        paper = self._prefs["paper"]
        mode_color = COLORS["blue"] if paper else COLORS["live"]
        mode_text = (
            "Paper keys — Alpaca Paper Trading dashboard (simulated funds)."
            if paper
            else "Live keys — Alpaca Live dashboard. Real money."
        )
        self._mode_label.configure(text=mode_text, text_color=mode_color)
        if self._hint_label is not None:
            self._hint_label.destroy()
            self._hint_label = None
        if self._edit_mode and has_alpaca_config(self._username, self._book_id):
            hint = read_user_env_key_hint(self._username, self._book_id)
            if hint:
                self._hint_label = ctk.CTkLabel(
                    self,
                    text=f"Saved key id ends with {hint}",
                    text_color=COLORS["muted"],
                    font=ctk.CTkFont(size=11),
                )
                self._hint_label.pack(padx=20, pady=(0, 6))

    def _field(self, parent, label: str, *, show: str | None = None, required: bool = True):
        ctk.CTkLabel(parent, text=label, anchor="w").pack(fill="x", pady=(6, 2))
        entry = ctk.CTkEntry(parent, show=show)
        entry.pack(fill="x")
        entry._required = required  # type: ignore[attr-defined]
        return entry

    def _save(self) -> None:
        key = self._api_key.get().strip()
        secret = self._api_secret.get().strip()
        if not key or not secret:
            messagebox.showerror("Setup", "API Key ID and Secret are required.")
            return
        paper = self._prefs["paper"]
        allow_live = self._prefs["allow_live"]
        if allow_live and not paper:
            if not messagebox.askyesno(
                "Live trading",
                f"Saving LIVE keys for {book_label(self._book_id)}.\n\n"
                "Real money can be traded. Continue?",
                icon="warning",
            ):
                return
        write_user_env(
            self._username,
            api_key=key,
            api_secret=secret,
            paper=paper,
            allow_live=allow_live,
            telegram_token=self._tg_token.get(),
            telegram_chat=self._tg_chat.get(),
            book_id=self._book_id,
        )
        _apply_user_paths(self._username, self._book_id)
        if self._edit_mode and bot_running(self._username, self._book_id):
            if messagebox.askyesno(
                "Restart bot",
                "Bot is running with the old keys.\n\nStop it now? "
                "Start Bot again after saving to use the new keys.",
            ):
                stop_bot(self._username, self._book_id)
        self.grab_release()
        self.destroy()
        self._on_complete()


class TradingDashboardApp(ctk.CTk):
    def __init__(
        self,
        username: str,
        book_id: str | None = None,
        *,
        on_logout=None,
        auto_start_bot: bool = False,
    ) -> None:
        super().__init__()
        self._username = username
        self._on_logout = on_logout
        self._auto_start_bot = auto_start_bot
        migrate_user_to_books(username)
        self._book_id = book_id or get_last_book_id()
        save_last_book_id(self._book_id)
        self._apply_user_paths(username, self._book_id)
        self.title(f"PythonTrading — {book_label(self._book_id)}")
        self.geometry("1000x780")
        self.minsize(960, 680)
        self.configure(fg_color=COLORS["bg"])

        self._refresh_job: str | None = None
        self._clock_job: str | None = None
        self._active_tab = "Overview"
        self._charts_dirty = True
        self._last_equity = 0.0
        self._spark_canvas: FigureCanvasTkAgg | None = None
        self._price_canvases: list[FigureCanvasTkAgg] = []
        self._tray_icon = None
        self._shutting_down = False

        if ICON_PATH.is_file():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

        # Header bar
        header_bar = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=16,
            border_width=1,
            border_color=COLORS["border"],
        )
        header_bar.pack(fill="x", padx=14, pady=(12, 6))
        self._book_var = ctk.StringVar(value=dropdown_label_for_book(self._book_id))

        header_inner = ctk.CTkFrame(header_bar, fg_color="transparent")
        header_inner.pack(fill="x", padx=12, pady=10)

        header_left = ctk.CTkFrame(header_inner, fg_color="transparent")
        header_left.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            header_left,
            text="☰",
            width=38,
            height=34,
            font=ctk.CTkFont(size=18),
            fg_color=COLORS["surface2"],
            hover_color=COLORS["accent"],
            corner_radius=10,
            command=self._open_book_menu,
        ).pack(side="left", padx=(0, 8))
        title_block = ctk.CTkFrame(header_left, fg_color="transparent")
        title_block.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_block,
            text="PythonTrading Monitor",
            font=_ctk_font("title"),
            text_color=COLORS["text"],
        ).pack(anchor="w")
        sub_row = ctk.CTkFrame(title_block, fg_color="transparent")
        sub_row.pack(anchor="w", pady=(2, 0))
        self._clock_label = ctk.CTkLabel(
            sub_row,
            text="",
            font=_ctk_font("body_sm"),
            text_color=COLORS["muted"],
        )
        self._clock_label.pack(side="left")
        ctk.CTkLabel(
            sub_row,
            text=f"  ·  {username}",
            font=_ctk_font("body_sm"),
            text_color=COLORS["text_dim"],
        ).pack(side="left")
        self._live_equity_label = ctk.CTkLabel(
            title_block,
            text="Live Equity: —",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
            text_color=COLORS["green"],
        )
        self._live_equity_label.pack(anchor="w", pady=(6, 0))
        self._book_menu = ctk.CTkOptionMenu(
            header_left,
            variable=self._book_var,
            values=_book_dropdown_values(),
            command=self._on_header_book_selected,
            width=172,
            font=_ctk_font("body_sm"),
            fg_color=COLORS["accent"],
            button_color=COLORS["surface2"],
            button_hover_color=COLORS["accent_hover"],
            dropdown_fg_color=COLORS["surface2"],
        )
        self._book_menu.pack(side="right", padx=(8, 0))

        header_right = ctk.CTkFrame(header_inner, fg_color="transparent")
        header_right.pack(side="right", anchor="ne")
        self._bot_badge = ctk.CTkLabel(
            header_right,
            text="Bot: —",
            font=_ctk_font("body_sm"),
            text_color=COLORS["muted"],
        )
        self._bot_badge.pack(side="right", padx=(12, 0))
        btn_row = ctk.CTkFrame(header_right, fg_color="transparent")
        btn_row.pack(side="right")
        ctk.CTkButton(
            btn_row,
            text="Refresh",
            width=78,
            height=32,
            corner_radius=10,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["accent"],
            command=self.refresh_data,
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            btn_row,
            text="Start",
            width=72,
            height=32,
            corner_radius=10,
            fg_color=COLORS["paper_ok_bg"],
            hover_color=COLORS["green_dim"],
            command=self._on_start_bot,
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            btn_row,
            text="Stop",
            width=68,
            height=32,
            corner_radius=10,
            fg_color=COLORS["live_bg"],
            hover_color=COLORS["live"],
            command=self._on_stop_bot,
        ).pack(side="left", padx=3)

        top_stack = ctk.CTkFrame(self, fg_color="transparent")
        top_stack.pack(fill="x", padx=14, pady=(0, 4))

        # Status pills: Live/Paper · Small Account · Regime · Bot
        status_row = ctk.CTkFrame(
            top_stack,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        status_row.pack(fill="x", pady=(0, 8))
        status_inner = ctk.CTkFrame(status_row, fg_color="transparent")
        status_inner.pack(fill="x", padx=12, pady=10)

        def _pill(parent, text: str, fg: str, text_color: str) -> ctk.CTkLabel:
            lbl = ctk.CTkLabel(
                parent,
                text=text,
                font=_ctk_font("body_sm"),
                fg_color=fg,
                text_color=text_color,
                corner_radius=8,
                padx=12,
                pady=4,
            )
            lbl.pack(side="left", padx=(0, 8))
            return lbl

        self._pill_mode = _pill(status_inner, "● PAPER", COLORS["paper_ok_bg"], COLORS["green"])
        self._pill_small = _pill(status_inner, "SMALL ACCOUNT", COLORS["small_bg"], COLORS["amber"])
        self._pill_small.pack_forget()
        self._pill_regime = _pill(status_inner, "Regime: —", COLORS["surface2"], COLORS["text_dim"])
        self._pill_bot = _pill(status_inner, "Bot: —", COLORS["surface2"], COLORS["muted"])

        self._small_panel = ctk.CTkFrame(top_stack, fg_color="transparent")
        self._small_body = ctk.CTkLabel(
            self._small_panel,
            text="",
            justify="left",
            font=_ctk_font("caption"),
            text_color=COLORS["amber"],
        )
        self._small_body.pack(anchor="w", padx=4)

        # Hero metrics: Equity · Cash · Open P&L · sparkline
        hero_row = ctk.CTkFrame(top_stack, fg_color="transparent")
        hero_row.pack(fill="x", pady=(0, 6))
        self._metric_cards: dict[str, MetricCard] = {}
        self._metric_cards["equity"] = MetricCard(hero_row, "Equity", hero=True)
        self._metric_cards["equity"].pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._metric_cards["cash"] = MetricCard(hero_row, "Cash", hero=True)
        self._metric_cards["cash"].pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._metric_cards["pnl"] = MetricCard(hero_row, "Open P&L", hero=True)
        self._metric_cards["pnl"].pack(side="left", fill="both", expand=True, padx=(0, 6))

        spark_wrap = ctk.CTkFrame(
            hero_row,
            fg_color=COLORS["card"],
            corner_radius=14,
            border_width=1,
            border_color=COLORS["border"],
            width=260,
        )
        spark_wrap.pack(side="right")
        spark_wrap.pack_propagate(False)
        ctk.CTkLabel(
            spark_wrap,
            text="EQUITY TREND",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        ).pack(anchor="w", padx=12, pady=(10, 0))
        self._spark_frame = ctk.CTkFrame(spark_wrap, fg_color="transparent", height=78)
        self._spark_frame.pack(fill="both", expand=True, padx=8, pady=(4, 10))

        metrics = ctk.CTkFrame(top_stack, fg_color="transparent")
        metrics.pack(fill="x", pady=(0, 8))
        for i, key in enumerate(("invested", "market")):
            card = MetricCard(metrics, key.title())
            card.grid(row=0, column=i, padx=4, sticky="nsew")
            metrics.grid_columnconfigure(i, weight=1)
            self._metric_cards[key] = card

        # Open positions — prominent top section
        pos_section = ctk.CTkFrame(
            top_stack,
            fg_color=COLORS["card"],
            corner_radius=14,
            border_width=1,
            border_color=COLORS["border"],
        )
        pos_section.pack(fill="x", pady=(0, 8))
        pos_head = ctk.CTkFrame(pos_section, fg_color="transparent")
        pos_head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            pos_head,
            text="Open Positions",
            font=_ctk_font("heading"),
            text_color=COLORS["text"],
        ).pack(side="left")
        self._pos_total = ctk.CTkLabel(
            pos_head,
            text="",
            font=_ctk_font("body_sm"),
            text_color=COLORS["muted"],
        )
        self._pos_total.pack(side="right")
        self._top_positions_table = DataTable(
            pos_section,
            ["Ticker", "Sleeve", "Qty", "Current", "P&L $", "P&L %"],
            height=4,
            large=True,
        )
        self._top_positions_table.pack(fill="x", padx=10, pady=(0, 10))

        # Recent trades — compact strip below positions
        trades_section = ctk.CTkFrame(
            top_stack,
            fg_color=COLORS["card"],
            corner_radius=14,
            border_width=1,
            border_color=COLORS["border"],
        )
        trades_section.pack(fill="x", pady=(0, 8))
        trades_head = ctk.CTkFrame(trades_section, fg_color="transparent")
        trades_head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            trades_head,
            text="Recent Trades",
            font=_ctk_font("heading"),
            text_color=COLORS["text"],
        ).pack(side="left")
        self._trades_hint = ctk.CTkLabel(
            trades_head,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._trades_hint.pack(side="right")
        self._top_trades_table = DataTable(
            trades_section,
            ["Time", "Ticker", "Side", "Sleeve", "Notional"],
            height=3,
            large=True,
        )
        self._top_trades_table.pack(fill="x", padx=10, pady=(0, 10))

        # Tabs
        self._tabs = ctk.CTkTabview(
            self,
            height=240,
            command=self._on_tab_changed,
            fg_color=COLORS["surface"],
            segmented_button_fg_color=COLORS["surface2"],
            segmented_button_selected_color=COLORS["accent"],
            segmented_button_selected_hover_color=COLORS["accent_hover"],
            segmented_button_unselected_color=COLORS["surface2"],
            segmented_button_unselected_hover_color=COLORS["card_hover"],
            text_color=COLORS["text"],
            text_color_disabled=COLORS["muted"],
        )
        self._tabs.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        try:
            self._tabs._segmented_button.configure(
                font=_ctk_font("body"),
                height=36,
                corner_radius=10,
            )
        except Exception:
            pass
        self._tab_overview = self._tabs.add("Overview")
        self._tab_positions = self._tabs.add("Positions")
        self._tab_trades = self._tabs.add("Trades")
        self._tab_wisdom = self._tabs.add("Wisdom")
        self._tab_charts = self._tabs.add("Charts")

        self._build_overview_tab()
        self._positions_table = DataTable(
            self._tab_positions,
            ["Ticker", "Sleeve", "Qty", "Entry", "Current", "P&L $", "P&L %"],
            height=12,
            large=True,
        )
        self._positions_table.pack(fill="both", expand=True, padx=10, pady=10)

        self._trades_table = DataTable(
            self._tab_trades,
            ["timestamp", "event", "symbol", "side", "notional", "sleeve"],
            height=12,
            large=True,
        )
        self._trades_table.pack(fill="both", expand=True, padx=10, pady=10)
        self._trades_tab_hint = ctk.CTkLabel(
            self._tab_trades,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="w",
        )
        self._trades_tab_hint.pack(fill="x", padx=12, pady=(0, 8))

        self._build_wisdom_tab()
        self._build_charts_tab()

        # Footer — status line only
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=14, pady=(0, 10))
        footer_inner = ctk.CTkFrame(footer, fg_color="transparent")
        footer_inner.pack(fill="x")
        self._charts_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            footer_inner,
            text="Charts on refresh",
            variable=self._charts_var,
            font=_ctk_font("caption"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            width=18,
            height=18,
        ).pack(side="left", padx=(0, 10))
        self._tray_var = ctk.BooleanVar(value=False)
        tray_cb = ctk.CTkCheckBox(
            footer_inner,
            text="Minimize to tray",
            variable=self._tray_var,
            font=_ctk_font("caption"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            width=18,
            height=18,
        )
        tray_cb.pack(side="left")
        if not TRAY_AVAILABLE:
            tray_cb.configure(state="disabled")
        self._status_label = ctk.CTkLabel(
            footer_inner,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._status_label.pack(side="right")

        self._tick_clock()
        self._start_clock()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if _needs_setup(username, self._book_id):
            self.after(200, self._show_setup_wizard)
        else:
            self._bootstrap_live_equity()
            self.refresh_data()
            self._schedule_refresh()
            if self._auto_start_bot:
                self.after(800, lambda: self._maybe_auto_start_bot(quiet=True))

    def _apply_user_paths(self, username: str, book_id: str | None = None) -> None:
        _apply_user_paths(username, book_id or self._book_id)

    def _open_book_menu(self) -> None:
        BookMenu(
            self,
            self._username,
            self._book_id,
            on_switch_book=self._on_header_book_selected,
            on_edit_keys=self._on_edit_keys,
            on_logout=self._on_logout_click,
        )

    def _on_header_book_selected(self, menu_label: str) -> None:
        book_id = book_id_for_dropdown_label(menu_label)
        if not book_id:
            return
        if book_id not in _selectable_book_ids():
            messagebox.showinfo("Coming soon", f"{menu_label} is not available yet.")
            self._book_var.set(dropdown_label_for_book(self._book_id))
            return
        self._switch_book(book_id)

    def _switch_book(self, book_id: str) -> None:
        if book_id == self._book_id:
            return
        if not book_enabled(book_id):
            messagebox.showinfo("Coming soon", f"{book_label(book_id)} is not available yet.")
            self._book_var.set(dropdown_label_for_book(self._book_id))
            return
        self._book_id = book_id
        save_last_book_id(book_id)
        self._book_var.set(dropdown_label_for_book(book_id))
        self.title(f"PythonTrading — {book_label(book_id)}")
        self._apply_user_paths(self._username, book_id)
        if _needs_setup(self._username, book_id):
            self._show_setup_wizard()
        else:
            _reset_equity_cache()
            self._bootstrap_live_equity()
            self.refresh_data()

    def _on_logout_click(self) -> None:
        if self._on_logout is None:
            return
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        self._stop_tray()
        self.destroy()
        self._on_logout()

    def _build_overview_tab(self) -> None:
        ctk.CTkLabel(
            self._tab_overview,
            text="Next actions",
            font=_ctk_font("heading"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 4))
        self._actions_frame = ctk.CTkFrame(self._tab_overview, fg_color="transparent")
        self._actions_frame.pack(fill="x", padx=14)
        self._regime_label = ctk.CTkLabel(self._tab_overview, text="")  # sync target for errors
        self._status_label_tab = ctk.CTkLabel(self._tab_overview, text="")
        self._cycle_label = ctk.CTkLabel(self._tab_overview, text="")

        ctk.CTkLabel(
            self._tab_overview,
            text="Wisdom",
            font=_ctk_font("heading"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 4))
        self._wisdom_line = ctk.CTkLabel(
            self._tab_overview,
            text="—",
            justify="left",
            anchor="w",
            font=_ctk_font("body_sm"),
            text_color=COLORS["muted"],
        )
        self._wisdom_line.pack(fill="x", padx=14)

        ctk.CTkLabel(
            self._tab_overview,
            text="Crypto vol sleeve",
            font=_ctk_font("heading"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 4))
        self._crypto_vol_panel = ctk.CTkFrame(
            self._tab_overview,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._crypto_vol_panel.pack(fill="x", padx=12, pady=(0, 4))
        self._crypto_vol_body = ctk.CTkLabel(
            self._crypto_vol_panel,
            text="—",
            justify="left",
            anchor="w",
            text_color=COLORS["muted"],
            wraplength=780,
        )
        self._crypto_vol_body.pack(fill="x", padx=10, pady=8)

    def _build_wisdom_tab(self) -> None:
        cards = ctk.CTkFrame(self._tab_wisdom, fg_color="transparent")
        cards.pack(fill="x", padx=8, pady=8)
        self._w_sharpe = MetricCard(cards, "Live Sharpe")
        self._w_ret = MetricCard(cards, "Live Return")
        self._w_vs = MetricCard(cards, "vs Active Sim")
        for i, card in enumerate((self._w_sharpe, self._w_ret, self._w_vs)):
            card.grid(row=0, column=i, padx=4, sticky="nsew")
            cards.grid_columnconfigure(i, weight=1)
        self._wisdom_rec = ctk.CTkLabel(
            self._tab_wisdom, text="", wraplength=820, justify="left", text_color=COLORS["muted"]
        )
        self._wisdom_rec.pack(fill="x", padx=12, pady=4)
        self._wisdom_table = DataTable(
            self._tab_wisdom, ["Mode", "Return%", "Sharpe", "Orders"], height=8
        )
        self._wisdom_table.pack(fill="both", expand=True, padx=8, pady=4)
        self._wisdom_hint = ctk.CTkLabel(
            self._tab_wisdom,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["muted"],
            anchor="w",
        )
        self._wisdom_hint.pack(fill="x", padx=12, pady=(0, 8))

    def _build_charts_tab(self) -> None:
        bar = ctk.CTkFrame(self._tab_charts, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(10, 6))
        ctk.CTkButton(
            bar,
            text="Redraw charts",
            width=120,
            height=32,
            corner_radius=10,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            command=self._draw_charts,
        ).pack(side="left")
        self._charts_hint = ctk.CTkLabel(
            bar,
            text="VTI + SPY · GLD when small account",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11),
        )
        self._charts_hint.pack(side="left", padx=10)
        self._charts_frame = ctk.CTkFrame(self._tab_charts, fg_color="transparent")
        self._charts_frame.pack(fill="both", expand=True, padx=8, pady=4)

    def _show_setup_wizard(self) -> None:
        AlpacaKeysDialog(
            self, self._username, self._book_id, on_complete=self._after_keys_saved
        )

    def _on_edit_keys(self) -> None:
        AlpacaKeysDialog(
            self,
            self._username,
            self._book_id,
            on_complete=self._after_keys_saved,
            edit_mode=True,
        )

    def _after_keys_saved(self) -> None:
        self._apply_user_paths(self._username, self._book_id)
        messagebox.showinfo("Alpaca keys", "Credentials saved.")
        self.refresh_data()
        if not self._refresh_job:
            self._schedule_refresh()
        if self._auto_start_bot:
            self.after(400, lambda: self._maybe_auto_start_bot(quiet=True))

    def _on_tab_changed(self) -> None:
        self._active_tab = self._tabs.get()
        if self._active_tab == "Charts":
            self._charts_dirty = True
            self._draw_charts()

    def _clear_frame(self, frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _update_status_row(
        self,
        equity: float,
        small_account: bool,
        *,
        regime: str = "—",
        halted: bool = False,
        bot_running_flag: bool = False,
    ) -> None:
        live = not config.PAPER_TRADING
        if live:
            self._pill_mode.configure(
                text="● LIVE TRADING",
                fg_color=COLORS["live_bg"],
                text_color="#fecaca",
            )
        else:
            self._pill_mode.configure(
                text="● PAPER",
                fg_color=COLORS["paper_ok_bg"],
                text_color=COLORS["green"],
            )

        if small_account:
            self._pill_small.pack(side="left", padx=(0, 8), before=self._pill_regime)
        else:
            self._pill_small.pack_forget()

        regime_short = regime.split(":")[-1].strip() if ":" in regime else regime
        self._pill_regime.configure(
            text=f"Regime: {regime_short}",
            fg_color=COLORS["surface2"],
            text_color=_regime_color(regime),
        )

        if halted:
            bot_text, bot_fg, bot_tc = "Bot: HALTED", COLORS["live_bg"], COLORS["red"]
        elif bot_running_flag:
            bot_text, bot_fg, bot_tc = "Bot: Running", COLORS["paper_ok_bg"], COLORS["green"]
        else:
            bot_text, bot_fg, bot_tc = "Bot: Stopped", COLORS["surface2"], COLORS["amber"]
        self._pill_bot.configure(text=bot_text, fg_color=bot_fg, text_color=bot_tc)

    def _tick_clock(self) -> None:
        now = datetime.now()
        self._clock_label.configure(
            text=now.strftime("%A, %b %d · %I:%M:%S %p").replace(" 0", " ")
        )

    def _start_clock(self) -> None:
        if self._clock_job:
            self.after_cancel(self._clock_job)

        def _loop() -> None:
            self._tick_clock()
            self._clock_job = self.after(1000, _loop)

        _loop()

    def _update_small_panel(self, equity: float, heartbeat: dict | None) -> None:
        if not (equity > 0 and config.is_small_account(equity)):
            self._small_panel.pack_forget()
            return
        chunk = equity * config.effective_risk_per_trade(equity)
        max_n = config.effective_max_notional_per_order(equity)
        vti_tgt = config.vti_core_allocation_pct()
        vti_val = 0.0
        vti_cap = 0.0
        if heartbeat:
            exp = heartbeat.get("sleeve_exposure") or {}
            vti_val = float(exp.get("vti_core_value") or 0)
            vti_cap = float(exp.get("vti_core_cap") or 0)
        text = (
            f"Typical trade size: ~${chunk:.2f} (1% of ${equity:,.2f})  ·  "
            f"Max order: ${max_n:.2f}\n"
            f"VTI target: {vti_tgt:.0%} (${vti_cap:,.2f})  ·  "
            f"Current VTI: ${vti_val:,.2f}"
        )
        self._small_body.configure(text=text)
        self._small_panel.pack(fill="x", pady=(0, 4))

    def _invested_pct(self, heartbeat: dict | None, equity: float, cash: float) -> float:
        if equity <= 0:
            return 0.0
        exposure = (heartbeat or {}).get("sleeve_exposure") or {}
        if exposure:
            total = sum(
                float(exposure.get(f"{k}_value") or 0)
                for k in ("vti_core", "spy", "crypto", "nyse", "metal")
            )
            if total > 0:
                return total / equity * 100
        return max(0.0, (equity - cash) / equity * 100)

    def _bootstrap_live_equity(self) -> None:
        """Force a fresh Alpaca read on startup / book switch before heartbeat fallback."""
        _reset_equity_cache()
        self._last_equity = 0.0
        eq, cash, err = _fetch_account_summary(retries=3)
        if eq is not None and eq > 0:
            self._last_equity = eq
            config.configure_account_profile(eq)
            self._update_live_equity_header(eq, err)
            return
        self._update_live_equity_header(0.0, err)

    def _update_live_equity_header(self, equity: float, acct_err: str | None) -> None:
        live = not config.PAPER_TRADING
        prefix = "Live" if live else "Paper"
        if equity > 0:
            color = COLORS["green"] if live else COLORS["blue"]
            self._live_equity_label.configure(
                text=f"{prefix} Equity: ${equity:,.2f}",
                text_color=color,
            )
            return
        if acct_err:
            short = acct_err if len(acct_err) <= 48 else acct_err[:45] + "…"
            self._live_equity_label.configure(
                text=f"{prefix} Equity: unavailable · {short}",
                text_color=COLORS["amber"],
            )
        else:
            self._live_equity_label.configure(
                text=f"{prefix} Equity: —",
                text_color=COLORS["muted"],
            )

    def refresh_data(self) -> None:
        _reset_equity_cache()
        heartbeat = _load_json(_resolve_path(config.HEARTBEAT_FILE))
        scorecard, scorecard_src = _load_scorecard(self._username, self._book_id)
        acct_eq, acct_cash, acct_err = _fetch_account_summary(retries=2)
        positions_df, pos_err = _fetch_positions()
        journal_df = _load_trade_history(self._username, self._book_id)

        equity, cash, acct_err = _resolve_equity_cash(acct_eq, acct_cash, acct_err, heartbeat)
        self._last_equity = equity
        if equity > 0:
            config.configure_account_profile(equity)
        small_account = equity > 0 and config.is_small_account(equity)

        self._update_live_equity_header(equity, acct_err)
        running = bot_running(self._username, self._book_id)
        self._update_small_panel(equity, heartbeat)

        cash_pct = (cash / equity * 100) if equity > 0 else 0.0
        invested = self._invested_pct(heartbeat, equity, cash)
        upl = 0.0
        if positions_df is not None and not positions_df.empty:
            upl = float(positions_df["P&L $"].sum())

        self._metric_cards["equity"].set(f"${equity:,.2f}")
        self._metric_cards["cash"].set(
            f"${cash:,.0f} ({cash_pct:.0f}%)" if equity > 0 else "—"
        )
        self._metric_cards["invested"].set(f"{invested:.1f}%")
        self._metric_cards["pnl"].set(
            f"${upl:+,.2f}", color=COLORS["green"] if upl >= 0 else COLORS["red"]
        )
        self._metric_cards["market"].set(_market_open_countdown(heartbeat))

        self._bot_badge.configure(
            text=f"Bot: {'Running' if running else 'Stopped'}",
            text_color=COLORS["green"] if running else COLORS["amber"],
        )

        self._fill_overview(heartbeat, equity, acct_err)
        self._fill_crypto_vol_panel()
        self._fill_positions(positions_df, pos_err, upl)
        self._fill_trades(journal_df)
        self._fill_wisdom(scorecard, scorecard_src, heartbeat)
        if ENABLE_SPARKLINE:
            self._draw_sparkline()

        if self._charts_var.get() or (self._active_tab == "Charts" and self._charts_dirty):
            self._draw_charts()
            self._charts_dirty = False

        mode = "LIVE" if not config.PAPER_TRADING else "Paper"
        ts = datetime.now().strftime("%H:%M:%S")
        self._status_label.configure(text=f"{mode} · {ts} · every {REFRESH_SECONDS}s")

    def _fill_crypto_vol_panel(self) -> None:
        hb = _load_json(_resolve_path(CRYPTO_VOL_HEARTBEAT_FILE))
        if hb is None:
            self._crypto_vol_body.configure(
                text="No heartbeat — set CRYPTO_VOL_SLEEVE_ENABLED=true in run_paper_bot."
            )
            return
        positions = hb.get("active_positions") or []
        cooldown = hb.get("cooldown_coins") or []
        last_sig = hb.get("last_signal_time")
        today_pnl = float(hb.get("today_pnl") or 0)
        pos_labels = ", ".join(p.get("symbol", "?") for p in positions) or "none"
        cd_labels = ", ".join(cooldown) or "none"
        sig_ts = str(last_sig or "—")[-19:] if last_sig else "—"
        extra = ""
        filters = hb.get("filters") or {}
        if filters.get("spy_gate"):
            extra = f" | SPY gate ({filters.get('spy_reason', '')})"
        elif filters.get("hour_ok") is False:
            extra = " | outside UTC entry hours"
        if hb.get("blocked"):
            extra = f" | blocked: {hb['blocked']}"
        pnl_color = COLORS["green"] if today_pnl >= 0 else COLORS["red"]
        self._crypto_vol_body.configure(
            text=(
                f"Positions ({len(positions)}): {pos_labels} | "
                f"Last signal: {sig_ts} | Cooldown: {cd_labels} | "
                f"Today PnL: ${today_pnl:+,.2f}{extra}"
            ),
            text_color=pnl_color if positions or today_pnl else COLORS["muted"],
        )

    def _fill_overview(
        self, heartbeat: dict | None, equity: float, acct_err: str | None
    ) -> None:
        if acct_err:
            self._update_status_row(
                equity,
                equity > 0 and config.is_small_account(equity),
                regime="Alpaca error",
                halted=True,
                bot_running_flag=bot_running(self._username, self._book_id),
            )
            self._pill_regime.configure(
                text=f"Alpaca: {acct_err[:40]}",
                text_color=COLORS["red"],
            )
            self._clear_frame(self._actions_frame)
            self._wisdom_line.configure(text="—")
            return
        if heartbeat is None:
            running = bot_running(self._username, self._book_id)
            self._update_status_row(
                equity,
                equity > 0 and config.is_small_account(equity),
                regime="Waiting…" if running else "No heartbeat",
                bot_running_flag=running,
            )
            self._clear_frame(self._actions_frame)
            btn_row = ctk.CTkFrame(self._actions_frame, fg_color="transparent")
            btn_row.pack(fill="x", pady=4)
            ctk.CTkButton(
                btn_row,
                text="Start Bot",
                width=100,
                fg_color="#166534",
                hover_color="#14532d",
                command=self._on_start_bot,
            ).pack(side="left", padx=(0, 8))
            ctk.CTkButton(
                btn_row,
                text="Refresh now",
                width=100,
                command=self.refresh_data,
            ).pack(side="left")
            if running:
                tail = read_bot_log_tail(self._username, self._book_id, max_chars=400)
                if tail:
                    last = tail.splitlines()[-1][:120]
                    ctk.CTkLabel(
                        self._actions_frame,
                        text=f"Log: {last}",
                        anchor="w",
                        text_color=COLORS["muted"],
                        font=ctk.CTkFont(size=11),
                        wraplength=780,
                    ).pack(fill="x", pady=(6, 0))
            self._wisdom_line.configure(text="—")
            return

        regime = heartbeat.get("regime", "—")
        halted = bool(heartbeat.get("halted"))
        self._update_status_row(
            equity,
            equity > 0 and config.is_small_account(equity),
            regime=str(regime),
            halted=halted,
            bot_running_flag=bot_running(self._username, self._book_id),
        )

        self._clear_frame(self._actions_frame)
        for action in _expected_actions(heartbeat):
            ctk.CTkLabel(
                self._actions_frame,
                text=f"• {action}",
                anchor="w",
                text_color=COLORS["text"],
                font=ctk.CTkFont(size=12),
            ).pack(fill="x", pady=1)

        wisdom = heartbeat.get("wisdom") or {}
        if wisdom:
            self._wisdom_line.configure(
                text=(
                    f"Mode {wisdom.get('mode', '—')}  ·  "
                    f"Gap {float(wisdom.get('gap') or 0):+.2f}  ·  "
                    f"Sizing ×{float(wisdom.get('sizing_multiplier') or 1):.2f}  ·  "
                    f"{'Paused' if wisdom.get('paused') else 'Active'}"
                )
            )
        else:
            self._wisdom_line.configure(text="—")

    def _position_rows(
        self, positions_df: pd.DataFrame
    ) -> list[dict]:
        rows = []
        for _, r in positions_df.iterrows():
            rows.append(
                {
                    "Ticker": r["Ticker"],
                    "Sleeve": r.get("Sleeve", ""),
                    "Qty": f"{r['Qty']:.4f}",
                    "Entry": f"${r['Entry']:,.2f}",
                    "Current": f"${r['Current']:,.2f}",
                    "P&L $": f"${r['P&L $']:+,.2f}",
                    "P&L %": f"{r['P&L %']:+.2f}%",
                    "_pnl": r["P&L $"],
                }
            )
        return rows

    def _fill_positions(
        self,
        positions_df: pd.DataFrame | None,
        pos_err: str | None,
        total_upl: float,
    ) -> None:
        if pos_err:
            self._top_positions_table.clear()
            self._positions_table.clear()
            self._pos_total.configure(text=pos_err, text_color=COLORS["red"])
            return
        if positions_df is None or positions_df.empty:
            self._top_positions_table.clear()
            self._positions_table.clear()
            self._pos_total.configure(
                text="No open positions — cash until next rebalance.",
                text_color=COLORS["muted"],
            )
            return
        rows = self._position_rows(positions_df)
        self._top_positions_table.set_rows(rows, pnl_col="_pnl")
        self._positions_table.set_rows(rows, pnl_col="_pnl")
        color = COLORS["green"] if total_upl >= 0 else COLORS["red"]
        self._pos_total.configure(
            text=f"{len(rows)} position(s) · unrealized P&L ${total_upl:+,.2f}",
            text_color=color,
        )

    def _fill_trades(self, journal_df: pd.DataFrame | None) -> None:
        if journal_df is None or journal_df.empty:
            self._top_trades_table.clear()
            self._trades_table.clear()
            src = book_journal_path(self._username, self._book_id)
            empty = (
                f"No trades yet · journal: {src.name}"
            )
            self._trades_hint.configure(text=empty)
            self._trades_tab_hint.configure(text=empty)
            return
        rows = []
        for _, row in journal_df.iterrows():
            item = row.to_dict()
            if hasattr(item.get("timestamp"), "strftime"):
                item["timestamp"] = item["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            notional = item.get("notional")
            if notional is not None and notional != "":
                try:
                    item["notional"] = f"${float(notional):,.2f}"
                except (TypeError, ValueError):
                    pass
            rows.append(item)

        self._trades_table.set_rows(rows)

        top_rows = []
        for item in rows[:5]:
            ts = str(item.get("timestamp", ""))
            if len(ts) > 16:
                ts = ts[5:16]
            top_rows.append(
                {
                    "Time": ts,
                    "Ticker": item.get("symbol", ""),
                    "Side": str(item.get("side", "")).upper(),
                    "Sleeve": item.get("sleeve", ""),
                    "Notional": item.get("notional", ""),
                }
            )
        self._top_trades_table.set_rows(top_rows)

        n_fill = sum(1 for r in rows if r.get("event") == "fill")
        n_sig = sum(1 for r in rows if r.get("event") == "signal")
        summary = f"{len(rows)} rows · {n_sig} signals · {n_fill} fills"
        self._trades_hint.configure(text=summary)
        self._trades_tab_hint.configure(text=summary)

    def _fill_wisdom(
        self,
        scorecard: dict | None,
        scorecard_src: str = "",
        heartbeat: dict | None = None,
    ) -> None:
        if scorecard is None:
            wisdom = (heartbeat or {}).get("wisdom") or {}
            if wisdom:
                gap = float(wisdom.get("gap") or 0)
                paused = bool(wisdom.get("paused"))
                self._w_sharpe.set(str(wisdom.get("mode", "—")))
                self._w_ret.set(
                    "Paused" if paused else "Active",
                    color=COLORS["red"] if paused else COLORS["green"],
                )
                self._w_vs.set(
                    f"{gap:+.2f}",
                    color=COLORS["green"] if gap >= 0 else COLORS["red"],
                )
                self._wisdom_rec.configure(
                    text=(
                        "Scorecard file not written yet — cards show live heartbeat wisdom "
                        f"(sizing ×{float(wisdom.get('sizing_multiplier') or 1):.2f}). "
                        "Full Sharpe/return metrics appear after the bot evaluates."
                    ),
                )
                self._wisdom_table.clear()
                self._wisdom_hint.configure(
                    text=f"No scorecard file yet for {book_label(self._book_id)}.",
                )
                return
            self._w_sharpe.set("—")
            self._w_ret.set("—")
            self._w_vs.set("—")
            self._wisdom_rec.configure(
                text=(
                    f"No scorecard for {book_label(self._book_id)}. "
                    "Start the bot — evaluation runs on cycle."
                ),
            )
            self._wisdom_table.clear()
            self._wisdom_hint.configure(text="")
            return
        live = scorecard.get("live") or {}
        sharpe = float(live.get("sharpe") or 0)
        ret = float(live.get("return_pct") or 0)
        vs_sim = scorecard.get("live_vs_active_sim_return_pp")
        if vs_sim is None:
            vs_sim = scorecard.get("live_vs_best_sim_return_pp")
        vs_val = float(vs_sim or 0)

        self._w_sharpe.set(f"{sharpe:.2f}", color=COLORS["green"] if sharpe >= 0 else COLORS["red"])
        self._w_ret.set(f"{ret:+.2f}%", color=COLORS["green"] if ret >= 0 else COLORS["red"])
        self._w_vs.set(
            f"{vs_val:+.2f} pp",
            color=COLORS["green"] if vs_val >= 0 else COLORS["red"],
        )
        rec = scorecard.get("recommendation") or ""
        ev = scorecard.get("evaluated_at", "—")
        self._wisdom_rec.configure(text=f"{rec}  (evaluated {ev})")
        src_note = (
            "per-book scorecard"
            if scorecard_src == "book"
            else "project scorecard (live segment)"
            if scorecard_src == "project"
            else ""
        )
        window = scorecard.get("window_days", "—")
        self._wisdom_hint.configure(
            text=f"{book_label(self._book_id)} · {window}-day window · {src_note}".strip(" ·"),
        )

        sim_modes = scorecard.get("simulated_modes") or {}
        rows = [
            {
                "Mode": mode,
                "Return%": f"{float(stats.get('return_pct') or 0):+.2f}",
                "Sharpe": f"{float(stats.get('sharpe') or 0):.2f}",
                "Orders": str(int(stats.get("orders") or 0)),
            }
            for mode, stats in sim_modes.items()
        ]
        self._wisdom_table.set_rows(rows)

    def _draw_sparkline(self) -> None:
        self._clear_frame(self._spark_frame)
        df = _load_equity_sparkline(self._username, self._book_id)
        if df is None or len(df) < 2:
            ctk.CTkLabel(
                self._spark_frame, text="—", text_color=COLORS["muted"], font=ctk.CTkFont(size=11)
            ).pack(expand=True)
            return
        start, end = float(df["equity"].iloc[0]), float(df["equity"].iloc[-1])
        color = COLORS["green"] if end >= start else COLORS["red"]
        fig = Figure(figsize=(2.4, 0.75), dpi=CHART_DPI, facecolor=COLORS["card"])
        ax = fig.add_subplot(111)
        ax.set_facecolor(COLORS["card"])
        y = df["equity"].values
        ax.plot(range(len(y)), y, color=color, linewidth=1.6)
        ax.fill_between(range(len(y)), y, y.min() * 0.999, color=color, alpha=0.15)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        fig.subplots_adjust(0, 0, 1, 1)
        canvas = FigureCanvasTkAgg(fig, master=self._spark_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        plt.close(fig)
        self._spark_canvas = canvas

    def _draw_charts(self) -> None:
        self._clear_frame(self._charts_frame)
        self._price_canvases.clear()
        row = ctk.CTkFrame(self._charts_frame, fg_color="transparent")
        row.pack(fill="both", expand=True)
        specs: list[tuple[str, str]] = [
            (config.VTI_CORE_SYMBOL, COLORS["blue"]),
            ("SPY", COLORS["amber"]),
        ]
        if self._last_equity > 0 and config.is_small_account(self._last_equity):
            specs.append(("GLD", "#fbbf24"))
        for idx, (symbol, color) in enumerate(specs):
            df = _load_daily_closes(symbol, CHART_DAYS)
            cell = ctk.CTkFrame(
                row,
                fg_color=COLORS["card"],
                corner_radius=12,
                border_width=1,
                border_color=COLORS["border"],
            )
            cell.grid(row=0, column=idx, padx=4, sticky="nsew")
            row.grid_columnconfigure(idx, weight=1)
            if df is None or df.empty:
                ctk.CTkLabel(
                    cell,
                    text=f"No data for {symbol}",
                    text_color=COLORS["muted"],
                ).pack(pady=30)
                continue
            if len(df) > 40:
                df = df.iloc[:: max(1, len(df) // 40)]
            fig = _light_line_chart(df, title=symbol, color=color, show_axis=True)
            canvas = FigureCanvasTkAgg(fig, master=cell)
            canvas.draw()
            canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)
            plt.close(fig)
            self._price_canvases.append(canvas)

    def _maybe_auto_start_bot(self, *, quiet: bool = False) -> None:
        if not has_alpaca_config(self._username, self._book_id):
            return
        if bot_running(self._username, self._book_id):
            self.refresh_data()
            return
        ok, msg = start_bot(self._username, self._book_id)
        if ok:
            self._bot_badge.configure(text="Bot: starting…", text_color=COLORS["amber"])
            self.after(8000, self.refresh_data)
        elif not quiet:
            tail = read_bot_log_tail(self._username, self._book_id)
            detail = f"\n\n{tail}" if tail else ""
            messagebox.showerror("Start Bot", msg + detail)
        else:
            self._regime_label.configure(
                text=f"Auto-start failed: {msg}",
                text_color=COLORS["red"],
            )
        self.refresh_data()

    def _on_start_bot(self) -> None:
        if not has_alpaca_config(self._username, self._book_id):
            messagebox.showwarning(
                "API keys",
                f"Add API keys for {book_label(self._book_id)} first (☰ menu).",
            )
            return
        if bot_running(self._username, self._book_id):
            messagebox.showinfo("Start Bot", f"Bot is already running for {book_label(self._book_id)}.")
            self.refresh_data()
            return
        ok, msg = start_bot(self._username, self._book_id)
        if ok:
            self._bot_badge.configure(text="Bot: starting…", text_color=COLORS["amber"])
            self.after(8000, self.refresh_data)
        else:
            tail = read_bot_log_tail(self._username, self._book_id)
            detail = f"\n\n{tail}" if tail else ""
            messagebox.showerror("Start Bot", msg + detail)
        self.refresh_data()

    def _on_stop_bot(self) -> None:
        if not bot_running(self._username, self._book_id):
            messagebox.showinfo("Stop Bot", f"No bot running for {book_label(self._book_id)}.")
            return
        if not messagebox.askyesno(
            "Stop Bot",
            f"Stop the bot for {book_label(self._book_id)}?\n\n"
            "Ends the trading loop; does not close positions.",
        ):
            return
        ok, msg = stop_bot(self._username, self._book_id)
        if ok:
            messagebox.showinfo("Stop Bot", msg)
        else:
            messagebox.showwarning("Stop Bot", msg)
        self.refresh_data()

    def _schedule_refresh(self) -> None:
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(REFRESH_SECONDS * 1000, self._auto_refresh_tick)

    def _auto_refresh_tick(self) -> None:
        self.refresh_data()
        self._schedule_refresh()

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _start_tray(self) -> None:
        if not TRAY_AVAILABLE or self._tray_icon is not None:
            return

        def on_show(_icon, _item) -> None:
            self.after(0, self._show_window)

        def on_quit(_icon, _item) -> None:
            self.after(0, self._shutdown)

        menu = pystray.Menu(
            pystray.MenuItem("Show dashboard", on_show, default=True),
            pystray.MenuItem("Quit", on_quit),
        )
        self._tray_icon = pystray.Icon(
            "pythontrading",
            _tray_image(),
            "PythonTrading Monitor",
            menu,
        )
        self._tray_icon.run_detached()

    def _stop_tray(self) -> None:
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        if self._clock_job:
            self.after_cancel(self._clock_job)
        self._stop_tray()
        self.destroy()

    def _on_close(self) -> None:
        if self._tray_var.get() and TRAY_AVAILABLE and not self._shutting_down:
            self.withdraw()
            self._start_tray()
            return
        self._shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="PythonTrading desktop monitor")
    parser.add_argument(
        "--launch-bot",
        action="store_true",
        help="Start run_all.py after login (uses your portal account's keys)",
    )
    args = parser.parse_args()
    launch_bot_after_login = args.launch_bot

    def open_dashboard(username: str) -> None:
        migrate_user_to_books(username)
        book_id = get_last_book_id()
        if launch_bot_after_login and has_alpaca_config(username, book_id):
            ok, msg = start_bot(username, book_id)
            if not ok:
                print(msg)
        app = TradingDashboardApp(
            username,
            book_id,
            on_logout=show_login,
            auto_start_bot=launch_bot_after_login,
        )
        app.mainloop()

    def show_login() -> None:
        login = LoginApp(on_success=open_dashboard)
        login.mainloop()

    show_login()


if __name__ == "__main__":
    main()
