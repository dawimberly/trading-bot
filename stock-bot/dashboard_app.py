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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk


def _app_root() -> Path:
    """Project/dist root — stock-bot source tree or dist/ beside Weinstein-Trading-Bot.exe."""
    from modules.runtime_paths import resolve_runtime_root

    return resolve_runtime_root()


PROJECT_ROOT = _app_root()
os.environ["PYTHONTRADING_ROOT"] = str(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from modules.ssl_certs import configure_ssl_certificates

    configure_ssl_certificates()
except ImportError:
    pass

from modules.runtime_paths import (  # noqa: E402
    BOT_EXE_NAME,
    live_bot_pids,
    resolve_bot_executable,
    resolve_bot_workdir,
    resolve_data_root,
    resolve_runtime_root,
    runtime_layout_label,
    runtime_log_dir,
)
from modules.portal_paths import (  # noqa: E402
    bind_project_root,
    book_heartbeat_path,
    book_journal_path,
    book_scorecard_path,
    ensure_book_journal,
    ensure_book_env,
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
from modules import status_metrics as sm  # noqa: E402
from modules.trading_books import (  # noqa: E402
    BOOKS,
    PRIMARY_PAPER_BOOK_ID,
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
from modules.csv_utils import coerce_trade_journal_df, read_csv_file  # noqa: E402
from modules.alpaca_client import build_trading_client, reset_trading_client_cache  # noqa: E402
from modules.alpaca_executor import AlpacaExecutor  # noqa: E402
from modules.portal_auth import authenticate, init_db, register_user  # noqa: E402
from modules.portal_bot import (  # noqa: E402
    bot_running,
    bot_status_label,
    read_bot_log_tail,
    refresh_bot,
    restart_bot,
    start_bot,
    stop_bot,
)

REFRESH_SECONDS = 45  # equity session open
REFRESH_SECONDS_CLOSED = 300  # overnight / session closed (5 min)
_BOOK_ENV_LOCK = threading.Lock()
_POSITIONS_CACHE_LOCK = threading.Lock()
# key -> {"at": monotonic, "df": DataFrame|None, "err": str|None}
_POSITIONS_CACHE: dict[str, dict] = {}
CRYPTO_VOL_HEARTBEAT_FILE = "crypto_vol_heartbeat.json"
POSITIONS_REFRESH_SEC = max(
    5, int(getattr(config, "DASHBOARD_POSITIONS_REFRESH_SEC", 12) or 12)
)
TRADES_LIMIT = 50
TRADE_EVENTS = frozenset({"signal", "exit", "fill"})
EQUITY_EVENTS = frozenset({"cycle", "startup"})
CHART_DAYS = 21
CHART_DPI = 72
SPARKLINE_POINTS = 48
# Quote-style ranges: short windows prefer 5m live tables; 1M uses daily.
CHART_RANGE_KEYS = ("1D", "5D", "1M")
CHART_RANGE_SPECS: dict[str, dict] = {
    "1D": {"prefer": "5m", "calendar_days": 1, "daily_bars": 2, "max_points": 96},
    "5D": {"prefer": "5m", "calendar_days": 5, "daily_bars": 5, "max_points": 120},
    "1M": {"prefer": "1d", "calendar_days": 32, "daily_bars": 22, "max_points": 48},
}
ENABLE_SPARKLINE = True  # draw from worker-preloaded data only (never journal on UI)
DASHBOARD_UI_TAG = "2026-08-31-today-pnl"

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


def _book_is_paper(book_id: str) -> bool:
    from modules.trading_books import default_env_prefs

    return bool(default_env_prefs(book_id).get("paper", True))


def _other_book_id(book_id: str) -> str:
    return "alpaca_paper" if book_id == "alpaca_live" else "alpaca_live"


def _heartbeat_matches_book(hb: dict | None, book_id: str) -> bool:
    if not hb:
        return False
    if hb.get("book_id"):
        return str(hb["book_id"]) == book_id
    if "paper" in hb:
        return bool(hb["paper"]) == _book_is_paper(book_id)
    return True


def _book_running_status(username: str, book_id: str) -> bool:
    """PID file when present; else infer from a fresh per-book heartbeat."""
    if bot_running(username, book_id):
        return True
    hb, _ = _load_active_heartbeat(username, book_id)
    if not hb or not _heartbeat_matches_book(hb, book_id):
        return False
    age = _heartbeat_age_minutes(hb)
    if age is None:
        return False
    return age < 120


def _et_equity_session_open_guess() -> bool:
    """Cheap local clock fallback (weekdays 09:30–16:00 ET) when heartbeat lacks flags."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def _equity_session_open_from_heartbeat(heartbeat: dict | None) -> bool | None:
    """Return True/False from heartbeat, or None if unknown."""
    if not heartbeat:
        return None
    if "equity_session_open" in heartbeat:
        return bool(heartbeat.get("equity_session_open"))
    scan = heartbeat.get("scan_schedule") or {}
    if "market_open" in scan:
        return bool(scan.get("market_open"))
    return None


def _format_book_status_block(
    username: str,
    book_id: str,
    *,
    running: bool | None = None,
    heartbeat: dict | None = None,
    hb_path: Path | None = None,
) -> list[str]:
    """Compact status lines for one Alpaca book (live or paper)."""
    lbl = book_label(book_id)
    mode = "paper" if _book_is_paper(book_id) else "live"
    if running is None:
        running = _book_running_status(username, book_id)
    if heartbeat is None or hb_path is None:
        heartbeat, hb_path = _load_active_heartbeat(username, book_id)
    run_txt = "running" if running else "stopped"
    age = _heartbeat_age_minutes(heartbeat)
    age_txt = f"{age:.0f} min ago" if age is not None else "no timestamp"
    stale = _heartbeat_is_stale(heartbeat, running=running)
    regime = (heartbeat or {}).get("regime") or "—"
    phase = _scan_phase_label(heartbeat)
    eq = float((heartbeat or {}).get("equity") or 0)
    eq_s = f"${eq:,.2f}" if eq > 0 else "n/a"
    try:
        hb_rel = os.path.relpath(str(hb_path), resolve_data_root(PROJECT_ROOT))
    except ValueError:
        hb_rel = str(hb_path)
    line1 = f"{lbl} ({mode}) · bot {run_txt} · equity {eq_s}"
    line2 = f"  regime={regime}"
    if phase:
        line2 += f" · {phase}"
    line2 += f" · hb {age_txt}"
    if stale:
        line2 += " · STALE"
    line2 += f" · {hb_rel}"
    return [line1, line2]


def _active_heartbeat_path(username: str, book_id: str) -> Path:
    """Per-book heartbeat only — never fall back to dist/ global files."""
    return book_heartbeat_path(username, book_id)


def _load_active_heartbeat(username: str, book_id: str) -> tuple[dict | None, Path]:
    path = _active_heartbeat_path(username, book_id)
    if not path.is_file():
        return None, path
    hb = _load_json(path)
    if not hb:
        return None, path
    if not _heartbeat_matches_book(hb, book_id):
        return None, path
    return hb, path


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _process_rss_mb() -> str | None:
    try:
        import psutil

        rss = psutil.Process().memory_info().rss / (1024 * 1024)
        return f"{rss:.0f} MB"
    except Exception:
        return None


def _heartbeat_age_minutes(heartbeat: dict | None) -> float | None:
    if not heartbeat or not heartbeat.get("timestamp"):
        return None
    try:
        ts = pd.Timestamp(heartbeat["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize(None)
        age = (pd.Timestamp.now() - ts).total_seconds() / 60.0
        return max(0.0, age)
    except Exception:
        return None


def _heartbeat_is_stale(
    heartbeat: dict | None,
    *,
    running: bool,
    max_age_min: float = 90,
) -> bool:
    """Match status.py grace periods for starting / warming up."""
    age = _heartbeat_age_minutes(heartbeat)
    if age is None:
        return False
    if heartbeat and heartbeat.get("status") == "starting" and age < 10:
        return False
    if running and age < 5:
        return False
    return age > max_age_min


def _heartbeat_on_disk_mismatch(username: str, book_id: str) -> bool:
    """True when on-disk heartbeat exists but tags a different book."""
    path = _active_heartbeat_path(username, book_id)
    if not path.is_file():
        return False
    hb = _load_json(path)
    if not hb:
        return False
    return not _heartbeat_matches_book(hb, book_id)


def _scan_phase_label(heartbeat: dict | None) -> str:
    scan = (heartbeat or {}).get("scan_schedule") or {}
    return str(scan.get("phase") or scan.get("label") or "").strip()


_SSL_MARK = "quotes SSL failed (using last mark)"
_VANGUARD_LEFTOVER = frozenset({"VTI", "VOO", "VEA", "VWO", "VXUS"})
_METAL_LEFTOVER = frozenset({"IAU", "GLD", "SLV", "CPER"})


def _compact_io_error(exc: BaseException | str) -> str:
    msg = str(exc or "")
    low = msg.lower()
    if any(tok in low for tok in ("ssl", "certificate_verify_failed", "certifi", "certificate verify")):
        try:
            from modules.ssl_certs import configure_ssl_certificates

            configure_ssl_certificates(force=True)
        except Exception:
            pass
        return _SSL_MARK
    first = msg.splitlines()[0].strip() if msg else "error"
    return first[:160]


def _infer_sleeve(symbol: str) -> str:
    sym = config.normalize_symbol(symbol or "")
    if not sym:
        return ""
    if config.is_crypto(sym):
        return "Crypto leftover"
    if sym in _VANGUARD_LEFTOVER:
        return "Vanguard leftover"
    if sym in _METAL_LEFTOVER or (hasattr(config, "is_metal_symbol") and config.is_metal_symbol(sym) and sym in {"GLD", "SLV", "CPER", "IAU"}):
        return "Metal leftover"
    if sym == getattr(config, "SPY_BOT_SYMBOL", "SPY") or sym in {"SPY", "QQQ"}:
        return "SPY leftover"
    return "NYSE"


def _path_for_resolve(path: Path) -> str:
    try:
        return os.path.relpath(path, PROJECT_ROOT)
    except ValueError:
        return str(path)


def _apply_user_paths(username: str, book_id: str) -> None:
    """Load book API keys and point dashboard at isolated bot files."""
    migrate_user_to_books(username)
    env_file = ensure_book_env(username, book_id)
    paper = _book_is_paper(book_id)
    config.reload_from_env(str(env_file), book_scoped=True)
    # Per-book flags must win over any stale global state.
    os.environ["PYTHONTRADING_ENV_FILE"] = str(env_file.resolve())
    os.environ["PAPER_TRADING"] = "true" if paper else "false"
    os.environ["ALLOW_LIVE_TRADING"] = "yes" if not paper else "no"
    config.PAPER_TRADING = paper
    config.ALLOW_LIVE_TRADING = not paper
    bd = env_file.parent
    hb = book_heartbeat_path(username, book_id)
    journal = ensure_book_journal(username, book_id)
    config.HEARTBEAT_FILE = str(hb.resolve())
    config.PAPER_JOURNAL_CSV = _path_for_resolve(journal)
    config.WISDOM_SCORECARD_FILE = _path_for_resolve(bd / "wisdom_scorecard.json")
    config.WISDOM_JOURNAL_FILE = _path_for_resolve(bd / "wisdom_journal.csv")
    os.environ["HEARTBEAT_FILE"] = str(hb.resolve())
    os.environ["PAPER_JOURNAL_CSV"] = str(journal.resolve())
    reset_trading_client_cache()


def _env_val(values: dict, *names: str) -> str:
    for name in names:
        raw = values.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _read_book_credentials(username: str, book_id: str) -> tuple[bool, str, str]:
    """Read Alpaca keys from the book .env file (immune to root .env overrides)."""
    from dotenv import dotenv_values

    env_file = ensure_book_env(username, book_id)
    vals = dotenv_values(env_file)
    paper = _book_is_paper(book_id)
    if paper:
        key = _env_val(vals, "PAPER_APCA_API_KEY_ID", "APCA_API_KEY_ID", "ALPACA_API_KEY")
        secret = _env_val(
            vals, "PAPER_APCA_API_SECRET_KEY", "APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY"
        )
    else:
        key = _env_val(vals, "APCA_API_KEY_ID", "ALPACA_API_KEY")
        secret = _env_val(vals, "APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY")
    if not key or not secret:
        book = book_label(book_id)
        raise ValueError(f"Alpaca credentials missing for {book} ({env_file.name})")
    return paper, key, secret


def _book_trading_client(username: str, book_id: str):
    """Alpaca client for book_id — explicit paper flag and book .env keys only."""
    paper, api_key, secret_key = _read_book_credentials(username, book_id)
    base_url = config.get_alpaca_base_url(paper=paper)
    return build_trading_client(api_key, secret_key, paper=paper, base_url=base_url)


def _make_book_executor(username: str, book_id: str) -> AlpacaExecutor:
    """Executor scoped to one book's .env keys and paper/live endpoint."""
    with _BOOK_ENV_LOCK:
        _apply_user_paths(username, book_id)
        paper, api_key, secret_key = _read_book_credentials(username, book_id)
    return AlpacaExecutor(
        paper=paper,
        credentials_fn=lambda: (api_key, secret_key),
        allow_live=not paper,
    )


def _needs_setup(username: str, book_id: str) -> bool:
    if not book_enabled(book_id):
        return False
    if not has_alpaca_config(username, book_id):
        return True
    try:
        _read_book_credentials(username, book_id)
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


def _positions_cache_key(username: str, book_id: str) -> str:
    return f"{username}\0{book_id}"


def _positions_cache_peek(
    username: str, book_id: str
) -> tuple[float | None, pd.DataFrame | None, str | None]:
    with _POSITIONS_CACHE_LOCK:
        ent = _POSITIONS_CACHE.get(_positions_cache_key(username, book_id))
        if not ent:
            return None, None, None
        df = ent.get("df")
        return (
            float(ent["at"]),
            df.copy() if df is not None else None,
            ent.get("err"),
        )


def _positions_cache_put(
    username: str,
    book_id: str,
    df: pd.DataFrame | None,
    err: str | None,
) -> None:
    with _POSITIONS_CACHE_LOCK:
        _POSITIONS_CACHE[_positions_cache_key(username, book_id)] = {
            "at": time.monotonic(),
            "df": df.copy() if df is not None else None,
            "err": err,
        }


def _positions_cache_age_sec(username: str, book_id: str) -> float | None:
    with _POSITIONS_CACHE_LOCK:
        ent = _POSITIONS_CACHE.get(_positions_cache_key(username, book_id))
        if not ent:
            return None
        return time.monotonic() - float(ent["at"])


def _positions_cache_fresh(
    username: str, book_id: str, *, stale_threshold: float | None = None
) -> bool:
    threshold = (
        POSITIONS_REFRESH_SEC if stale_threshold is None else float(stale_threshold)
    )
    age = _positions_cache_age_sec(username, book_id)
    return age is not None and age < threshold


def _positions_fingerprint(
    df: pd.DataFrame | None, err: str | None = None
) -> object:
    """Stable fingerprint so unchanged positions skip table rebuild."""
    if err:
        return ("err", str(err))
    if df is None:
        return None
    if getattr(df, "empty", True):
        return ("empty",)
    rows: list[tuple] = []
    has_atr = "ATR Stop" in df.columns
    for _, r in df.iterrows():
        try:
            qty = round(float(r.get("Qty") or 0), 6)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            entry = round(float(r.get("Entry") or 0), 4)
        except (TypeError, ValueError):
            entry = 0.0
        try:
            cost = round(float(r.get("Cost $") or 0), 4)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            pnl = round(float(r.get("P&L $") or 0), 4)
        except (TypeError, ValueError):
            pnl = 0.0
        try:
            total = round(float(r.get("Total $") or 0), 4)
        except (TypeError, ValueError):
            total = 0.0
        try:
            value = round(float(r.get("Value $") or 0), 4)
        except (TypeError, ValueError):
            value = 0.0
        rows.append(
            (
                str(r.get("Ticker") or ""),
                qty,
                entry,
                cost,
                pnl,
                total,
                value,
                str(r.get("First fill") or r.get("Opened") or ""),
                str(r.get("ATR Stop") or "") if has_atr else "",
                DASHBOARD_UI_TAG,
            )
        )
    return tuple(rows)


def _fetch_account_summary(
    *, username: str, book_id: str, retries: int = 2
) -> tuple[float | None, float | None, str | None, float | None]:
    """Fresh Alpaca account read for the selected book.

    Returns (equity, cash, err, last_equity). last_equity is prior-session close
    when Alpaca provides it — used as a Today P&L fallback before the bot
    records a day-open anchor.
    """
    last_err: str | None = None
    last_equity: float | None = None
    for attempt in range(max(1, retries)):
        try:
            client = _book_trading_client(username, book_id)
            acct = client.get_account()
            equity = float(acct.equity)
            cash = float(acct.cash)
            raw_last = getattr(acct, "last_equity", None)
            if raw_last is not None:
                try:
                    last_eq = float(raw_last)
                    if last_eq > 0:
                        last_equity = last_eq
                except (TypeError, ValueError):
                    pass
            if equity > 0:
                return equity, cash, None, last_equity
            last_err = "Account equity is zero"
        except ValueError as exc:
            last_err = _compact_io_error(exc)
        except Exception as exc:  # noqa: BLE001
            last_err = _compact_io_error(exc)
        if attempt + 1 < retries:
            time.sleep(0.35)
    return None, None, last_err, last_equity


def _resolve_equity_cash(
    acct_eq: float | None,
    acct_cash: float | None,
    acct_err: str | None,
    heartbeat: dict | None,
    *,
    username: str,
    book_id: str,
) -> tuple[float, float, str | None]:
    """Prefer live Alpaca equity; ignore stale heartbeat when API keys are configured."""
    hb_eq = float((heartbeat or {}).get("equity") or 0)
    hb_cash = float((heartbeat or {}).get("cash") or 0)

    if acct_eq is not None and acct_eq > 0:
        cash = acct_cash if acct_cash is not None else hb_cash
        return acct_eq, cash, acct_err

    paper = _book_is_paper(book_id)
    try:
        _read_book_credentials(username, book_id)
        has_keys = True
    except ValueError:
        has_keys = False

    if has_keys:
        # Keys present but API failed — do not show old bot heartbeat (often $100 backtest).
        return 0.0, hb_cash, acct_err or "Could not fetch live account equity"

    if hb_eq > 0:
        return hb_eq, hb_cash, acct_err

    return 0.0, hb_cash, acct_err


def _format_position_opened(opened: datetime | None) -> str:
    if opened is None:
        return "—"
    try:
        if opened.tzinfo is not None:
            opened = opened.astimezone(timezone.utc).replace(tzinfo=None)
        return opened.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(opened)[:16]


def _position_opened_at(pos, client, journal_df, sym: str) -> datetime | None:
    """Best-effort open time: Alpaca position created_at, then journal, then order history."""
    created = getattr(pos, "created_at", None) or getattr(pos, "createdAt", None)
    if created is not None:
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                created = None
        if isinstance(created, datetime):
            return created.replace(tzinfo=None) if created.tzinfo else created

    try:
        from modules.paper_journal import _first_alpaca_buy_time, journal_opened_at

        opened = journal_opened_at(journal_df, sym) if journal_df is not None else None
        if opened is None:
            opened = _first_alpaca_buy_time(client, sym)
        if opened is not None and isinstance(opened, datetime):
            return opened.replace(tzinfo=None) if opened.tzinfo else opened
    except Exception:
        pass
    return None


def _day_pnl_from_session_open(
    equity: float,
    heartbeat: dict | None = None,
    *,
    paper: bool,
    last_equity: float | None = None,
    book_id: str | None = None,
    username: str | None = None,
) -> tuple[float | None, float | None]:
    """Open-to-now (or close) account P&L vs the book's session-open equity.

    Prefer the daily-loss anchor (both paper and live). Fall back to daily-bank
    open (paper) then Alpaca last_equity (prior close) if the bot has not
    recorded today's open yet.
    """
    if equity is None or float(equity) <= 0:
        return None, None
    if paper and username and book_id:
        try:
            from modules.trading_safety import prime_paper_day_open_from_book_env

            prime_paper_day_open_from_book_env(
                book_id=book_id,
                equity=float(equity),
                paper=True,
                env_file=ensure_book_env(username, book_id),
            )
        except Exception:  # noqa: BLE001
            pass
    open_eq: float | None = None
    try:
        from modules.trading_safety import get_daily_loss_status

        dl = get_daily_loss_status(
            paper=paper, current_equity=float(equity), book_id=book_id
        )
        raw = dl.get("open_equity")
        if raw is not None and float(raw) > 0:
            open_eq = float(raw)
    except Exception:  # noqa: BLE001
        pass
    if open_eq is None:
        bank = (heartbeat or {}).get("daily_bank") or {}
        raw = bank.get("open_equity")
        try:
            if raw is not None and float(raw) > 0:
                open_eq = float(raw)
        except (TypeError, ValueError):
            pass
    if open_eq is None and last_equity is not None:
        try:
            if float(last_equity) > 0:
                # Ignore prior-close fallback when it disagrees with live equity (paper reset).
                if abs(float(last_equity) - float(equity)) / float(equity) > 0.02:
                    pass
                else:
                    open_eq = float(last_equity)
        except (TypeError, ValueError):
            pass
    if open_eq is None or open_eq <= 0:
        return None, None
    pnl = float(equity) - open_eq
    pct = 100.0 * (float(equity) / open_eq - 1.0)
    return pnl, pct


def _format_day_pnl(pnl: float, pct: float) -> str:
    """Keep live-scale cents; round large paper moves to dollars."""
    if abs(pnl) >= 1000:
        return f"${pnl:+,.0f} ({pct:+.2f}%)"
    return f"${pnl:+,.2f} ({pct:+.2f}%)"


def _open_pnl_from_heartbeat(heartbeat: dict | None) -> tuple[float, float, str]:
    """Fallback Open P&L from heartbeat sleeve marks when positions aren't loaded yet."""
    if not heartbeat:
        return 0.0, 0.0, ""
    try:
        sleeves = heartbeat.get("sleeve_pnl") or {}
        upl = 0.0
        for v in sleeves.values():
            if isinstance(v, dict):
                upl += float(v.get("unrealized_pnl") or 0)
            else:
                try:
                    upl += float(v or 0)
                except (TypeError, ValueError):
                    continue
        equity = float(heartbeat.get("equity") or 0)
        cash = float(heartbeat.get("cash") or 0)
        invested = max(0.0, equity - cash) if equity > 0 else 0.0
        cost = max(invested - upl, 1e-6) if invested > 0 else 0.0
        pct = 100.0 * upl / cost if cost > 1e-6 else 0.0
        if abs(upl) < 1e-9:
            return 0.0, 0.0, ""
        return upl, pct, "from heartbeat"
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, ""


def _open_pnl_from_positions(positions_df: pd.DataFrame | None) -> tuple[float, float, str]:
    """Portfolio open P&L $ and % weighted by position size (biggest holdings dominate %).

    Returns (upl_usd, upl_pct, top_note). pct uses cost basis (value - pnl) so it
    matches how Alpaca reports each row's P&L % — not a disconnected day/form metric.
    """
    if positions_df is None or getattr(positions_df, "empty", True):
        return 0.0, 0.0, ""
    try:
        work = positions_df.copy()
        if "Value $" not in work.columns or "P&L $" not in work.columns:
            return 0.0, 0.0, ""
        work["_value"] = pd.to_numeric(work["Value $"], errors="coerce").fillna(0.0)
        work["_pnl"] = pd.to_numeric(work["P&L $"], errors="coerce").fillna(0.0)
        if "P&L %" in work.columns:
            work["_pct"] = pd.to_numeric(work["P&L %"], errors="coerce").fillna(0.0)
        else:
            work["_pct"] = 0.0
        upl = float(work["_pnl"].sum())
        # Cost basis ≈ market value − unrealized (longs); keep abs for shorts.
        cost = float((work["_value"] - work["_pnl"]).abs().sum())
        if cost > 1e-6:
            pct = 100.0 * upl / cost
        else:
            total_v = float(work["_value"].abs().sum())
            pct = 100.0 * upl / total_v if total_v > 1e-6 else 0.0
        # Largest holding by |value| — show its % so the hero reads correlated.
        top = work.reindex(work["_value"].abs().sort_values(ascending=False).index)
        top_note = ""
        if not top.empty:
            row = top.iloc[0]
            ticker = str(row.get("Ticker") or "?")
            top_pct = float(row["_pct"])
            top_note = f"{ticker} {top_pct:+.2f}%"
        return upl, pct, top_note
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, ""


def _fetch_positions(
    username: str,
    book_id: str,
    *,
    detail: bool = True,
) -> tuple[pd.DataFrame | None, str | None]:
    """Fetch Alpaca positions. detail=False skips slow journal First fill + ATR columns."""
    try:
        client = _book_trading_client(username, book_id)
        positions = client.get_all_positions()
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, _compact_io_error(exc)

    cols = ["Ticker", "Sleeve", "First fill", "Qty", "Entry", "Cost $", "Value $", "P&L $", "P&L %"]
    if detail and config.effective_atr_sizing_enabled() and _book_is_paper(book_id):
        cols.append("ATR Stop")
    if not positions:
        return pd.DataFrame(columns=cols), None

    journal_df = None
    if detail:
        try:
            from modules.paper_journal import read_journal

            journal_df = read_journal(path=book_journal_path(username, book_id))
        except Exception:
            journal_df = None

    rows = []
    for pos in positions:
        sym = config.normalize_symbol(pos.symbol)
        try:
            qty = float(pos.qty or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            entry = float(getattr(pos, "avg_entry_price", 0) or 0)
        except (TypeError, ValueError):
            entry = 0.0
        current_raw = getattr(pos, "current_price", None)
        try:
            current = float(current_raw) if current_raw is not None else 0.0
        except (TypeError, ValueError):
            current = 0.0
        if current != current:  # NaN
            current = 0.0
        try:
            market_value = float(getattr(pos, "market_value", 0) or 0)
        except (TypeError, ValueError):
            market_value = 0.0
        if not market_value and qty and current:
            market_value = qty * current
        try:
            cost_basis = float(getattr(pos, "cost_basis", 0) or 0)
        except (TypeError, ValueError):
            cost_basis = 0.0
        if not cost_basis and qty and entry:
            cost_basis = abs(qty) * entry
        # Prefer Alpaca avg entry; fall back to cost/qty so Buy $ never blanks when Cost $ is known.
        if (not entry or entry != entry) and qty and cost_basis:
            try:
                entry = abs(cost_basis) / abs(qty)
            except ZeroDivisionError:
                pass
        opened = None
        if detail:
            opened = _position_opened_at(pos, client, journal_df, sym)
        rows.append(
            {
                "Ticker": sym,
                "Sleeve": _infer_sleeve(sym),
                "First fill": _format_position_opened(opened) if detail else "—",
                "_opened": opened,
                "Qty": qty,
                "Entry": entry,
                "Cost $": cost_basis,
                "Value $": market_value,
                "Current": current,
                "P&L $": float(getattr(pos, "unrealized_pl", 0) or 0),
                "P&L %": float(getattr(pos, "unrealized_plpc", 0) or 0) * 100,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Value $", ascending=False)
    if detail and "ATR Stop" in cols and not df.empty:
        try:
            from modules.pipeline_strategies import load_pipeline_data
            from modules.risk_management import atr_stop_price

            atr_data = load_pipeline_data()
            stops: list[str] = []
            for _, row in df.iterrows():
                sym = str(row["Ticker"])
                qty = float(row.get("Qty") or 0)
                side = "short" if qty < 0 else "long"
                stop_px = atr_stop_price(atr_data, sym, side=side)
                stops.append(f"${stop_px:,.2f}" if stop_px is not None else "—")
            df["ATR Stop"] = stops
        except Exception:
            df["ATR Stop"] = "—"
    return df, None


def _fetch_insider_signals_snapshot() -> tuple[list[dict], str | None]:
    """Top insider signals for dashboard (paper monitor)."""
    try:
        if not config.effective_insider_monitor_enabled():
            return [], "Insider monitor off (paper / research only)"
        from modules.insider_monitor import _sig_type, get_recent_insider_signals

        rows: list[dict] = []
        for sig in get_recent_insider_signals(days=7, min_score=60)[:5]:
            st = _sig_type(sig)
            if st == "executive_sell":
                display_type = "exec_sell"
            elif st == "insider_sell":
                display_type = "insider_sell"
            else:
                display_type = st
            ticker = sig.get("ticker") or sig.get("company") or "?"
            desc = str(sig.get("description") or "").replace("\n", " ")
            if len(desc) > 56:
                desc = desc[:53] + "..."
            fdate = str(sig.get("filing_date") or "")[:10] or "—"
            score = int(sig.get("score") or 0)
            row_tag = ""
            if display_type == "cluster_buy":
                row_tag = "cluster_buy"
            elif display_type in ("exec_sell", "insider_sell"):
                row_tag = "exec_sell"
            rows.append(
                {
                    "Ticker": ticker,
                    "Type": display_type,
                    "Score": score,
                    "Description": desc,
                    "Filing Date": fdate,
                    "_score": score,
                    "_tag": row_tag,
                }
            )
        rows.sort(key=lambda r: int(r.get("_score") or 0), reverse=True)
        if not rows:
            return [], None
        return rows, None
    except Exception as exc:
        return [], str(exc)


def _fetch_short_activity_snapshot(
    *,
    positions_df,
    journal_df,
    heartbeat: dict | None,
    book_paper: bool,
) -> tuple[list[dict], str | None, dict | None]:
    """Short sleeve activity for paper book dashboard panel."""
    if not book_paper:
        return [], "Short activity — paper book only", None
    try:
        if not config.effective_opportunistic_short_enabled():
            return [], "Protective shorts off", None
        from modules.short_activity import (
            gather_short_activity,
            short_activity_dashboard_rows,
        )

        regime = str((heartbeat or {}).get("regime") or "")
        snap = gather_short_activity(
            positions_df=positions_df,
            journal_df=journal_df,
            regime=regime,
        )
        rows, err = short_activity_dashboard_rows(snap)
        return rows, err, snap
    except Exception as exc:
        return [], str(exc), None


def _fetch_rvol_snapshot() -> tuple[list[dict], str | None]:
    """Top RVOL / ORB setups for dashboard (paper scanner)."""
    try:
        if not config.effective_rvol_scanner_enabled() and not config.effective_orb_enabled() and not config.effective_catalyst_scoring_enabled():
            return [], "RVOL/ORB scanner off (paper / research only)"
        from modules.pipeline_strategies import load_pipeline_data
        from modules.volume_analysis import rvol_dashboard_rows

        data = load_pipeline_data()
        return rvol_dashboard_rows(data, limit=10), None
    except Exception as exc:
        return [], str(exc)


def _fetch_orb_momentum_snapshot() -> tuple[list[dict], str | None]:
    """ORB+RVOL momentum sleeve signals and open positions."""
    try:
        if not config.effective_orb_momentum_enabled():
            return [], "ORB momentum sleeve off"
        from modules.orb_momentum_sleeve import orb_momentum_dashboard_rows
        from modules.pipeline_strategies import load_pipeline_data

        data = load_pipeline_data()
        return orb_momentum_dashboard_rows(data, limit=10), None
    except Exception as exc:
        return [], str(exc)


def _fetch_sector_rotation_snapshot() -> tuple[list[dict], str | None]:
    """Sector rotation leaders + target weights."""
    try:
        if not config.effective_sector_rotation_enabled():
            return [], "Sector rotation off"
        from modules.pipeline_strategies import load_pipeline_data
        from modules.sector_rotation import sector_rotation_dashboard_rows

        data = load_pipeline_data()
        return sector_rotation_dashboard_rows(data, limit=11), None
    except Exception as exc:
        return [], str(exc)


def _fetch_vol_breakout_snapshot() -> tuple[list[dict], str | None]:
    """ATR volatility-breakout signals and open positions."""
    try:
        if not config.effective_vol_breakout_enabled():
            return [], "Vol breakout off"
        from modules.pipeline_strategies import load_pipeline_data
        from modules.vol_breakout_sleeve import vol_breakout_dashboard_rows

        data = load_pipeline_data()
        return vol_breakout_dashboard_rows(data, limit=10), None
    except Exception as exc:
        return [], str(exc)


def _fetch_thinking_snapshot() -> tuple[dict | None, str | None]:
    """Ollama thinking engine status for dashboard pill / overview."""
    try:
        from modules.thinking_engine import thinking_dashboard_snapshot

        return thinking_dashboard_snapshot(), None
    except Exception as exc:
        return None, str(exc)


def _fetch_strategy_performance_snapshot() -> tuple[list[dict], str | None, str | None]:
    """Per-strategy rolling ratings + MTF / exit summaries (paper research)."""
    mtf_summary = ""
    try:
        if config.effective_multi_timeframe_enabled():
            from modules.multi_timeframe import multi_timeframe_dashboard_summary

            mtf_summary = multi_timeframe_dashboard_summary()
    except Exception:
        mtf_summary = ""
    try:
        if config.effective_exit_optimization_enabled():
            from modules.exit_management import exit_dashboard_status

            exit_note = exit_dashboard_status(days=7)
            if exit_note:
                mtf_summary = f"{mtf_summary} · {exit_note}" if mtf_summary else exit_note
    except Exception:
        pass
    try:
        if config.effective_correlation_guard_enabled():
            from modules.risk_management import correlation_dashboard_status

            corr_note = correlation_dashboard_status()
            if corr_note:
                mtf_summary = f"{mtf_summary} · {corr_note}" if mtf_summary else corr_note
    except Exception:
        pass
    try:
        if not config.PAPER_TRADING and not config.paper_aggressive_context():
            return [], "Strategy performance tracking (paper / research only)", mtf_summary
        from modules.strategy_performance import dashboard_rows

        return dashboard_rows(days=30), None, mtf_summary
    except Exception as exc:
        return [], str(exc), mtf_summary


def _fetch_sharpe_history_snapshot() -> tuple[dict | None, str | None]:
    """All-time / since-update Sharpe + version markers for the dashboard."""
    try:
        from modules.sharpe_history import dashboard_sharpe_payload

        return dashboard_sharpe_payload(), None
    except Exception as exc:
        return None, str(exc)


def _fetch_conviction_snapshot() -> tuple[dict | None, str | None]:
    """Rolling conviction sizing metrics (paper)."""
    try:
        if not config.effective_conviction_sizing_enabled():
            return None, "Conviction sizing off (paper / research only)"
        from modules.risk_management import conviction_dashboard_snapshot

        return conviction_dashboard_snapshot(), None
    except Exception as exc:
        return None, str(exc)


def _fetch_exit_events_snapshot() -> tuple[list[dict], str | None]:
    try:
        if not config.effective_exit_optimization_enabled():
            return [], "Exit optimization off (paper / research only)"
        from modules.exit_management import exit_dashboard_rows

        return exit_dashboard_rows(days=7), None
    except Exception as exc:
        return [], str(exc)


def _collect_refresh_snapshot(
    username: str,
    book_id: str,
    *,
    fast: bool = False,
    fetch_positions: bool = True,
    positions_detail: bool = True,
) -> dict:
    """Network / disk work for dashboard refresh (safe off UI thread)."""

    snap: dict = {
        "book_id": book_id,
        "book_label": book_label(book_id),
        "book_paper": _book_is_paper(book_id),
        "fast": fast,
        "partial_errors": [],
        "positions_fetched": False,
    }

    with _BOOK_ENV_LOCK:
        _apply_user_paths(username, book_id)
        _reset_equity_cache()

        heartbeat, heartbeat_path = None, book_heartbeat_path(username, book_id)
        hb_exc: str | None = None
        try:
            heartbeat, heartbeat_path = _load_active_heartbeat(username, book_id)
        except Exception as exc:  # noqa: BLE001
            hb_exc = str(exc)

        # Cheap local JSON — always load so Wisdom tab populates on fast refresh too.
        scorecard, scorecard_src = None, ""
        try:
            scorecard, scorecard_src = _load_scorecard(username, book_id)
        except Exception as exc:  # noqa: BLE001
            snap["partial_errors"].append(f"scorecard: {exc}")

        acct_eq, acct_cash, acct_err = 0.0, 0.0, hb_exc
        last_equity: float | None = None
        # Alpaca network outside lock — holding lock during HTTP made book switches wait.
        try:
            heartbeat_mismatch = _heartbeat_on_disk_mismatch(username, book_id)
        except Exception:
            heartbeat_mismatch = False

    try:
        acct_eq, acct_cash, acct_err, last_equity = _fetch_account_summary(
            username=username, book_id=book_id, retries=1 if fast else 2
        )
    except Exception as exc:  # noqa: BLE001
        acct_err = str(exc)
        last_equity = None

    positions_df, pos_err = None, None
    if fetch_positions:
        try:
            positions_df, pos_err = _fetch_positions(
                username, book_id, detail=positions_detail
            )
        except Exception as exc:  # noqa: BLE001
            pos_err = str(exc)
        _positions_cache_put(username, book_id, positions_df, pos_err)
        snap["positions_fetched"] = True
    else:
        _, positions_df, pos_err = _positions_cache_peek(username, book_id)

    try:
        equity, cash, acct_err = _resolve_equity_cash(
            acct_eq,
            acct_cash,
            acct_err,
            heartbeat,
            username=username,
            book_id=book_id,
        )
    except Exception as exc:  # noqa: BLE001
        equity, cash = float(acct_eq or 0), float(acct_cash or 0)
        acct_err = str(exc)

    # --- heavy I/O outside _BOOK_ENV_LOCK (UI may need that lock for book switch) ---
    journal_df = None
    if not fast:
        try:
            # Extra fill history so Positions "Total $" (realized+open) is less truncated.
            journal_df = _load_trade_history(username, book_id, limit=max(TRADES_LIMIT, 200))
        except Exception as exc:  # noqa: BLE001
            snap["partial_errors"].append(f"journal: {_compact_io_error(exc)}")

    chart_equity_df = None
    sparkline_df = None
    # Journal equity is heavy — only on full refresh; UI reuses last sparkline on fast.
    if not fast:
        try:
            chart_equity_df = _load_equity_sparkline(
                username,
                book_id,
                # Dense enough for 1D/5D account charts without re-reading on UI.
                max_points=max(SPARKLINE_POINTS, CHART_DAYS * 3, 240),
            )
            if chart_equity_df is not None and len(chart_equity_df) > SPARKLINE_POINTS:
                step = max(1, len(chart_equity_df) // SPARKLINE_POINTS)
                sparkline_df = (
                    chart_equity_df.iloc[::step].tail(SPARKLINE_POINTS).reset_index(drop=True)
                )
            else:
                sparkline_df = chart_equity_df
        except Exception as exc:  # noqa: BLE001
            snap["partial_errors"].append(f"sparkline: {exc}")

    recent_orders_df = None
    if not fast:
        try:
            recent_orders_df = _fetch_alpaca_fills(username, book_id, limit=12)
        except Exception as exc:  # noqa: BLE001
            snap["partial_errors"].append(f"fills: {_compact_io_error(exc)}")

    try:
        running = _book_running_status(username, book_id)
    except Exception:
        running = False

    mode = "paper" if _book_is_paper(book_id) else "live"
    if not running:
        bot_label = "Bot: Stopped"
    elif fast:
        # Skip WMI/cmdline on fast path — PID detail only on full refresh.
        bot_label = f"Bot: Running · {book_label(book_id)} ({mode})"
    else:
        try:
            bot_label = bot_status_label(username, book_id)
        except Exception:
            bot_label = f"Bot: Running · {book_label(book_id)} ({mode})"

    this_book_status: list[str] = []
    other_book_status: list[str] = []
    try:
        this_book_status = _format_book_status_block(
            username,
            book_id,
            running=running,
            heartbeat=heartbeat,
            hb_path=Path(heartbeat_path) if heartbeat_path else None,
        )
    except Exception as exc:  # noqa: BLE001
        this_book_status = [f"status error: {exc}"]

    other_book = _other_book_id(book_id)
    try:
        other_book_running = _book_running_status(username, other_book)
    except Exception:
        other_book_running = False
    try:
        other_book_status = _format_book_status_block(
            username, other_book, running=other_book_running
        )
    except Exception as exc:  # noqa: BLE001
        other_book_status = [f"status error: {exc}"]
    try:
        heartbeat_stale = _heartbeat_is_stale(heartbeat, running=running)
    except Exception:
        heartbeat_stale = False

    insider_rows, insider_err = [], None
    short_rows, short_err, short_snap = [], None, None
    rvol_rows, rvol_err = [], None
    orb_mom_rows, orb_mom_err = [], None
    sector_rot_rows, sector_rot_err = [], None
    vol_bo_rows, vol_bo_err = [], None
    strategy_rows, strategy_err, strategy_mtf = [], None, None
    exit_rows, exit_err = [], None
    conviction_snap, conviction_err = None, None
    sharpe_hist, sharpe_hist_err = None, None
    thinking_snap, thinking_err = None, None
    bot_health = None

    if not fast:
        with _BOOK_ENV_LOCK:
            _apply_user_paths(username, book_id)
            try:
                insider_rows, insider_err = _fetch_insider_signals_snapshot()
            except Exception as exc:  # noqa: BLE001
                insider_err = str(exc)
            try:
                short_rows, short_err, short_snap = _fetch_short_activity_snapshot(
                    positions_df=positions_df,
                    journal_df=journal_df,
                    heartbeat=heartbeat,
                    book_paper=_book_is_paper(book_id),
                )
            except Exception as exc:  # noqa: BLE001
                short_err = str(exc)
            try:
                rvol_rows, rvol_err = _fetch_rvol_snapshot()
            except Exception as exc:  # noqa: BLE001
                rvol_err = str(exc)
            try:
                orb_mom_rows, orb_mom_err = _fetch_orb_momentum_snapshot()
            except Exception as exc:  # noqa: BLE001
                orb_mom_err = str(exc)
            try:
                sector_rot_rows, sector_rot_err = _fetch_sector_rotation_snapshot()
            except Exception as exc:  # noqa: BLE001
                sector_rot_err = str(exc)
            try:
                vol_bo_rows, vol_bo_err = _fetch_vol_breakout_snapshot()
            except Exception as exc:  # noqa: BLE001
                vol_bo_err = str(exc)
            try:
                strategy_rows, strategy_err, strategy_mtf = (
                    _fetch_strategy_performance_snapshot()
                )
            except Exception as exc:  # noqa: BLE001
                strategy_err = str(exc)
            try:
                exit_rows, exit_err = _fetch_exit_events_snapshot()
            except Exception as exc:  # noqa: BLE001
                exit_err = str(exc)
            try:
                conviction_snap, conviction_err = _fetch_conviction_snapshot()
            except Exception as exc:  # noqa: BLE001
                conviction_err = str(exc)
            try:
                sharpe_hist, sharpe_hist_err = _fetch_sharpe_history_snapshot()
            except Exception as exc:  # noqa: BLE001
                sharpe_hist_err = str(exc)
            try:
                thinking_snap, thinking_err = _fetch_thinking_snapshot()
            except Exception as exc:  # noqa: BLE001
                thinking_err = str(exc)
            if _book_is_paper(book_id) or config.effective_thinking_engine_enabled():
                try:
                    from modules.bot_health import (
                        calculate_health_score,
                        gather_health_context,
                    )

                    hctx = gather_health_context(
                        heartbeat, journal_df=journal_df, short_snap=short_snap
                    )
                    bot_health = calculate_health_score(**hctx)
                except Exception:
                    bot_health = None

    sleeve_pnl = None
    sleeve_pnl_text = ""
    if _book_is_paper(book_id):
        try:
            from modules.paper_journal import (
                compute_paper_sleeve_pnl,
                format_paper_sleeve_pnl_table,
                positions_from_dashboard_df,
            )

            sleeve_pnl = compute_paper_sleeve_pnl(
                journal_path=book_journal_path(username, book_id),
                positions=positions_from_dashboard_df(positions_df),
                equity=equity,
                cash=cash,
                spy_off=True,
                write_snapshot=True,
            )
            sleeve_pnl_text = format_paper_sleeve_pnl_table(sleeve_pnl, compact=True)
        except Exception as exc:  # noqa: BLE001
            snap["partial_errors"].append(f"sleeve_pnl: {_compact_io_error(exc)}")

    snap.update(
        {
            "other_book": other_book,
            "other_book_running": other_book_running,
            "heartbeat": heartbeat,
            "heartbeat_stale": heartbeat_stale,
            "heartbeat_mismatch": heartbeat_mismatch,
            "heartbeat_path": str(heartbeat_path),
            "scorecard": scorecard,
            "scorecard_src": scorecard_src,
            "acct_eq": acct_eq,
            "acct_cash": acct_cash,
            "acct_err": acct_err,
            "positions_df": positions_df,
            "pos_err": pos_err,
            "journal_df": journal_df,
            "recent_orders_df": recent_orders_df,
            "sparkline_df": sparkline_df,
            "chart_equity_df": chart_equity_df,
            "bot_label": bot_label,
            "this_book_status": this_book_status,
            "other_book_status": other_book_status,
            "running": running,
            "equity": equity,
            "cash": cash,
            "last_equity": last_equity,
            "runtime_layout": runtime_layout_label(PROJECT_ROOT),
            "bot_exe": str(resolve_bot_executable(PROJECT_ROOT) or ""),
            "insider_rows": insider_rows,
            "insider_err": insider_err,
            "short_rows": short_rows,
            "short_err": short_err,
            "short_snap": short_snap,
            "rvol_rows": rvol_rows,
            "rvol_err": rvol_err,
            "orb_mom_rows": orb_mom_rows,
            "orb_mom_err": orb_mom_err,
            "sector_rot_rows": sector_rot_rows,
            "sector_rot_err": sector_rot_err,
            "vol_bo_rows": vol_bo_rows,
            "vol_bo_err": vol_bo_err,
            "strategy_rows": strategy_rows,
            "strategy_err": strategy_err,
            "strategy_mtf": strategy_mtf,
            "exit_rows": exit_rows,
            "exit_err": exit_err,
            "conviction_snap": conviction_snap,
            "conviction_err": conviction_err,
            "sharpe_hist": sharpe_hist,
            "sharpe_hist_err": sharpe_hist_err,
            "thinking_snap": thinking_snap,
            "thinking_err": thinking_err,
            "bot_health": bot_health,
            "sleeve_pnl": sleeve_pnl,
            "sleeve_pnl_text": sleeve_pnl_text,
        }
    )
    return snap


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


def _resolve_db_path() -> Path:
    """market_data.db — always via config.resolve_db_path (never empty dist stub)."""
    return config.resolve_db_path()


def _read_csv_tail(path: Path, max_rows: int) -> pd.DataFrame:
    """Backward-compatible wrapper — uses safe tail reader."""
    from modules.csv_utils import read_csv_tail as _safe_read_csv_tail

    return _safe_read_csv_tail(path, max_rows)


def _read_trade_journal_csv(path: Path, *, tail_rows: int | None = None) -> pd.DataFrame:
    """Portal journal via recon reader (ragged fill rows). Not pd.read_csv skip."""
    try:
        analysis = PROJECT_ROOT / "scripts" / "analysis"
        if str(analysis) not in sys.path:
            sys.path.insert(0, str(analysis))
        from trade_reconciliation import read_journal_csv

        df, _warnings = read_journal_csv(path)
    except Exception:
        df = read_csv_file(path, tail_rows=tail_rows)
        if df.empty:
            return df
        return coerce_trade_journal_df(df)
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    return coerce_trade_journal_df(df)


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


def _fetch_alpaca_fills(username: str, book_id: str, limit: int = TRADES_LIMIT) -> pd.DataFrame:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        client = _book_trading_client(username, book_id)
        # Fetch extra closed orders so buy/sell pairs can form closed trades.
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=max(limit * 4, 120),
            nested=True,
        )
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
            price = float(avg)
            sym = config.normalize_symbol(order.symbol)
            rows.append(
                {
                    "timestamp": str(filled_at)[:19].replace("T", " "),
                    "event": "fill",
                    "symbol": sym,
                    "side": str(getattr(order, "side", "")).split(".")[-1].lower(),
                    "notional": round(qty * price, 2),
                    "qty": qty,
                    "price": price,
                    "sleeve": _infer_sleeve(sym),
                }
            )
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


def _closed_trades_from_fills(
    fills_df: pd.DataFrame,
    *,
    limit: int = TRADES_LIMIT,
) -> list[dict]:
    """FIFO-match buy/sell fills into closed trades with entry/exit prices and dates."""
    from collections import defaultdict, deque

    if fills_df is None or fills_df.empty:
        return []

    work = fills_df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp")

    longs: dict[str, deque] = defaultdict(deque)
    shorts: dict[str, deque] = defaultdict(deque)
    closed: list[dict] = []

    def _lot_fields(row) -> tuple[str, str, float, float, object, str] | None:
        sym = config.normalize_symbol(str(row.get("symbol") or ""))
        side = str(row.get("side") or "").lower()
        event = str(row.get("event") or "").lower()
        if event in ("exit", "sell", "close") and not side:
            side = "sell"
        if event in ("entry", "buy") and not side:
            side = "buy"
        if not sym or side not in ("buy", "sell"):
            return None
        try:
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            notional = float(row.get("notional") or 0)
        except (TypeError, ValueError):
            notional = 0.0
        if qty <= 0 and price > 0 and notional > 0:
            qty = notional / price
        if price <= 0 and qty > 0 and notional > 0:
            price = notional / qty
        if price <= 0 or qty <= 0:
            return None
        sleeve = str(row.get("sleeve") or "").strip() or _infer_sleeve(sym)
        return sym, side, price, qty, row["timestamp"], sleeve

    def _close(
        *,
        sym: str,
        entry: dict,
        exit_price: float,
        match_qty: float,
        exit_ts,
        is_short: bool,
    ) -> None:
        entry_px = float(entry["price"])
        if is_short:
            pnl = (entry_px - exit_price) * match_qty
            buy_px, sell_px = exit_price, entry_px
            buy_ts, sell_ts = exit_ts, entry["ts"]
        else:
            pnl = (exit_price - entry_px) * match_qty
            buy_px, sell_px = entry_px, exit_price
            buy_ts, sell_ts = entry["ts"], exit_ts
        pnl_pct = 100.0 * (pnl / (entry_px * match_qty)) if entry_px > 0 and match_qty > 0 else 0.0
        sleeve = str(entry.get("sleeve") or _infer_sleeve(sym))
        closed.append(
            {
                "Ticker": sym,
                "Qty": match_qty,
                "Entry": buy_px,
                "Exit": sell_px,
                "P&L $": pnl,
                "P&L %": pnl_pct,
                "Bought": buy_ts,
                "Sold": sell_ts,
                "Sleeve": sleeve,
                "_qty": match_qty,
                "_entry": buy_px,
                "_exit": sell_px,
                "_pnl": pnl,
                "_pnl_pct": pnl_pct,
                # Compatibility fields for status / short-activity consumers
                "timestamp": sell_ts,
                "event": "fill",
                "symbol": sym,
                "side": "sell",
                "notional": round(buy_px * match_qty, 2),
                "sleeve": sleeve,
            }
        )

    for _, row in work.iterrows():
        parsed = _lot_fields(row)
        if not parsed:
            continue
        sym, side, price, qty, ts, sleeve = parsed
        remaining = qty
        if side == "buy":
            while remaining > 1e-9 and shorts[sym]:
                entry = shorts[sym][0]
                match_qty = min(remaining, float(entry["qty"]))
                _close(
                    sym=sym,
                    entry=entry,
                    exit_price=price,
                    match_qty=match_qty,
                    exit_ts=ts,
                    is_short=True,
                )
                remaining -= match_qty
                entry["qty"] = float(entry["qty"]) - match_qty
                if float(entry["qty"]) <= 1e-9:
                    shorts[sym].popleft()
            if remaining > 1e-9:
                longs[sym].append(
                    {"price": price, "qty": remaining, "ts": ts, "sleeve": sleeve}
                )
        else:
            while remaining > 1e-9 and longs[sym]:
                entry = longs[sym][0]
                match_qty = min(remaining, float(entry["qty"]))
                _close(
                    sym=sym,
                    entry=entry,
                    exit_price=price,
                    match_qty=match_qty,
                    exit_ts=ts,
                    is_short=False,
                )
                remaining -= match_qty
                entry["qty"] = float(entry["qty"]) - match_qty
                if float(entry["qty"]) <= 1e-9:
                    longs[sym].popleft()
            if remaining > 1e-9:
                shorts[sym].append(
                    {"price": price, "qty": remaining, "ts": ts, "sleeve": sleeve}
                )

    closed.sort(key=lambda r: r["Sold"], reverse=True)
    return closed[:limit]


def _realized_pnl_by_ticker(
    journal_df: pd.DataFrame | None,
    *,
    limit: int = 5000,
) -> dict[str, float]:
    """Sum FIFO-matched closed-trade P&L by ticker (realized only)."""
    if journal_df is None or getattr(journal_df, "empty", True):
        return {}
    closed = _closed_trades_from_fills(journal_df, limit=limit)
    out: dict[str, float] = {}
    for trade in closed:
        sym = config.normalize_symbol(str(trade.get("Ticker") or trade.get("symbol") or ""))
        if not sym:
            continue
        try:
            pnl = float(trade.get("_pnl") or trade.get("P&L $") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        out[sym] = out.get(sym, 0.0) + pnl
    return out


def _load_trade_history(
    username: str,
    book_id: str,
    *,
    limit: int = TRADES_LIMIT,
) -> pd.DataFrame | None:
    """Journal signals/exits for this book, plus Alpaca fills when journal is thin."""
    journal_parts: list[pd.DataFrame] = []
    for path in _journal_search_paths(username, book_id):
        try:
            part = _filter_journal_for_book(
                path,
                _read_trade_journal_csv(path, tail_rows=max(limit * 2, 120)),
                book_id,
            )
        except Exception:
            continue
        if not part.empty:
            journal_parts.append(part)

    if journal_parts:
        journal_df = pd.concat(journal_parts, ignore_index=True)
        journal_df = journal_df.drop_duplicates(
            subset=[c for c in ("timestamp", "event", "symbol", "side", "notional") if c in journal_df.columns]
        )
    else:
        journal_df = pd.DataFrame()

    fills_df = _fetch_alpaca_fills(username, book_id, limit=limit)
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
    keep = ["timestamp", "event", "symbol", "side", "notional", "qty", "price", "sleeve"]
    cols = [c for c in keep if c in journal_df.columns]
    out = journal_df[cols].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp", ascending=False)
    # Keep enough fill history for buy/sell pairing in the Trades tab.
    return out.head(max(limit * 4, 120)).reset_index(drop=True)


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
    tail_rows = max(max_points * 8, 256)
    for path in _journal_search_paths(username, book_id):
        try:
            raw = _read_trade_journal_csv(path, tail_rows=tail_rows)
            part = _filter_equity_journal(path, raw, book_id)
        except Exception:
            continue
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
    return _load_price_table(table, limit=max(2, int(days)))


def _load_intraday_closes(symbol: str, *, calendar_days: int) -> pd.DataFrame | None:
    """5m (live) table for symbol — last N calendar days by bar timestamp."""
    table = config.normalize_symbol(symbol)
    # Pull enough 5m bars for ~N sessions (RTH ~78 bars/day).
    limit = max(80, int(calendar_days) * 100)
    df = _load_price_table(table, limit=limit)
    if df is None or df.empty:
        return None
    return _slice_bars_by_calendar_days(df, calendar_days=calendar_days, ts_col="Date")


def _load_price_table(table: str, *, limit: int) -> pd.DataFrame | None:
    db_path = _resolve_db_path()
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error:
            return None
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
            params=(int(limit),),
        )
    except (sqlite3.Error, pd.errors.DatabaseError, ValueError):
        return None
    finally:
        conn.close()
    if df.empty or "Date" not in df.columns or "Close" not in df.columns:
        return None
    df = df.sort_values("Date").reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce", utc=True)
    try:
        df["Date"] = df["Date"].dt.tz_convert("America/New_York").dt.tz_localize(None)
    except Exception:
        try:
            df["Date"] = df["Date"].dt.tz_localize(None)
        except Exception:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    out = df.dropna(subset=["Date", "Close"])
    return out if not out.empty else None


def _slice_bars_by_calendar_days(
    df: pd.DataFrame, *, calendar_days: int, ts_col: str
) -> pd.DataFrame | None:
    if df is None or df.empty or ts_col not in df.columns:
        return None
    work = df.copy()
    work[ts_col] = pd.to_datetime(work[ts_col], errors="coerce")
    work = work.dropna(subset=[ts_col]).sort_values(ts_col)
    if work.empty:
        return None
    if calendar_days <= 1:
        last_day = work[ts_col].iloc[-1].normalize()
        sliced = work[work[ts_col] >= last_day]
    else:
        cutoff = work[ts_col].iloc[-1] - pd.Timedelta(days=max(1, int(calendar_days)))
        sliced = work[work[ts_col] >= cutoff]
    if sliced.empty:
        sliced = work.tail(min(len(work), 40))
    return sliced.reset_index(drop=True)


def _downsample_chart_df(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if df is None or len(df) <= max_points:
        return df
    step = max(1, len(df) // max_points)
    return df.iloc[::step].tail(max_points).reset_index(drop=True)


def _load_chart_closes(symbol: str, range_key: str) -> pd.DataFrame | None:
    """Broker-style range: 1D/5D prefer 5m live bars; 1M uses daily."""
    key = range_key if range_key in CHART_RANGE_SPECS else "1M"
    spec = CHART_RANGE_SPECS[key]
    max_points = int(spec["max_points"])
    if spec["prefer"] == "5m":
        intra = _load_intraday_closes(symbol, calendar_days=int(spec["calendar_days"]))
        if intra is not None and len(intra) >= 2:
            return _downsample_chart_df(intra, max_points)
    daily = _load_daily_closes(symbol, days=int(spec["daily_bars"]))
    if daily is None or daily.empty:
        return None
    return _downsample_chart_df(daily, max_points)


def _slice_equity_for_chart_range(
    eq_df: pd.DataFrame | None, range_key: str
) -> pd.DataFrame | None:
    if eq_df is None or getattr(eq_df, "empty", True):
        return None
    key = range_key if range_key in CHART_RANGE_SPECS else "1M"
    spec = CHART_RANGE_SPECS[key]
    work = eq_df.copy()
    ts_col = "ts" if "ts" in work.columns else ("Date" if "Date" in work.columns else None)
    val_col = "equity" if "equity" in work.columns else ("Close" if "Close" in work.columns else None)
    if ts_col is None or val_col is None:
        return None
    sliced = _slice_bars_by_calendar_days(
        work.rename(columns={ts_col: "Date"})[["Date", val_col]].rename(
            columns={val_col: "Close"}
        ),
        calendar_days=int(spec["calendar_days"]),
        ts_col="Date",
    )
    if sliced is None or len(sliced) < 2:
        # Fall back to densest available tail.
        tail_n = min(len(work), int(spec["max_points"]))
        tail = work.tail(tail_n)
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(tail[ts_col], errors="coerce"),
                "Close": pd.to_numeric(tail[val_col], errors="coerce"),
            }
        ).dropna()
    return _downsample_chart_df(sliced, int(spec["max_points"]))


def _pct_change_title(base_title: str, df: pd.DataFrame, *, value_col: str = "Close") -> str:
    if df is None or len(df) < 2:
        return base_title
    try:
        start = float(df[value_col].iloc[0])
        end = float(df[value_col].iloc[-1])
        if abs(start) < 1e-9:
            return base_title
        pct = 100.0 * (end / start - 1.0)
        return f"{base_title}  {pct:+.1f}%"
    except Exception:  # noqa: BLE001
        return base_title


def _charts_panel_specs(
    *,
    positions_df: pd.DataFrame | None,
    book_paper: bool,
) -> list[dict]:
    """Six panels: paper book shows account + largest holdings; live uses market mix."""
    holding_colors = (
        COLORS["accent"],
        "#38bdf8",
        "#fbbf24",
        "#a78bfa",
        "#34d399",
        COLORS["amber"],
    )
    market_fallbacks: list[tuple[str, str, str]] = [
        ("SPY", "Whole market (SPY)", COLORS["amber"]),
        ("VTI", "All stocks (VTI)", COLORS["blue"]),
        ("QQQ", "Big tech (QQQ)", "#38bdf8"),
        ("GLD", "Gold (GLD)", "#fbbf24"),
        ("BTC-USD", "Bitcoin", "#a78bfa"),
    ]

    panels: list[dict] = [
        {"kind": "equity", "title": "Your account", "color": COLORS["green"]},
    ]
    used_syms: set[str] = set()

    if book_paper and positions_df is not None and not getattr(positions_df, "empty", True):
        work = positions_df.copy()
        if "Value $" in work.columns and "Ticker" in work.columns:
            work["_value"] = pd.to_numeric(work["Value $"], errors="coerce").fillna(0.0)
            top = work.reindex(work["_value"].abs().sort_values(ascending=False).index)
            for i, (_, row) in enumerate(top.head(5).iterrows()):
                sym = str(row.get("Ticker") or "").strip().upper()
                if not sym or sym in used_syms:
                    continue
                used_syms.add(sym)
                panels.append(
                    {
                        "kind": "symbol",
                        "symbol": sym,
                        "title": sym,
                        "color": holding_colors[i % len(holding_colors)],
                    }
                )

    for sym, label, color in market_fallbacks:
        if len(panels) >= 6:
            break
        if sym in used_syms:
            continue
        panels.append({"kind": "symbol", "symbol": sym, "title": label, "color": color})
        used_syms.add(sym)

    while len(panels) < 6:
        sym, label, color = market_fallbacks[(len(panels) - 1) % len(market_fallbacks)]
        if sym in used_syms:
            break
        panels.append({"kind": "symbol", "symbol": sym, "title": label, "color": color})
        used_syms.add(sym)

    return panels[:6]


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


def _format_last_trade(snap: dict | None) -> str:
    if not snap:
        return "none yet"
    orders_df = snap.get("recent_orders_df")
    if isinstance(orders_df, pd.DataFrame) and not orders_df.empty:
        row = orders_df.iloc[0]
        ts = str(row.get("timestamp", "?"))[:19]
        side = str(row.get("side", "?")).upper()
        sym = row.get("symbol", "?")
        notional = float(row.get("notional") or 0)
        if notional > 0:
            return f"{ts}  {side} {sym}  ${notional:,.0f}"
        return f"{ts}  {side} {sym}"
    journal_df = snap.get("journal_df")
    if isinstance(journal_df, pd.DataFrame) and not journal_df.empty:
        trade_events = {"fill", "buy", "sell", "game_plan", "signal"}
        if "event" in journal_df.columns:
            fills = journal_df[journal_df["event"].astype(str).isin(trade_events)]
            if not fills.empty:
                row = fills.iloc[0]
            else:
                row = journal_df.iloc[0]
        else:
            row = journal_df.iloc[0]
        ts = str(row.get("timestamp", "?"))[:19]
        sym = row.get("symbol") or "?"
        side = str(row.get("side") or row.get("event") or "?")
        return f"{ts}  {side} {sym}"
    return "none yet"


def _expected_actions(heartbeat: dict | None) -> list[str]:
    if not heartbeat:
        return ["Start Bot to begin trading cycles (first heartbeat ~60s)."]
    lines: list[str] = []
    gates = heartbeat.get("entry_skip_reason")
    if gates and gates != "traded":
        lines.append(f"Last cycle — no entries: {gates}")
    elif gates == "traded":
        lines.append("Last cycle — entries placed (traded).")
    daily = heartbeat.get("entry_skip_daily") or {}
    if daily.get("cycles"):
        lines.append(
            f"Today: {daily.get('traded_cycles', 0)} traded / "
            f"{daily.get('skipped_cycles', 0)} skipped "
            f"(top: {daily.get('top_skip', '—')})"
        )
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
    phase = _scan_phase_label(heartbeat)
    if phase and not scan.get("market_open"):
        lines.append(f"Overnight / closed session: {phase}.")
    if vti_tgt <= 0 and vti_val > 0:
        lines.append("Vanguard leftover held (core OFF — not a buy target).")
    elif vti_tgt > 0 and vti_cap > 0 and vti_val < vti_cap * 0.95:
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
    return live_bot_pids()


def _bot_python() -> str:
    """Interpreter for run_all.py — reuse portal resolver (prefers working venv)."""
    try:
        from modules.portal_bot import _python as resolve_bot_python

        return resolve_bot_python()
    except Exception:
        pass
    for venv_py in (
        PROJECT_ROOT.parent / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
    ):
        if venv_py.is_file():
            return str(venv_py)
    return sys.executable


def _launch_bot() -> tuple[bool, str]:
    if _find_run_all_pids():
        return False, f"{BOT_EXE_NAME} or run_all.py is already running."
    exe = resolve_bot_executable(PROJECT_ROOT)
    if exe is not None:
        workdir = resolve_bot_workdir(PROJECT_ROOT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        subprocess.Popen(
            [str(exe)],
            cwd=str(workdir),
            creationflags=flags,
        )
        return True, f"Started {BOT_EXE_NAME} in {workdir}."
    run_all = PROJECT_ROOT / "run_all.py"
    if not run_all.is_file():
        return False, f"Neither {BOT_EXE_NAME} nor run_all.py found under {PROJECT_ROOT}"
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
        return 0, f"No {BOT_EXE_NAME} or run_all.py process found."
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
    height: float = 1.45,
    show_axis: bool = False,
) -> Figure:
    fig = Figure(figsize=(3.6, height), dpi=CHART_DPI, facecolor=COLORS["surface"])
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
    """Compact metric tile: value left of descriptor (saves vertical space)."""

    def __init__(
        self,
        master,
        title: str,
        *,
        hero: bool = False,
        **kwargs,
    ):
        self._hero = hero
        pad_x = 10 if hero else 8
        pad_y = 4 if hero else 5
        if hero:
            kwargs.setdefault("height", 36)
        super().__init__(
            master,
            corner_radius=12,
            fg_color=COLORS["card"],
            border_width=1,
            border_color=COLORS["border"],
            **kwargs,
        )
        if hero:
            self.pack_propagate(False)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)

        value_size = 18 if hero else 14
        self._value = ctk.CTkLabel(
            row,
            text="—",
            font=ctk.CTkFont(family="Segoe UI", size=value_size, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
        )
        self._value.pack(side="left", padx=(0, 8))
        self._title = ctk.CTkLabel(
            row,
            text=title.upper(),
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="w",
        )
        self._title.pack(side="left", fill="x", expand=True)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        for w in (row, self._title, self._value):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)

    def _on_enter(self, _event=None) -> None:
        self.configure(fg_color=COLORS["card_hover"], border_color=COLORS["accent"])

    def _on_leave(self, _event=None) -> None:
        self.configure(fg_color=COLORS["card"], border_color=COLORS["border"])

    def set(self, text: str, color: str | None = None) -> None:
        # Long Open P&L lines need a smaller font so value + label stay one row.
        size = 18 if self._hero else 14
        if self._hero and len(text) > 16:
            size = 13 if len(text) > 28 else 15
        elif not self._hero and len(text) > 14:
            size = 12
        self._value.configure(
            text=text,
            text_color=color or COLORS["text"],
            font=ctk.CTkFont(family="Segoe UI", size=size, weight="bold"),
        )


class DataTable(ctk.CTkFrame):
    """Lightweight dark table via ttk.Treeview."""

    def __init__(
        self,
        master,
        columns: list[str],
        *,
        height: int = 8,
        large: bool = False,
        fit_height: bool = False,
    ):
        super().__init__(master, fg_color=COLORS["surface"] if fit_height else "transparent")
        self._fit_height = fit_height
        self._fit_min_rows = max(4, int(height))
        style = ttk.Style()
        style.theme_use("clam")
        # ttk only auto-builds Treeview layouts when the style name ends with ".Treeview".
        style_name = "Dash.Large.Treeview" if large else "Dash.Treeview"
        self._row_height = 34 if large else 26
        row_h = self._row_height
        font = ("Segoe UI", 12) if large else ("Segoe UI", 10)
        head_font = ("Segoe UI", 11, "bold") if large else ("Segoe UI", 10, "bold")
        self._heading_pad = 34 if large else 28
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
        self._rows: list[dict] = []
        self._pnl_col: str | None = None
        self._tag_col: str | None = None
        self._sort_col: str | None = None
        self._sort_reverse = False
        self._sort_keys = {
            "Qty": "_qty",
            "Entry": "_entry",
            "Buy $": "_entry",
            "Exit": "_exit",
            "Current": "_current",
            "Current $": "_current",
            "Cost $": "_cost",
            "Value $": "_value",
            "P&L $": "_pnl",
            "P&L %": "_pnl_pct",
            "Total $": "_total",
            "Bought": "Bought",
            "Sold": "Sold",
            "Score": "_score",
        }
        self._tree = ttk.Treeview(
            self,
            columns=columns,
            show="headings",
            height=height,
            style=style_name,
            selectmode="browse",
        )
        for col in columns:
            self._tree.heading(
                col,
                text=col,
                command=lambda c=col: self._sort_by_column(c),
            )
            if large:
                widths = {
                    "Ticker": 96,
                    "symbol": 96,
                    "Sleeve": 140,
                    "sleeve": 88,
                    "Qty": 80,
                    "Entry": 88,
                    "Buy $": 96,
                    "Exit": 88,
                    "Current": 92,
                    "Current $": 96,
                    "Cost $": 100,
                    "Value $": 100,
                    "P&L $": 88,
                    "P&L %": 72,
                    "Total $": 100,
                    "Bought": 118,
                    "Sold": 118,
                    "First fill": 118,
                    "Time": 118,
                    "Side": 56,
                    "Notional": 88,
                    "Type": 92,
                    "Score": 56,
                    "Description": 280,
                    "Filing Date": 96,
                }
                width = widths.get(col, 80)
            else:
                width = 88 if col in ("Ticker", "symbol", "event", "sleeve") else 72
            left_cols = (
                "Ticker",
                "symbol",
                "Time",
                "timestamp",
                "event",
                "Sleeve",
                "sleeve",
                "Bought",
                "Sold",
                "First fill",
            )
            right_cols = (
                "Qty",
                "Entry",
                "Buy $",
                "Exit",
                "Current",
                "Current $",
                "Cost $",
                "Value $",
                "P&L $",
                "P&L %",
                "Total $",
                "Notional",
            )
            if col in left_cols:
                anchor = "w"
            elif col in right_cols:
                anchor = "e"
            else:
                anchor = "center"
            self._tree.column(col, width=width, anchor=anchor, stretch=col in ("Ticker", "symbol"))
        self._tree.tag_configure("profit", foreground=COLORS["green"])
        self._tree.tag_configure("loss", foreground=COLORS["red"])
        self._tree.tag_configure("cluster_buy", foreground=COLORS["green"])
        self._tree.tag_configure("exec_sell", foreground=COLORS["red"])
        self._tree.tag_configure("insider_sell", foreground=COLORS["red"])
        self._tree.tag_configure("rvol_high", foreground=COLORS["accent"])
        self._tree.tag_configure("rvol_strong", foreground=COLORS["green"])
        self._tree.tag_configure("orb_up", foreground=COLORS["green"])
        self._tree.tag_configure("catalyst_high", foreground=COLORS["green"])
        self._tree.tag_configure("blank", foreground=COLORS["surface"])
        scroll_y = ctk.CTkScrollbar(self, command=self._tree.yview)
        scroll_x = ctk.CTkScrollbar(self, orientation="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        if self._fit_height:
            self.bind("<Configure>", self._on_fit_configure)

    def _on_fit_configure(self, event=None) -> None:
        if not self._fit_height:
            return
        try:
            avail = int(event.height) if event is not None else int(self.winfo_height())
        except Exception:
            return
        if avail < 40:
            return
        usable = max(40, avail - 18 - self._heading_pad)
        rows = max(self._fit_min_rows, usable // max(1, self._row_height))
        rows = min(rows, 60)
        try:
            if int(self._tree.cget("height")) != rows:
                self._tree.configure(height=rows)
                self._pad_blank_rows()
        except Exception:
            pass

    def _pad_blank_rows(self) -> None:
        """Fill unused Treeview slots with dark blank rows (avoids Windows white void)."""
        if not self._fit_height:
            return
        try:
            target = int(self._tree.cget("height"))
        except Exception:
            return
        data_rows = len(self._tree.get_children())
        for item in self._tree.get_children():
            tags = self._tree.item(item, "tags")
            if tags and "blank" in tags:
                self._tree.delete(item)
        data_rows = len(self._tree.get_children())
        blank_vals = [""] * len(self._columns)
        for _ in range(max(0, target - data_rows)):
            self._tree.insert("", "end", values=blank_vals, tags=("blank",))

    def clear(self, *, keep_rows: bool = False) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        if not keep_rows:
            self._rows = []

    def set_rows(self, rows: list[dict], *, pnl_col: str | None = None, tag_col: str | None = None) -> None:
        self._rows = list(rows)
        self._pnl_col = pnl_col
        self._tag_col = tag_col
        if self._sort_col:
            self._apply_sort(self._sort_col, self._sort_reverse)
        else:
            self._render_rows()

    def _sort_by_column(self, col: str) -> None:
        if not self._rows:
            return
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = col in self._sort_keys
        self._apply_sort(col, self._sort_reverse)

    def _row_sort_key(self, row: dict, col: str) -> tuple:
        sort_key = self._sort_keys.get(col)
        if sort_key and sort_key in row:
            try:
                return (0, float(row[sort_key]))
            except (TypeError, ValueError):
                return (1, str(row.get(col, "")).lower())
        raw = row.get(col, "")
        try:
            cleaned = (
                str(raw)
                .replace("$", "")
                .replace(",", "")
                .replace("%", "")
                .replace("+", "")
                .strip()
            )
            return (0, float(cleaned))
        except ValueError:
            return (1, str(raw).lower())

    def _apply_sort(self, col: str, reverse: bool) -> None:
        self._rows.sort(key=lambda r: self._row_sort_key(r, col), reverse=reverse)
        self._render_rows()

    def _render_rows(self) -> None:
        self.clear(keep_rows=True)
        for row in self._rows:
            values = [row.get(c, "") for c in self._columns]
            tag = ""
            if self._tag_col and self._tag_col in row:
                tag = str(row.get(self._tag_col) or "")
            elif row.get("_tag"):
                tag = str(row.get("_tag") or "")
            if not tag and self._pnl_col and self._pnl_col in row:
                try:
                    tag = "profit" if float(row[self._pnl_col]) >= 0 else "loss"
                except (TypeError, ValueError):
                    tag = ""
            self._tree.insert("", "end", values=values, tags=(tag,) if tag else ())
        self._pad_blank_rows()

    def selected_row(self) -> dict | None:
        sel = self._tree.selection()
        if not sel:
            return None
        values = self._tree.item(sel[0], "values")
        return {
            col: values[i] if i < len(values) else ""
            for i, col in enumerate(self._columns)
        }


class ScrollTextPanel(ctk.CTkFrame):
    """Read-only scrollable text block for status logs and errors."""

    def __init__(
        self,
        master,
        *,
        height: int = 200,
        font_key: str = "body_sm",
        text_color: str | None = None,
    ):
        super().__init__(
            master,
            fg_color=COLORS["surface2"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self.pack_propagate(False)
        pad = 16
        self.configure(height=height + pad)
        self._default_color = text_color or COLORS["text"]
        self._text = ctk.CTkTextbox(
            self,
            height=height,
            font=_ctk_font(font_key),
            fg_color=COLORS["surface2"],
            text_color=self._default_color,
            border_width=0,
            wrap="word",
            activate_scrollbars=True,
        )
        self._text.pack(fill="both", expand=True, padx=8, pady=8)
        self._text.configure(state="disabled")

    def set_text(self, text: str, *, text_color: str | None = None) -> None:
        color = text_color or self._default_color
        self._text.configure(state="normal", text_color=color)
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text or "")
        self._text.configure(state="disabled")
        self._text.update_idletasks()
        self._text.see("end")


OVERVIEW_STATUS_HEIGHT = 360
OVERVIEW_ACTIONS_HEIGHT = 180


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
        # Do not open the dashboard inside this mainloop — nesting CTk roots
        # leaves orphan Sign-in windows when launching repeatedly.
        self._on_success(username)
        self.quit()


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
        self.title(f"PythonTrading - {book_label(self._book_id)}"
                   + (f" - {DASHBOARD_UI_TAG}" if DASHBOARD_UI_TAG else ""))
        self.geometry("1000x780")
        self.minsize(960, 680)
        self.configure(fg_color=COLORS["bg"])

        self._refresh_job: str | None = None
        self._clock_job: str | None = None
        self._active_tab = "Positions"
        self._charts_dirty = True
        self._last_equity = 0.0
        self._spark_canvas: FigureCanvasTkAgg | None = None
        self._price_canvases: list[FigureCanvasTkAgg] = []
        self._tray_icon = None
        self._shutting_down = False
        self._refresh_busy = False
        self._refresh_pending = False
        self._refresh_pending_force_positions = False
        self._refresh_seq = 0
        self._refresh_auto_cycles = 0
        self._status_restore_job: str | None = None
        self._last_positions_df: pd.DataFrame | None = None
        self._last_positions_fp: object | None = None
        self._last_realized_by_ticker: dict[str, float] = {}
        self._last_sparkline_df: pd.DataFrame | None = None
        self._last_sparkline_fp: object | None = None
        self._last_chart_equity_df: pd.DataFrame | None = None
        self._last_heartbeat: dict | None = None
        self._refresh_interval_sec_cached = REFRESH_SECONDS
        self._chart_range = "5D"

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
        header_bar.pack(fill="x", padx=10, pady=(8, 4))
        self._book_var = ctk.StringVar(value=dropdown_label_for_book(self._book_id))

        header_inner = ctk.CTkFrame(header_bar, fg_color="transparent")
        header_inner.pack(fill="x", padx=10, pady=6)
        header_inner.grid_columnconfigure(0, weight=1)
        header_inner.grid_columnconfigure(1, weight=0)

        header_left = ctk.CTkFrame(header_inner, fg_color="transparent")
        header_left.grid(row=0, column=0, sticky="nw", padx=(0, 10))
        header_left.grid_columnconfigure(1, weight=1)

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
        ).grid(row=0, column=0, sticky="nw", padx=(0, 8))

        title_block = ctk.CTkFrame(header_left, fg_color="transparent")
        title_block.grid(row=0, column=1, sticky="ew")
        title_block.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            title_block,
            text="PythonTrading Monitor",
            font=_ctk_font("title"),
            text_color=COLORS["text"],
            anchor="w",
            wraplength=520,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        sub_row = ctk.CTkFrame(title_block, fg_color="transparent")
        sub_row.grid(row=1, column=0, sticky="w", pady=(2, 0))
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
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=COLORS["green"],
            anchor="w",
            wraplength=520,
            justify="left",
        )
        self._live_equity_label.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._since_start_label = ctk.CTkLabel(
            title_block,
            text="Since Start: —",
            font=_ctk_font("body_sm"),
            text_color=COLORS["text_dim"],
            anchor="w",
            wraplength=520,
            justify="left",
        )
        self._since_start_label.grid(row=3, column=0, sticky="w", pady=(0, 0))
        self._equity_error_label = ctk.CTkLabel(
            title_block,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["amber"],
            anchor="w",
            wraplength=520,
            justify="left",
        )
        self._equity_error_label.grid(row=4, column=0, sticky="w", pady=(2, 0))

        header_right = ctk.CTkFrame(header_inner, fg_color="transparent")
        header_right.grid(row=0, column=1, sticky="ne")

        controls_shell = ctk.CTkFrame(
            header_right,
            fg_color=COLORS["surface2"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["amber"],
        )
        controls_shell.pack(anchor="e")
        controls_row = ctk.CTkFrame(controls_shell, fg_color="transparent")
        controls_row.pack(padx=6, pady=3)

        self._bot_badge = ctk.CTkLabel(
            controls_row,
            text="Bot: —",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="e",
            justify="right",
            wraplength=140,
        )
        self._bot_badge.grid(row=0, column=0, padx=(0, 8), pady=2, sticky="e")

        self._book_menu = ctk.CTkOptionMenu(
            controls_row,
            variable=self._book_var,
            values=_book_dropdown_values(),
            command=self._on_header_book_selected,
            width=148,
            height=30,
            font=_ctk_font("body_sm"),
            fg_color=COLORS["accent"],
            button_color=COLORS["surface"],
            button_hover_color=COLORS["accent_hover"],
            dropdown_fg_color=COLORS["surface2"],
            text_color=COLORS["text"],
        )
        self._book_menu.grid(row=0, column=1, padx=(0, 4), pady=2, sticky="e")

        _header_btn_style = dict(
            height=30,
            corner_radius=10,
            font=_ctk_font("caption"),
            border_width=1,
            border_color=COLORS["border"],
        )
        self._refresh_btn = ctk.CTkButton(
            controls_row,
            text="Refresh",
            width=72,
            fg_color=COLORS["surface"],
            hover_color=COLORS["accent"],
            text_color=COLORS["text"],
            command=self._on_manual_refresh,
            **_header_btn_style,
        )
        self._refresh_btn.grid(row=0, column=2, padx=2, pady=2, sticky="e")
        self._refresh_bot_btn = ctk.CTkButton(
            controls_row,
            text="Refresh Bot",
            width=92,
            fg_color=COLORS["card"],
            hover_color=COLORS["accent"],
            text_color=COLORS["blue"],
            command=self._on_refresh_bot,
            **_header_btn_style,
        )
        self._refresh_bot_btn.grid(row=0, column=3, padx=2, pady=2, sticky="e")
        ctk.CTkButton(
            controls_row,
            text="Start",
            width=64,
            fg_color=COLORS["paper_ok_bg"],
            hover_color=COLORS["green_dim"],
            text_color=COLORS["green"],
            command=self._on_start_bot,
            **_header_btn_style,
        ).grid(row=0, column=4, padx=2, pady=2, sticky="e")
        ctk.CTkButton(
            controls_row,
            text="Stop",
            width=58,
            fg_color=COLORS["live_bg"],
            hover_color=COLORS["live"],
            text_color=COLORS["red"],
            command=self._on_stop_bot,
            **_header_btn_style,
        ).grid(row=0, column=5, padx=2, pady=2, sticky="e")
        self._restart_bot_btn = ctk.CTkButton(
            controls_row,
            text="Restart Both",
            width=100,
            fg_color=COLORS["small_bg"],
            hover_color=COLORS["small"],
            text_color=COLORS["amber"],
            command=self._on_restart_both,
            **_header_btn_style,
        )
        self._restart_bot_btn.grid(row=0, column=6, padx=(2, 0), pady=2, sticky="e")

        def _resize_header_labels(_event=None) -> None:
            avail = max(240, header_left.winfo_width() - 56)
            wrap = min(520, avail)
            for widget in (
                self._live_equity_label,
                self._since_start_label,
                self._equity_error_label,
            ):
                widget.configure(wraplength=wrap)
            badge_wrap = min(420, max(160, header_right.winfo_width()))
            self._bot_badge.configure(wraplength=badge_wrap)

        header_left.bind("<Configure>", _resize_header_labels)
        header_right.bind("<Configure>", _resize_header_labels)

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
        status_inner.pack(fill="x", padx=10, pady=6)

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
        self._pill_runtime = _pill(
            status_inner,
            runtime_layout_label(PROJECT_ROOT),
            COLORS["surface2"],
            COLORS["blue"],
        )
        self._pill_small = _pill(status_inner, "SMALL ACCOUNT", COLORS["small_bg"], COLORS["amber"])
        self._pill_small.pack_forget()
        self._pill_regime = _pill(status_inner, "Regime: —", COLORS["surface2"], COLORS["text_dim"])
        self._pill_entry_gates = _pill(
            status_inner, "Gates: —", COLORS["surface2"], COLORS["amber"]
        )
        self._pill_health = _pill(status_inner, "Health: —", COLORS["surface2"], COLORS["muted"])
        self._pill_daily_bank = _pill(
            status_inner, "Bank: —", COLORS["surface2"], COLORS["muted"]
        )
        self._pill_thinking = _pill(
            status_inner, "Think: —", COLORS["surface2"], COLORS["muted"]
        )
        self._pill_thinking.pack_forget()
        self._pill_bot = _pill(status_inner, "Bot: —", COLORS["surface2"], COLORS["muted"])
        self._pill_hb = _pill(status_inner, "Heartbeat: —", COLORS["surface2"], COLORS["muted"])
        self._pill_conviction = _pill(
            status_inner, "Conviction: —", COLORS["surface2"], COLORS["muted"]
        )
        self._pill_conviction.pack_forget()

        stats_banner = ctk.CTkFrame(
            top_stack,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        stats_banner.pack(fill="x", pady=(0, 4))
        stats_inner = ctk.CTkFrame(stats_banner, fg_color="transparent")
        stats_inner.pack(fill="x", padx=12, pady=6)
        self._stats_line1 = ctk.CTkLabel(
            stats_inner,
            text="Account Total: —",
            font=_ctk_font("body_sm"),
            text_color=COLORS["text"],
            anchor="w",
            justify="left",
        )
        self._stats_line1.pack(fill="x")
        self._stats_line2 = ctk.CTkLabel(
            stats_inner,
            text="Daily Breaker: —   ·   Insight: —",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        self._stats_line2.pack(fill="x", pady=(4, 0))
        self._sleeve_pnl_body = ctk.CTkLabel(
            stats_inner,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["text_dim"],
            anchor="w",
            justify="left",
            wraplength=980,
        )

        self._small_panel = ctk.CTkFrame(top_stack, fg_color="transparent")
        self._small_body = ctk.CTkLabel(
            self._small_panel,
            text="",
            justify="left",
            font=_ctk_font("caption"),
            text_color=COLORS["amber"],
        )
        self._small_body.pack(anchor="w", padx=4)

        # Hero metrics: Equity · Cash · Open P&L · Today · sparkline
        hero_row = ctk.CTkFrame(top_stack, fg_color="transparent")
        hero_row.pack(fill="x", pady=(0, 6))
        self._metric_cards: dict[str, MetricCard] = {}
        self._metric_cards["equity"] = MetricCard(hero_row, "Account Total", hero=True)
        self._metric_cards["equity"].pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._metric_cards["cash"] = MetricCard(hero_row, "Cash", hero=True)
        self._metric_cards["cash"].pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._metric_cards["pnl"] = MetricCard(hero_row, "Open P&L", hero=True)
        self._metric_cards["pnl"].pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._metric_cards["today"] = MetricCard(hero_row, "Today", hero=True)
        self._metric_cards["today"].pack(side="left", fill="both", expand=True, padx=(0, 6))

        spark_wrap = ctk.CTkFrame(
            hero_row,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
            width=220,
            height=36,
        )
        spark_wrap.pack(side="right")
        spark_wrap.pack_propagate(False)
        spark_row = ctk.CTkFrame(spark_wrap, fg_color="transparent")
        spark_row.pack(fill="both", expand=True, padx=8, pady=4)
        self._spark_frame = ctk.CTkFrame(spark_row, fg_color="transparent", width=140)
        self._spark_frame.pack(side="left", fill="both", expand=True)
        self._spark_frame.pack_propagate(False)
        ctk.CTkLabel(
            spark_row,
            text="EQUITY TREND",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="w",
        ).pack(side="left", padx=(6, 0))

        metrics = ctk.CTkFrame(top_stack, fg_color="transparent")
        metrics.pack(fill="x", pady=(0, 4))
        for i, key in enumerate(("invested", "market")):
            card = MetricCard(metrics, key.title())
            card.grid(row=0, column=i, padx=4, sticky="nsew")
            metrics.grid_columnconfigure(i, weight=1)
            self._metric_cards[key] = card

        # Main tabbed content — positions, overview, trades, wisdom, charts
        self._tabs = ctk.CTkTabview(
            self,
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

        self._tab_positions = self._tabs.add("Positions")
        self._tab_positions.grid_rowconfigure(1, weight=1)
        self._tab_positions.grid_columnconfigure(0, weight=1)

        pos_actions = ctk.CTkFrame(
            self._tab_positions,
            fg_color=COLORS["surface2"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        pos_actions.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 4))
        pos_actions.grid_columnconfigure(0, weight=1)
        pos_top = ctk.CTkFrame(pos_actions, fg_color="transparent")
        pos_top.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 2))
        ctk.CTkLabel(
            pos_top,
            text="Open Positions",
            font=_ctk_font("heading"),
            text_color=COLORS["text"],
        ).pack(side="left")
        ctk.CTkLabel(
            pos_top,
            text="P&L is vs avg Entry, not First fill date.",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        ).pack(side="left", padx=(12, 0))
        self._pos_total = ctk.CTkLabel(
            pos_top,
            text="",
            font=_ctk_font("body_sm"),
            text_color=COLORS["muted"],
        )
        self._pos_total.pack(side="right")
        pos_btn_row = ctk.CTkFrame(pos_actions, fg_color="transparent")
        pos_btn_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        ctk.CTkButton(
            pos_btn_row,
            text="Close all",
            width=84,
            height=28,
            corner_radius=10,
            fg_color=COLORS["small"],
            hover_color=COLORS["small_bg"],
            text_color=COLORS["text"],
            font=_ctk_font("caption"),
            command=self._on_sell_all_positions,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            pos_btn_row,
            text="Sell stock",
            width=84,
            height=28,
            corner_radius=10,
            fg_color=COLORS["live_bg"],
            hover_color=COLORS["live"],
            font=_ctk_font("caption"),
            command=self._on_sell_selected_position,
        ).pack(side="left")

        self._positions_table = DataTable(
            self._tab_positions,
            [
                "Ticker",
                "Sleeve",
                "First fill",
                "Qty",
                "Buy $",
                "Current $",
                "Cost $",
                "Value $",
                "P&L $",
                "P&L %",
                "Total $",
                "ATR Stop",
            ],
            height=8,
            large=True,
            fit_height=True,
        )
        # Keep money columns readable (Treeview can crush mid columns on narrow widths).
        try:
            for col, width in (
                ("Buy $", 100),
                ("Current $", 100),
                ("Cost $", 108),
                ("Value $", 108),
                ("P&L $", 96),
                ("P&L %", 80),
                ("Total $", 108),
            ):
                self._positions_table._tree.column(col, width=width, minwidth=80, stretch=False)
        except Exception:
            pass
        self._positions_table.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        self._positions_empty_label = ctk.CTkLabel(
            self._tab_positions,
            text="No open positions\nCash idle until the next rebalance cycle.",
            font=_ctk_font("body"),
            text_color=COLORS["muted"],
            justify="center",
        )

        self._tab_signals = self._tabs.add("Signals")
        self._signals_scroll = ctk.CTkScrollableFrame(
            self._tab_signals,
            fg_color=COLORS["surface"],
            scrollbar_button_color=COLORS["surface2"],
            scrollbar_button_hover_color=COLORS["accent"],
        )
        self._signals_scroll.pack(fill="both", expand=True, padx=10, pady=10)

        self._insider_expanded = False
        self._insider_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._insider_section.pack(fill="x", padx=10, pady=(0, 10))
        insider_head = ctk.CTkFrame(self._insider_section, fg_color="transparent")
        insider_head.pack(fill="x", padx=10, pady=(8, 4))
        self._insider_toggle_btn = ctk.CTkButton(
            insider_head,
            text="▶ Insider Signals",
            width=160,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["blue"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_insider_section,
        )
        self._insider_toggle_btn.pack(side="left")
        self._insider_status = ctk.CTkLabel(
            insider_head,
            text="SEC Form 4 / 13D — top 5 (score >= 60)",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._insider_status.pack(side="left", padx=(10, 0))
        self._insider_body = ctk.CTkFrame(self._insider_section, fg_color="transparent")
        self._insider_table = DataTable(
            self._insider_body,
            ["Ticker", "Type", "Score", "Description", "Filing Date"],
            height=5,
            large=True,
        )
        self._insider_table.pack(fill="both", expand=True)
        self._insider_empty_label = ctk.CTkLabel(
            self._insider_body,
            text="No insider signals loaded",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._rvol_expanded = False
        self._rvol_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._rvol_section.pack(fill="x", padx=10, pady=(0, 10))
        rvol_head = ctk.CTkFrame(self._rvol_section, fg_color="transparent")
        rvol_head.pack(fill="x", padx=10, pady=(8, 4))
        self._rvol_toggle_btn = ctk.CTkButton(
            rvol_head,
            text="▶ RVOL & ORB",
            width=180,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["accent"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_rvol_section,
        )
        self._rvol_toggle_btn.pack(side="left")
        self._rvol_status = ctk.CTkLabel(
            rvol_head,
            text="RVOL + opening-range breakouts (paper)",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._rvol_status.pack(side="left", padx=(10, 0))
        self._rvol_body = ctk.CTkFrame(self._rvol_section, fg_color="transparent")
        self._rvol_table = DataTable(
            self._rvol_body,
            ["Symbol", "RVOL", "ORB", "Signal"],
            height=5,
            large=True,
        )
        self._rvol_table.pack(fill="both", expand=True)
        self._rvol_empty_label = ctk.CTkLabel(
            self._rvol_body,
            text="No high-RVOL names loaded",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._orb_mom_expanded = False
        self._orb_mom_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._orb_mom_section.pack(fill="x", padx=10, pady=(0, 10))
        orb_mom_head = ctk.CTkFrame(self._orb_mom_section, fg_color="transparent")
        orb_mom_head.pack(fill="x", padx=10, pady=(8, 4))
        self._orb_mom_toggle_btn = ctk.CTkButton(
            orb_mom_head,
            text="▶ ORB Momentum",
            width=160,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["green"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_orb_mom_section,
        )
        self._orb_mom_toggle_btn.pack(side="left")
        self._orb_mom_status = ctk.CTkLabel(
            orb_mom_head,
            text="30m OR break + RVOL>=2 · ATR stop · 1.5R target",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._orb_mom_status.pack(side="left", padx=(10, 0))
        self._orb_mom_body = ctk.CTkFrame(self._orb_mom_section, fg_color="transparent")
        self._orb_mom_table = DataTable(
            self._orb_mom_body,
            ["Symbol", "Status", "RVOL", "Stop", "Target", "Conv"],
            height=5,
            large=True,
        )
        self._orb_mom_table.pack(fill="both", expand=True)
        self._orb_mom_empty_label = ctk.CTkLabel(
            self._orb_mom_body,
            text="No ORB momentum signals",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._sector_rot_expanded = False
        self._sector_rot_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._sector_rot_section.pack(fill="x", padx=10, pady=(0, 10))
        sector_rot_head = ctk.CTkFrame(self._sector_rot_section, fg_color="transparent")
        sector_rot_head.pack(fill="x", padx=10, pady=(8, 4))
        self._sector_rot_toggle_btn = ctk.CTkButton(
            sector_rot_head,
            text="▶ Sector Rotation",
            width=160,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["green"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_sector_rot_section,
        )
        self._sector_rot_toggle_btn.pack(side="left")
        self._sector_rot_status = ctk.CTkLabel(
            sector_rot_head,
            text="Momentum + RS vs SPY · monthly/regime rebalance",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._sector_rot_status.pack(side="left", padx=(10, 0))
        self._sector_rot_body = ctk.CTkFrame(self._sector_rot_section, fg_color="transparent")
        self._sector_rot_table = DataTable(
            self._sector_rot_body,
            ["Sector", "ETF", "Score", "RS vs SPY", "Target", "Status"],
            height=6,
            large=True,
        )
        self._sector_rot_table.pack(fill="both", expand=True)
        self._sector_rot_empty_label = ctk.CTkLabel(
            self._sector_rot_body,
            text="No sector rotation targets",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._vol_bo_expanded = False
        self._vol_bo_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._vol_bo_section.pack(fill="x", padx=10, pady=(0, 10))
        vol_bo_head = ctk.CTkFrame(self._vol_bo_section, fg_color="transparent")
        vol_bo_head.pack(fill="x", padx=10, pady=(8, 4))
        self._vol_bo_toggle_btn = ctk.CTkButton(
            vol_bo_head,
            text="▶ Vol Breakout",
            width=150,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["green"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_vol_bo_section,
        )
        self._vol_bo_toggle_btn.pack(side="left")
        self._vol_bo_status = ctk.CTkLabel(
            vol_bo_head,
            text="ATR expansion + RVOL + MTF · risk ≤1%",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._vol_bo_status.pack(side="left", padx=(10, 0))
        self._vol_bo_body = ctk.CTkFrame(self._vol_bo_section, fg_color="transparent")
        self._vol_bo_table = DataTable(
            self._vol_bo_body,
            ["Symbol", "Status", "ATR×", "RVOL", "Stop", "Target", "Conv"],
            height=5,
            large=True,
        )
        self._vol_bo_table.pack(fill="both", expand=True)
        self._vol_bo_empty_label = ctk.CTkLabel(
            self._vol_bo_body,
            text="No vol breakout signals",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._strategy_expanded = False
        self._strategy_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._strategy_section.pack(fill="x", padx=10, pady=(0, 10))
        strategy_head = ctk.CTkFrame(self._strategy_section, fg_color="transparent")
        strategy_head.pack(fill="x", padx=10, pady=(8, 4))
        self._strategy_toggle_btn = ctk.CTkButton(
            strategy_head,
            text="▶ Strategy Performance",
            width=200,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["green"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_strategy_section,
        )
        self._strategy_toggle_btn.pack(side="left")
        self._strategy_status = ctk.CTkLabel(
            strategy_head,
            text="Rolling 30d ratings per strategy (paper)",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._strategy_status.pack(side="left", padx=(10, 0))
        self._strategy_body = ctk.CTkFrame(self._strategy_section, fg_color="transparent")
        self._strategy_table = DataTable(
            self._strategy_body,
            ["Strategy", "Rating", "Score", "Return%", "Sharpe", "Win%", "Trades", "PnL", "AvgHold"],
            height=6,
            large=True,
        )
        self._strategy_table.pack(fill="both", expand=True)
        self._strategy_empty_label = ctk.CTkLabel(
            self._strategy_body,
            text="No strategy metrics yet",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._sharpe_expanded = False
        self._sharpe_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._sharpe_section.pack(fill="x", padx=10, pady=(0, 10))
        sharpe_head = ctk.CTkFrame(self._sharpe_section, fg_color="transparent")
        sharpe_head.pack(fill="x", padx=10, pady=(8, 4))
        self._sharpe_toggle_btn = ctk.CTkButton(
            sharpe_head,
            text="▶ Sharpe History",
            width=160,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["green"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_sharpe_section,
        )
        self._sharpe_toggle_btn.pack(side="left")
        self._sharpe_status = ctk.CTkLabel(
            sharpe_head,
            text="All-time · since major update · version markers",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._sharpe_status.pack(side="left", padx=(10, 0))
        self._sharpe_body = ctk.CTkFrame(self._sharpe_section, fg_color="transparent")
        self._sharpe_summary = ctk.CTkLabel(
            self._sharpe_body,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        self._sharpe_summary.pack(fill="x", pady=(0, 4))
        self._sharpe_table = DataTable(
            self._sharpe_body,
            ["Date", "From", "To", "Type", "Sharpe30d", "SharpeAll"],
            height=4,
            large=True,
        )
        self._sharpe_table.pack(fill="both", expand=True)
        self._sharpe_empty_label = ctk.CTkLabel(
            self._sharpe_body,
            text="No Sharpe history yet — updates at EOD",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._short_expanded = False
        self._short_section = ctk.CTkFrame(
            self._signals_scroll,
            fg_color=COLORS["surface"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._short_section.pack(fill="x", padx=10, pady=(0, 10))
        short_head = ctk.CTkFrame(self._short_section, fg_color="transparent")
        short_head.pack(fill="x", padx=10, pady=(8, 4))
        self._short_toggle_btn = ctk.CTkButton(
            short_head,
            text="▶ Short Activity",
            width=160,
            height=28,
            corner_radius=8,
            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],
            text_color=COLORS["amber"],
            font=_ctk_font("body_sm"),
            anchor="w",
            command=self._toggle_short_section,
        )
        self._short_toggle_btn.pack(side="left")
        self._short_status = ctk.CTkLabel(
            short_head,
            text="Protective + sector shorts (paper)",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )
        self._short_status.pack(side="left", padx=(10, 0))
        self._short_body = ctk.CTkFrame(self._short_section, fg_color="transparent")
        self._short_table = DataTable(
            self._short_body,
            ["Type", "Symbol", "Detail", "Notional", "Trigger"],
            height=5,
            large=True,
        )
        self._short_table.pack(fill="both", expand=True)
        self._short_summary = ctk.CTkLabel(
            self._short_body,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        self._short_summary.pack(fill="x", pady=(4, 0))
        self._short_empty_label = ctk.CTkLabel(
            self._short_body,
            text="No short activity this week",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
        )

        self._tab_overview = self._tabs.add("Overview")
        self._build_overview_tab()

        self._tab_trades = self._tabs.add("Activities")
        self._trades_tab_hint = ctk.CTkLabel(
            self._tab_trades,
            text="",
            font=_ctk_font("caption"),
            text_color=COLORS["muted"],
            anchor="w",
        )
        self._trades_tab_hint.pack(fill="x", padx=12, pady=(10, 4))
        self._trades_table = DataTable(
            self._tab_trades,
            ["Time", "Ticker", "Side", "Qty", "Notional", "P&L $", "Sleeve"],
            height=14,
            large=True,
        )
        self._trades_table.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._tab_wisdom = self._tabs.add("Wisdom")
        self._build_wisdom_tab()

        self._tab_charts = self._tabs.add("Charts")
        self._build_charts_tab()

        self._tabs.set("Positions")
        self._active_tab = "Positions"

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
        self.bind("<F5>", self._on_f5_refresh)
        if _needs_setup(username, self._book_id):
            self.after(200, self._show_setup_wizard)
        else:
            self.refresh_data(full=True)
            self._schedule_refresh()
            if getattr(config, "DASHBOARD_RESTART_BOTS_ON_OPEN", True) and not self._auto_start_bot:
                self.after(600, self._restart_both_on_open)
            elif self._auto_start_bot:
                self.after(800, lambda: self._maybe_auto_start_bot(quiet=True))

    def _apply_user_paths(self, username: str, book_id: str | None = None) -> None:
        with _BOOK_ENV_LOCK:
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

    def _clear_book_panels_for_switch(self, book_id: str) -> None:
        """Reset overview panels so stale book data is not shown during switch."""
        mode = "Paper" if _book_is_paper(book_id) else "Live"
        self._live_status_panel.set_text(
            f"Switching to {book_label(book_id)} ({mode})…",
            text_color=COLORS["muted"],
        )
        self._clear_frame(self._actions_scroll)
        self._wisdom_line.configure(text="—")
        if hasattr(self, "_overview_last_trade"):
            self._overview_last_trade.configure(text="Last trade: —", text_color=COLORS["muted"])
            self._overview_next_action.configure(text="Next expected: —", text_color=COLORS["muted"])
        self._live_equity_label.configure(
            text=f"Switching to {mode}…",
            text_color=COLORS["muted"],
        )
        self._equity_error_label.configure(text="")
        self._stats_line1.configure(text="Account Total: —")
        self._stats_line2.configure(text="Loading…")
        self._since_start_label.configure(text="Since Start: —")
        if "today" in getattr(self, "_metric_cards", {}):
            self._metric_cards["today"].set("—", color=COLORS["muted"])
        self._last_positions_df = None
        self._last_positions_fp = None
        self._last_realized_by_ticker = {}
        self._last_sparkline_df = None
        self._last_sparkline_fp = None
        self._last_chart_equity_df = None

    def _start_book_async(self, book_id: str) -> None:
        self._status_label.configure(text=f"Starting {book_label(book_id)} bot…")
        self._bot_badge.configure(text="Bot: starting…", text_color=COLORS["amber"])

        def _worker() -> None:
            ok, msg = start_bot(self._username, book_id)

            def _finish() -> None:
                if not ok:
                    messagebox.showwarning("Start Bot", msg)
                self.refresh_data()

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True, name="dashboard-book-start").start()

    def _restart_book_async(self, book_id: str) -> None:
        if getattr(self, "_bot_restart_busy", False):
            return
        self._bot_restart_busy = True
        self._set_bot_action_buttons_busy(
            True, status=f"Restarting {book_label(book_id)} bot…"
        )
        self._status_label.configure(text=f"Restarting {book_label(book_id)} bot…")
        self._bot_badge.configure(text="Bot: restarting…", text_color=COLORS["amber"])
        self._pill_bot.configure(
            text="Bot: restarting…",
            fg_color=COLORS["small_bg"],
            text_color=COLORS["amber"],
        )

        def _worker() -> None:
            ok, msg = restart_bot(self._username, book_id)

            def _finish() -> None:
                self._bot_restart_busy = False
                self._set_bot_action_buttons_busy(False)
                if not ok:
                    messagebox.showwarning("Restart Bot", msg)
                self.refresh_data()

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True, name="dashboard-book-restart").start()

    def _restart_both_async(
        self,
        *,
        confirm: bool = True,
        on_done=None,
        status: str = "Restarting live + paper…",
    ) -> None:
        """Clean-restart primary paper + live (reload env/code). Positions stay open."""
        if getattr(self, "_bot_restart_busy", False):
            return
        if confirm and not messagebox.askyesno(
            "Restart Both",
            "Clean-restart Alpaca Live + Paper?\n\n"
            "Stops both trading loops, clears stale PIDs, then relaunches so "
            "env/code updates take effect.\n"
            "Open positions are not closed.\n\nContinue?",
            icon="warning",
        ):
            return
        self._bot_restart_busy = True
        self._set_bot_action_buttons_busy(True, status=status)
        self._status_label.configure(text=status)
        self._bot_badge.configure(text="Bot: restarting both…", text_color=COLORS["amber"])
        self._pill_bot.configure(
            text="Bot: restarting both…",
            fg_color=COLORS["small_bg"],
            text_color=COLORS["amber"],
        )

        def _worker() -> None:
            from scripts.owner_reset import clean_restart_both_bots

            try:
                ok, msg = clean_restart_both_bots(self._username)
            except Exception as exc:
                ok, msg = False, str(exc)

            def _finish() -> None:
                self._bot_restart_busy = False
                self._set_bot_action_buttons_busy(False)
                if on_done is not None:
                    on_done(ok, msg)
                    return
                if not ok:
                    messagebox.showwarning("Restart Both", msg)
                else:
                    self._status_label.configure(text="Live + paper restarted.")
                self.refresh_data()

            self.after(0, _finish)

        threading.Thread(
            target=_worker, daemon=True, name="dashboard-restart-both"
        ).start()

    def _restart_both_on_open(self) -> None:
        """Dashboard open/reopen: reload env/code for both books (no confirm)."""
        self._restart_both_async(
            confirm=False,
            status="Open reset: restarting live + paper…",
        )

    def _set_bot_action_buttons_busy(self, busy: bool, *, status: str | None = None) -> None:
        state = "disabled" if busy else "normal"
        refresh_bot_text = "…" if busy else "Refresh Bot"
        try:
            self._refresh_btn.configure(state=state)
            self._refresh_bot_btn.configure(text=refresh_bot_text, state=state)
            self._restart_bot_btn.configure(state=state)
        except Exception:
            pass
        if status:
            self._status_label.configure(text=status)
            self._bot_badge.configure(text="Bot: working…", text_color=COLORS["amber"])
            self._pill_bot.configure(
                text="Bot: working…",
                fg_color=COLORS["small_bg"],
                text_color=COLORS["amber"],
            )

    def _refresh_bot_async(self, book_id: str) -> None:
        self._set_bot_action_buttons_busy(
            True,
            status=f"Refresh Bot: {book_label(book_id)}…",
        )
        progress_msgs: list[str] = []

        def _progress(msg: str) -> None:
            progress_msgs.append(msg)

            def _ui() -> None:
                self._status_label.configure(text=f"Refresh Bot: {msg}")

            self.after(0, _ui)

        def _worker() -> None:
            ok, msg = refresh_bot(
                self._username,
                book_id,
                progress=_progress,
            )

            def _finish() -> None:
                self._set_bot_action_buttons_busy(False)
                if ok:
                    messagebox.showinfo("Refresh Bot", msg)
                else:
                    messagebox.showwarning("Refresh Bot", msg)
                self.refresh_data()

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True, name="dashboard-refresh-bot").start()

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
        self.title(
            f"PythonTrading - {book_label(book_id)}"
            + (f" - {DASHBOARD_UI_TAG}" if DASHBOARD_UI_TAG else "")
        )
        # Abandon in-flight refresh for the old book (apply checks _refresh_seq).
        self._refresh_seq += 1
        self._refresh_busy = False
        self._refresh_pending = False
        self._refresh_pending_force_positions = False
        self._clear_book_panels_for_switch(book_id)
        self._status_label.configure(text="Loading account data…")
        self._bot_badge.configure(text="Bot: …", text_color=COLORS["muted"])
        self.update_idletasks()

        if _needs_setup(self._username, book_id):
            self._show_setup_wizard()
            return

        # Fast path first (equity + positions) — full journal/scanners deferred.
        # A full refresh on switch was the long paper↔live delay (multi‑MB journal).
        self.refresh_data(full=False, force_positions=True)
        switch_seq = self._refresh_seq

        def _deferred_full() -> None:
            if self._book_id != book_id or self._refresh_seq != switch_seq:
                return
            self.refresh_data(full=True)

        self.after(400, _deferred_full)

    def _on_logout_click(self) -> None:
        if self._on_logout is None:
            return
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        self._stop_tray()
        # Signal outer session loop first, then end this mainloop (no nested login).
        self._on_logout()
        self.destroy()

    def _build_overview_tab(self) -> None:
        self._overview_body = ctk.CTkScrollableFrame(
            self._tab_overview,
            fg_color="transparent",
            scrollbar_fg_color=COLORS["surface2"],
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["accent"],
        )
        self._overview_body.pack(fill="both", expand=True)

        activity_panel = ctk.CTkFrame(
            self._overview_body,
            fg_color=COLORS["surface2"],
            corner_radius=12,
            border_width=2,
            border_color=COLORS["accent"],
        )
        activity_panel.pack(fill="x", padx=12, pady=(10, 8))
        ctk.CTkLabel(
            activity_panel,
            text="Activity",
            font=_ctk_font("heading"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 2))
        self._overview_last_trade = ctk.CTkLabel(
            activity_panel,
            text="Last trade: —",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=COLORS["text"],
            anchor="w",
            wraplength=820,
            justify="left",
        )
        self._overview_last_trade.pack(fill="x", padx=12, pady=(0, 4))
        self._overview_next_action = ctk.CTkLabel(
            activity_panel,
            text="Next expected: —",
            font=_ctk_font("body_sm"),
            text_color=COLORS["amber"],
            anchor="w",
            wraplength=820,
            justify="left",
        )
        self._overview_next_action.pack(fill="x", padx=12, pady=(0, 10))

        live_panel = ctk.CTkFrame(
            self._overview_body,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
            height=OVERVIEW_STATUS_HEIGHT + 52,
        )
        live_panel.pack(fill="x", padx=12, pady=(0, 8))
        live_panel.pack_propagate(False)
        self._overview_status_title = ctk.CTkLabel(
            live_panel,
            text="Bot status (both books)",
            font=_ctk_font("heading"),
            anchor="w",
        )
        self._overview_status_title.pack(fill="x", padx=12, pady=(10, 4))
        self._live_status_panel = ScrollTextPanel(
            live_panel,
            height=OVERVIEW_STATUS_HEIGHT,
            font_key="body",
            text_color=COLORS["text"],
        )
        self._live_status_panel.pack(fill="x", padx=12, pady=(0, 10))
        self._live_status_panel.set_text("Waiting for refresh…", text_color=COLORS["muted"])

        ctk.CTkLabel(
            self._overview_body,
            text="Next actions",
            font=_ctk_font("heading"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(4, 4))
        self._actions_scroll = ctk.CTkScrollableFrame(
            self._overview_body,
            height=OVERVIEW_ACTIONS_HEIGHT,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
            scrollbar_fg_color=COLORS["surface2"],
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["accent"],
        )
        self._actions_scroll.pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(
            self._overview_body,
            text="Wisdom",
            font=_ctk_font("heading"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(8, 4))
        self._wisdom_line = ctk.CTkLabel(
            self._overview_body,
            text="—",
            justify="left",
            anchor="w",
            font=_ctk_font("body_sm"),
            text_color=COLORS["muted"],
            wraplength=820,
        )
        self._wisdom_line.pack(fill="x", padx=14)

        self._crypto_vol_heading = ctk.CTkLabel(
            self._overview_body,
            text="Crypto vol sleeve",
            font=_ctk_font("heading"),
            anchor="w",
        )
        self._crypto_vol_heading.pack(fill="x", padx=14, pady=(12, 4))
        self._crypto_vol_panel = ctk.CTkFrame(
            self._overview_body,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._crypto_vol_panel.pack(fill="x", padx=12, pady=(0, 10))
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
        cards.pack(fill="x", padx=8, pady=(8, 4))
        self._w_sharpe = MetricCard(cards, "Live Sharpe")
        self._w_ret = MetricCard(cards, "Live Return")
        self._w_vs = MetricCard(cards, "Live vs Best Sim")
        for i, card in enumerate((self._w_sharpe, self._w_ret, self._w_vs)):
            card.grid(row=0, column=i, padx=4, sticky="nsew")
            cards.grid_columnconfigure(i, weight=1)
        self._wisdom_rec = ctk.CTkLabel(
            self._tab_wisdom, text="", wraplength=900, justify="left", text_color=COLORS["muted"]
        )
        self._wisdom_rec.pack(fill="x", padx=12, pady=(2, 4))
        self._wisdom_chart_frame = ctk.CTkFrame(
            self._tab_wisdom,
            fg_color=COLORS["card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
            height=220,
        )
        self._wisdom_chart_frame.pack(fill="x", padx=8, pady=(0, 6))
        self._wisdom_chart_frame.pack_propagate(False)
        self._wisdom_chart_canvas: FigureCanvasTkAgg | None = None
        self._wisdom_table = DataTable(
            self._tab_wisdom,
            ["Source", "Return%", "Sharpe", "ΔRet vs Live", "ΔSharpe", "Notes"],
            height=8,
            large=True,
        )
        self._wisdom_table.pack(fill="both", expand=True, padx=8, pady=(0, 4))
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
        self._chart_range_var = ctk.StringVar(value=getattr(self, "_chart_range", "5D"))
        range_seg = ctk.CTkSegmentedButton(
            bar,
            values=list(CHART_RANGE_KEYS),
            variable=self._chart_range_var,
            command=self._on_chart_range_changed,
            height=32,
            font=_ctk_font("caption"),
            fg_color=COLORS["surface2"],
            selected_color=COLORS["accent"],
            selected_hover_color=COLORS["accent_hover"],
            unselected_color=COLORS["surface"],
            unselected_hover_color=COLORS["card_hover"],
            text_color=COLORS["text"],
        )
        range_seg.pack(side="left", padx=(10, 0))
        self._charts_hint = ctk.CTkLabel(
            bar,
            text="1D / 5D use 5m bars · 1M uses daily",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11),
        )
        self._charts_hint.pack(side="left", padx=10)
        self._charts_frame = ctk.CTkFrame(self._tab_charts, fg_color="transparent")
        self._charts_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self._tab_charts.grid_rowconfigure(1, weight=1)

    def _on_chart_range_changed(self, value: str | None = None) -> None:
        key = str(value or self._chart_range_var.get() or "5D").upper()
        if key not in CHART_RANGE_SPECS:
            key = "5D"
        self._chart_range = key
        self._draw_charts()

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

    def _toggle_rvol_section(self) -> None:
        self._rvol_expanded = not self._rvol_expanded
        arrow = "▼" if self._rvol_expanded else "▶"
        self._rvol_toggle_btn.configure(text=f"{arrow} RVOL & ORB")
        if self._rvol_expanded:
            self._rvol_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._rvol_body.pack_forget()

    def _toggle_orb_mom_section(self) -> None:
        self._orb_mom_expanded = not self._orb_mom_expanded
        arrow = "▼" if self._orb_mom_expanded else "▶"
        self._orb_mom_toggle_btn.configure(text=f"{arrow} ORB Momentum")
        if self._orb_mom_expanded:
            self._orb_mom_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._orb_mom_body.pack_forget()

    def _toggle_sector_rot_section(self) -> None:
        self._sector_rot_expanded = not self._sector_rot_expanded
        arrow = "▼" if self._sector_rot_expanded else "▶"
        self._sector_rot_toggle_btn.configure(text=f"{arrow} Sector Rotation")
        if self._sector_rot_expanded:
            self._sector_rot_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._sector_rot_body.pack_forget()

    def _toggle_vol_bo_section(self) -> None:
        self._vol_bo_expanded = not self._vol_bo_expanded
        arrow = "▼" if self._vol_bo_expanded else "▶"
        self._vol_bo_toggle_btn.configure(text=f"{arrow} Vol Breakout")
        if self._vol_bo_expanded:
            self._vol_bo_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._vol_bo_body.pack_forget()

    def _toggle_strategy_section(self) -> None:
        self._strategy_expanded = not self._strategy_expanded
        arrow = "▼" if self._strategy_expanded else "▶"
        self._strategy_toggle_btn.configure(text=f"{arrow} Strategy Performance")
        if self._strategy_expanded:
            self._strategy_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._strategy_body.pack_forget()

    def _toggle_sharpe_section(self) -> None:
        self._sharpe_expanded = not self._sharpe_expanded
        arrow = "▼" if self._sharpe_expanded else "▶"
        self._sharpe_toggle_btn.configure(text=f"{arrow} Sharpe History")
        if self._sharpe_expanded:
            self._sharpe_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._sharpe_body.pack_forget()

    def _fill_rvol_stocks(
        self,
        rows: list[dict] | None,
        err: str | None,
        *,
        book_paper: bool,
    ) -> None:
        self._rvol_empty_label.place_forget()
        if not book_paper:
            self._rvol_section.pack_forget()
            return
        self._rvol_section.pack(fill="x", padx=10, pady=(0, 10))
        if err:
            self._rvol_table.clear()
            self._rvol_status.configure(text=err[:120], text_color=COLORS["amber"])
            self._rvol_empty_label.configure(text=err)
            self._rvol_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        if not (
            config.effective_rvol_scanner_enabled()
            or config.effective_orb_enabled()
            or config.effective_catalyst_scoring_enabled()
        ):
            self._rvol_section.pack_forget()
            return
        self._rvol_status.configure(
            text=(
                f"RVOL min {config.RVOL_MIN_THRESHOLD:.1f}x · "
                f"ORB {config.ORB_BREAKOUT_MINUTES}m · "
                f"Catalyst min {int(config.CATALYST_MIN_SCORE)}"
            ),
            text_color=COLORS["muted"],
        )
        if not rows:
            self._rvol_table.clear()
            self._rvol_empty_label.configure(text="No RVOL / ORB / catalyst setups")
            self._rvol_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._rvol_table._sort_col = "RVOL"
        self._rvol_table._sort_reverse = True
        self._rvol_table.set_rows(rows, tag_col="_tag")

    def _fill_orb_momentum(
        self,
        rows: list[dict] | None,
        err: str | None,
        *,
        book_paper: bool,
    ) -> None:
        self._orb_mom_empty_label.place_forget()
        show = config.effective_orb_momentum_enabled() or book_paper
        if not show:
            self._orb_mom_section.pack_forget()
            return
        self._orb_mom_section.pack(fill="x", padx=10, pady=(0, 10))
        live = " · LIVE opt-in" if config.orb_momentum_live_sleeve_enabled() else " · paper"
        if err:
            self._orb_mom_table.clear()
            self._orb_mom_status.configure(text=err[:140], text_color=COLORS["amber"])
            self._orb_mom_empty_label.configure(text=err)
            self._orb_mom_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._orb_mom_status.configure(
            text=(
                f"{int(config.ORB_BREAKOUT_MINUTES)}m OR · RVOL>={config.ORB_RVOL_MIN:.1f}x · "
                f"risk {config.ORB_MOMENTUM_RISK_PCT:.0%} · "
                f"max {config.ORB_MOMENTUM_MAX_SIZE_PCT:.0%} · "
                f"RR {config.ORB_MOMENTUM_RR:.1f}:1{live}"
            ),
            text_color=COLORS["muted"],
        )
        if not rows:
            self._orb_mom_table.clear()
            self._orb_mom_empty_label.configure(text="No ORB momentum signals / opens")
            self._orb_mom_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._orb_mom_table.set_rows(rows, tag_col="_tag")

    def _fill_sector_rotation(
        self,
        rows: list[dict] | None,
        err: str | None,
        *,
        book_paper: bool,
    ) -> None:
        self._sector_rot_empty_label.place_forget()
        show = config.effective_sector_rotation_enabled() or book_paper
        if not show:
            self._sector_rot_section.pack_forget()
            return
        self._sector_rot_section.pack(fill="x", padx=10, pady=(0, 10))
        live = (
            " · LIVE opt-in"
            if config.sector_rotation_live_sleeve_enabled()
            else " · paper"
        )
        if err:
            self._sector_rot_table.clear()
            self._sector_rot_status.configure(text=err[:140], text_color=COLORS["amber"])
            self._sector_rot_empty_label.configure(text=err)
            self._sector_rot_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._sector_rot_status.configure(
            text=(
                f"top {int(config.SECTOR_ROTATION_TOP_N)} · "
                f"max/sector {config.SECTOR_ROTATION_MAX_SECTOR_PCT:.0%} · "
                f"sleeve {config.SECTOR_ROTATION_CAP_PCT:.0%} · "
                f"monthly/regime{live}"
            ),
            text_color=COLORS["muted"],
        )
        if not rows:
            self._sector_rot_table.clear()
            self._sector_rot_empty_label.configure(text="No sector rotation targets")
            self._sector_rot_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._sector_rot_table.set_rows(rows, tag_col="_tag")

    def _fill_vol_breakout(
        self,
        rows: list[dict] | None,
        err: str | None,
        *,
        book_paper: bool,
    ) -> None:
        self._vol_bo_empty_label.place_forget()
        show = config.effective_vol_breakout_enabled() or book_paper
        if not show:
            self._vol_bo_section.pack_forget()
            return
        self._vol_bo_section.pack(fill="x", padx=10, pady=(0, 10))
        if err:
            self._vol_bo_table.clear()
            self._vol_bo_status.configure(text=err[:140], text_color=COLORS["amber"])
            self._vol_bo_empty_label.configure(text=err)
            self._vol_bo_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._vol_bo_status.configure(
            text=(
                f"ATR expand>={config.VOL_BREAKOUT_ATR_EXPAND_MULT:.1f}x · "
                f"RVOL>={config.VOL_BREAKOUT_RVOL_MIN:.1f} · "
                f"risk≤{config.VOL_BREAKOUT_RISK_PCT:.0%} · "
                f"RR {config.VOL_BREAKOUT_RR:.1f}:1 · paper"
            ),
            text_color=COLORS["muted"],
        )
        if not rows:
            self._vol_bo_table.clear()
            self._vol_bo_empty_label.configure(text="No vol breakout signals / opens")
            self._vol_bo_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._vol_bo_table.set_rows(rows, tag_col="_tag")

    def _fill_strategy_performance(
        self,
        rows: list[dict] | None,
        err: str | None,
        *,
        book_paper: bool,
        mtf_summary: str | None = None,
        exit_rows: list[dict] | None = None,
    ) -> None:
        self._strategy_empty_label.place_forget()
        if not book_paper:
            self._strategy_section.pack_forget()
            return
        self._strategy_section.pack(fill="x", padx=10, pady=(0, 10))
        status_base = "Rolling 30d · Excellent/Good/Fair/Weak ratings"
        if mtf_summary:
            status_base = f"{status_base} · {mtf_summary}"
        if err:
            self._strategy_table.clear()
            self._strategy_status.configure(text=err[:160], text_color=COLORS["amber"])
            self._strategy_empty_label.configure(text=err)
            self._strategy_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._strategy_status.configure(
            text=status_base[:200],
            text_color=COLORS["muted"],
        )
        if not rows:
            self._strategy_table.clear()
            self._strategy_empty_label.configure(text="No closed trades — metrics populate after exits")
            self._strategy_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._strategy_table._sort_col = "Score"
        self._strategy_table._sort_reverse = True
        display_rows = list(rows)
        for er in exit_rows or []:
            display_rows.append(
                {
                    "Strategy": f"EXIT · {er.get('Reason', '?')}",
                    "Rating": str(er.get("Symbol", "")),
                    "Score": str(er.get("Time", ""))[-5:],
                    "Return%": "—",
                    "Sharpe": "—",
                    "Win%": er.get("Partial", "—"),
                    "Trades": er.get("Sleeve", "—"),
                    "PnL": "—",
                    "AvgHold": "—",
                }
            )
        self._strategy_table.set_rows(display_rows)

    def _fill_sharpe_history(
        self,
        payload: dict | None,
        err: str | None,
    ) -> None:
        self._sharpe_empty_label.place_forget()
        self._sharpe_section.pack(fill="x", padx=10, pady=(0, 10))
        if err:
            self._sharpe_table.clear()
            self._sharpe_summary.configure(text="")
            self._sharpe_status.configure(text=err[:160], text_color=COLORS["amber"])
            self._sharpe_empty_label.configure(text=err)
            self._sharpe_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        snap = (payload or {}).get("snapshot") or {}
        versions = (payload or {}).get("versions") or []

        def _fmt(v: object) -> str:
            try:
                return f"{float(v):.2f}" if v is not None else "n/a"
            except (TypeError, ValueError):
                return "n/a"

        all_s = _fmt(snap.get("sharpe_all"))
        since_s = _fmt(snap.get("sharpe_since_update"))
        proj_s = _fmt(snap.get("projected_sharpe"))
        d30 = _fmt(snap.get("sharpe_30d"))
        d90 = _fmt(snap.get("sharpe_90d"))
        deploy = snap.get("deployment_date") or "n/a"
        major = snap.get("last_major_update_date") or "n/a"
        ver = snap.get("version") or "?"
        conf = snap.get("projected_confidence") or "n/a"
        horizon = snap.get("projected_horizon_days") or 30
        self._sharpe_status.configure(
            text=f"RR v{ver} · all-time {all_s} · projected {proj_s} · since major {since_s}",
            text_color=COLORS["muted"],
        )
        self._sharpe_summary.configure(
            text=(
                f"All-time Sharpe: {all_s} (since {deploy})\n"
                f"Projected Sharpe: {proj_s} (next {horizon}d, {conf})\n"
                f"Since last major update: {since_s} ({major})  ·  "
                f"30d / 90d: {d30} / {d90}"
            )
        )
        if not versions:
            self._sharpe_table.clear()
            self._sharpe_empty_label.configure(
                text="No version markers yet — first EOD will seed history"
            )
            self._sharpe_empty_label.place(relx=0.5, rely=0.55, anchor="center")
            return
        rows = []
        for m in versions:
            rows.append(
                {
                    "Date": str(m.get("date") or "")[:10],
                    "From": str(m.get("from") or "—"),
                    "To": str(m.get("to") or "—"),
                    "Type": "major" if m.get("major") else "patch",
                    "Sharpe30d": _fmt(m.get("sharpe_30d")),
                    "SharpeAll": _fmt(m.get("sharpe_all")),
                }
            )
        self._sharpe_table.set_rows(rows)

    def _toggle_short_section(self) -> None:
        self._short_expanded = not self._short_expanded
        arrow = "▼" if self._short_expanded else "▶"
        self._short_toggle_btn.configure(text=f"{arrow} Short Activity")
        if self._short_expanded:
            self._short_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._short_body.pack_forget()

    def _fill_short_activity(
        self,
        rows: list[dict] | None,
        err: str | None,
        snap: dict | None,
        *,
        book_paper: bool,
    ) -> None:
        self._short_empty_label.place_forget()
        if not book_paper:
            self._short_section.pack_forget()
            return
        self._short_section.pack(fill="x", padx=10, pady=(0, 10))
        if err:
            self._short_table.clear()
            self._short_status.configure(text=err[:120], text_color=COLORS["amber"])
            self._short_summary.configure(text="")
            self._short_empty_label.configure(text=err)
            self._short_empty_label.place(relx=0.5, rely=0.45, anchor="center")
            return
        from modules.short_activity import format_short_activity_status

        status = format_short_activity_status(snap or {})
        self._short_status.configure(text=status, text_color=COLORS["muted"])
        summary_parts: list[str] = []
        if snap:
            banner = snap.get("banner") or ""
            if banner:
                summary_parts.append(banner)
            n_week = len(snap.get("week_trades") or [])
            if n_week:
                summary_parts.append(f"{n_week} journal event(s) this week")
        self._short_summary.configure(text=" · ".join(summary_parts))
        if not rows:
            self._short_table.clear()
            self._short_empty_label.configure(text="No open shorts or recent fires")
            self._short_empty_label.place(relx=0.5, rely=0.45, anchor="center")
            return
        self._short_table.set_rows(rows, tag_col="_tag")

    def _toggle_insider_section(self) -> None:
        self._insider_expanded = not self._insider_expanded
        arrow = "▼" if self._insider_expanded else "▶"
        self._insider_toggle_btn.configure(text=f"{arrow} Insider Signals")
        if self._insider_expanded:
            self._insider_body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        else:
            self._insider_body.pack_forget()

    def _fill_insider_signals(self, rows: list[dict] | None, err: str | None) -> None:
        self._insider_empty_label.place_forget()
        if err:
            self._insider_table.clear()
            self._insider_status.configure(text=err[:120], text_color=COLORS["amber"])
            self._insider_empty_label.configure(text=err)
            self._insider_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        if not rows:
            self._insider_table.clear()
            self._insider_status.configure(
                text="No high-quality signals",
                text_color=COLORS["muted"],
            )
            self._insider_empty_label.configure(text="No high-quality signals")
            self._insider_empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self._insider_status.configure(
            text=f"{len(rows)} signal(s) — sorted by score — refresh {REFRESH_SECONDS}s",
            text_color=COLORS["muted"],
        )
        self._insider_table._sort_col = "Score"
        self._insider_table._sort_reverse = True
        self._insider_table.set_rows(rows, tag_col="_tag")

    def _on_tab_changed(self) -> None:
        self._active_tab = self._tabs.get()
        if self._active_tab == "Charts":
            self._charts_dirty = True
            self._draw_charts()
        elif self._active_tab == "Positions":
            if not _positions_cache_fresh(self._username, self._book_id):
                self.refresh_data(force_positions=True)
        elif self._active_tab == "Wisdom":
            # Ensure scorecard/heartbeat paint immediately when opening the tab.
            self.refresh_data(full=False)

    def _clear_frame(self, frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _scroll_frame_to_bottom(self, frame: ctk.CTkScrollableFrame) -> None:
        try:
            frame._parent_canvas.yview_moveto(1.0)  # noqa: SLF001
        except Exception:
            pass

    def _update_live_status_panel(
        self,
        snap: dict,
        heartbeat: dict | None,
        *,
        running: bool,
    ) -> None:
        book_id = snap.get("book_id") or self._book_id
        other_book = snap.get("other_book") or _other_book_id(book_id)
        this_status = snap.get("this_book_status") or []
        other_status = snap.get("other_book_status") or []
        lines: list[str] = []
        lines.append("This book")
        lines.extend(this_status if this_status else [f"{book_label(book_id)} · status n/a"])
        lines.append("")
        lines.append("Other book")
        lines.extend(
            other_status if other_status else [f"{book_label(other_book)} · status n/a"]
        )

        hb_path = snap.get("heartbeat_path") or ""
        try:
            hb_rel = os.path.relpath(hb_path, resolve_data_root(PROJECT_ROOT)) if hb_path else "—"
        except ValueError:
            hb_rel = hb_path or "—"
        hb_age = _heartbeat_age_minutes(heartbeat)
        age_txt = f"{hb_age:.0f} min ago" if hb_age is not None else "no timestamp"
        hb_status = (heartbeat or {}).get("status") or "—"
        regime = (heartbeat or {}).get("regime") or "—"
        phase = _scan_phase_label(heartbeat)
        log_dir = runtime_log_dir(PROJECT_ROOT)
        log_hint = "logs/run_all.log" if (log_dir / "run_all.log").is_file() else "logs/"
        runtime = snap.get("runtime_layout") or runtime_layout_label(PROJECT_ROOT)
        exe_hint = ""
        bot_exe = snap.get("bot_exe") or ""
        if bot_exe:
            try:
                exe_hint = f" · bot: {os.path.relpath(bot_exe, PROJECT_ROOT)}"
            except ValueError:
                exe_hint = f" · bot: {Path(bot_exe).name}"
        run_txt = "running" if running else "stopped"
        stale = bool(snap.get("heartbeat_stale"))
        if snap.get("heartbeat_mismatch"):
            lines.extend(["", "Note: this book heartbeat file has wrong book tag — ignored"])
        orders_df = snap.get("recent_orders_df")
        if isinstance(orders_df, pd.DataFrame) and not orders_df.empty:
            bits = []
            for _, row in orders_df.head(8).iterrows():
                bits.append(
                    f"{row.get('timestamp', '?')} {row.get('side', '?')} "
                    f"{row.get('symbol', '?')} ${float(row.get('notional') or 0):,.0f}"
                )
            orders_txt = "\n  ".join(bits)
        else:
            orders_txt = "none (journal/Alpaca fills empty)"
        lines.extend(
            [
                "",
                f"Detail ({book_label(book_id)}): {runtime} · bot {run_txt} · "
                f"heartbeat {hb_rel} · {age_txt} · status={hb_status} · regime={regime}"
                + (f" · {phase}" if phase else "")
                + (" · STALE" if stale else ""),
                "",
                "Recent orders:",
                f"  {orders_txt}",
                "",
                f"Logs: {log_hint}{exe_hint}",
            ]
        )
        cycle_err = (heartbeat or {}).get("last_cycle_error")
        if cycle_err:
            err_at = (heartbeat or {}).get("last_cycle_error_at") or ""
            lines.extend(["", f"Last cycle error ({err_at}):"])
            lines.append(_compact_io_error(cycle_err))
        self._live_status_panel.set_text(
            "\n".join(lines),
            text_color=COLORS["red"] if stale else COLORS["text"],
        )
        if hasattr(self, "_pill_runtime"):
            self._pill_runtime.configure(text=runtime)

    def _update_status_row(
        self,
        equity: float,
        small_account: bool,
        *,
        regime: str = "—",
        halted: bool = False,
        bot_running_flag: bool = False,
        book_paper: bool | None = None,
        heartbeat: dict | None = None,
        snap: dict | None = None,
    ) -> None:
        if heartbeat is None and snap is not None:
            heartbeat = snap.get("heartbeat")
        heartbeat = heartbeat or {}
        live = not (book_paper if book_paper is not None else _book_is_paper(self._book_id))
        if live:
            self._pill_mode.configure(
                text="● LIVE TRADING",
                fg_color=COLORS["live_bg"],
                text_color="#fecaca",
            )
            if hasattr(self, "_overview_status_title"):
                self._overview_status_title.configure(text="Bot status (both books)")
        else:
            self._pill_mode.configure(
                text="● PAPER TRADING",
                fg_color=COLORS["paper_ok_bg"],
                text_color=COLORS["green"],
            )
            if hasattr(self, "_overview_status_title"):
                self._overview_status_title.configure(text="Bot status (both books)")

        if small_account:
            self._pill_small.pack(side="left", padx=(0, 8), before=self._pill_regime)
        else:
            self._pill_small.pack_forget()

        regime_short = regime.split(":")[-1].strip() if ":" in regime else regime
        hmm = heartbeat.get("markov_hmm") or {}
        if hmm.get("ok") and hmm.get("predicted"):
            conf = hmm.get("confidence")
            conf_s = f" {float(conf):.0%}" if conf is not None else ""
            regime_text = (
                f"Regime: {regime_short} → HMM {hmm.get('predicted')}{conf_s}"
            )
        else:
            regime_text = f"Regime: {regime_short}"
        self._pill_regime.configure(
            text=regime_text,
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

    def _update_health_pill(self, health: dict | None, *, book_paper: bool) -> None:
        if not hasattr(self, "_pill_health"):
            return
        # Show health for paper always; for live when thinking is enabled (system-wide).
        show = book_paper or config.effective_thinking_engine_enabled()
        if not show:
            self._pill_health.pack_forget()
            return
        self._pill_health.pack(side="left", padx=(0, 8), before=self._pill_bot)
        if not health:
            self._pill_health.configure(
                text="Health: —",
                fg_color=COLORS["surface2"],
                text_color=COLORS["muted"],
            )
            return
        score = int(health.get("score") or 0)
        grade = str(health.get("grade") or "")
        color_key = str(health.get("color") or "yellow")
        band = {"green": "Green", "yellow": "Yellow", "red": "Red"}.get(color_key, "")
        if color_key == "green":
            fg, tc = COLORS["paper_ok_bg"], COLORS["green"]
        elif color_key == "red":
            fg, tc = COLORS["live_bg"], COLORS["red"]
        else:
            fg, tc = COLORS["small_bg"], COLORS["amber"]
        self._pill_health.configure(
            text=f"Health: {score}/100 ({grade}{f' · {band}' if band else ''})",
            fg_color=fg,
            text_color=tc,
        )

    def _update_daily_bank_pill(self, heartbeat: dict | None, *, book_paper: bool) -> None:
        if not hasattr(self, "_pill_daily_bank"):
            return
        bank = (heartbeat or {}).get("daily_bank") or {}
        if not book_paper or not bank.get("enabled"):
            self._pill_daily_bank.pack_forget()
            return
        self._pill_daily_bank.pack(side="left", padx=(0, 8), before=self._pill_bot)
        if bank.get("banked"):
            locked = float(bank.get("locked_gain_pct") or 0)
            self._pill_daily_bank.configure(
                text=f"Bank: locked +{locked:.2f}%",
                fg_color=COLORS["paper_ok_bg"],
                text_color=COLORS["green"],
            )
        else:
            gain = float(bank.get("gain_pct") or 0)
            thr = float(bank.get("threshold_pct") or 0.8)
            self._pill_daily_bank.configure(
                text=f"Bank: {gain:+.2f}% / {thr:g}%",
                fg_color=COLORS["surface2"],
                text_color=COLORS["muted"],
            )

    def _update_thinking_pill(self, snap: dict | None, err: str | None = None) -> None:
        if not hasattr(self, "_pill_thinking"):
            return
        snap = snap or {}
        status = str(snap.get("status") or "OFF").upper()
        if status in ("", "OFF") or (err and status != "ON"):
            self._pill_thinking.pack_forget()
            return
        self._pill_thinking.pack(side="left", padx=(0, 8), before=self._pill_bot)
        detail = str(snap.get("detail") or "")[:36]
        if status == "ON":
            fg, tc = COLORS["paper_ok_bg"], COLORS["green"]
        elif status == "FALLBACK":
            fg, tc = COLORS["small_bg"], COLORS["amber"]
        else:
            fg, tc = COLORS["surface2"], COLORS["muted"]
        text = f"Think: {status}"
        if detail:
            text = f"Think: {status} · {detail}"
        self._pill_thinking.configure(text=text[:48], fg_color=fg, text_color=tc)

    def _update_heartbeat_pill(
        self, heartbeat: dict | None, *, running: bool, stale: bool
    ) -> None:
        if not hasattr(self, "_pill_hb"):
            return
        self._pill_hb.pack(side="left", padx=(0, 8), before=self._pill_bot)
        age_min = _heartbeat_age_minutes(heartbeat)
        if age_min is None:
            self._pill_hb.configure(
                text="Heartbeat: none",
                fg_color=COLORS["surface2"],
                text_color=COLORS["muted"],
            )
            return

        if age_min < 1:
            age_txt = "just now"
        elif age_min < 60:
            age_txt = f"{age_min:.0f}m ago"
        else:
            age_txt = f"{age_min / 60:.1f}h ago"

        if not running:
            status, fg, tc = "Stopped", COLORS["surface2"], COLORS["amber"]
        elif stale:
            status, fg, tc = "Stalled", COLORS["live_bg"], COLORS["red"]
        else:
            status, fg, tc = "Responding", COLORS["paper_ok_bg"], COLORS["green"]
        self._pill_hb.configure(
            text=f"Bot: {status} · hb {age_txt}",
            fg_color=fg,
            text_color=tc,
        )

    def _update_conviction_pill(self, conviction: dict | None, *, book_paper: bool) -> None:
        if not hasattr(self, "_pill_conviction"):
            return
        if not book_paper or not conviction or not conviction.get("enabled"):
            self._pill_conviction.pack_forget()
            return
        self._pill_conviction.pack(side="left", padx=(0, 8), before=self._pill_bot)
        avg = conviction.get("avg_7d", "—")
        level = conviction.get("level", "—")
        if isinstance(avg, float):
            text = f"Conviction: {avg:.2f} ({level})"
        else:
            text = f"Conviction: {avg} ({level})"
        if level == "High":
            fg, tc = COLORS["paper_ok_bg"], COLORS["green"]
        elif level in ("Low", "Weak"):
            fg, tc = COLORS["small_bg"], COLORS["amber"]
        else:
            fg, tc = COLORS["surface2"], COLORS["blue"]
        self._pill_conviction.configure(text=text, fg_color=fg, text_color=tc)

    def _update_entry_gates_pill(self, heartbeat: dict | None) -> None:
        if not hasattr(self, "_pill_entry_gates"):
            return
        gates = (heartbeat or {}).get("entry_skip_reason") or "—"
        if gates == "traded":
            label = "Gates: traded"
            color = COLORS["green"]
        elif gates == "—":
            label = "Gates: —"
            color = COLORS["text_dim"]
        else:
            short = gates if len(gates) <= 42 else gates[:39] + "…"
            label = f"Gates: {short}"
            color = COLORS["amber"]
        self._pill_entry_gates.configure(
            text=label,
            fg_color=COLORS["surface2"],
            text_color=color,
        )
        self._pill_entry_gates.pack(side="left", padx=(0, 8), before=self._pill_bot)

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
            caps = heartbeat.get("sleeve_caps") or {}
            if caps.get("vti_core") is not None:
                vti_tgt = float(caps.get("vti_core") or 0)
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

    def _book_is_paper_chase(self) -> bool:
        spec = BOOKS.get(self._book_id) or {}
        return bool(spec.get("paper_chase"))

    def _update_sleeve_pnl_panel(self, snap: dict) -> None:
        """Paper-only all-time / since-2026-08-21 sleeve P&L. Hidden on live."""
        paper = bool(snap.get("book_paper", _book_is_paper(self._book_id)))
        if not paper:
            self._sleeve_pnl_body.pack_forget()
            return
        text = snap.get("sleeve_pnl_text")
        if text:
            self._sleeve_pnl_body.configure(text=str(text))
        elif not str(self._sleeve_pnl_body.cget("text") or "").strip():
            self._sleeve_pnl_body.configure(text="Sleeve P&L: loading…")
        self._sleeve_pnl_body.pack(fill="x", pady=(6, 0))

    def _update_stats_banner(
        self, equity: float, heartbeat: dict | None, acct_err: str | None
    ) -> None:
        if acct_err or equity <= 0:
            if acct_err:
                short = acct_err if len(acct_err) <= 100 else acct_err[:97] + "…"
                line1 = f"Account Total: unavailable — {short}"
                line2 = "Check Alpaca keys in Settings · Daily Breaker: — · Insight: —"
            else:
                line1 = "Account Total: —   ·   Since Start: —   ·   Regime: —"
                line2 = "Waiting for Alpaca account data…"
            self._stats_line1.configure(text=line1)
            self._stats_line2.configure(text=line2)
            self._since_start_label.configure(text="Since Start: —")
            return
        paper_chase = self._book_is_paper_chase()
        live_only = self._book_id == "alpaca_live"
        extra = [book_journal_path(self._username, self._book_id)]
        line1, line2, since_detail = sm.dashboard_stats_lines(
            equity=equity,
            heartbeat=heartbeat,
            paper_chase=paper_chase,
            live_only=live_only,
            extra_journal_paths=extra,
        )
        self._stats_line1.configure(text=line1)
        self._stats_line2.configure(text=line2)
        self._since_start_label.configure(text=since_detail)

    def _update_live_equity_header(
        self, equity: float, acct_err: str | None, *, paper: bool | None = None
    ) -> None:
        live = not (paper if paper is not None else config.PAPER_TRADING)
        prefix = "Live" if live else "Paper"
        if equity > 0:
            color = COLORS["green"] if live else COLORS["blue"]
            self._live_equity_label.configure(
                text=f"{prefix} Equity: ${equity:,.2f}",
                text_color=color,
            )
            self._equity_error_label.configure(text="")
            return
        if acct_err:
            short = acct_err if len(acct_err) <= 48 else acct_err[:45] + "…"
            self._live_equity_label.configure(
                text=f"{prefix} Equity: unavailable",
                text_color=COLORS["amber"],
            )
            self._equity_error_label.configure(
                text=f"Alpaca: {acct_err}",
                text_color=COLORS["amber"],
            )
        else:
            self._live_equity_label.configure(
                text=f"{prefix} Equity: —",
                text_color=COLORS["muted"],
            )
            self._equity_error_label.configure(
                text="Loading account data…",
                text_color=COLORS["muted"],
            )

    def _on_manual_refresh(self) -> None:
        self.refresh_data(full=True, force_positions=True)

    def _on_f5_refresh(self, _event=None) -> str:
        self.refresh_data(full=True, force_positions=True)
        return "break"

    def _set_refresh_buttons_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        refresh_text = "…" if busy else "Refresh"
        try:
            self._refresh_btn.configure(text=refresh_text, state=state)
        except Exception:
            pass
        try:
            self._refresh_bot_btn.configure(state=state)
        except Exception:
            pass

    def _set_refresh_status_message(
        self, text: str, *, restore_text: str | None = None, restore_ms: int = 4000
    ) -> None:
        self._status_label.configure(text=text)
        if self._status_restore_job:
            try:
                self.after_cancel(self._status_restore_job)
            except Exception:
                pass
            self._status_restore_job = None
        if restore_text:

            def _restore() -> None:
                self._status_restore_job = None
                self._status_label.configure(text=restore_text)

            self._status_restore_job = self.after(restore_ms, _restore)

    def _footer_status_line(self, snap: dict, heartbeat: dict | None, running: bool) -> str:
        mode = "LIVE" if not snap.get("book_paper", _book_is_paper(self._book_id)) else "Paper"
        ts = datetime.now().strftime("%H:%M:%S")
        mem = _process_rss_mb()
        hb_age = _heartbeat_age_minutes(heartbeat)
        heartbeat_stale = bool(snap.get("heartbeat_stale"))
        interval = int(getattr(self, "_refresh_interval_sec_cached", REFRESH_SECONDS) or REFRESH_SECONDS)
        parts = [mode, ts, f"every {interval}s"]
        if snap.get("fast"):
            parts.append("fast refresh")
        if mem:
            parts.append(mem)
        if running and heartbeat_stale and hb_age is not None:
            parts.append(f"hb stale {hb_age:.0f}m")
        errs = snap.get("partial_errors") or []
        if errs:
            shown = []
            for e in errs:
                s = _compact_io_error(e)
                if s not in shown:
                    shown.append(s)
            parts.append("; ".join(shown[:3]))
        return " · ".join(parts)

    def refresh_data(
        self, *, full: bool | None = None, force_positions: bool = False
    ) -> None:
        if self._refresh_busy:
            self._refresh_pending = True
            self._refresh_pending_force_positions = (
                self._refresh_pending_force_positions or force_positions
            )
            return
        if full is None:
            full = False
        fast = not full
        include_charts = bool(
            getattr(self, "_charts_var", None) and self._charts_var.get()
        )
        positions_tab = getattr(self, "_active_tab", "") == "Positions"
        cache_fresh = _positions_cache_fresh(self._username, self._book_id)
        # Open P&L hero needs positions even when Positions tab isn't active.
        fetch_positions = bool(force_positions or not cache_fresh)
        # Heavy Opened/ATR enrichment only on Positions tab or manual Refresh.
        positions_detail = bool(force_positions or positions_tab)
        self._refresh_busy = True
        self._refresh_seq += 1
        seq = self._refresh_seq
        username = self._username
        book_id = self._book_id
        self._set_refresh_buttons_busy(True)
        self._set_refresh_status_message("Refreshing data…")
        if fetch_positions and positions_detail:
            has_rows = (
                self._last_positions_df is not None
                and not getattr(self._last_positions_df, "empty", True)
            )
            self._set_positions_loading(clear_table=not has_rows)

        def _worker() -> None:
            try:
                snap = _collect_refresh_snapshot(
                    username,
                    book_id,
                    fast=fast,
                    fetch_positions=fetch_positions,
                    positions_detail=positions_detail,
                )
            except Exception as exc:  # noqa: BLE001
                snap = {
                    "book_id": book_id,
                    "book_paper": _book_is_paper(book_id),
                    "fast": fast,
                    "equity": 0.0,
                    "cash": 0.0,
                    "acct_err": str(exc),
                    "heartbeat": None,
                    "scorecard": None,
                    "scorecard_src": "",
                    "positions_df": None,
                    "pos_err": str(exc),
                    "positions_fetched": fetch_positions,
                    "journal_df": None,
                    "running": False,
                    "partial_errors": [str(exc)],
                }

            def _apply() -> None:
                # Stale worker after book switch: do not clear busy for the newer refresh.
                if seq != self._refresh_seq or book_id != self._book_id:
                    return
                try:
                    self._apply_refresh_snapshot(snap, include_charts=include_charts)
                    refreshed_at = datetime.now().strftime("%H:%M")
                    footer = self._footer_status_line(
                        snap, snap.get("heartbeat"), bool(snap.get("running"))
                    )
                    self._set_refresh_status_message(
                        f"Refreshed at {refreshed_at}",
                        restore_text=footer,
                    )
                finally:
                    if seq == self._refresh_seq:
                        self._refresh_busy = False
                        self._set_refresh_buttons_busy(False)
                        if self._refresh_pending:
                            pending_force = self._refresh_pending_force_positions
                            self._refresh_pending = False
                            self._refresh_pending_force_positions = False
                            self.after(
                                150,
                                lambda: self.refresh_data(
                                    full=False, force_positions=pending_force
                                ),
                            )

            self.after(0, _apply)

        threading.Thread(target=_worker, daemon=True, name="dashboard-refresh").start()

    def _apply_refresh_core(self, snap: dict) -> None:
        """Fast UI path: equity, small-account banner, sparkline, positions."""
        heartbeat = snap.get("heartbeat") or {}
        self._last_heartbeat = heartbeat if isinstance(heartbeat, dict) else {}
        acct_err = snap.get("acct_err")
        positions_df = snap.get("positions_df")
        if positions_df is None or getattr(positions_df, "empty", True):
            # Keep last known holdings so Open P&L doesn't blank between refreshes.
            if self._last_positions_df is not None and not getattr(
                self._last_positions_df, "empty", True
            ):
                positions_df = self._last_positions_df
        pos_err = snap.get("pos_err")
        equity = float(snap.get("equity") or 0)
        cash = float(snap.get("cash") or 0)
        running = bool(snap.get("running"))

        self._last_equity = equity
        self._last_cash_pct = (cash / equity * 100) if equity > 0 else None
        if equity > 0:
            config.configure_account_profile(equity)

        self._update_live_equity_header(equity, acct_err, paper=snap.get("book_paper"))
        self._update_stats_banner(equity, heartbeat, acct_err)
        self._update_sleeve_pnl_panel(snap)
        self._update_small_panel(equity, heartbeat)

        cash_pct = (cash / equity * 100) if equity > 0 else 0.0
        invested = self._invested_pct(heartbeat, equity, cash)
        upl, upl_pct, top_hold = _open_pnl_from_positions(positions_df)
        if abs(upl) < 1e-9 and not top_hold:
            # Empty positions fetch = flat book; do not show stale heartbeat sleeve marks.
            if positions_df is None or getattr(positions_df, "empty", True):
                if snap.get("positions_fetched"):
                    upl, upl_pct, top_hold = 0.0, 0.0, ""
                else:
                    upl, upl_pct, top_hold = _open_pnl_from_heartbeat(heartbeat)
            else:
                upl, upl_pct, top_hold = _open_pnl_from_heartbeat(heartbeat)

        self._metric_cards["equity"].set(f"${equity:,.2f}")
        self._metric_cards["cash"].set(
            f"${cash:,.0f} ({cash_pct:.0f}%)" if equity > 0 else "—"
        )
        self._metric_cards["invested"].set(f"{invested:.1f}%")
        if abs(upl) < 1e-9 and not top_hold:
            self._metric_cards["pnl"].set("—", color=COLORS["muted"])
        else:
            # Keep hero text short so it always fits; top holding is optional.
            pnl_txt = f"${upl:+,.0f} ({upl_pct:+.2f}%)"
            if top_hold and top_hold != "from heartbeat" and len(top_hold) <= 14:
                pnl_txt = f"{pnl_txt} · {top_hold}"
            self._metric_cards["pnl"].set(
                pnl_txt,
                color=COLORS["green"] if upl >= 0 else COLORS["red"],
            )
        day_pnl, day_pct = _day_pnl_from_session_open(
            equity,
            heartbeat,
            paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
            last_equity=snap.get("last_equity"),
            book_id=self._book_id,
            username=self._username,
        )
        if day_pnl is None or day_pct is None:
            self._metric_cards["today"].set("—", color=COLORS["muted"])
        else:
            self._metric_cards["today"].set(
                _format_day_pnl(day_pnl, day_pct),
                color=COLORS["green"] if day_pnl >= 0 else COLORS["red"],
            )
        self._metric_cards["market"].set(_market_open_countdown(heartbeat))

        small_acct = equity > 0 and config.is_small_account(equity)
        regime = "—"
        if heartbeat:
            regime = str(heartbeat.get("regime") or heartbeat.get("wisdom_regime") or "—")
        self._update_status_row(
            equity,
            small_acct,
            regime=regime,
            halted=bool(heartbeat.get("halted")),
            bot_running_flag=running,
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
            heartbeat=heartbeat,
            snap=snap,
        )

        bot_label = snap.get("bot_label")
        if not bot_label:
            bot_label = (
                f"Bot: Running · {snap.get('book_label', book_label(self._book_id))} "
                f"({'paper' if snap.get('book_paper') else 'live'})"
                if running
                else "Bot: Stopped"
            )
        self._bot_badge.configure(
            text=str(bot_label),
            text_color=COLORS["green"] if running else COLORS["amber"],
        )

        self._update_live_status_panel(snap, heartbeat, running=running)
        # Realized-by-ticker for Positions "Total $" (realized + open). Keep last on fast refresh.
        journal_df = snap.get("journal_df")
        if journal_df is not None and not getattr(journal_df, "empty", True):
            try:
                self._last_realized_by_ticker = _realized_pnl_by_ticker(journal_df)
            except Exception:
                pass
        self._fill_positions(
            positions_df,
            pos_err,
            upl,
            realized_by_ticker=getattr(self, "_last_realized_by_ticker", None) or {},
        )
        self._fill_trades(snap.get("journal_df"))

        if snap.get("sparkline_df") is not None:
            self._last_sparkline_df = snap.get("sparkline_df")
        if snap.get("chart_equity_df") is not None:
            self._last_chart_equity_df = snap.get("chart_equity_df")
        if ENABLE_SPARKLINE:
            self._draw_sparkline(df=self._last_sparkline_df)

    def _apply_refresh_extended(self, snap: dict) -> None:
        """Slower panels: overview extras, scanners, health."""
        heartbeat = snap.get("heartbeat")
        acct_err = snap.get("acct_err")
        equity = float(snap.get("equity") or 0)
        running = bool(snap.get("running"))

        self._fill_overview(heartbeat, equity, acct_err, snap=snap)
        self._update_health_pill(
            snap.get("bot_health"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        self._update_daily_bank_pill(
            heartbeat,
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        self._update_thinking_pill(snap.get("thinking_snap"), snap.get("thinking_err"))
        self._update_heartbeat_pill(
            heartbeat,
            running=running,
            stale=bool(snap.get("heartbeat_stale")),
        )
        self._update_conviction_pill(
            snap.get("conviction_snap"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        self._fill_crypto_vol_panel()
        self._fill_insider_signals(snap.get("insider_rows"), snap.get("insider_err"))
        self._fill_rvol_stocks(
            snap.get("rvol_rows"),
            snap.get("rvol_err"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        self._fill_orb_momentum(
            snap.get("orb_mom_rows"),
            snap.get("orb_mom_err"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        self._fill_sector_rotation(
            snap.get("sector_rot_rows"),
            snap.get("sector_rot_err"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        self._fill_vol_breakout(
            snap.get("vol_bo_rows"),
            snap.get("vol_bo_err"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        self._fill_strategy_performance(
            snap.get("strategy_rows"),
            snap.get("strategy_err"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
            mtf_summary=snap.get("strategy_mtf"),
            exit_rows=snap.get("exit_rows"),
        )
        self._fill_sharpe_history(
            snap.get("sharpe_hist"),
            snap.get("sharpe_hist_err"),
        )
        self._fill_short_activity(
            snap.get("short_rows"),
            snap.get("short_err"),
            snap.get("short_snap"),
            book_paper=bool(snap.get("book_paper", _book_is_paper(self._book_id))),
        )
        # Wisdom filled in _apply_refresh_snapshot (fast + full) so the tab is not empty.

    def _apply_refresh_snapshot(self, snap: dict, *, include_charts: bool = False) -> None:
        self._apply_refresh_core(snap)
        # Wisdom cards/table are light; chart redraw is gated inside _fill_wisdom.
        self._fill_wisdom(
            snap.get("scorecard"),
            snap.get("scorecard_src") or "",
            snap.get("heartbeat"),
        )
        if not snap.get("fast"):
            self._apply_refresh_extended(snap)
        else:
            equity = float(snap.get("equity") or 0)
            heartbeat = snap.get("heartbeat")
            acct_err = snap.get("acct_err")
            running = bool(snap.get("running"))
            self._fill_overview(heartbeat, equity, acct_err, snap=snap)
            self._update_heartbeat_pill(
                heartbeat,
                running=running,
                stale=bool(snap.get("heartbeat_stale")),
            )

        if include_charts:
            self._draw_charts()
            self._charts_dirty = False
        elif getattr(self, "_active_tab", "") == "Charts":
            self._charts_dirty = True

    def _fill_crypto_vol_panel(self) -> None:
        try:
            crypto_on = bool(config.effective_crypto_enabled())
        except Exception:
            crypto_on = False
        if not crypto_on:
            if hasattr(self, "_crypto_vol_heading"):
                self._crypto_vol_heading.pack_forget()
            if hasattr(self, "_crypto_vol_panel"):
                self._crypto_vol_panel.pack_forget()
            return
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
        self,
        heartbeat: dict | None,
        equity: float,
        acct_err: str | None,
        *,
        snap: dict | None = None,
    ) -> None:
        last_trade = _format_last_trade(snap)
        if acct_err:
            acct_err = _compact_io_error(acct_err)
            self._overview_last_trade.configure(
                text=f"Last trade: {last_trade}",
                text_color=COLORS["muted"],
            )
            self._overview_next_action.configure(
                text="Next expected: fix Alpaca connection, then Start Bot",
                text_color=COLORS["red"],
            )
            self._update_status_row(
                equity,
                equity > 0 and config.is_small_account(equity),
                regime="Alpaca error",
                halted=True,
                bot_running_flag=_book_running_status(self._username, self._book_id),
                book_paper=_book_is_paper(self._book_id),
                heartbeat=heartbeat,
                snap=snap,
            )
            self._pill_regime.configure(
                text=f"Alpaca: {acct_err[:40]}",
                text_color=COLORS["red"],
            )
            self._clear_frame(self._actions_scroll)
            self._live_status_panel.set_text(
                f"Alpaca error:\n{acct_err}",
                text_color=COLORS["red"],
            )
            self._wisdom_line.configure(text="—")
            return
        if heartbeat is None:
            self._overview_last_trade.configure(
                text=f"Last trade: {last_trade}",
                text_color=COLORS["muted"],
            )
            self._overview_next_action.configure(
                text="Next expected: Start Bot (first heartbeat ~60s)",
                text_color=COLORS["amber"],
            )
            running = _book_running_status(self._username, self._book_id)
            self._update_status_row(
                equity,
                equity > 0 and config.is_small_account(equity),
                regime="Waiting…" if running else "No heartbeat",
                bot_running_flag=running,
                book_paper=_book_is_paper(self._book_id),
                heartbeat=heartbeat,
                snap=snap,
            )
            self._clear_frame(self._actions_scroll)
            btn_row = ctk.CTkFrame(self._actions_scroll, fg_color="transparent")
            btn_row.pack(fill="x", pady=4, padx=8)
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
                tail = read_bot_log_tail(self._username, self._book_id, max_chars=4000)
                if tail:
                    log_panel = ScrollTextPanel(
                        self._actions_scroll,
                        height=140,
                        font_key="caption",
                        text_color=COLORS["muted"],
                    )
                    log_panel.pack(fill="x", padx=8, pady=(8, 8))
                    log_panel.set_text(f"Bot log tail:\n\n{tail}")
            self.after(50, lambda: self._scroll_frame_to_bottom(self._actions_scroll))
            self._wisdom_line.configure(text="—")
            return

        actions = _expected_actions(heartbeat)
        self._overview_last_trade.configure(
            text=f"Last trade: {last_trade}",
            text_color=COLORS["text"],
        )
        self._overview_next_action.configure(
            text=f"Next expected: {actions[0] if actions else 'Monitoring — no immediate actions.'}",
            text_color=COLORS["amber"],
        )

        regime = heartbeat.get("regime", "—")
        halted = bool(heartbeat.get("halted"))
        phase = _scan_phase_label(heartbeat)
        if phase and not (heartbeat.get("scan_schedule") or {}).get("market_open"):
            regime = f"{regime} · {phase}"
        self._update_status_row(
            equity,
            equity > 0 and config.is_small_account(equity),
            regime=str(regime),
            halted=halted,
            bot_running_flag=_book_running_status(self._username, self._book_id),
            book_paper=_book_is_paper(self._book_id),
            heartbeat=heartbeat,
            snap=snap,
        )
        self._update_entry_gates_pill(heartbeat)

        self._clear_frame(self._actions_scroll)
        cycle_err = heartbeat.get("last_cycle_error")
        if cycle_err:
            err_panel = ScrollTextPanel(
                self._actions_scroll,
                height=120,
                font_key="body_sm",
                text_color=COLORS["red"],
            )
            err_panel.pack(fill="x", padx=8, pady=(8, 4))
            err_at = heartbeat.get("last_cycle_error_at") or ""
            err_panel.set_text(f"Last cycle error ({err_at}):\n\n{cycle_err}")
        for action in actions[1:]:
            ctk.CTkLabel(
                self._actions_scroll,
                text=f"• {action}",
                anchor="w",
                justify="left",
                text_color=COLORS["text"],
                font=_ctk_font("body_sm"),
                wraplength=760,
            ).pack(fill="x", padx=10, pady=2)
        self.after(50, lambda: self._scroll_frame_to_bottom(self._actions_scroll))

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
        self,
        positions_df: pd.DataFrame,
        *,
        realized_by_ticker: dict[str, float] | None = None,
    ) -> list[dict]:
        rows = []
        realized_map = realized_by_ticker or {}
        for _, r in positions_df.iterrows():
            try:
                qty = float(r.get("Qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                entry = float(r.get("Entry", 0) or 0)
            except (TypeError, ValueError):
                entry = 0.0
            if entry != entry:  # NaN
                entry = 0.0
            try:
                current = float(r.get("Current", 0) or 0)
            except (TypeError, ValueError):
                current = 0.0
            if current != current:
                current = 0.0
            try:
                cost = float(r.get("Cost $", 0) or 0)
            except (TypeError, ValueError):
                cost = 0.0
            if not cost and qty and entry:
                cost = abs(qty) * entry
            # Always prefer a usable per-share buy price for the Buy $ column.
            if (not entry or entry <= 0) and cost and qty:
                try:
                    entry = abs(float(cost)) / abs(float(qty))
                except ZeroDivisionError:
                    entry = 0.0
            try:
                market_value = float(r.get("Value $", qty * current))
            except (TypeError, ValueError):
                market_value = qty * current if qty and current else 0.0
            if market_value != market_value:
                market_value = 0.0
            try:
                pnl = float(r.get("P&L $", 0) or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            try:
                pnl_pct = float(r.get("P&L %", 0) or 0)
            except (TypeError, ValueError):
                pnl_pct = 0.0
            # Prefer live mark; fall back to Value$/qty so Current $ never blanks.
            if (not current or current <= 0) and market_value and qty:
                try:
                    current = abs(float(market_value)) / abs(float(qty))
                except ZeroDivisionError:
                    current = 0.0
            ticker = str(r.get("Ticker") or "?")
            sym = config.normalize_symbol(ticker)
            try:
                realized = float(realized_map.get(sym) or realized_map.get(ticker) or 0)
            except (TypeError, ValueError):
                realized = 0.0
            # Prefer explicit Total $ from frame if present; else realized + open.
            try:
                total = float(r.get("Total $")) if "Total $" in positions_df.columns else None
            except (TypeError, ValueError):
                total = None
            if total is None or total != total:
                total = realized + pnl
            buy_txt = f"${entry:,.2f}" if entry > 0 else "—"
            cur_txt = f"${current:,.2f}" if current > 0 else "—"
            rows.append(
                {
                    "Ticker": ticker,
                    "Sleeve": r.get("Sleeve", ""),
                    "First fill": r.get("First fill") or r.get("Opened") or "—",
                    "Qty": f"{qty:.4f}",
                    "Buy $": buy_txt,
                    "Current $": cur_txt,
                    "Entry": buy_txt,  # keep alias for any older callers
                    "Cost $": f"${cost:,.2f}",
                    "Value $": f"${market_value:,.2f}",
                    "P&L $": f"${pnl:+,.2f}",
                    "P&L %": f"{pnl_pct:+.2f}%",
                    "Total $": f"${total:+,.2f}",
                    "ATR Stop": r.get("ATR Stop", "—"),
                    "_qty": qty,
                    "_entry": entry,
                    "_current": current,
                    "_cost": cost,
                    "_value": market_value,
                    "_pnl": pnl,
                    "_pnl_pct": pnl_pct,
                    "_realized": realized,
                    "_total": total,
                }
            )
        return rows

    def _set_positions_loading(self, *, clear_table: bool = True) -> None:
        """Show a loading placeholder on the Positions tab while refresh runs."""
        try:
            self._positions_empty_label.place_forget()
            if clear_table:
                self._positions_table.clear()
            self._pos_total.configure(text="Loading positions…", text_color=COLORS["amber"])
            if clear_table:
                self._positions_empty_label.configure(
                    text="Loading positions…",
                    text_color=COLORS["muted"],
                )
                self._positions_empty_label.place(relx=0.5, rely=0.45, anchor="center")
        except Exception:
            pass

    def _positions_lot_line(self, work_df, total_upl: float) -> tuple[str, str]:
        n = 0
        nyse = 0
        if work_df is not None and not getattr(work_df, "empty", True):
            n = int(len(work_df))
            sleeves = work_df["Sleeve"].astype(str) if "Sleeve" in work_df.columns else None
            if sleeves is not None:
                nyse = int((sleeves == "NYSE").sum())
        leftover = max(0, n - nyse)
        pct = getattr(self, "_last_cash_pct", None)
        cash_s = f"{pct:.0f}%" if isinstance(pct, (int, float)) else "n/a"
        line = (
            f"Alpaca lots: {n}  ·  NYSE active: {nyse}  ·  leftover: {leftover}  ·  cash {cash_s}"
        )
        color = COLORS["green"] if total_upl >= 0 else COLORS["red"]
        return line, color

    def _fill_positions(
        self,
        positions_df: pd.DataFrame | None,
        pos_err: str | None,
        total_upl: float,
        *,
        realized_by_ticker: dict[str, float] | None = None,
    ) -> None:
        try:
            realized_map = dict(realized_by_ticker or {})
            work_df = positions_df
            if work_df is not None and not getattr(work_df, "empty", True):
                work_df = work_df.copy()
                totals: list[float] = []
                for _, r in work_df.iterrows():
                    sym = config.normalize_symbol(str(r.get("Ticker") or ""))
                    try:
                        upl = float(r.get("P&L $") or 0)
                    except (TypeError, ValueError):
                        upl = 0.0
                    realized = float(realized_map.get(sym) or 0.0)
                    totals.append(realized + upl)
                work_df["Total $"] = totals

            fp = _positions_fingerprint(work_df, pos_err)
            if (
                fp is not None
                and fp == getattr(self, "_last_positions_fp", None)
                and not pos_err
            ):
                # Data unchanged — skip tree rebuild; keep total text in sync.
                line, color = self._positions_lot_line(work_df, total_upl)
                self._pos_total.configure(text=line, text_color=color)
                return

            self._positions_empty_label.place_forget()
            if pos_err:
                self._last_positions_df = None
                self._last_positions_fp = fp
                self._positions_table.clear()
                self._pos_total.configure(text=_compact_io_error(pos_err), text_color=COLORS["red"])
                self._positions_empty_label.configure(
                    text=f"Could not load positions\n{_compact_io_error(pos_err)[:120]}",
                    text_color=COLORS["red"],
                )
                self._positions_empty_label.place(relx=0.5, rely=0.45, anchor="center")
                return
            if work_df is None or getattr(work_df, "empty", True):
                self._last_positions_df = None
                self._last_positions_fp = fp
                self._positions_table.clear()
                line, color = self._positions_lot_line(work_df, total_upl)
                self._pos_total.configure(text=line, text_color=COLORS["muted"])
                self._positions_empty_label.configure(
                    text="No open positions\nCash idle until the next rebalance cycle.",
                    text_color=COLORS["muted"],
                )
                self._positions_empty_label.place(relx=0.5, rely=0.45, anchor="center")
                return
            self._last_positions_df = work_df.copy()
            rows = self._position_rows(
                work_df, realized_by_ticker=realized_map
            )
            self._positions_table.set_rows(rows, pnl_col="_pnl")
            self._last_positions_fp = fp
            line, color = self._positions_lot_line(work_df, total_upl)
            self._pos_total.configure(text=line, text_color=color)
        except Exception as exc:  # noqa: BLE001
            self._last_positions_df = None
            self._last_positions_fp = None
            try:
                self._positions_table.clear()
            except Exception:
                pass
            msg = f"Positions display error: {_compact_io_error(exc)}"
            try:
                self._pos_total.configure(text=msg[:80], text_color=COLORS["red"])
                self._positions_empty_label.configure(
                    text=f"Could not render positions\n{str(exc)[:120]}",
                    text_color=COLORS["red"],
                )
                self._positions_empty_label.place(relx=0.5, rely=0.45, anchor="center")
            except Exception:
                pass

    def _fill_trades(self, journal_df: pd.DataFrame | None) -> None:
        """Activities: portal event=fill rows for this book (recon reader)."""
        path = book_journal_path(self._username, self._book_id)
        raw = None
        fills = None
        try:
            raw = _read_trade_journal_csv(path)
            if raw is not None and not raw.empty and "event" in raw.columns:
                ev = raw["event"].astype(str).str.lower()
                fills = raw.loc[ev == "fill"].copy()
        except Exception as exc:
            self._trades_table.clear()
            self._trades_tab_hint.configure(
                text=_compact_io_error(exc),
                text_color=COLORS["red"],
            )
            return
        if fills is None or fills.empty:
            n_all = 0
            try:
                n_all = 0 if raw is None or raw.empty else int((raw["event"].astype(str).str.lower() == "fill").sum())
            except Exception:
                n_all = 0
            self._trades_table.clear()
            if n_all == 0:
                self._trades_tab_hint.configure(
                    text=f"No portal fills · journal: {path.name}",
                    text_color=COLORS["muted"],
                )
            else:
                self._trades_tab_hint.configure(
                    text=f"{n_all} fill(s) in journal but none parsed for display",
                    text_color=COLORS["amber"],
                )
            return

        fills["timestamp"] = pd.to_datetime(fills["timestamp"], errors="coerce")
        fills = fills.dropna(subset=["timestamp"]).sort_values("timestamp", ascending=False)
        fills = fills.head(TRADES_LIMIT)

        def _fmt_ts(ts) -> str:
            if hasattr(ts, "strftime"):
                return ts.strftime("%Y-%m-%d %H:%M")
            s = str(ts or "")
            return s[:16].replace("T", " ") if s else "—"

        rows = []
        for rec in fills.to_dict(orient="records"):
            sym = config.normalize_symbol(str(rec.get("symbol") or ""))
            qty = rec.get("qty")
            try:
                qty_f = float(qty) if qty not in (None, "") else 0.0
            except (TypeError, ValueError):
                qty_f = 0.0
            notional = rec.get("notional")
            try:
                not_f = float(notional) if notional not in (None, "") else 0.0
            except (TypeError, ValueError):
                not_f = 0.0
            pnl_raw = rec.get("realized_pnl")
            try:
                pnl_f = float(pnl_raw) if pnl_raw not in (None, "") else 0.0
            except (TypeError, ValueError):
                pnl_f = 0.0
            rows.append(
                {
                    "Time": _fmt_ts(rec.get("timestamp")),
                    "Ticker": sym or "—",
                    "Side": str(rec.get("side") or "—"),
                    "Qty": f"{qty_f:.4g}" if qty_f else "—",
                    "Notional": f"${not_f:,.0f}" if not_f else "—",
                    "P&L $": f"${pnl_f:+,.2f}" if pnl_raw not in (None, "") else "—",
                    "Sleeve": _infer_sleeve(sym),
                    "_pnl": pnl_f,
                }
            )
        self._trades_table.set_rows(rows, pnl_col="_pnl")
        self._trades_tab_hint.configure(
            text=f"{len(rows)} portal fill(s) · {path.name} · newest first",
            text_color=COLORS["text_dim"],
        )

    def _clear_wisdom_chart(self) -> None:
        frame = getattr(self, "_wisdom_chart_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        self._wisdom_chart_canvas = None

    def _draw_wisdom_compare_chart(self, scorecard: dict) -> None:
        """Left: real vs backtest equity (indexed 100). Right: return bars Live vs sims.

        UI-thread only — never load/parse journals here (that froze the dashboard).
        """
        self._clear_wisdom_chart()
        frame = getattr(self, "_wisdom_chart_frame", None)
        if frame is None:
            return
        live = scorecard.get("live") or {}
        sim_modes = scorecard.get("simulated_modes") or {}
        best = str(scorecard.get("best_sim_mode") or "")
        active = str(live.get("mode") or "")
        live_ret = float(live.get("return_pct") or 0)

        labels = ["LIVE"]
        returns = [live_ret]
        colors = [COLORS["green"] if live_ret >= 0 else COLORS["red"]]
        for mode, stats in sim_modes.items():
            if "return_pct" not in stats or "error" in stats:
                continue
            labels.append(str(mode))
            returns.append(float(stats.get("return_pct") or 0))
            if mode == best:
                colors.append(COLORS["amber"])
            elif mode == active:
                colors.append(COLORS["blue"])
            else:
                colors.append(COLORS["muted"])

        fig = Figure(figsize=(9.2, 2.35), dpi=CHART_DPI, facecolor=COLORS["card"])
        ax_line = fig.add_subplot(121)
        ax_bar = fig.add_subplot(122)
        for ax in (ax_line, ax_bar):
            ax.set_facecolor(COLORS["card"])
            ax.tick_params(colors=COLORS["muted"], labelsize=7)
            for spine in ax.spines.values():
                spine.set_color(COLORS["chart_grid"])

        curves = scorecard.get("comparison_curves") or {}
        live_curve = (live.get("curve_norm") or curves.get("live") or {})
        plotted = False
        if live_curve.get("values"):
            y = list(live_curve["values"])
            ax_line.plot(range(len(y)), y, color=COLORS["green"], linewidth=2.0, label="Real (live)")
            plotted = True
        for key, curve in curves.items():
            if key == "live" or not isinstance(curve, dict):
                continue
            vals = curve.get("values") or []
            if len(vals) < 2:
                continue
            label = key.replace("sim:", "BT ")
            color = COLORS["amber"] if best and best in key else COLORS["blue"]
            ax_line.plot(range(len(vals)), vals, color=color, linewidth=1.6, label=label)
            plotted = True
        if plotted:
            ax_line.axhline(100.0, color=COLORS["chart_grid"], linewidth=0.8, linestyle="--")
            ax_line.set_title("Equity indexed to 100", color=COLORS["text"], fontsize=9, pad=4)
            ax_line.legend(loc="best", fontsize=7, frameon=False, labelcolor=COLORS["text_dim"])
            ax_line.set_xticks([])
        else:
            # No stored curves yet — end-level compare only (keeps UI snappy).
            best_ret = float((sim_modes.get(best) or {}).get("return_pct") or 0)
            ax_line.bar(
                [0, 1],
                [100.0 + live_ret, 100.0 + best_ret],
                color=[COLORS["green"], COLORS["amber"]],
                width=0.55,
            )
            ax_line.set_xticks([0, 1])
            ax_line.set_xticklabels(
                ["Real end", f"Best BT\n({best or '—'})"],
                fontsize=7,
                color=COLORS["muted"],
            )
            ax_line.axhline(100.0, color=COLORS["chart_grid"], linewidth=0.8, linestyle="--")
            ax_line.set_title("End level (100 = start)", color=COLORS["text"], fontsize=9, pad=4)

        x = range(len(labels))
        ax_bar.bar(x, returns, color=colors, width=0.7)
        ax_bar.axhline(0.0, color=COLORS["chart_grid"], linewidth=0.8)
        ax_bar.set_xticks(list(x))
        ax_bar.set_xticklabels(labels, rotation=35, ha="right", fontsize=7, color=COLORS["muted"])
        ax_bar.set_title("Return % — Real vs backtest modes", color=COLORS["text"], fontsize=9, pad=4)
        fig.subplots_adjust(left=0.07, right=0.98, top=0.86, bottom=0.28, wspace=0.28)
        canvas = FigureCanvasTkAgg(fig, master=frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)
        plt.close(fig)
        self._wisdom_chart_canvas = canvas

    def _fill_wisdom(
        self,
        scorecard: dict | None,
        scorecard_src: str = "",
        heartbeat: dict | None = None,
    ) -> None:
        # Avoid redrawing matplotlib on every 45s refresh unless data/tab needs it.
        sc_fp = None
        if scorecard is not None:
            sc_fp = (
                scorecard.get("evaluated_at"),
                scorecard.get("best_sim_mode"),
                (scorecard.get("live") or {}).get("return_pct"),
                (scorecard.get("live") or {}).get("sharpe"),
                scorecard.get("live_vs_best_sim_return_pp"),
            )
        on_wisdom = getattr(self, "_active_tab", "") == "Wisdom"
        same_scorecard = sc_fp is not None and sc_fp == getattr(self, "_last_wisdom_fp", None)
        if scorecard is None:
            self._clear_wisdom_chart()
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
        vs_sim = scorecard.get("live_vs_best_sim_return_pp")
        if vs_sim is None:
            vs_sim = scorecard.get("live_vs_active_sim_return_pp")
        vs_val = float(vs_sim or 0)
        best = str(scorecard.get("best_sim_mode") or "")
        active = str(live.get("mode") or "")

        self._w_sharpe.set(
            f"{sharpe:.2f}", color=COLORS["green"] if sharpe >= 0 else COLORS["red"]
        )
        self._w_ret.set(f"{ret:+.2f}%", color=COLORS["green"] if ret >= 0 else COLORS["red"])
        self._w_vs.set(
            f"{vs_val:+.2f} pp" + (f" ({best})" if best else ""),
            color=COLORS["green"] if vs_val >= 0 else COLORS["red"],
        )
        rec = scorecard.get("recommendation") or ""
        ev = scorecard.get("evaluated_at", "—")
        self._wisdom_rec.configure(text=f"{rec}  (evaluated {ev})")

        sim_modes = scorecard.get("simulated_modes") or {}
        rows: list[dict] = [
            {
                "Source": f"LIVE · {active or '—'}",
                "Return%": f"{ret:+.2f}",
                "Sharpe": f"{sharpe:.3f}",
                "ΔRet vs Live": "—",
                "ΔSharpe": "—",
                "Notes": f"{live.get('from_date', '?')} → {live.get('to_date', '?')}",
                "_tag": "profit" if ret >= 0 else "loss",
            }
        ]
        distinct_sharpes = {round(float(s.get("sharpe") or 0), 3) for s in sim_modes.values() if "return_pct" in s}
        for mode, stats in sorted(
            sim_modes.items(),
            key=lambda kv: float((kv[1] or {}).get("return_pct") or -999),
            reverse=True,
        ):
            if "return_pct" not in stats or "error" in stats:
                continue
            s_ret = float(stats.get("return_pct") or 0)
            s_sh = float(stats.get("sharpe") or 0)
            tags = []
            if mode == best:
                tags.append("best BT")
            if mode == active:
                tags.append("active mode")
            paused = int(stats.get("paused_days") or 0)
            metal = int(stats.get("metal_trades") or 0)
            orders = int(stats.get("orders") or 0)
            note_bits = []
            if tags:
                note_bits.append(", ".join(tags))
            if orders:
                note_bits.append(f"{orders} orders")
            if paused:
                note_bits.append(f"{paused}d paused")
            if metal:
                note_bits.append(f"{metal} metal")
            rows.append(
                {
                    "Source": f"BT · {mode}",
                    "Return%": f"{s_ret:+.2f}",
                    "Sharpe": f"{s_sh:.3f}",
                    "ΔRet vs Live": f"{s_ret - ret:+.2f}",
                    "ΔSharpe": f"{s_sh - sharpe:+.3f}",
                    "Notes": " · ".join(note_bits) or "—",
                    "_tag": "profit" if s_ret >= ret else "loss",
                }
            )

        self._wisdom_table.set_rows(rows, tag_col="_tag")
        need_chart = (not same_scorecard) or (
            on_wisdom and getattr(self, "_wisdom_chart_canvas", None) is None
        )
        if need_chart:
            try:
                self._draw_wisdom_compare_chart(scorecard)
                self._last_wisdom_fp = sc_fp
            except Exception:  # noqa: BLE001
                self._clear_wisdom_chart()
        else:
            self._last_wisdom_fp = sc_fp

        src_note = (
            "per-book scorecard"
            if scorecard_src == "book"
            else "project scorecard (live segment)"
            if scorecard_src == "project"
            else ""
        )
        window = scorecard.get("window_days", "—")
        cluster_note = ""
        if len(distinct_sharpes) <= 1 and len(sim_modes) > 1:
            cluster_note = " · sim modes nearly identical (compare LIVE vs best BT)"
        curve_note = (
            " · equity overlay ready"
            if scorecard.get("comparison_curves") or (live.get("curve_norm") or {}).get("values")
            else " · equity overlay after next wisdom eval"
        )
        self._wisdom_hint.configure(
            text=(
                f"{book_label(self._book_id)} · {window}-day window · {src_note}"
                f"{cluster_note}{curve_note}"
            ).strip(" ·"),
        )

    def _draw_sparkline(self, df: pd.DataFrame | None = None) -> None:
        """Draw from worker-preloaded equity only — never read journal on UI thread."""
        if df is None:
            df = self._last_sparkline_df
        if df is None or len(df) < 2:
            if getattr(self, "_last_sparkline_fp", None) == ("empty",):
                return
            self._last_sparkline_fp = ("empty",)
            self._clear_frame(self._spark_frame)
            ctk.CTkLabel(
                self._spark_frame, text="—", text_color=COLORS["muted"], font=ctk.CTkFont(size=11)
            ).pack(expand=True)
            return
        try:
            y0 = float(df["equity"].iloc[0])
            y1 = float(df["equity"].iloc[-1])
            n = int(len(df))
            fp = (n, round(y0, 4), round(y1, 4))
        except Exception:
            fp = None
        if fp is not None and fp == getattr(self, "_last_sparkline_fp", None):
            return
        self._last_sparkline_fp = fp
        self._clear_frame(self._spark_frame)
        start, end = float(df["equity"].iloc[0]), float(df["equity"].iloc[-1])
        color = COLORS["green"] if end >= start else COLORS["red"]
        fig = Figure(figsize=(1.8, 0.28), dpi=CHART_DPI, facecolor=COLORS["card"])
        ax = fig.add_subplot(111)
        ax.set_facecolor(COLORS["card"])
        y = df["equity"].values
        ax.plot(range(len(y)), y, color=color, linewidth=1.4)
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
        book_paper = _book_is_paper(self._book_id)
        range_key = str(
            getattr(self, "_chart_range", None)
            or (self._chart_range_var.get() if hasattr(self, "_chart_range_var") else "5D")
            or "5D"
        ).upper()
        if range_key not in CHART_RANGE_SPECS:
            range_key = "5D"
        self._chart_range = range_key
        panels = _charts_panel_specs(
            positions_df=self._last_positions_df,
            book_paper=book_paper,
        )
        source_hint = "5m bars" if CHART_RANGE_SPECS[range_key]["prefer"] == "5m" else "daily bars"
        self._charts_hint.configure(
            text=(
                f"{range_key} · {source_hint} · "
                + " · ".join(str(p.get("title") or p.get("symbol") or "") for p in panels)
            )
        )

        grid = ctk.CTkFrame(self._charts_frame, fg_color="transparent")
        grid.pack(fill="both", expand=True)
        for r in range(2):
            grid.grid_rowconfigure(r, weight=1)
        for c in range(3):
            grid.grid_columnconfigure(c, weight=1)

        for idx, spec in enumerate(panels):
            row_i, col_i = divmod(idx, 3)
            cell = ctk.CTkFrame(
                grid,
                fg_color=COLORS["card"],
                corner_radius=12,
                border_width=1,
                border_color=COLORS["border"],
            )
            cell.grid(row=row_i, column=col_i, padx=4, pady=4, sticky="nsew")

            kind = spec.get("kind")
            color = spec.get("color", COLORS["accent"])
            plot_df: pd.DataFrame | None = None

            if kind == "equity":
                # Worker-preloaded journal only (never read CSV on UI thread).
                eq_df = self._last_chart_equity_df
                if eq_df is None or getattr(eq_df, "empty", True):
                    eq_df = self._last_sparkline_df
                plot_df = _slice_equity_for_chart_range(eq_df, range_key)
                if plot_df is None or plot_df.empty:
                    ctk.CTkLabel(
                        cell,
                        text="No account history",
                        text_color=COLORS["muted"],
                    ).pack(pady=30)
                    continue
                title = _pct_change_title(
                    f"{spec.get('title') or 'Your account'} ({range_key})", plot_df
                )
            else:
                symbol = str(spec.get("symbol") or "")
                plot_df = _load_chart_closes(symbol, range_key)
                if plot_df is None or plot_df.empty:
                    ctk.CTkLabel(
                        cell,
                        text=f"No {range_key} data for {symbol}",
                        text_color=COLORS["muted"],
                    ).pack(pady=30)
                    continue
                title = _pct_change_title(
                    f"{spec.get('title') or symbol} ({range_key})", plot_df
                )

            fig = _light_line_chart(
                plot_df,
                title=title,
                color=color,
                show_axis=True,
                height=1.45,
            )
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

    def _position_qty(self, ticker: str) -> float | None:
        if self._last_positions_df is None or self._last_positions_df.empty:
            return None
        match = self._last_positions_df[self._last_positions_df["Ticker"] == ticker]
        if match.empty:
            return None
        return float(match.iloc[0]["Qty"])

    def _sell_confirm_message(self, *, action: str, detail: str) -> str:
        msg = f"{action}\n\n{detail}"
        if self._book_id == "alpaca_live":
            msg += (
                "\n\n⚠ LIVE ACCOUNT — REAL MONEY.\n"
                "This order executes immediately at market price."
            )
        return msg

    def _on_sell_selected_position(self) -> None:
        if not has_alpaca_config(self._username, self._book_id):
            messagebox.showwarning(
                "API keys",
                f"Add API keys for {book_label(self._book_id)} first (☰ menu).",
            )
            return
        row = self._positions_table.selected_row()
        if not row:
            messagebox.showwarning("Sell Position", "Select a position row first.")
            return
        ticker = str(row.get("Ticker") or row.get("symbol") or "").strip()
        if not ticker:
            messagebox.showwarning("Sell Position", "Could not read ticker from selection.")
            return
        qty = self._position_qty(ticker)
        if qty is None:
            messagebox.showwarning(
                "Sell Position",
                f"Could not find quantity for {ticker}. Refresh and try again.",
            )
            return
        qty_label = f"{qty:.4f}".rstrip("0").rstrip(".")
        verb = "Cover entire short" if qty < 0 else "Sell entire"
        if not messagebox.askyesno(
            "Sell Position",
            self._sell_confirm_message(
                action=f"{verb} {ticker} position ({qty_label} shares)?",
                detail="Submits a market order to close the full position.",
            ),
            icon="warning",
        ):
            return
        self._pos_total.configure(text=f"Closing {ticker}…", text_color=COLORS["amber"])
        self.update_idletasks()

        def _worker() -> None:
            err: str | None = None
            try:
                executor = _make_book_executor(self._username, self._book_id)
                order = executor.execute_full_exit(
                    ticker,
                    reason="manual_dashboard",
                    sleeve=_infer_sleeve(ticker),
                )
                if order is None:
                    err = (
                        f"Could not submit close for {ticker} "
                        "(no position, below minimum notional, or trading blocked)."
                    )
            except Exception as exc:  # noqa: BLE001
                err = str(exc)

            def _finish() -> None:
                if err:
                    messagebox.showerror("Sell Position", err)
                else:
                    messagebox.showinfo(
                        "Sell Position",
                        f"Close order submitted for {ticker}.",
                    )
                self.refresh_data()

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_sell_all_positions(self) -> None:
        if not has_alpaca_config(self._username, self._book_id):
            messagebox.showwarning(
                "API keys",
                f"Add API keys for {book_label(self._book_id)} first (☰ menu).",
            )
            return
        if self._last_positions_df is None or self._last_positions_df.empty:
            messagebox.showinfo("Sell All", "No open positions.")
            return
        tickers = self._last_positions_df["Ticker"].astype(str).tolist()
        n = len(tickers)
        if not messagebox.askyesno(
            "Sell All",
            self._sell_confirm_message(
                action=f"Close all {n} open position(s)?",
                detail="Submits a market close order for each position.",
            ),
            icon="warning",
        ):
            return
        self._pos_total.configure(text=f"Closing {n} position(s)…", text_color=COLORS["amber"])
        self.update_idletasks()

        def _worker() -> None:
            errors: list[str] = []
            closed = 0
            try:
                executor = _make_book_executor(self._username, self._book_id)
                for ticker in tickers:
                    try:
                        order = executor.execute_full_exit(
                            ticker,
                            reason="manual_dashboard",
                            sleeve=_infer_sleeve(ticker),
                        )
                        if order is None:
                            errors.append(f"{ticker}: order not submitted")
                        else:
                            closed += 1
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{ticker}: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

            def _finish() -> None:
                if errors:
                    detail = "\n".join(errors[:8])
                    if len(errors) > 8:
                        detail += f"\n… and {len(errors) - 8} more"
                    messagebox.showwarning(
                        "Sell All",
                        f"Submitted {closed} of {n} close order(s).\n\n{detail}",
                    )
                else:
                    messagebox.showinfo(
                        "Sell All",
                        f"Submitted close orders for {closed} position(s).",
                    )
                self.refresh_data()

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

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

    def _on_restart_bot(self) -> None:
        """Legacy single-book restart — prefer Restart Both."""
        self._on_restart_both()

    def _on_restart_both(self) -> None:
        if getattr(self, "_bot_restart_busy", False):
            return
        paper_ok = has_alpaca_config(self._username, PRIMARY_PAPER_BOOK_ID)
        live_ok = has_alpaca_config(self._username, "alpaca_live")
        if not paper_ok and not live_ok:
            messagebox.showwarning(
                "API keys",
                "Add API keys for paper and/or live first (☰ menu).",
            )
            return
        self._restart_both_async(confirm=True)

    def _on_refresh_bot(self) -> None:
        if not has_alpaca_config(self._username, self._book_id):
            messagebox.showwarning(
                "API keys",
                f"Add API keys for {book_label(self._book_id)} first (☰ menu).",
            )
            return
        if not messagebox.askyesno(
            "Refresh Bot",
            f"Refresh Bot for {book_label(self._book_id)}?\n\n"
            "This will:\n"
            "  1. Stop the trading loop (open positions stay open)\n"
            "  2. Download fresh daily bars (fetch_data.py --daily)\n"
            "  3. Restart the bot in the correct paper/live mode\n\n"
            "May take several minutes while market data downloads.\n\n"
            "Continue?",
            icon="warning",
        ):
            return
        self._refresh_bot_async(self._book_id)

    def _equity_session_is_open(self) -> bool:
        known = _equity_session_open_from_heartbeat(getattr(self, "_last_heartbeat", None))
        if known is not None:
            return known
        return _et_equity_session_open_guess()

    def _refresh_interval_sec(self) -> int:
        sec = REFRESH_SECONDS if self._equity_session_is_open() else REFRESH_SECONDS_CLOSED
        self._refresh_interval_sec_cached = int(sec)
        return self._refresh_interval_sec_cached

    def _schedule_refresh(self) -> None:
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        delay_ms = max(5, self._refresh_interval_sec()) * 1000
        self._refresh_job = self.after(delay_ms, self._auto_refresh_tick)

    def _auto_refresh_tick(self) -> None:
        self._refresh_auto_cycles += 1
        # Full refresh less often when closed (every ~30 min vs ~4.5 min open).
        every_n = 6 if self._equity_session_is_open() else 2
        full = self._refresh_auto_cycles % every_n == 0
        self.refresh_data(full=full)
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
            self.after(0, self._close_with_bot_reset)

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

    def _close_with_bot_reset(self) -> None:
        """Quit UI after clean-restarting live + paper (env/code update)."""
        if self._shutting_down or getattr(self, "_close_reset_busy", False):
            return
        restart_on_close = bool(
            getattr(config, "DASHBOARD_RESTART_BOTS_ON_CLOSE", True)
        )
        stop_on_close = bool(getattr(config, "DASHBOARD_STOP_BOTS_ON_CLOSE", False))
        if not restart_on_close and not stop_on_close:
            self._shutdown()
            return

        self._close_reset_busy = True
        try:
            self.protocol("WM_DELETE_WINDOW", lambda: None)
        except Exception:
            pass
        status = (
            "Closing: restarting live + paper…"
            if restart_on_close
            else "Closing: stopping portal bots…"
        )
        try:
            self._status_label.configure(text=status)
            self._set_bot_action_buttons_busy(True, status=status)
            self.update_idletasks()
        except Exception:
            pass

        def _worker() -> None:
            ok, msg = True, ""
            try:
                if restart_on_close:
                    from scripts.owner_reset import clean_restart_both_bots

                    ok, msg = clean_restart_both_bots(self._username)
                else:
                    from scripts.owner_reset import stop_both_bots

                    ok, msg = stop_both_bots(self._username)
            except Exception as exc:
                ok, msg = False, str(exc)

            def _finish() -> None:
                self._close_reset_busy = False
                if not ok:
                    try:
                        messagebox.showwarning(
                            "Dashboard close",
                            f"Bot reset had issues (UI will still close):\n\n{msg}",
                        )
                    except Exception:
                        pass
                self._shutdown()

            self.after(0, _finish)

        threading.Thread(
            target=_worker, daemon=True, name="dashboard-close-reset"
        ).start()

    def _on_close(self) -> None:
        # Close always clean-restarts live + paper (env/code update), then exits.
        # Tray "minimize on X" used to skip that and only hide the window — which
        # left bots on stale processes. Use the tray icon Show after Quit only if
        # you relaunch; X / Quit = reset both + close UI.
        self._close_with_bot_reset()


def _hide_venv_stub_parent_window() -> None:
    """Hide empty ghost window from Windows venv pythonw launcher (parent of real UI)."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={os.getpid()}\").ParentProcessId",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        parent_pid = int((out or "").strip())
    except Exception:
        return
    if parent_pid <= 0 or parent_pid == os.getpid():
        return
    try:
        cmd_out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={parent_pid}\").CommandLine",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        if "dashboard_app" not in (cmd_out or ""):
            return
    except Exception:
        return

    user32 = ctypes.windll.user32
    SW_HIDE = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # type: ignore[misc]
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) != parent_pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            user32.ShowWindow(hwnd, SW_HIDE)
        return True

    user32.EnumWindows(_enum, 0)


def _focus_existing_dashboard_window() -> bool:
    """Bring an existing monitor window to the front (best-effort)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    me = os.getpid()
    try:
        from modules.runtime_paths import find_dashboard_monitor_pids, find_dashboard_script_pids

        targets = set(find_dashboard_script_pids(PROJECT_ROOT) + find_dashboard_monitor_pids(PROJECT_ROOT))
    except Exception:
        targets = set()
    targets.discard(me)
    if not targets:
        return False

    found = ctypes.wintypes.HWND(0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # type: ignore[misc]
        nonlocal found
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in targets:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        if "PythonTrading" not in title and "Sign in" not in title:
            return True
        found = hwnd
        return False

    user32.EnumWindows(_enum, 0)
    if not found:
        return False
    SW_RESTORE = 9
    user32.ShowWindow(found, SW_RESTORE)
    user32.SetForegroundWindow(found)
    return True


_DASHBOARD_MUTEX_HANDLE = None


def _acquire_dashboard_singleton() -> object | None:
    """One visible monitor at a time (Windows named mutex). Keep handle alive."""
    if sys.platform != "win32":
        return True
    import ctypes

    global _DASHBOARD_MUTEX_HANDLE  # noqa: PLW0603
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\PythonTradingDashboardSingleton")
    if not handle:
        return True
    ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        _focus_existing_dashboard_window()
        kernel32.CloseHandle(handle)
        return None
    _DASHBOARD_MUTEX_HANDLE = handle
    return handle


def main() -> None:
    parser = argparse.ArgumentParser(description="PythonTrading desktop monitor")
    parser.add_argument(
        "--launch-bot",
        action="store_true",
        help="Start run_all.py after login (uses your portal account's keys)",
    )
    args = parser.parse_args()
    launch_bot_after_login = args.launch_bot

    singleton = _acquire_dashboard_singleton()
    if singleton is None:
        return
    _hide_venv_stub_parent_window()

    # Sequential screens — never nest LoginApp.mainloop inside TradingDashboardApp.mainloop.
    session: dict[str, object] = {"username": None, "screen": "login"}

    def on_login_ok(username: str) -> None:
        session["username"] = username
        session["screen"] = "dashboard"

    def on_logout() -> None:
        session["username"] = None
        session["screen"] = "login"

    while session["screen"] == "login" or session["screen"] == "dashboard":
        if session["screen"] == "login":
            login = LoginApp(on_success=on_login_ok)
            login.mainloop()
            try:
                login.destroy()
            except Exception:
                pass
            if session["screen"] != "dashboard":
                break
            continue

        username = str(session["username"] or "")
        migrate_user_to_books(username)
        book_id = get_last_book_id()
        if launch_bot_after_login and has_alpaca_config(username, book_id):
            ok, msg = start_bot(username, book_id)
            if not ok:
                print(msg)
        try:
            app = TradingDashboardApp(
                username,
                book_id,
                on_logout=on_logout,
                auto_start_bot=launch_bot_after_login,
            )
        except Exception as exc:
            import traceback

            log_dir = PROJECT_ROOT / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            crash_log = log_dir / "dashboard_crash.log"
            crash_log.write_text(
                f"{datetime.now(timezone.utc).isoformat()}Z\n{traceback.format_exc()}",
                encoding="utf-8",
            )
            messagebox.showerror(
                "PythonTrading Monitor",
                f"Could not open dashboard:\n{exc}\n\nSee {crash_log}",
            )
            session["screen"] = "login"
            continue
        app.mainloop()
        try:
            app.destroy()
        except Exception:
            pass
        if session["screen"] == "login":
            continue
        break


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        crash_log = log_dir / "dashboard_crash.log"
        crash_log.write_text(
            f"{datetime.now(timezone.utc).isoformat()}Z\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        try:
            messagebox.showerror("PythonTrading Monitor", f"Startup failed:\n{exc}\n\nSee {crash_log}")
        except Exception:
            print(exc, file=sys.stderr)
        raise
