#!/usr/bin/env python3
"""
UFC Predictor Desktop Dashboard — customtkinter GUI with multi-book tabs.

Launch:
    python src/ufc_dashboard.py
    python src/ufc_dashboard.py --debug
    dist/ufc-dashboard.exe
    dist/ufc-dashboard.exe --debug
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Debug / console (before heavy imports)
# ---------------------------------------------------------------------------

_DEBUG_MODE = "--debug" in sys.argv


def _debug_log(msg: str) -> None:
    if _DEBUG_MODE:
        print(f"[dashboard] {msg}", flush=True)


def _enable_debug_console() -> None:
    """Attach a console when running a --windowed EXE with --debug."""
    if not _DEBUG_MODE or not getattr(sys, "frozen", False):
        return
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = open("CONERR$", "w", encoding="utf-8", errors="replace")
        _debug_log("Debug console enabled")
    except Exception:
        pass


def _suppress_console_output() -> None:
    """Windowed EXE: discard stdout/stderr so startup cannot flash a console."""
    if _DEBUG_MODE or not getattr(sys, "frozen", False):
        return
    try:
        from src.safe_io import install_safe_stdout

        install_safe_stdout()
    except Exception:
        pass
    try:
        _null = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = _null
        sys.stderr = _null
    except Exception:
        pass


def _show_fatal_error(title: str, message: str) -> None:
    """Last-resort error UI when the dashboard cannot start."""
    _debug_log(f"FATAL: {title}: {message}")
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        if not getattr(sys, "frozen", False):
            raise
        log_path = Path(sys.executable).resolve().parent / "data" / "logs" / "dashboard_crash.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"{title}\n\n{message}", encoding="utf-8")
        except OSError:
            pass


# --- Bootstrap (EXE-safe) -----------------------------------------------------

_ENTRY = Path(__file__).resolve()
_ROOT = _ENTRY.parents[1]
_STARTUP_ERROR: str | None = None
_FROZEN = getattr(sys, "frozen", False)

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if _FROZEN and _DEBUG_MODE:
    _enable_debug_console()
elif _FROZEN:
    _suppress_console_output()


def _init_tk_backends() -> None:
    """Warm up Tk before CustomTkinter (avoids CTkFrame master=None on frozen EXE)."""
    import tkinter as tk

    probe = tk.Tk()
    probe.withdraw()
    probe.update_idletasks()
    probe.destroy()


def _init_customtkinter():
    """Early CustomTkinter setup — must run before any CTk widgets."""
    import customtkinter as ctk

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    if _FROZEN:
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
    return ctk


# Early bootstrap + CustomTkinter (subclass needs ctk.CTk at class definition time)
_CTK_BASE: type = object
_CTK_FRAME: type = object

try:
    from src.project_paths import bootstrap

    _ROOT = bootstrap(entry_file=_ENTRY, env_log=_debug_log if _DEBUG_MODE else None)
    _debug_log(f"Project root: {_ROOT}")
    _init_tk_backends()
    ctk = _init_customtkinter()
    _CTK_BASE = ctk.CTk
    _CTK_FRAME = ctk.CTkFrame
except Exception as exc:
    _STARTUP_ERROR = f"Bootstrap / GUI init failed:\n{exc}\n\n{traceback.format_exc()}"
    ctk = None
    _show_fatal_error("UFC Dashboard — bootstrap error", _STARTUP_ERROR)


def _load_dependencies(progress: Callable[[str], None] | None = None) -> None:
    """Import ML + service deps (after CustomTkinter is ready)."""
    global np, pd, matplotlib, FigureCanvasTkAgg, Figure, ttk, config
    global generate_alerts, parse_explanation_json, build_fight_brief
    global threshold_context_for_alerts, detect_card_change, run_full_analysis
    global run_quick_odds_refresh, extract_bet_candidates, kelly_stake
    global strategy_from_profile, example_threshold_table

    def _step(msg: str) -> None:
        if progress:
            progress(msg)
        _debug_log(msg)

    try:
        _step("Loading XGBoost…")
        import xgboost  # noqa: F401

        _step("Loading LightGBM…")
        import lightgbm  # noqa: F401

        _step("Loading NumPy / Pandas…")
        import numpy as np
        import pandas as pd

        _step("Loading Matplotlib…")
        import matplotlib

        matplotlib.use("TkAgg")
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure
        from tkinter import ttk

        _step("Loading config and services…")
        import config

        config.refresh_runtime_env()
        _debug_log(f"ENABLE_PROPS loaded as: {config.ENABLE_PROPS}")
        _debug_log(f"Loaded MYBOOKIE_ENABLED = {config.MYBOOKIE_ENABLED}")
        from src.alerts import generate_alerts
        from src.explainability import parse_explanation_json
        from src.fight_brief import build_fight_brief
        from src.parlay_builder import threshold_context_for_alerts
        from src.dashboard_service import (
            detect_card_change,
            run_full_analysis,
            run_quick_odds_refresh,
        )
        from src.strategy import extract_bet_candidates, kelly_stake, strategy_from_profile
        from ufc_betting_bot.modules.dynamic_thresholds import example_threshold_table

        _step("Dependencies ready.")
    except Exception as exc:
        raise RuntimeError(f"{exc}\n\n{traceback.format_exc()}") from exc


# Module-level placeholders (filled by _load_dependencies in main)
# ctk is set during bootstrap above — do not reset here.
np = None
pd = None
matplotlib = None
FigureCanvasTkAgg = None
Figure = None
ttk = None
config = None
generate_alerts = None
parse_explanation_json = None
build_fight_brief = None
threshold_context_for_alerts = None
detect_card_change = None
run_full_analysis = None
run_quick_odds_refresh = None
extract_bet_candidates = None
kelly_stake = None
strategy_from_profile = None
example_threshold_table = None


class SplashScreen:
    """Tkinter splash (not CTk) — only one CTk root allowed in the app."""

    def __init__(self) -> None:
        import tkinter as tk

        self.root = tk.Tk()
        self.root.title("UFC Predictor")
        self.root.geometry("440x170")
        self.root.resizable(False, False)
        self.root.configure(bg="#1a1a1a")
        tk.Label(
            self.root,
            text="UFC Predictor Dashboard",
            bg="#1a1a1a",
            fg="#e8e8e8",
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(24, 8))
        self.label = tk.Label(
            self.root,
            text="Starting…",
            bg="#1a1a1a",
            fg="#9ca3af",
            font=("Segoe UI", 10),
        )
        self.label.pack(pady=6)

    def set_status(self, text: str) -> None:
        if hasattr(self, "label"):
            self.label.configure(text=text)
            self.root.update_idletasks()

    def close(self) -> None:
        if hasattr(self, "root"):
            try:
                self.root.destroy()
            except Exception:
                pass

    def pump(self) -> None:
        if hasattr(self, "root"):
            self.root.update()


# --- Data engine --------------------------------------------------------------


class DashboardPayload:
    """In-memory analysis snapshot for all tabs."""

    def __init__(self) -> None:
        self.generated_at = ""
        self.event_label = ""
        self.profile = "paper"
        self.cards: list[dict[str, Any]] = []
        self.combined: pd.DataFrame = pd.DataFrame()
        self.books: dict[str, dict[str, Any]] = {}
        self.risk_metrics: dict[str, Any] = {}
        self.threshold_ctx: dict[str, Any] = {}
        self.errors: list[str] = []
        self.odds_updated_at = ""
        self.from_cache = False
        self.prop_backtest: dict[str, Any] = {}

    @property
    def all_preds(self) -> pd.DataFrame:
        frames = [c["predictions"] for c in self.cards if not c["predictions"].empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _result_to_payload(data: dict[str, Any]) -> DashboardPayload:
    p = DashboardPayload()
    p.generated_at = data.get("generated_at", "")
    p.event_label = data.get("event_label", "")
    p.profile = config.normalize_profile(data.get("profile", "paper"))
    p.cards = data.get("cards", [])
    p.combined = data.get("combined", pd.DataFrame())
    p.books = data.get("books", {})
    p.risk_metrics = data.get("risk_metrics", {})
    p.threshold_ctx = data.get("threshold_ctx", {})
    p.errors = data.get("errors", [])
    p.odds_updated_at = data.get("odds_updated_at", p.generated_at)
    p.from_cache = bool(data.get("from_cache", False))
    p.prop_backtest = data.get("prop_backtest", {})
    return p


def run_dashboard_analysis(
    *,
    event_mode: str,
    profile: str,
    force_refresh_odds: bool = False,
    explain: bool = True,
    use_cache: bool = True,
    progress: Callable[[str, float | None], None] | None = None,
    budget_state: dict[str, Any] | None = None,
) -> DashboardPayload:
    def _prog(msg: str, pct: float | None = None) -> None:
        if progress:
            progress(msg, pct)

    data = run_full_analysis(
        event_mode=event_mode,
        profile=profile,
        force_refresh_odds=force_refresh_odds,
        explain=explain,
        use_cache=use_cache,
        progress=_prog,
        budget_state=budget_state,
    )
    data["profile"] = profile
    return _result_to_payload(data)


def _ensure_props_config() -> bool:
    """Reload .env and refresh ENABLE_PROPS (props tabs + analysis)."""
    if config is None:
        return False
    try:
        from src.project_paths import reload_runtime_env

        reload_runtime_env(_ROOT, log=_debug_log if _DEBUG_MODE else None)
    except Exception as exc:
        _debug_log(f"Props config reload failed: {exc}")
        config.refresh_runtime_env()
    enabled = bool(config.ENABLE_PROPS)
    _debug_log(f"ENABLE_PROPS loaded as: {enabled}")
    return enabled


def _top_shap(row: pd.Series) -> str:
    if pd.notna(row.get("shap_explanation")):
        exp = parse_explanation_json(row.get("shap_explanation"))
        toward = exp.get("toward_pick") or exp.get("top_features") or []
        if toward:
            return str(toward[0].get("label", ""))[:48]
    return ""


def _pick_edge(row: pd.Series) -> tuple[float | None, str | None]:
    f1 = str(row.get("fighter_1", ""))
    f2 = str(row.get("fighter_2", ""))
    pick = str(row.get("predicted_winner", ""))
    if pd.notna(row.get("edge_pct")):
        edge = float(row["edge_pct"]) / 100.0
    elif pd.notna(row.get("best_edge")):
        edge = float(row["best_edge"])
    elif pd.notna(row.get("edge_f1")):
        edge = max(float(row.get("edge_f1", 0)), float(row.get("edge_f2", 0)))
    else:
        return None, pick or None
    return edge, pick or None


def _site_odds(row: pd.Series, pick: str | None) -> str:
    if not pick:
        pick = str(row.get("predicted_winner", "") or "")
    f1 = str(row.get("fighter_1", ""))
    has_odds = bool(row.get("odds_matched")) or pd.notna(row.get("f1_odds")) or pd.notna(row.get("f2_odds"))
    if not pick or not has_odds:
        return "—"
    if pick == f1 and pd.notna(row.get("f1_odds")):
        return f"{float(row['f1_odds']):.2f}"
    if pd.notna(row.get("f2_odds")):
        return f"{float(row['f2_odds']):.2f}"
    return "—"


def _kelly_pct(row: pd.Series, bankroll: float, strategy) -> str:
    cand = extract_bet_candidates(row, config=strategy)
    if cand is None or bankroll <= 0:
        return "—"
    stake = kelly_stake(
        bankroll,
        prob=cand.prob,
        decimal_odds=cand.decimal_odds,
        edge=cand.edge,
        config=strategy,
    )
    if stake <= 0:
        return "—"
    return f"{stake / bankroll * 100:.2f}%"


class _ToolTip:
    """Hover tooltip for CustomTkinter widgets (uses tk.Toplevel)."""

    def __init__(self, widget, text: str, *, delay_ms: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: Any = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return
        import tkinter as tk

        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        lbl = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#1e293b",
            foreground="#e2e8f0",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=8,
            pady=6,
            wraplength=320,
        )
        lbl.pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def _model_prob_for_row(row: pd.Series) -> float:
    """Model win probability for the predicted pick (used for table sorting)."""
    from src.strategy import _pick_model_prob

    _pick, prob, _fight = _pick_model_prob(row)
    return float(prob) if pd.notna(prob) else 0.0


def _sort_preds_by_model_prob(preds: pd.DataFrame) -> pd.DataFrame:
    if preds is None or preds.empty:
        return preds
    scored = preds.copy()
    pick = scored.get("predicted_winner", pd.Series("", index=scored.index)).astype(str)
    f1 = scored.get("fighter_1", pd.Series("", index=scored.index)).astype(str)
    f2 = scored.get("fighter_2", pd.Series("", index=scored.index)).astype(str)
    p1 = pd.to_numeric(scored.get("prob_f1_win", scored.get("predicted_prob")), errors="coerce")
    p2 = pd.to_numeric(scored.get("prob_f2_win"), errors="coerce")
    scored["_sort_prob"] = np.where(
        pick.eq(f1),
        p1,
        np.where(pick.eq(f2), p2, p1),
    ).astype(float)
    scored["_sort_prob"] = scored["_sort_prob"].fillna(0.0)
    return scored.sort_values("_sort_prob", ascending=False).drop(columns="_sort_prob").reset_index(drop=True)


def _rows_for_table(
    preds: pd.DataFrame,
    bankroll: float,
    strategy,
    *,
    compact: bool = False,
) -> list[tuple]:
    rows: list[tuple] = []
    for _, row in _sort_preds_by_model_prob(preds).iterrows():
        edge, pick = _pick_edge(row)
        f1, f2 = str(row.get("fighter_1", "")), str(row.get("fighter_2", ""))
        prob = row.get("predicted_prob", row.get("prob_f1_win"))
        if pd.notna(prob) and pick == f2 and pd.notna(row.get("prob_f2_win")):
            prob = row["prob_f2_win"]
        edge_txt = f"{edge * 100:+.1f}%" if edge is not None else "—"
        prob_txt = f"{float(prob):.0%}" if pd.notna(prob) else "—"
        sort_prob = _model_prob_for_row(row)
        if compact:
            rows.append(
                (
                    f"{f1} vs {f2}",
                    pick or "—",
                    prob_txt,
                    _site_odds(row, pick),
                    edge_txt,
                    _kelly_pct(row, bankroll, strategy),
                    sort_prob,
                )
            )
        else:
            brief = build_fight_brief(row, edge_pct=edge * 100 if edge else None)[:120]
            rows.append(
                (
                    f"{f1} vs {f2}",
                    pick or "—",
                    prob_txt,
                    _site_odds(row, pick),
                    edge_txt,
                    _kelly_pct(row, bankroll, strategy),
                    brief,
                    _top_shap(row) or "—",
                    sort_prob,
                )
            )
    rows.sort(key=lambda r: r[-1], reverse=True)
    return [r[:-1] for r in rows]


# --- UI helpers ---------------------------------------------------------------


def _normalize_ranked_parlays(
    parlays: list[dict[str, Any]],
    preds: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    """Ensure parlays have rank + fighter names on every leg."""
    from src.parlay_builder import (
        enrich_parlays_for_display,
        format_recommended_parlay_legs,
        leg_pick_label,
        leg_stats_suffix,
    )

    sorted_p = sorted(parlays, key=lambda x: x.get("expected_value", 0), reverse=True)
    enriched = enrich_parlays_for_display(sorted_p, preds)
    out: list[dict[str, Any]] = []
    for i, p in enumerate(enriched):
        item = dict(p)
        item["rank"] = i + 1
        if not item.get("min_leg_edge") and item.get("legs"):
            item["min_leg_edge"] = min(leg.get("edge", 0) for leg in item["legs"])
        item["leg_labels"] = [leg_pick_label(leg) for leg in item.get("legs", [])]
        item["_leg_rows"] = format_recommended_parlay_legs(item)
        out.append(item)
    return out


def _render_ranked_parlays(
    parent,
    parlays: list[dict[str, Any]],
    *,
    title: str = "Recommended Parlays",
    preds: pd.DataFrame | None = None,
) -> None:
    from src.parlay_builder import format_recommended_parlay_header

    ranked = _normalize_ranked_parlays(parlays, preds=preds)
    if not ranked:
        return
    ctk.CTkLabel(
        parent,
        text=title,
        font=ctk.CTkFont(size=13, weight="bold"),
        anchor="w",
    ).pack(fill="x", pady=(8, 4))
    for p in ranked:
        block = ctk.CTkFrame(parent, fg_color="transparent")
        block.pack(fill="x", padx=(4, 0), pady=(0, 8))
        ctk.CTkLabel(
            block,
            text=format_recommended_parlay_header(p),
            anchor="w",
            text_color="#a5b4fc",
            font=ctk.CTkFont(size=12, weight="bold"),
            wraplength=1050,
            justify="left",
        ).pack(fill="x")
        for leg_row in p.get("_leg_rows", []):
            ctk.CTkLabel(
                block,
                text=leg_row,
                anchor="w",
                text_color="#d1d5db",
                font=ctk.CTkFont(size=12),
                wraplength=1050,
                justify="left",
            ).pack(fill="x", padx=(12, 0))


class DataTable(_CTK_FRAME):
    """Scrollable ttk tree table with edge coloring."""

    COLUMNS = (
        "Fight",
        "Pick",
        "Prob",
        "Odds",
        "Edge",
        "Kelly",
        "Brief",
        "SHAP",
    )
    COMPACT_COLUMNS = ("Fight", "Pick", "Prob", "Odds", "Edge", "Kelly")

    def __init__(self, master, *, height: int = 12, compact: bool = False, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self._compact = compact
        columns = self.COMPACT_COLUMNS if compact else self.COLUMNS
        style = ttk.Style()
        style.theme_use("clam")
        row_h = 26 if compact else 30
        style.configure(
            "Dash.Treeview",
            background="#1e1e1e",
            foreground="#e8e8e8",
            fieldbackground="#1e1e1e",
            rowheight=row_h,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Dash.Treeview.Heading",
            background="#2b2b2b",
            foreground="#ffffff",
            font=("Segoe UI", 10, "bold"),
            padding=(6, 4),
        )
        self.tree = ttk.Treeview(
            self,
            columns=columns,
            show="headings",
            height=height,
            style="Dash.Treeview",
        )
        if compact:
            widths = (240, 150, 56, 64, 76, 72)
        else:
            widths = (220, 130, 58, 58, 72, 68, 300, 140)
        for col, w in zip(columns, widths):
            self.tree.heading(col, text=col, anchor="w")
            stretch = col in ("Fight", "Brief")
            self.tree.column(col, width=w, minwidth=48, anchor="w", stretch=stretch)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(0, 0), pady=(2, 0))
        vsb.grid(row=0, column=1, sticky="ns")
        if not compact:
            hsb.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("pos", foreground="#34d399")
        self.tree.tag_configure("neg", foreground="#f87171")
        self.tree.tag_configure("neutral", foreground="#b0b0b0")
        self.tree.tag_configure("even", background="#1a1f2e")
        self.tree.tag_configure("odd", background="#1e1e1e")
        self._bind_mousewheel()

    def _bind_mousewheel(self) -> None:
        def _on_wheel(event) -> None:
            if event.delta:
                self.tree.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.tree.bind("<MouseWheel>", _on_wheel)
        self.tree.bind("<Enter>", lambda _e: self.tree.focus_set())

    def load_rows(self, rows: list[tuple]) -> None:
        self.tree.delete(*self.tree.get_children())
        empty_cols = 6 if self._compact else 8
        if not rows:
            self.tree.insert(
                "",
                "end",
                values=tuple(["No fights loaded — click Refresh"] + [""] * (empty_cols - 1)),
            )
            return
        for i, row in enumerate(rows):
            edge_txt = row[4] if len(row) > 4 else ""
            edge_tag = "neutral"
            if edge_txt not in ("—", ""):
                try:
                    edge_tag = "pos" if float(edge_txt.replace("%", "").replace("+", "")) > 0 else "neg"
                except ValueError:
                    pass
            zebra = "even" if i % 2 == 0 else "odd"
            self.tree.insert("", "end", values=row, tags=(edge_tag, zebra))


class TopRecommendedBetsPanel(_CTK_FRAME):
    """Prominent #1–#5 recommendations on the Overview tab."""

    _PANEL_BG = "#0c1222"
    _INNER_BG = "#111827"
    _ROW_BG = "#1e293b"
    _HERO_BG = "#1e1b4b"
    _HERO_BORDER = "#fbbf24"
    _BORDER = "#334155"

    def __init__(self, master, **kwargs) -> None:
        super().__init__(
            master,
            fg_color=self._PANEL_BG,
            corner_radius=12,
            border_width=2,
            border_color="#475569",
            **kwargs,
        )
        inner = ctk.CTkFrame(self, fg_color=self._INNER_BG, corner_radius=10)
        inner.pack(fill="both", expand=True, padx=4, pady=4)

        hdr = ctk.CTkFrame(inner, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(14, 10))
        title_block = ctk.CTkFrame(hdr, fg_color="transparent")
        title_block.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_block,
            text="Top Recommended Bets",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#f8fafc",
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            title_block,
            text="Actionable picks from selected books · stakes scaled to card budget",
            font=ctk.CTkFont(size=11),
            text_color="#64748b",
            anchor="w",
        ).pack(fill="x", pady=(2, 0))
        self.pool_label = ctk.CTkLabel(
            hdr,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#86efac",
            anchor="e",
        )
        self.pool_label.pack(side="right", padx=(12, 0))

        self.list_frame = ctk.CTkFrame(inner, fg_color="transparent")
        self.list_frame.pack(fill="x", padx=12, pady=(0, 14))
        self.empty_label = ctk.CTkLabel(
            inner,
            text="Refresh to load top edges from your selected books.",
            text_color="#64748b",
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self.empty_label.pack(fill="x", padx=16, pady=(0, 14))

    @staticmethod
    def _odds_line(bet: dict[str, Any]) -> str:
        book = str(bet.get("book") or "—")
        am = str(bet.get("american_odds") or "—")
        dec = str(bet.get("odds_display") or "—")
        return f"{book}  {am} ({dec})"

    def _render_hero(self, bet: dict[str, Any]) -> None:
        card = ctk.CTkFrame(
            self.list_frame,
            fg_color=self._HERO_BG,
            corner_radius=10,
            border_width=2,
            border_color=self._HERO_BORDER,
        )
        card.pack(fill="x", pady=(0, 8))

        ribbon = ctk.CTkFrame(card, fg_color="#fbbf24", corner_radius=6, height=28)
        ribbon.pack(fill="x", padx=10, pady=(10, 6))
        ribbon.pack_propagate(False)
        ctk.CTkLabel(
            ribbon,
            text="#1  BEST BET",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#1c1917",
        ).pack(side="left", padx=12, pady=4)

        edge_pct = float(bet.get("edge_pct") or 0)
        ctk.CTkLabel(
            ribbon,
            text=f"{edge_pct:+.1f}% model edge",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#1c1917",
        ).pack(side="right", padx=12)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=(0, 12))
        bet_type = str(bet.get("bet_type") or "Bet")
        ctk.CTkLabel(
            body,
            text=bet_type,
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#a5b4fc",
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            body,
            text=str(bet.get("display_label") or bet.get("pick_line") or bet.get("pick") or "—"),
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color="#f8fafc",
            anchor="w",
            wraplength=900,
            justify="left",
        ).pack(fill="x", pady=(4, 6))
        ctk.CTkLabel(
            body,
            text=self._odds_line(bet),
            font=ctk.CTkFont(size=13),
            text_color="#cbd5e1",
            anchor="w",
        ).pack(fill="x")

        stake = float(bet.get("suggested_stake") or 0)
        stake_row = ctk.CTkFrame(body, fg_color="#312e81", corner_radius=8)
        stake_row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(
            stake_row,
            text=f"Suggested stake: ${stake:.2f}",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#fde68a",
            anchor="w",
        ).pack(side="left", padx=12, pady=8)
        prob = bet.get("prob")
        if prob is not None:
            ctk.CTkLabel(
                stake_row,
                text=f"Model win prob {float(prob):.0%}",
                font=ctk.CTkFont(size=11),
                text_color="#94a3b8",
                anchor="e",
            ).pack(side="right", padx=12)

    def _render_row(self, bet: dict[str, Any], rank: int) -> None:
        row = ctk.CTkFrame(
            self.list_frame,
            fg_color=self._ROW_BG,
            corner_radius=8,
            border_width=1,
            border_color=self._BORDER,
        )
        row.pack(fill="x", pady=3)

        rank_colors = ("#fbbf24", "#cbd5e1", "#cd7f32", "#94a3b8", "#94a3b8")
        rank_color = rank_colors[min(rank - 1, 4)]

        left = ctk.CTkFrame(row, fg_color="transparent", width=44)
        left.pack(side="left", padx=(12, 4), pady=10)
        left.pack_propagate(False)
        ctk.CTkLabel(
            left,
            text=f"#{rank}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=rank_color,
        ).pack(anchor="w")

        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="x", expand=True, padx=4, pady=10)
        label = str(bet.get("display_label") or bet.get("pick_line") or bet.get("pick") or "—")
        ctk.CTkLabel(
            mid,
            text=label,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#f1f5f9",
            anchor="w",
            wraplength=420,
            justify="left",
        ).pack(fill="x")
        ctk.CTkLabel(
            mid,
            text=self._odds_line(bet),
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        edge_pct = float(bet.get("edge_pct") or 0)
        edge_color = "#34d399" if edge_pct > 0 else "#f87171"
        ctk.CTkLabel(
            row,
            text=f"{edge_pct:+.1f}%",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=edge_color,
            width=56,
        ).pack(side="left", padx=4, pady=10)

        stake = float(bet.get("suggested_stake") or 0)
        ctk.CTkLabel(
            row,
            text=f"${stake:.2f}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#fde68a",
            width=64,
            anchor="e",
        ).pack(side="right", padx=14, pady=10)

    def render(
        self,
        bets: list[dict[str, Any]],
        *,
        highlight_parlay: dict[str, Any] | None = None,
    ) -> None:
        del highlight_parlay  # unified list via aggregate_overview_recommendations
        for w in self.list_frame.winfo_children():
            w.destroy()

        if not bets:
            self.pool_label.configure(text="")
            self.list_frame.pack_forget()
            self.empty_label.pack(fill="x", padx=16, pady=(0, 14))
            return

        self.empty_label.pack_forget()
        self.list_frame.pack(fill="x", padx=12, pady=(0, 14))
        pool = float(bets[0].get("card_pool_usd") or 0)
        self.pool_label.configure(text=f"Card budget: ${pool:,.2f}")

        for bet in bets:
            rank = int(bet.get("rank") or 0)
            if rank == 1:
                self._render_hero(bet)
            elif rank > 1:
                self._render_row(bet, rank)


class GrokAnalysisPanel(_CTK_FRAME):
    """Optional Grok narrative read on top fights/props (runs in background thread)."""

    def __init__(self, master, *, on_run: Callable[[], None] | None = None, **kwargs) -> None:
        super().__init__(master, fg_color="#111827", corner_radius=10, **kwargs)
        self._on_run = on_run

        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            hdr,
            text="Grok Analysis",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#f8fafc",
        ).pack(side="left")
        self.status_label = ctk.CTkLabel(
            hdr,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8",
            anchor="e",
        )
        self.status_label.pack(side="right", fill="x", expand=True, padx=(8, 0))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 6))
        self.run_btn = ctk.CTkButton(
            btn_row,
            text="Run Grok Analysis",
            width=160,
            fg_color="#4c1d95",
            hover_color="#6d28d9",
            command=on_run,
        )
        self.run_btn.pack(side="left")
        self.hint_label = ctk.CTkLabel(
            btn_row,
            text="Optional — does not block refresh. Adjusts Kelly sizing when complete.",
            font=ctk.CTkFont(size=11),
            text_color="#64748b",
            anchor="w",
        )
        self.hint_label.pack(side="left", padx=(12, 0))

        self.scroll = ctk.CTkScrollableFrame(self, label_text="Narrative edges")
        self.scroll.pack(fill="both", expand=True, padx=10, pady=(4, 12))

    def set_busy(self, busy: bool, message: str = "") -> None:
        state = "disabled" if busy else "normal"
        try:
            self.run_btn.configure(state=state)
        except Exception:
            pass
        if message:
            self.status_label.configure(text=message)

    def render(self, result: dict[str, Any] | None, *, available: bool) -> None:
        for w in self.scroll.winfo_children():
            w.destroy()

        if not available:
            self.status_label.configure(text="Disabled — set GROK_ENABLED=true + API key in .env")
            self._pack_message(
                "Grok integration is off",
                "Add GROK_ENABLED=true and GROK_API_KEY (or XAI_API_KEY) to .env, then restart.",
                color="#fbbf24",
            )
            return

        if not result:
            self.status_label.configure(text="Not run yet")
            self._pack_message(
                "No Grok analysis yet",
                "Click Run Grok Analysis after Refresh Next Two loads top fights and props.",
            )
            return

        if not result.get("ok"):
            self.status_label.configure(text="Last run failed")
            self._pack_message("Grok error", str(result.get("error") or "Unknown error"), color="#f87171")
            return

        cache_note = " (cached)" if result.get("from_cache") else ""
        self.status_label.configure(
            text=f"{result.get('generated_at', '—')}{cache_note}  |  model {result.get('model', config.GROK_MODEL)}"
        )
        summary = str(result.get("summary") or "").strip()
        if summary:
            self._pack_message("Card summary", summary, color="#e2e8f0", title_size=13)

        picks = result.get("picks") or []
        if not picks:
            self._pack_message("No picks returned", "Grok did not return per-pick analysis.")
            return

        for pick in picks:
            self._render_pick_card(pick)

    def _pack_message(
        self,
        title: str,
        body: str,
        *,
        color: str = "#94a3b8",
        title_size: int = 14,
    ) -> None:
        frame = ctk.CTkFrame(self.scroll, fg_color="#1e293b", corner_radius=8)
        frame.pack(fill="x", padx=4, pady=6)
        ctk.CTkLabel(
            frame,
            text=title,
            font=ctk.CTkFont(size=title_size, weight="bold"),
            text_color="#f1f5f9",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            frame,
            text=body,
            font=ctk.CTkFont(size=12),
            text_color=color,
            anchor="w",
            justify="left",
            wraplength=1000,
        ).pack(fill="x", padx=12, pady=(0, 10))

    def _render_pick_card(self, pick: dict[str, Any]) -> None:
        factor = pick.get("kelly_adjustment", 1.0)
        factor_color = "#34d399" if float(factor) >= 1.0 else "#fbbf24"
        frame = ctk.CTkFrame(self.scroll, fg_color="#0f172a", corner_radius=8, border_width=1, border_color="#334155")
        frame.pack(fill="x", padx=4, pady=5)
        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            hdr,
            text=str(pick.get("id") or "Pick"),
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#f8fafc",
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            hdr,
            text=f"Kelly ×{float(factor):.2f}  ·  {pick.get('conviction', 'medium')}",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=factor_color,
        ).pack(side="right")

        for label, key in (
            ("Narrative edge", "narrative_edge"),
            ("Crowd positioning", "crowd_positioning"),
        ):
            text = str(pick.get(key) or "").strip()
            if text:
                ctk.CTkLabel(
                    frame,
                    text=f"{label}: {text}",
                    font=ctk.CTkFont(size=11),
                    text_color="#cbd5e1",
                    anchor="w",
                    justify="left",
                    wraplength=980,
                ).pack(fill="x", padx=12, pady=(0, 4))

        risks = pick.get("invalidation_risks") or []
        if risks:
            risk_txt = "Invalidation: " + "; ".join(str(r) for r in risks[:4])
            ctk.CTkLabel(
                frame,
                text=risk_txt,
                font=ctk.CTkFont(size=10),
                text_color="#f87171",
                anchor="w",
                justify="left",
                wraplength=980,
            ).pack(fill="x", padx=12, pady=(0, 10))


class BookTab(_CTK_FRAME):
    """Reusable betting book tab layout."""

    def __init__(self, master, title: str, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.title = title
        self.warning_box = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#fbbf24",
            wraplength=1100,
        )
        self.warning_box.pack(fill="x", padx=12, pady=(8, 0))
        self.warning_box.pack_forget()
        self.summary = ctk.CTkLabel(
            self,
            text="Run Refresh to load data.",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#cbd5e1",
        )
        self.summary.pack(fill="x", padx=12, pady=(8, 2))
        self.stake_box = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="#93c5fd",
        )
        self.stake_box.pack_forget()
        self.table = DataTable(self, height=11, compact=True)
        self.table.pack(fill="both", expand=True, padx=10, pady=4)
        self.bets_frame = ctk.CTkScrollableFrame(self, height=88, label_text="Parlays")
        self.bets_frame.pack(fill="x", padx=10, pady=(2, 8))

    def render(
        self,
        book_data: dict[str, Any],
        threshold_ctx: dict[str, Any],
        *,
        budget_state: dict[str, Any] | None = None,
        profile: str | None = None,
    ) -> None:
        from src.strategy import (
            allocate_card_budget_per_book,
            book_display_name,
            budget_aware_alerts,
            collect_dashboard_risk_warnings,
            effective_card_budget_usd,
        )

        preds: pd.DataFrame = book_data.get("predictions", pd.DataFrame())
        alerts: dict = budget_aware_alerts(
            book_data.get("alerts") or {},
            budget_state,
            self.title,
            profile=profile,
        )
        matched = book_data.get("odds_matched", 0)
        total = book_data.get("odds_total", 0)
        source = book_data.get("source", self.title)
        warning = book_data.get("warning") or book_data.get("error") or ""

        bankroll = float(
            (budget_state or {}).get("total_bankroll")
            or alerts.get("bankroll")
            or config.INITIAL_BANKROLL
        )
        strategy = strategy_from_profile(bankroll=bankroll)

        risk_warnings = collect_dashboard_risk_warnings(alerts, budget_state, bankroll=bankroll)
        if warning:
            risk_warnings.insert(0, ("warn", warning))
        _apply_risk_warning_label(self.warning_box, risk_warnings)

        book_pool = 0.0
        alloc_line = ""
        if budget_state:
            plan = allocate_card_budget_per_book(budget_state, profile=profile)
            info = plan.get(self.title, {})
            if info.get("enabled"):
                book_pool = float(info.get("allocation") or 0)
                card_eff, _ = effective_card_budget_usd(budget_state, profile=profile)
                alloc_line = (
                    f"Book card pool: ${book_pool:.2f} "
                    f"({float(info.get('share_pct') or 0):.0f}% of ${card_eff:.0f} card budget)  |  "
                    f"Balance: ${float(info.get('balance') or 0):.2f}"
                )
            else:
                alloc_line = f"{book_display_name(self.title)} disabled in Budget Manager."

        if alloc_line:
            self.stake_box.configure(text=alloc_line)
            self.stake_box.pack(fill="x", padx=12, pady=(0, 4))
        else:
            self.stake_box.pack_forget()

        summary_line = (
            f"{source}  |  Odds {matched}/{total}  |  "
            f"{len(alerts.get('singles') or [])} singles  |  "
            f"{len(alerts.get('parlays') or [])} parlays"
        )
        td = threshold_ctx.get("thresholds") or alerts.get("threshold_detail")
        if td:
            summary_line += f"  |  Min edge {td.get('alert_min_edge', 0):.1%}"
        elif config.is_live_profile() or config.is_paper_profile():
            summary_line += f"  |  Min edge {config.ALERT_MIN_EDGE:.1%}"

        self.summary.configure(text=summary_line)

        self.table.load_rows(_rows_for_table(preds, bankroll, strategy, compact=True))

        for w in self.bets_frame.winfo_children():
            w.destroy()
        parlays = alerts.get("parlays") or []
        max_parlays = config.profile_int("max_parlays_show")
        _render_ranked_parlays(
            self.bets_frame,
            parlays[:max_parlays],
            preds=preds,
            title="Parlays" if parlays else "",
        )


class PropsTable(_CTK_FRAME):
    """Prop singles table: type+fight, fighter, book odds, edge, budget-scaled stake."""

    COLUMNS = ("Prop Type", "Fighter", "Odds", "Edge", "Suggested Stake")

    def __init__(self, master, *, height: int = 14, book_name: str = "", **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.book_name = book_name
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Props.Treeview",
            background="#1e1e1e",
            foreground="#e8e8e8",
            fieldbackground="#1e1e1e",
            rowheight=30,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Props.Treeview.Heading",
            background="#2b2b2b",
            foreground="#ffffff",
            font=("Segoe UI", 10, "bold"),
            padding=(6, 4),
        )
        self.tree = ttk.Treeview(
            self,
            columns=self.COLUMNS,
            show="headings",
            height=height,
            style="Props.Treeview",
        )
        widths = (280, 130, 120, 88, 108)
        for col, w in zip(self.COLUMNS, widths):
            self.tree.heading(col, text=col, anchor="w")
            stretch = col == "Prop Type"
            self.tree.column(col, width=w, minwidth=56, anchor="w", stretch=stretch)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("pos", foreground="#34d399")
        self.tree.tag_configure("neg", foreground="#f87171")
        self.tree.tag_configure("neutral", foreground="#b0b0b0")
        self.tree.tag_configure("synth", foreground="#fbbf24")
        self.tree.tag_configure("relaxed", foreground="#a78bfa")
        self.tree.tag_configure("even", background="#1a1f2e")
        self.tree.tag_configure("odd", background="#1e1e1e")
        self.tree.bind("<MouseWheel>", self._on_wheel)
        self.tree.bind("<Enter>", lambda _e: self.tree.focus_set())

    def _on_wheel(self, event) -> None:
        if event.delta:
            self.tree.yview_scroll(int(-1 * (event.delta / 120)), "units")

    @staticmethod
    def _format_odds(s: dict[str, Any], book_name: str) -> str:
        from src.parlay_builder import decimal_to_american

        dec = float(s.get("odds", 0) or 0)
        if dec <= 1:
            return "—"
        source = str(s.get("odds_source", "synthetic")).lower()
        am = decimal_to_american(dec)
        short_book = book_name.replace(".eu", "") or str(s.get("book", "")).replace(".eu", "")
        tag = "Live" if source == "live" else "Model"
        return f"{am} ({dec:.2f}) {tag}"

    def load_singles(self, singles: list[dict[str, Any]]) -> None:
        self.tree.delete(*self.tree.get_children())
        if not singles:
            self.tree.insert(
                "",
                "end",
                values=("No props match current filters", "—", "—", "—", "—"),
                tags=("neutral", "even"),
            )
            return
        for i, s in enumerate(singles):
            fight = str(s.get("fight", ""))
            prop_type = str(s.get("prop_type", s.get("prop_key", "")))
            if fight:
                prop_cell = f"{prop_type}  ·  {fight}"
            else:
                prop_cell = prop_type

            fighter = str(s.get("fighter", "—"))
            if fighter in ("", "—") and s.get("label"):
                fighter = "—"

            source = str(s.get("odds_source", "synthetic")).lower()
            edge_pct = s.get("edge_pct")
            if edge_pct is not None:
                edge_txt = f"{float(edge_pct):+.1f}%"
                edge_tag = "pos" if float(edge_pct) > 0 else "neg" if float(edge_pct) < 0 else "neutral"
            elif source == "synthetic":
                edge_txt = f"{float(s.get('prob', 0)):.0%} model"
                edge_tag = "relaxed" if not s.get("strict_qualified", True) else "synth"
            else:
                edge_txt = "—"
                edge_tag = "neutral"

            stake = float(s.get("suggested_stake") or 0)
            if s.get("book_disabled"):
                stake_txt = "Book off"
            elif stake > 0:
                stake_txt = f"${stake:.2f}"
            else:
                stake_txt = "—"

            row = (
                prop_cell,
                fighter,
                self._format_odds(s, self.book_name),
                edge_txt,
                stake_txt,
            )
            zebra = "even" if i % 2 == 0 else "odd"
            self.tree.insert("", "end", values=row, tags=(edge_tag, zebra))


class BookPropsTab(_CTK_FRAME):
    """Prop bets for one book — singles; DraftKings also shows parlays."""

    _BOOK_EMPTY_HINTS: dict[str, str] = {
        "BetNow.eu": (
            "No ranked props for BetNow yet. Set BETNOW_COOKIE in .env for live method/total lines, "
            "then Refresh Next Two. Synthetic props need ≥{min_prob:.0%} model probability."
        ),
        "DraftKings": (
            "No ranked props for DraftKings yet. Live totals and method markets load on Refresh when "
            "ENABLE_PROPS=true. Synthetic props need ≥{min_prob:.0%} model probability."
        ),
        "MyBookie": (
            "No ranked props for MyBookie yet. Enable MYBOOKIE_ENABLED=true and refresh for live lines. "
            "Synthetic props need ≥{min_prob:.0%} model probability."
        ),
    }

    def __init__(
        self,
        master,
        *,
        book_name: str,
        book_note: str,
        show_parlays: bool = False,
        show_all_var: Any = None,
        profile_getter: Callable[[], str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self.book_name = book_name
        self.book_note = book_note
        self.show_parlays = show_parlays
        self.show_all_var = show_all_var
        self.profile_getter = profile_getter
        self._last_payload: DashboardPayload | None = None
        self._last_budget_state: dict[str, Any] | None = None
        self.summary = ctk.CTkLabel(
            self,
            text="Props — click Refresh Next Two to load ranked prop lines.",
            anchor="w",
            justify="left",
        )
        self.summary.pack(fill="x", padx=12, pady=(10, 4))
        self.risk_warning_box = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#f87171",
            wraplength=1100,
        )
        self.risk_warning_box.pack(fill="x", padx=12, pady=(0, 4))
        self.risk_warning_box.pack_forget()
        self.filter_label = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
        )
        self.filter_label.pack(fill="x", padx=12, pady=(0, 4))
        self.controls = ctk.CTkFrame(self, fg_color="transparent")
        self.controls.pack(fill="x", padx=12, pady=(0, 4))
        self.show_all_switch = ctk.CTkSwitch(
            self.controls,
            text="Show all props (relaxed)",
            variable=show_all_var if show_all_var is not None else ctk.BooleanVar(value=False),
            command=self._on_show_all_toggle,
        )
        self.show_all_hint = ctk.CTkLabel(
            self.controls,
            text=(
                "Strict filter shows props with live edge or high model confidence. "
                "Relaxed includes lower-confidence model lines for research."
            ),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color="#64748b",
            wraplength=900,
            justify="left",
        )
        self.backtest_box = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color="#64748b",
        )
        self.backtest_box.pack_forget()
        self.scroll = ctk.CTkScrollableFrame(self, label_text="Ranked prop singles")
        self.scroll.pack(fill="both", expand=True, padx=10, pady=6)

    def _on_show_all_toggle(self) -> None:
        val = bool(self.show_all_var.get()) if self.show_all_var is not None else False
        _debug_log(f"Show all props toggled ({self.book_name}): {val}")
        if self._last_payload is not None:
            profile = self.profile_getter() if self.profile_getter else None
            self.render(self._last_payload, budget_state=self._last_budget_state, profile=profile)

    def _is_paper_profile(self) -> bool:
        if self.profile_getter:
            return config.normalize_profile(self.profile_getter()) == "paper"
        return config.is_paper_profile()

    def _update_show_all_visibility(self) -> None:
        if self._is_paper_profile():
            self.show_all_switch.pack(side="left", padx=(0, 8))
            self.show_all_hint.pack(side="left", fill="x", expand=True)
        else:
            self.show_all_switch.pack_forget()
            self.show_all_hint.pack_forget()
            if self.show_all_var is not None:
                self.show_all_var.set(False)

    def _update_show_all_controls(self, meta: dict[str, Any], shown: int) -> None:
        if not self._is_paper_profile():
            return
        total = int(meta.get("total_found", 0))
        strict = int(meta.get("strict_count", 0))
        relaxed = int(meta.get("relaxed_count", max(0, total - strict)))
        min_prob = config.PROP_MIN_MODEL_PROB
        relaxed_floor = config.PROP_SHOW_ALL_MIN_PROB
        if relaxed:
            self.show_all_switch.configure(
                text=f"Show all props (+{relaxed} relaxed)"
            )
        else:
            self.show_all_switch.configure(text="Show all props (relaxed)")
        show_all = bool(self.show_all_var.get()) if self.show_all_var is not None else False
        if show_all:
            self.show_all_hint.configure(
                text=(
                    f"Showing all {shown} ranked props — strict ({strict}) plus relaxed "
                    f"(model ≥{relaxed_floor:.0%}, below strict {min_prob:.0%}). "
                    "Stakes scaled to your Budget Manager card pool."
                ),
                text_color="#94a3b8",
            )
        else:
            self.show_all_hint.configure(
                text=(
                    f"Strict only: {strict} props with live edge ≥{config.PROP_MIN_EDGE:.0%} "
                    f"or model ≥{min_prob:.0%}. "
                    f"Toggle on to add {relaxed} relaxed research lines (≥{relaxed_floor:.0%} model)."
                ),
                text_color="#64748b",
            )

    def _paper_display_cap(self) -> int:
        return min(36, int(config.PROP_MAX_RESULTS))

    def _filter_singles(self, singles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        show_all = bool(self.show_all_var.get()) if self.show_all_var is not None else False
        if show_all and self._is_paper_profile():
            return singles
        return [s for s in singles if s.get("strict_qualified", True)]

    def _render_props_table(self, singles: list[dict[str, Any]]) -> None:
        cap = self._paper_display_cap() if self._is_paper_profile() else min(12, config.PROP_MAX_RESULTS)
        display = singles[:cap]
        table = PropsTable(
            self.scroll,
            height=min(cap, max(6, len(display) + 1)),
            book_name=self.book_name,
        )
        table.pack(fill="x", padx=2, pady=(2, 6))
        table.load_singles(display)
        if len(singles) > len(display):
            ctk.CTkLabel(
                self.scroll,
                text=f"Showing top {len(display)} of {len(singles)} props (Paper cap {cap}).",
                anchor="w",
                text_color="#64748b",
                font=ctk.CTkFont(size=11),
            ).pack(fill="x", padx=4, pady=(0, 4))

    def _empty_state_hint(
        self,
        *,
        total_found: int,
        strict_count: int,
        book_warning: str,
        book_disabled: bool = False,
    ) -> str:
        if book_disabled:
            return (
                f"{self.book_name.replace('.eu', '')} is unchecked in Budget Manager — "
                "enable it to see prop stakes and recommendations for this book."
            )
        min_prob = config.PROP_MIN_MODEL_PROB
        relaxed_floor = config.PROP_SHOW_ALL_MIN_PROB
        show_all = bool(self.show_all_var.get()) if self.show_all_var is not None else False
        relaxed = max(0, total_found - strict_count)

        if total_found and not show_all and self._is_paper_profile() and relaxed:
            return (
                f"No strict props to display ({strict_count} pass ≥{min_prob:.0%} model / "
                f"≥{config.PROP_MIN_EDGE:.0%} live edge).\n\n"
                f"{relaxed} relaxed candidates (model ≥{relaxed_floor:.0%}) are hidden — "
                "turn on **Show all props (relaxed)** above."
            )
        if total_found and show_all:
            return (
                f"All {total_found} ranked props are below the table display cap — "
                "try Refresh Next Two after odds load."
            )

        template = self._BOOK_EMPTY_HINTS.get(
            self.book_name,
            "No ranked props for this book yet. Refresh Next Two after ENABLE_PROPS=true.",
        )
        hint = template.format(min_prob=min_prob)
        if book_warning:
            hint = f"{book_warning}\n\n{hint}"
        return hint

    def _pack_empty_state(self, title: str, body: str, *, color: str = "#9ca3af") -> None:
        frame = ctk.CTkFrame(self.scroll, fg_color="#1a1f2e", corner_radius=8)
        frame.pack(fill="x", padx=8, pady=12)
        ctk.CTkLabel(
            frame,
            text=title,
            anchor="w",
            text_color="#e2e8f0",
            font=ctk.CTkFont(size=14, weight="bold"),
            wraplength=1000,
            justify="left",
        ).pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            frame,
            text=body,
            anchor="w",
            text_color=color,
            wraplength=1000,
            justify="left",
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", padx=14, pady=(0, 12))

    def _render_backtest(self, payload: "DashboardPayload") -> None:
        bt = payload.prop_backtest or {}
        if bt:
            roi = bt.get("roi_pct")
            acc = bt.get("acc_mean_prop_accuracy") or bt.get("mean_prop_accuracy")
            parts = []
            if roi is not None:
                parts.append(f"backtest prop ROI {float(roi):+.1f}%")
            if acc is not None:
                parts.append(f"mean prop accuracy {float(acc):.0%}")
            if parts:
                self.backtest_box.configure(text="Historical: " + "  |  ".join(parts))
                self.backtest_box.pack(fill="x", padx=12, pady=(0, 4))
            else:
                self.backtest_box.configure(text="")
                self.backtest_box.pack_forget()
        else:
            self.backtest_box.configure(text="")
            self.backtest_box.pack_forget()

    def _format_single_line(self, s: dict[str, Any]) -> tuple[str, str]:
        """Legacy one-line formatter (parlay legs)."""
        from src.parlay_builder import decimal_to_american

        am = decimal_to_american(float(s.get("odds", 0) or 0))
        source = str(s.get("odds_source", "synthetic")).lower()
        badge = "Live odds" if source == "live" else "Synthetic"
        badge_color = "#34d399" if source == "live" else "#fbbf24"
        edge_pct = s.get("edge_pct")
        if edge_pct is not None:
            edge_part = f"edge {float(edge_pct):+.1f}%"
        else:
            edge_part = f"model {float(s.get('prob', 0)):.0%}"
        text = (
            f"• {s.get('label', '')}  |  "
            f"prob {s.get('prob', 0):.0%}  |  "
            f"{am} ({s.get('odds', 0):.2f})  |  "
            f"{edge_part}  |  "
            f"{badge}"
        )
        line_color = badge_color if source == "live" else "#3dd68c"
        return text, line_color

    def render(
        self,
        payload: "DashboardPayload",
        *,
        budget_state: dict[str, Any] | None = None,
        profile: str | None = None,
    ) -> None:
        self._last_payload = payload
        self._last_budget_state = budget_state
        for w in self.scroll.winfo_children():
            w.destroy()
        self._update_show_all_visibility()

        overview = payload.books.get("Overview", {})
        alerts = overview.get("alerts") or {}
        from src.strategy import attach_prop_stakes, collect_dashboard_risk_warnings

        risk_warnings = collect_dashboard_risk_warnings(alerts, budget_state)
        _apply_risk_warning_label(self.risk_warning_box, risk_warnings)

        props_on = _ensure_props_config()
        if not props_on:
            self.summary.configure(text="Prop betting disabled — set ENABLE_PROPS=true in .env")
            self.filter_label.configure(text="")
            self.backtest_box.configure(text="")
            self._pack_empty_state(
                "Props disabled",
                "Add ENABLE_PROPS=true to .env, then click Refresh Next Two "
                "to load method and total markets from each book.",
                color="#fbbf24",
            )
            return

        if self.book_name == "MyBookie" and not config.MYBOOKIE_ENABLED:
            self.summary.configure(text="MyBookie disabled — set MYBOOKIE_ENABLED=true in .env")
            self.backtest_box.configure(text="")
            self._pack_empty_state(
                "MyBookie props unavailable",
                "Set MYBOOKIE_ENABLED=true in .env and refresh to fetch live method/total lines.",
            )
            return

        book_disabled = False
        pool_line = ""
        if budget_state:
            from src.strategy import allocate_card_budget_per_book, book_display_name

            plan = allocate_card_budget_per_book(budget_state, profile=profile)
            info = plan.get(self.book_name, {})
            book_disabled = not info.get("enabled", True)
            pool = float(info.get("allocation") or 0)
            if book_disabled:
                pool_line = f"{book_display_name(self.book_name)} disabled in Budget Manager."
            elif pool > 0:
                pool_line = f"Prop stake pool: ${pool:.2f} (from Budget Manager card budget)."

        has_cards = bool(payload.cards) or not payload.combined.empty
        if not has_cards:
            self.summary.configure(text=f"{self.book_name} — {self.book_note}")
            self.backtest_box.configure(text="")
            self._pack_empty_state(
                "No card loaded",
                "Click Refresh Next Two to pull fights, model predictions, and prop markets.",
                color="#fbbf24",
            )
            return

        cap = self._paper_display_cap() if self._is_paper_profile() else config.PROP_MAX_RESULTS
        self.summary.configure(
            text=(
                f"{self.book_name.replace('.eu', '')} props  |  "
                f"Up to {cap} lines  |  "
                f"Strict: ≥{config.PROP_MIN_MODEL_PROB:.0%} model or ≥{config.PROP_MIN_EDGE:.0%} live edge"
                + (f"  |  {pool_line}" if pool_line else "")
            )
        )
        self._render_backtest(payload)

        book = payload.books.get(self.book_name, {})
        props = book.get("props") or {}
        singles_all = props.get("singles") or []
        singles = self._filter_singles(singles_all)
        singles = attach_prop_stakes(singles, budget_state, self.book_name, profile=profile)
        meta = props.get("singles_meta") or {}
        parlays = props.get("parlays") or [] if self.show_parlays else []
        book_warning = str(book.get("warning") or book.get("error") or "").strip()

        total_found = int(meta.get("total_found", len(singles_all)))
        strict_count = int(meta.get("strict_count", 0))
        shown = len(singles)
        self._update_show_all_controls(meta, shown)
        if total_found:
            if shown < total_found:
                self.filter_label.configure(
                    text=f"Showing {shown} of {total_found} ranked props."
                )
            else:
                self.filter_label.configure(text=f"{total_found} props ranked for this card.")
        else:
            self.filter_label.configure(text="")

        if book_warning:
            ctk.CTkLabel(
                self.scroll,
                text=f"Note: {book_warning}",
                anchor="w",
                text_color="#fbbf24",
                font=ctk.CTkFont(size=12),
                wraplength=1100,
                justify="left",
            ).pack(fill="x", padx=8, pady=(4, 2))

        live_lines = props.get("live_prop_lines") or {}
        live_n = int(live_lines.get("live", 0))
        prop_rows = int(props.get("prop_odds_rows", 0))
        if prop_rows:
            ctk.CTkLabel(
                self.scroll,
                text=f"Live prop lines fetched: {live_n} (of {prop_rows} market rows)",
                anchor="w",
                text_color="#9ca3af",
                font=ctk.CTkFont(size=12),
            ).pack(fill="x", padx=8)

        if not singles and not parlays:
            relaxed_hidden = max(0, total_found - strict_count)
            hint = self._empty_state_hint(
                total_found=total_found,
                strict_count=strict_count,
                book_warning=book_warning,
                book_disabled=book_disabled,
            )
            title = "No props to show"
            if book_disabled:
                title = "Book disabled in Budget Manager"
            elif total_found and relaxed_hidden and not bool(self.show_all_var.get()):
                title = f"{relaxed_hidden} relaxed props hidden"
            self._pack_empty_state(title, hint.replace("**", ""))
            return

        if singles:
            self._render_props_table(singles)

        if parlays:
            ctk.CTkLabel(self.scroll, text="Prop / mixed parlays", anchor="w").pack(fill="x", padx=4, pady=(6, 0))
            for p in parlays[:5]:
                hdr = (
                    f"Parlay #{p.get('rank', 0)}  |  {p.get('n_legs', 0)}-Team  |  "
                    f"prob {p.get('combined_prob', 0):.0%}  |  "
                    f"odds {p.get('combined_odds', 0):.2f}  |  "
                    f"EV {p.get('expected_value', 0):+.0%}"
                )
                if p.get("correlation_adjusted"):
                    hdr += "  |  corr-adj"
                ctk.CTkLabel(self.scroll, text=hdr, anchor="w", text_color="#60a5fa").pack(fill="x", padx=8)
                for line in p.get("_leg_rows") or []:
                    ctk.CTkLabel(self.scroll, text=line, anchor="w").pack(fill="x", padx=16)


def _apply_risk_warning_label(label: ctk.CTkLabel, warnings: list[tuple[str, str]]) -> None:
    """Show or hide a tab warning label from unified risk warnings."""
    from src.strategy import format_risk_warnings

    text, color = format_risk_warnings(warnings)
    if text:
        label.configure(text=text, text_color=color)
        label.pack(fill="x", padx=12, pady=(8, 0))
    else:
        label.pack_forget()


def _budget_badge_style(pool_usd: float, *, books_enabled: bool) -> tuple[str, str]:
    """Badge colors for Available-this-card (uses strategy helper when present)."""
    try:
        from src.strategy import budget_availability_badge_style

        return budget_availability_badge_style(pool_usd, books_enabled=books_enabled)
    except ImportError:
        if not books_enabled:
            return "#451a1a", "#fca5a5"
        if pool_usd > 50:
            return "#14532d", "#86efac"
        if pool_usd >= 20:
            return "#713f12", "#fde047"
        return "#451a1a", "#fca5a5"


class BudgetManagerBar(_CTK_FRAME):
    """Bankroll, card budget, and per-book toggles — sits beside Profile selector."""

    def __init__(
        self,
        master,
        *,
        on_save: Callable[[dict[str, Any]], None],
        on_change: Callable[[dict[str, Any]], None] | None = None,
        profile_getter: Callable[[], str],
        leading_frame: _CTK_FRAME | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="#0f172a", corner_radius=10, border_width=1, border_color="#334155", **kwargs)
        self._on_save = on_save
        self._on_change = on_change
        self._profile_getter = profile_getter
        self._persisted_state = config.default_budget_state()
        self._refreshing = False
        self._refresh_after_id: str | None = None
        self._pending_notify_parent = True

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 8))
        if leading_frame is not None:
            leading_frame.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(
            header,
            text="Budget Manager",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#f8fafc",
        ).pack(side="left")

        self.available_badge = ctk.CTkFrame(header, fg_color="#14532d", corner_radius=8)
        self.available_badge.pack(side="right", padx=(12, 0))
        self.available_label = ctk.CTkLabel(
            self.available_badge,
            text="Available this card: —",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#86efac",
            anchor="e",
        )
        self.available_label.pack(padx=12, pady=6)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 6))

        self.total_bankroll_var = ctk.StringVar(value=f"{config.DEFAULT_TOTAL_BANKROLL:g}")
        self.card_budget_var = ctk.StringVar(value=f"{config.DEFAULT_CARD_BUDGET:g}")
        self.use_betnow_var = ctk.BooleanVar(value=True)
        self.use_dk_var = ctk.BooleanVar(value=True)
        self.use_myb_var = ctk.BooleanVar(value=True)

        _BANKROLL_TIP = (
            "Total betting bankroll. Enabled books split this evenly for per-book balances. "
            "Kelly % and Max Safe Bet use fractional Kelly from this total (0.5% hard cap)."
        )
        _CARD_BUDGET_TIP = (
            "Max dollars to risk on this card across all enabled books. "
            "Top 3 stakes are scaled proportionally to fit this pool."
        )

        self._add_field(
            row,
            "Total Bankroll $",
            self.total_bankroll_var,
            width=72,
            tooltip=_BANKROLL_TIP,
        )
        self.card_budget_label = ctk.CTkLabel(row, text="Card Budget $", anchor="w", width=96)
        self.card_budget_label.pack(side="left", padx=(10, 2))
        _ToolTip(self.card_budget_label, _CARD_BUDGET_TIP)
        self.card_budget_entry = ctk.CTkEntry(row, textvariable=self.card_budget_var, width=56)
        self.card_budget_entry.pack(side="left", padx=(0, 4))
        self.card_budget_entry.bind("<KeyRelease>", lambda _e: self._schedule_refresh())
        _ToolTip(self.card_budget_entry, _CARD_BUDGET_TIP)
        self.card_cap_hint = ctk.CTkLabel(row, text="", text_color="#9ca3af", anchor="w")
        self.card_cap_hint.pack(side="left", padx=(0, 10))
        self.live_cap_warning = ctk.CTkLabel(
            row,
            text="",
            text_color="#f87171",
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        )
        self.live_cap_warning.pack(side="left", padx=(0, 8))

        ctk.CTkCheckBox(
            row, text="Use BetNow", variable=self.use_betnow_var, width=100, command=self._schedule_refresh
        ).pack(side="left", padx=(0, 4))
        ctk.CTkCheckBox(
            row, text="Use DraftKings", variable=self.use_dk_var, width=118, command=self._schedule_refresh
        ).pack(side="left", padx=(0, 4))
        ctk.CTkCheckBox(
            row, text="Use MyBookie", variable=self.use_myb_var, width=118, command=self._schedule_refresh
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(row, text="Save", width=64, command=self._save).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            row,
            text="Reset",
            width=64,
            fg_color="#4b5563",
            hover_color="#6b7280",
            command=self._reset_defaults,
        ).pack(side="right")

        self.warning_box = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#f87171",
            wraplength=1100,
        )
        self.warning_box.pack(fill="x", padx=12, pady=(0, 8))
        self.warning_box.pack_forget()

    def _add_field(
        self,
        parent,
        label: str,
        var: ctk.StringVar,
        *,
        width: int = 72,
        tooltip: str | None = None,
    ) -> None:
        lbl = ctk.CTkLabel(parent, text=label, anchor="w", width=72)
        lbl.pack(side="left", padx=(0, 2))
        entry = ctk.CTkEntry(parent, textvariable=var, width=width)
        entry.pack(side="left", padx=(0, 4))
        entry.bind("<KeyRelease>", lambda _e: self._schedule_refresh())
        if tooltip:
            _ToolTip(lbl, tooltip)
            _ToolTip(entry, tooltip)

    def _parse_float(self, var: ctk.StringVar, default: float = 0.0) -> float:
        try:
            return max(float(str(var.get()).strip().replace("$", "").replace(",", "")), 0.0)
        except ValueError:
            return default

    def get_state(self) -> dict[str, Any]:
        br = self._parse_float(self.total_bankroll_var, config.DEFAULT_TOTAL_BANKROLL)
        n_enabled = max(
            sum(
                [
                    bool(self.use_betnow_var.get()),
                    bool(self.use_dk_var.get()),
                    bool(self.use_myb_var.get()),
                ]
            ),
            1,
        )
        per_book = br / n_enabled
        return {
            "total_bankroll": br,
            "card_budget": self._parse_float(self.card_budget_var, config.DEFAULT_CARD_BUDGET),
            "betnow_balance": per_book,
            "draftkings_balance": per_book,
            "mybookie_balance": per_book,
            "use_betnow": bool(self.use_betnow_var.get()),
            "use_draftkings": bool(self.use_dk_var.get()),
            "use_mybookie": bool(self.use_myb_var.get()),
        }

    def load(self, state: dict[str, Any]) -> None:
        normalized = config.normalize_budget_state(state)
        self._persisted_state = normalized
        self.total_bankroll_var.set(f"{normalized['total_bankroll']:.2f}".rstrip("0").rstrip("."))
        self.card_budget_var.set(f"{normalized['card_budget']:.2f}".rstrip("0").rstrip("."))
        self.use_betnow_var.set(bool(normalized["use_betnow"]))
        self.use_dk_var.set(bool(normalized["use_draftkings"]))
        self.use_myb_var.set(bool(normalized["use_mybookie"]))
        self._refresh_live(notify_parent=False)

    def refresh_warnings(self) -> None:
        """Refresh budget bar labels only — does not notify the app."""
        self._refresh_live(notify_parent=False)

    def _schedule_refresh(self, *, notify_parent: bool = True) -> None:
        """Debounce UI refresh to avoid re-entrant budget handler loops."""
        self._pending_notify_parent = notify_parent
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except Exception:
                pass
        self._refresh_after_id = self.after(10, self._run_scheduled_refresh)

    def _run_scheduled_refresh(self) -> None:
        self._refresh_after_id = None
        self._refresh_live(notify_parent=self._pending_notify_parent)

    def _refresh_live(self, *, notify_parent: bool = True) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh_live_ui()
            master = self.winfo_toplevel()
            payload = getattr(master, "_payload", None)
            if payload is not None:
                n = len(payload.combined) if not payload.combined.empty else 0
                _debug_log(f"BudgetManagerBar._refresh_live: current payload combined fights={n}")
            if notify_parent and self._on_change:
                state = config.normalize_budget_state(self.get_state())
                self._on_change(state)
        finally:
            self._refreshing = False

    def _refresh_live_ui(self) -> None:
        from src.strategy import (
            available_card_budget_text,
            available_card_budget_usd,
            collect_dashboard_risk_warnings,
            format_risk_warnings,
        )

        profile = self._profile_getter()
        state = config.normalize_budget_state(self.get_state())
        avail = available_card_budget_text(state, profile=profile)
        self.available_label.configure(text=avail)
        books_enabled = bool(
            state.get("use_betnow") or state.get("use_draftkings") or state.get("use_mybookie")
        )
        pool_usd = available_card_budget_usd(state, profile=profile)
        bg, fg = _budget_badge_style(pool_usd, books_enabled=books_enabled)
        self.available_badge.configure(fg_color=bg)
        self.available_label.configure(text_color=fg)

        is_live = config.normalize_profile(profile) == "live"
        br = float(state["total_bankroll"])
        live_cap = config.live_card_budget_cap_usd(br)
        raw_card = float(state["card_budget"])

        if is_live:
            over_cap = raw_card > live_cap
            self.card_cap_hint.configure(
                text=f"Live cap ${live_cap:g}",
                text_color="#f87171" if over_cap else "#9ca3af",
            )
            self.live_cap_warning.configure(
                text="Exceeds Live $12 cap — stakes use capped amount" if over_cap else "",
            )
            self.card_budget_label.configure(text_color="#f87171" if over_cap else "#e2e8f0")
            self.card_budget_entry.configure(
                border_color="#ef4444" if over_cap else "#565b5e",
                text_color="#fecaca" if over_cap else "#DCE4EE",
            )
        else:
            safe = config.max_card_stake_cap(br)
            self.card_cap_hint.configure(
                text=f"Safe ~${safe:g}",
                text_color="#9ca3af",
            )
            self.live_cap_warning.configure(text="")
            self.card_budget_label.configure(text_color="#e2e8f0")
            self.card_budget_entry.configure(border_color="#565b5e", text_color="#DCE4EE")

        warnings = collect_dashboard_risk_warnings(None, state, bankroll=br)
        text, color = format_risk_warnings(warnings, max_lines=3)
        if text:
            self.warning_box.configure(text=text, text_color=color)
            self.warning_box.pack(fill="x", padx=12, pady=(0, 8))
        else:
            self.warning_box.pack_forget()

    def _save(self) -> None:
        state = config.normalize_budget_state(self.get_state())
        profile = self._profile_getter()
        if config.normalize_profile(profile) == "live":
            live_cap = config.live_card_budget_cap_usd(state["total_bankroll"])
            if state["card_budget"] > live_cap:
                state["card_budget"] = live_cap
                self.card_budget_var.set(f"{live_cap:g}")
        saved = config.save_budget(state)
        self._persisted_state = saved
        self.load(saved)
        self._on_save(saved)

    def _reset_defaults(self) -> None:
        self.load(config.default_budget_state())


class GaneFoulScenarioPanel(_CTK_FRAME):
    """Prominent speculative panel for Pereira vs Gane foul/eye-poke tail risk."""

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, fg_color="#2a1215", corner_radius=8, **kwargs)
        self._border = ctk.CTkFrame(self, fg_color="#7f1d1d", corner_radius=10)
        self._border.pack(fill="x", padx=10, pady=(10, 6))
        self._inner = ctk.CTkFrame(self._border, fg_color="#1f1012", corner_radius=8)
        self._inner.pack(fill="x", padx=2, pady=2)

        ctk.CTkLabel(
            self._inner,
            text="⚠  GANE EYE POKE / FOUL SCENARIO",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#fca5a5",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(
            self._inner,
            text="HIGH RISK / SPECULATIVE — tail-event hedge only, not a model core pick",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#fbbf24",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 6))

        self.fight_label = ctk.CTkLabel(
            self._inner,
            text="Alex Pereira vs Ciryl Gane",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#e5e7eb",
            anchor="w",
        )
        self.fight_label.pack(fill="x", padx=12, pady=(0, 4))

        self.best_bet_label = ctk.CTkLabel(
            self._inner, text="", anchor="w", justify="left", wraplength=1050, text_color="#93c5fd"
        )
        self.best_bet_label.pack(fill="x", padx=12, pady=2)

        self.odds_label = ctk.CTkLabel(
            self._inner, text="", anchor="w", justify="left", wraplength=1050, text_color="#d1d5db"
        )
        self.odds_label.pack(fill="x", padx=12, pady=2)

        self.model_label = ctk.CTkLabel(
            self._inner, text="", anchor="w", justify="left", wraplength=1050, text_color="#3dd68c"
        )
        self.model_label.pack(fill="x", padx=12, pady=2)

        self.stake_label = ctk.CTkLabel(
            self._inner, text="", anchor="w", justify="left", text_color="#fde68a"
        )
        self.stake_label.pack(fill="x", padx=12, pady=2)

        self.explain_label = ctk.CTkLabel(
            self._inner,
            text="",
            anchor="w",
            justify="left",
            wraplength=1050,
            font=ctk.CTkFont(size=11),
            text_color="#9ca3af",
        )
        self.explain_label.pack(fill="x", padx=12, pady=(4, 10))

        self.empty_label = ctk.CTkLabel(
            self._inner,
            text="Load card data (Refresh) to evaluate the Gane foul scenario.",
            anchor="w",
            text_color="#6b7280",
        )
        self.empty_label.pack(fill="x", padx=12, pady=(0, 10))

    def render(self, scenario: dict[str, Any] | None) -> None:
        scenario = scenario or {}
        if not scenario.get("found"):
            self.fight_label.configure(text=scenario.get("fight_label", "Alex Pereira vs Ciryl Gane"))
            self.best_bet_label.configure(text="")
            self.odds_label.configure(text="")
            self.model_label.configure(text="")
            self.stake_label.configure(text="")
            self.explain_label.configure(text="")
            self.empty_label.configure(
                text=scenario.get("message", "Load card data (Refresh) to evaluate the Gane foul scenario.")
            )
            self.empty_label.pack(fill="x", padx=12, pady=(0, 10))
            return

        self.empty_label.pack_forget()
        best = scenario.get("best_bet") or {}
        book_quotes = scenario.get("book_quotes") or {}
        ml_prob = float(scenario.get("gane_ml_prob") or 0)
        ko_prob = float(scenario.get("gane_ko_prob") or 0)
        prop_key = str(best.get("prop_key", ""))
        model_p = float(best.get("model_prob") or (ml_prob if prop_key == "moneyline" else ko_prob))
        edge = best.get("edge")
        edge_txt = f"{float(edge):+.1%}" if edge is not None else "—"

        ev = str(scenario.get("event_name", "")).strip()
        fight_txt = scenario.get("fight_label", "Alex Pereira vs Ciryl Gane")
        if ev:
            fight_txt = f"{fight_txt}  ({ev})"
        self.fight_label.configure(text=fight_txt)

        self.best_bet_label.configure(
            text=(
                f"Best available proxy: {best.get('prop_label', '—')} @ {best.get('book', '—')}  "
                f"({best.get('american', '—')}, {float(best.get('decimal_odds', 0)):.2f} decimal)"
            )
        )

        lines = []
        for book in ("BetNow.eu", "DraftKings", "MyBookie"):
            q = book_quotes.get(book, {})
            short = book.replace(".eu", "")
            ml = q.get("moneyline_american", "—")
            ml_dec = q.get("moneyline_decimal")
            ml_part = f"ML {ml}" + (f" ({float(ml_dec):.2f})" if ml_dec else "")
            if q.get("method_available"):
                meth = q.get("method_american", "—")
                meth_dec = q.get("method_decimal")
                meth_part = f"KO/TKO {meth}" + (f" ({float(meth_dec):.2f})" if meth_dec else "")
                lines.append(f"{short}: {ml_part}  |  {meth_part}")
            else:
                lines.append(f"{short}: {ml_part}  |  KO/TKO —")
        self.odds_label.configure(text="Odds by book:  " + "     ".join(lines))

        self.model_label.configure(
            text=(
                f"Model: Gane ML {ml_prob:.1%}  |  Gane by KO/TKO (method proxy) {ko_prob:.1%}  |  "
                f"Edge on best line ({best.get('prop_label', '')}): {edge_txt}"
            )
        )

        stake = float(scenario.get("suggested_stake_usd") or 2.5)
        self.stake_label.configure(
            text=(
                f"Suggested speculative stake: ${stake:.2f} "
                f"(target range {scenario.get('stake_range', '$2–$3')} — lottery hedge only)"
            )
        )

        self.explain_label.configure(text=str(scenario.get("explanation", "")))


# --- Main application ---------------------------------------------------------


class UFCDashboardApp(_CTK_BASE):
    def __init__(self) -> None:
        if ctk is None or _CTK_BASE is object:
            raise RuntimeError("CustomTkinter failed to initialize")
        try:
            super().__init__()
        except Exception as exc:
            raise RuntimeError(f"Failed to create main window: {exc}") from exc

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("UFC Predictor Dashboard")
        self.geometry("1280x860")
        self.minsize(1024, 720)

        self._payload: DashboardPayload | None = None
        self._busy = False
        self._auto_watch = False
        self._last_full_refresh_ts: float | None = None
        self._last_odds_ts: float | None = None
        self._next_odds_ts: float | None = None
        self._next_card_ts: float | None = None
        self._model_ready = self._check_model_ready()
        self._render_token = 0
        self._busy_watchdog_id: str | None = None
        self._rendered_sections: set[str] = set()
        self._grok_result: dict[str, Any] | None = None
        self._grok_busy = False
        self._updating_budget = False
        self._budget_after_id: str | None = None

        config.UFC_PROFILE = "paper"
        config.apply_profile_overrides()
        self._budget_state = config.apply_budget_state()

        self.show_all_props_var = ctk.BooleanVar(value=False)
        self.profile_var = ctk.StringVar(value="Paper")
        self.event_var = ctk.StringVar(value="Next Two Cards")

        self._build_mode_banner()
        self.control_header = ctk.CTkFrame(self, fg_color="transparent")
        self.control_header.pack(fill="x", padx=8, pady=(4, 2))
        self._build_control_header()
        self._build_tabs()
        self._build_status_area()
        self._schedule_status_tick()
        self.after(100, self._ensure_controls_enabled)
        self.after(200, self._load_background_cache_on_startup)

    def _wrap_button_click(self, name: str, handler: Callable[..., None]) -> Callable[..., None]:
        """Bind a button command with debug logging and safe error handling."""

        def wrapped(*args, **kwargs) -> None:
            _debug_log(f"Button clicked: {name}")
            try:
                handler(*args, **kwargs)
            except Exception as exc:
                tb = traceback.format_exc()
                _debug_log(f"Button {name} failed: {tb}")
                self._show_error(f"{name}: {exc}")
                self._finish_busy()

        return wrapped

    def _ensure_controls_enabled(self) -> None:
        """Guarantee toolbar controls are interactive after startup/layout."""
        if self._busy:
            _debug_log("Controls check: busy — refresh buttons stay disabled until work finishes")
            return
        self._set_busy(False)
        _debug_log("Controls enabled (state=normal)")

    def _load_background_cache_on_startup(self) -> None:
        """Load midnight/startup background snapshot if fresh (<24h)."""
        if self._busy or self._payload is not None:
            return

        def worker() -> None:
            try:
                from src.background_runner import load_background_snapshot

                data = load_background_snapshot(max_age_hours=24)
                if data is None:
                    self.after(
                        0,
                        lambda: self.status.configure(
                            text="Ready — click Refresh to analyze (no recent background cache)."
                        ),
                    )
                    return

                payload = _result_to_payload(data)
                manifest = data.get("_manifest") or {}
                full_ts = self._iso_to_epoch(manifest.get("full_run_at") or manifest.get("saved_at"))
                odds_ts = self._iso_to_epoch(manifest.get("odds_updated_at"))

                def apply() -> None:
                    if self._payload is not None or self._busy:
                        return
                    if full_ts is not None:
                        self._last_full_refresh_ts = full_ts
                    if odds_ts is not None:
                        self._last_odds_ts = odds_ts
                    trigger = manifest.get("trigger", "background")
                    run_type = manifest.get("run_type", "full")
                    self._sync_profile_menu(payload.profile)
                    self._log_loaded_fights("background cache", payload)
                    self._apply_payload(payload, full_refresh=bool(full_ts), odds_refresh=bool(odds_ts))
                    self.status.configure(
                        text=f"Loaded background cache ({run_type}/{trigger}) — {payload.event_label}"
                    )

                self.after(0, apply)
            except Exception as exc:
                _debug_log(f"Background cache load failed: {exc}")
                self.after(
                    0,
                    lambda: self.status.configure(
                        text=f"Ready — click Refresh Next Two (cache load failed: {exc})."
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _iso_to_epoch(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            from src.background_runner import _parse_iso

            dt = _parse_iso(ts)
            return dt.timestamp() if dt else None
        except (ValueError, TypeError):
            return None

    def _check_model_ready(self) -> bool:
        try:
            from main import _model_exists

            return _model_exists()
        except Exception as exc:
            _debug_log(f"Model check failed: {exc}")
            return False

    def _build_status_area(self) -> None:
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=14, pady=(0, 8))

        self.status = ctk.CTkLabel(frame, text="Ready — click Refresh to analyze.", anchor="w")
        self.status.pack(fill="x")

        self.status_bar = ctk.CTkLabel(
            frame,
            text=self._format_status_bar(),
            anchor="w",
            text_color="#9ca3af",
            font=ctk.CTkFont(size=12),
        )
        self.status_bar.pack(fill="x", pady=(4, 0))

        self.progress = ctk.CTkProgressBar(frame, height=10)
        self.progress.set(0)
        self._progress_visible = False

    def _format_status_bar(self) -> str:
        model_txt = "Model ready" if self._model_ready else "Model missing"
        if self._last_full_refresh_ts is None:
            full_txt = "Last full refresh: —"
        else:
            mins = max(0, int((time.time() - self._last_full_refresh_ts) / 60))
            full_txt = f"Last full refresh: {mins} min ago" if mins else "Last full refresh: just now"
        if self._last_odds_ts is None:
            odds_txt = "Odds updated: —"
        else:
            mins = max(0, int((time.time() - self._last_odds_ts) / 60))
            odds_txt = f"Odds updated: {mins} min ago" if mins else "Odds updated: just now"
        return f"{model_txt}  |  {full_txt}  |  {odds_txt}"

    def _schedule_status_tick(self) -> None:
        self._update_status_bar()
        self.after(30_000, self._schedule_status_tick)

    def _update_status_bar(self) -> None:
        if hasattr(self, "status_bar"):
            self.status_bar.configure(text=self._format_status_bar())

    def _show_progress(self, pct: float | None) -> None:
        if pct is None:
            if self._progress_visible:
                self.progress.pack_forget()
                self._progress_visible = False
            return
        if not self._progress_visible:
            self.progress.pack(fill="x", pady=(6, 0))
            self._progress_visible = True
        self.progress.set(max(0.0, min(1.0, pct)))

    def _build_mode_banner(self) -> None:
        self.mode_banner = ctk.CTkFrame(self, height=56, corner_radius=0)
        self.mode_banner.pack(fill="x", padx=0, pady=0)
        self.mode_banner.pack_propagate(False)
        self.mode_banner_label = ctk.CTkLabel(
            self.mode_banner,
            text="",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="center",
            justify="center",
        )
        self.mode_banner_label.pack(fill="both", expand=True, padx=12, pady=6)
        self._update_mode_banner()

    def _update_mode_banner(self) -> None:
        br = float(self._budget_state.get("total_bankroll") or config.INITIAL_BANKROLL)
        cap = config.max_card_stake_cap(br)
        card_budget = float(self._budget_state.get("card_budget") or cap)
        if config.is_live_profile():
            max_bet = br * config.profile_value("max_bet_fraction")
            live_cap = config.live_card_budget_cap_usd(br)
            self.mode_banner.configure(fg_color="#7f1d1d")
            self.mode_banner_label.configure(
                text_color="#fecaca",
                text=(
                    "LIVE MODE — Real money at risk. Use small stakes and strict edge filters.\n"
                    f"Bankroll ${br:,.0f}  |  Card budget ${card_budget:,.0f} (hard cap ${live_cap:,.0f})  |  "
                    f"Max bet ~${max_bet:,.0f}  |  Min edge {config.ALERT_MIN_EDGE:.0%}"
                ),
            )
        else:
            self.mode_banner.configure(fg_color="#1e3a5f")
            self.mode_banner_label.configure(
                text_color="#bfdbfe",
                text=(
                    "PAPER MODE — Simulation only. No real wagers; use to test edges and sizing.\n"
                    f"Bankroll ${br:,.0f}  |  Card budget ${card_budget:,.0f} "
                    f"(safe ~${cap:,.0f})  |  Min edge {config.ALERT_MIN_EDGE:.0%}"
                ),
            )

    def _current_budget_state(self) -> dict[str, Any]:
        if hasattr(self, "budget_bar"):
            return config.normalize_budget_state(self.budget_bar.get_state())
        return self._budget_state

    def _overview_recommendations(self) -> list[dict[str, Any]]:
        """Top 3–5 picks for Overview, respecting budget, books, profile, and Grok."""
        if self._payload is None:
            return []
        from src.grok_analysis import apply_grok_kelly_adjustments
        from src.strategy import aggregate_overview_recommendations

        profile = self._profile_from_menu(self.profile_var.get())
        return apply_grok_kelly_adjustments(
            aggregate_overview_recommendations(
                self._payload.books, self._budget_state, limit=5, profile=profile
            ),
            self._grok_result,
        )

    def _refresh_top_recommendations(self) -> None:
        if hasattr(self, "top_bets_panel"):
            self.top_bets_panel.render(self._overview_recommendations())

    def _build_control_header(self) -> None:
        """Profile selector + Budget Manager + action toolbar."""
        selector_row = ctk.CTkFrame(self.control_header, fg_color="transparent")
        selector_row.pack(side="left", padx=(4, 0), pady=(6, 0))
        ctk.CTkLabel(selector_row, text="Profile", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(4, 4)
        )
        self.profile_menu = ctk.CTkOptionMenu(
            selector_row,
            variable=self.profile_var,
            values=["Paper", "Live"],
            width=110,
            command=self._wrap_button_click("Profile", self._on_profile_change),
        )
        self.profile_menu.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(selector_row, text="Event", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(4, 4)
        )
        self.event_menu = ctk.CTkOptionMenu(
            selector_row,
            variable=self.event_var,
            values=["Next Two Cards", "Next Card", "Freedom 250"],
            width=170,
        )
        self.event_menu.pack(side="left", padx=(0, 4))

        self.budget_bar = BudgetManagerBar(
            self.control_header,
            on_save=self._on_budget_saved,
            on_change=self._on_budget_live_change,
            profile_getter=lambda: self._profile_from_menu(self.profile_var.get()),
            leading_frame=selector_row,
        )
        self.budget_bar.pack(fill="x", padx=4, pady=(4, 2))
        self.budget_bar.load(self._budget_state)

        self._build_action_bar(self.control_header)

    def _build_budget_bar(self, master=None) -> None:
        """Legacy hook — budget bar is built via _build_control_header."""
        return

    def _profile_from_menu(self, display: str) -> str:
        return config.normalize_profile(display)

    def _sync_profile_menu(self, profile: str) -> None:
        p = config.normalize_profile(profile)
        self.profile_var.set("Live" if p == "live" else "Paper")

    def _build_action_bar(self, master=None) -> None:
        parent = master if master is not None else self
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(4, 2))

        self.refresh_btn = ctk.CTkButton(
            bar,
            text="Refresh Next Two",
            width=130,
            state="normal",
            command=self._wrap_button_click("Refresh Next Two", self._on_refresh),
        )
        self.refresh_btn.pack(side="left", padx=(8, 4))

        self.quick_odds_btn = ctk.CTkButton(
            bar,
            text="Quick Odds Refresh",
            width=150,
            state="normal",
            fg_color="#2d6a4f",
            hover_color="#40916c",
            command=self._wrap_button_click("Quick Odds Refresh", self._on_quick_odds),
        )
        self.quick_odds_btn.pack(side="left", padx=4)

        self.new_card_btn = ctk.CTkButton(
            bar,
            text="Process New Card",
            width=140,
            state="normal",
            fg_color="#7c3aed",
            hover_color="#8b5cf6",
            command=self._wrap_button_click("Process New Card", self._on_process_new_card),
        )
        self.new_card_btn.pack(side="left", padx=4)

        from src.grok_analysis import grok_available

        self.grok_btn = ctk.CTkButton(
            bar,
            text="Grok Analysis",
            width=130,
            state="normal",
            fg_color="#4c1d95" if grok_available() else "#374151",
            hover_color="#6d28d9" if grok_available() else "#4b5563",
            command=self._wrap_button_click("Grok Analysis", self._on_grok_analysis),
        )
        self.grok_btn.pack(side="left", padx=4)

        self.auto_watch_var = ctk.BooleanVar(value=False)
        self.auto_watch_switch = ctk.CTkSwitch(
            bar,
            text="Enable Auto Watch",
            variable=self.auto_watch_var,
            command=self._wrap_button_click("Auto Watch", self._on_auto_watch_toggle),
        )
        self.auto_watch_switch.pack(side="left", padx=12)

        self.meta_label = ctk.CTkLabel(bar, text="", text_color="#9ca3af")
        self.meta_label.pack(side="right", padx=12)

    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=6)

        self.tab_overview = self.tabs.add("Overview")
        self.tab_betnow = self.tabs.add("BetNow.eu")
        self.tab_dk = self.tabs.add("DraftKings")
        self.tab_mybookie = self.tabs.add("MyBookie")
        self.tab_next_two = self.tabs.add("Next Two Cards")
        self.tab_props_betnow = self.tabs.add("Props - BetNow")
        self.tab_props_dk = self.tabs.add("Props - DraftKings")
        self.tab_props_mybookie = self.tabs.add("Props - MyBookie")
        self.tab_risk = self.tabs.add("Risk Analysis")
        self.tab_grok = self.tabs.add("Grok Analysis")

        self.top_bets_panel = TopRecommendedBetsPanel(self.tab_overview)
        self.top_bets_panel.pack(fill="x", padx=8, pady=(8, 8))
        self.overview_summary = ctk.CTkLabel(
            self.tab_overview,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8",
        )
        self.overview_summary.pack(fill="x", padx=12, pady=(0, 4))
        self.overview_risk_box = ctk.CTkLabel(
            self.tab_overview,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#f87171",
            wraplength=1100,
        )
        self.overview_risk_box.pack(fill="x", padx=12, pady=(0, 4))
        self.overview_risk_box.pack_forget()
        self.gane_foul_panel = GaneFoulScenarioPanel(self.tab_overview)
        self.gane_foul_panel.pack(fill="x", padx=8, pady=(0, 6))
        ctk.CTkLabel(
            self.tab_overview,
            text="All Fights",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            text_color="#cbd5e1",
        ).pack(fill="x", padx=12, pady=(4, 2))
        self.overview_table = DataTable(self.tab_overview, compact=True, height=10)
        self.overview_table.pack(fill="both", expand=True, padx=10, pady=6)

        self.grok_panel = GrokAnalysisPanel(
            self.tab_grok,
            on_run=self._wrap_button_click("Grok Analysis Tab", self._on_grok_analysis),
        )
        self.grok_panel.pack(fill="both", expand=True, padx=8, pady=8)

        self.betnow_tab = BookTab(self.tab_betnow, "BetNow.eu")
        self.betnow_tab.pack(fill="both", expand=True)
        self.dk_tab = BookTab(self.tab_dk, "DraftKings")
        self.dk_tab.pack(fill="both", expand=True)
        self.mybookie_tab = BookTab(self.tab_mybookie, "MyBookie")
        self.mybookie_tab.pack(fill="both", expand=True)

        self.next_two_scroll = ctk.CTkScrollableFrame(
            self.tab_next_two, label_text="Upcoming cards (closest first)"
        )
        self.next_two_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        self.props_betnow_tab = BookPropsTab(
            self.tab_props_betnow,
            book_name="BetNow.eu",
            book_note="Singles only — props cannot be parlayed",
            show_parlays=False,
            show_all_var=self.show_all_props_var,
            profile_getter=lambda: self._profile_from_menu(self.profile_var.get()),
        )
        self.props_betnow_tab.pack(fill="both", expand=True)
        self.props_dk_tab = BookPropsTab(
            self.tab_props_dk,
            book_name="DraftKings",
            book_note="Singles + 2–3 leg prop/mixed parlays (correlation-adjusted)",
            show_parlays=True,
            show_all_var=self.show_all_props_var,
            profile_getter=lambda: self._profile_from_menu(self.profile_var.get()),
        )
        self.props_dk_tab.pack(fill="both", expand=True)
        self.props_mybookie_tab = BookPropsTab(
            self.tab_props_mybookie,
            book_name="MyBookie",
            book_note="Singles + 2–3 leg prop/mixed parlays (live method/round props when scraped)",
            show_parlays=True,
            show_all_var=self.show_all_props_var,
            profile_getter=lambda: self._profile_from_menu(self.profile_var.get()),
        )
        self.props_mybookie_tab.pack(fill="both", expand=True)

        try:
            self.tabs._segmented_button.configure(command=self._on_tab_selected)
        except Exception:
            _debug_log("Tab lazy-load hook unavailable on this CustomTkinter build")

        self.risk_summary = ctk.CTkLabel(self.tab_risk, text="", anchor="w", justify="left")
        self.risk_summary.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(
            self.tab_risk,
            text="Model Insights — discovered interaction features",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            text_color="#cbd5e1",
        ).pack(fill="x", padx=12, pady=(4, 2))
        self.model_insights_box = ctk.CTkLabel(
            self.tab_risk,
            text="Train the model to populate interaction discoveries.",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8",
            wraplength=1100,
        )
        self.model_insights_box.pack(fill="x", padx=12, pady=(0, 8))
        self.risk_chart_frame = ctk.CTkFrame(self.tab_risk)
        self.risk_chart_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self._risk_fig = Figure(figsize=(7, 3.2), dpi=100, facecolor="#1a1a1a")
        self._risk_canvas = FigureCanvasTkAgg(self._risk_fig, master=self.risk_chart_frame)
        self._risk_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.threshold_chart_frame = ctk.CTkFrame(self.tab_risk)
        self.threshold_chart_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self._thresh_fig = Figure(figsize=(7, 2.8), dpi=100, facecolor="#1a1a1a")
        self._thresh_canvas = FigureCanvasTkAgg(self._thresh_fig, master=self.threshold_chart_frame)
        self._thresh_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _on_profile_change(self, value: str) -> None:
        _debug_log(f"Profile changed: {value}")
        config.UFC_PROFILE = self._profile_from_menu(value)
        config.apply_profile_overrides()
        self._update_mode_banner()
        if config.is_live_profile():
            self.show_all_props_var.set(False)
        elif config.is_paper_profile():
            self.show_all_props_var.set(True)
        if hasattr(self, "budget_bar"):
            self.budget_bar.refresh_warnings()
        if self._payload is not None:
            self._render_all_tabs(self._payload)

    def _on_budget_live_change(self, state: dict[str, Any]) -> None:
        self._budget_state = state
        if self._updating_budget:
            return
        if self._budget_after_id is not None:
            try:
                self.after_cancel(self._budget_after_id)
            except Exception:
                pass
        self._budget_after_id = self.after(10, self._apply_budget_live_change)

    def _apply_budget_live_change(self) -> None:
        self._budget_after_id = None
        if self._updating_budget:
            return
        self._updating_budget = True
        try:
            self._update_mode_banner()
            self._refresh_top_recommendations()
        finally:
            self._updating_budget = False

    def _on_budget_saved(self, state: dict[str, Any]) -> None:
        self._budget_state = state
        config.apply_profile_overrides()
        self._update_mode_banner()
        self.status.configure(text="Budget saved (data/budget.json + .env)")
        if self._payload is not None:
            self._render_all_tabs(self._payload)

    def _on_auto_watch_toggle(self) -> None:
        self._auto_watch = bool(self.auto_watch_var.get())
        now = time.time()
        if self._auto_watch:
            self._next_card_ts = now + config.DASHBOARD_CARD_CHECK_MINUTES * 60
            self._next_odds_ts = now + config.DASHBOARD_AUTO_ODDS_MINUTES * 60
            self.status.configure(text="Auto watch enabled — monitoring card + odds.")
            self._auto_watch_tick()
        else:
            self._next_card_ts = None
            self._next_odds_ts = None
            self.status.configure(text="Auto watch disabled.")
        self._update_status_bar()

    def _auto_watch_tick(self) -> None:
        if not self._auto_watch:
            return
        now = time.time()
        if self._next_card_ts and now >= self._next_card_ts and not self._busy:
            self._next_card_ts = now + config.DASHBOARD_CARD_CHECK_MINUTES * 60
            threading.Thread(target=self._check_card_change_worker, daemon=True).start()
        if (
            self._next_odds_ts
            and now >= self._next_odds_ts
            and not self._busy
            and self._payload is not None
            and not self._payload.combined.empty
        ):
            self._next_odds_ts = now + config.DASHBOARD_AUTO_ODDS_MINUTES * 60
            self._run_quick_odds_async(auto=True)
        self.after(30_000, self._auto_watch_tick)

    def _check_card_change_worker(self) -> None:
        try:
            changed, event_name, _ = detect_card_change(event_index=0)
            if changed:
                self.after(
                    0,
                    lambda: self._on_new_card_detected(event_name),
                )
        except Exception as exc:
            self.after(0, lambda: self.status.configure(text=f"Card check failed: {exc}"))

    def _on_new_card_detected(self, event_name: str) -> None:
        self.status.configure(text=f"New card detected: {event_name} — running analysis…")
        if event_name and event_name not in self.event_menu.cget("values"):
            vals = list(self.event_menu.cget("values"))
            if event_name not in vals:
                vals.insert(0, event_name)
                self.event_menu.configure(values=vals)
        self.event_var.set(event_name if event_name else "Next Card")
        self._run_new_card_analysis(event_name)

    def _on_progress(self, msg: str, pct: float | None = None) -> None:
        self.status.configure(text=msg)
        self._show_progress(pct)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        _debug_log(f"Busy={busy} — toolbar state={state}")

        if self._busy_watchdog_id is not None:
            try:
                self.after_cancel(self._busy_watchdog_id)
            except Exception:
                pass
            self._busy_watchdog_id = None
        if busy:
            self._busy_watchdog_id = self.after(600_000, self._busy_watchdog)

        self.refresh_btn.configure(state=state)
        self.quick_odds_btn.configure(state=state)
        self.new_card_btn.configure(state=state)
        if not busy:
            self.quick_odds_btn.configure(fg_color="#2d6a4f", hover_color="#40916c")
            self.new_card_btn.configure(fg_color="#7c3aed", hover_color="#8b5cf6")
        self.profile_menu.configure(state=state)
        self.event_menu.configure(state=state)

    def _busy_watchdog(self) -> None:
        self._busy_watchdog_id = None
        if not self._busy:
            return
        _debug_log("Busy watchdog fired — resetting stuck busy state")
        self.status.configure(text="Operation timed out — controls re-enabled. Try Refresh again.")
        self._finish_busy()

    def _log_loaded_fights(self, source: str, payload: DashboardPayload) -> None:
        """Debug log for fight row counts after refresh or cache load."""
        combined = payload.combined
        n_combined = len(combined) if combined is not None and not combined.empty else 0
        ov = payload.books.get("Overview", {}).get("predictions", pd.DataFrame())
        n_overview = len(ov) if isinstance(ov, pd.DataFrame) and not ov.empty else 0
        card_counts = [
            len(c.get("predictions", []))
            for c in payload.cards
            if isinstance(c.get("predictions"), pd.DataFrame) and not c["predictions"].empty
        ]
        _debug_log(
            f"{source}: fights loaded combined={n_combined} overview={n_overview} "
            f"cards={len(payload.cards)} per_card={card_counts} label={payload.event_label!r}"
        )

    def _on_refresh(self) -> None:
        """Full prediction + odds for the two soonest upcoming cards."""
        if self._busy:
            _debug_log("Refresh ignored — already busy")
            self.status.configure(text="Already running — wait for the current operation to finish.")
            return
        self._set_busy(True)
        self.event_var.set("Next Two Cards")
        self._on_progress("Refresh: next two upcoming cards (predictions + odds)…", 0.02)

        def worker() -> None:
            try:
                _ensure_props_config()
                payload = run_dashboard_analysis(
                    event_mode="Next Two Cards",
                    profile=self._profile_from_menu(self.profile_var.get()),
                    force_refresh_odds=True,
                    explain=True,
                    use_cache=True,
                    budget_state=self._current_budget_state(),
                    progress=lambda m, p=None: self.after(0, lambda msg=m, pct=p: self._on_progress(msg, pct)),
                )
                self.after(0, lambda: self._apply_payload(payload, full_refresh=True, odds_refresh=True))
            except Exception as exc:
                tb = traceback.format_exc()
                _debug_log(tb)
                self.after(0, lambda: self._show_error(f"{exc}\n{tb}"))
            finally:
                self.after(0, self._finish_busy)

        threading.Thread(target=worker, daemon=True).start()

    def _on_process_new_card(self) -> None:
        if self._busy:
            _debug_log("Process New Card ignored — already busy")
            self.status.configure(text="Already running — wait for the current operation to finish.")
            return
        self._set_busy(True)
        self._on_progress("Checking for new UFC card…", 0.05)

        def worker() -> None:
            delegated = False
            try:
                changed, event_name, _ = detect_card_change(event_index=0)
                if not changed:
                    self.after(
                        0,
                        lambda: self.status.configure(text="No new card detected — cache is current."),
                    )
                    return
                delegated = True
                self.after(0, lambda: self._run_new_card_analysis(event_name))
            except Exception as exc:
                tb = traceback.format_exc()
                _debug_log(tb)
                self.after(0, lambda: self._show_error(str(exc)))
            finally:
                if not delegated:
                    self.after(0, self._finish_busy)

        threading.Thread(target=worker, daemon=True).start()

    def _run_new_card_analysis(self, event_name: str) -> None:
        self._set_busy(True)
        self._on_progress(f"New card: {event_name} — full analysis…", 0.05)
        if event_name:
            vals = list(self.event_menu.cget("values"))
            if event_name not in vals:
                vals.insert(0, event_name)
                self.event_menu.configure(values=vals)
            self.event_var.set(event_name)

        def worker() -> None:
            try:
                payload = run_dashboard_analysis(
                    event_mode=self.event_var.get(),
                    profile=self._profile_from_menu(self.profile_var.get()),
                    force_refresh_odds=True,
                    explain=True,
                    use_cache=True,
                    budget_state=self._current_budget_state(),
                    progress=lambda m, p=None: self.after(0, lambda msg=m, pct=p: self._on_progress(msg, pct)),
                )
                self.after(0, lambda: self._apply_payload(payload, full_refresh=True, odds_refresh=True))
            except Exception as exc:
                tb = traceback.format_exc()
                _debug_log(tb)
                self.after(0, lambda: self._show_error(f"{exc}\n{tb}"))
            finally:
                self.after(0, self._finish_busy)

        threading.Thread(target=worker, daemon=True).start()

    def _on_quick_odds(self) -> None:
        if self._busy:
            _debug_log("Quick Odds ignored — already busy")
            self.status.configure(text="Already running — wait for the current operation to finish.")
            return
        if self._payload is None or self._payload.combined.empty:
            self.status.configure(text="Run Full Refresh first — need cached predictions.")
            return
        self._run_quick_odds_async(auto=False)

    def _run_quick_odds_async(self, *, auto: bool) -> None:
        if self._busy or self._payload is None:
            return
        self._set_busy(True)
        books_q = "BetNow + DraftKings" + (" + MyBookie" if config.MYBOOKIE_ENABLED else "")
        label = "Auto quick odds…" if auto else f"Quick odds refresh ({books_q})…"
        self._on_progress(label, 0.1)
        base = self._payload.combined.copy()
        event_label = self._payload.event_label

        def worker() -> None:
            try:
                result = run_quick_odds_refresh(
                    base,
                    event_label=event_label,
                    budget_state=self._current_budget_state(),
                    progress=lambda m, p=None: self.after(0, lambda msg=m, pct=p: self._on_progress(msg, pct)),
                )
                self.after(0, lambda: self._apply_quick_odds(result))
            except Exception as exc:
                self.after(0, lambda: self._show_error(str(exc)))
            finally:
                self.after(0, self._finish_busy)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_quick_odds(self, result: dict[str, Any]) -> None:
        if self._payload is None:
            return
        books = result.get("books", {})
        for name, data in books.items():
            self._payload.books[name] = data
        if result.get("threshold_ctx"):
            self._payload.threshold_ctx = result["threshold_ctx"]
        self._payload.odds_updated_at = result.get("odds_updated_at", self._payload.odds_updated_at)
        self._apply_payload(self._payload, odds_refresh=True, quick=True)

    def _finish_busy(self) -> None:
        self._set_busy(False)
        self._show_progress(None)

    def _show_error(self, msg: str) -> None:
        self.status.configure(text=f"Error: {msg[:240]}")

    def _apply_payload(
        self,
        payload: DashboardPayload,
        *,
        full_refresh: bool = False,
        odds_refresh: bool = False,
        quick: bool = False,
    ) -> None:
        self._log_loaded_fights("_apply_payload", payload)
        self._payload = payload
        now = time.time()
        if full_refresh:
            self._last_full_refresh_ts = now
        if odds_refresh:
            self._last_odds_ts = now
            if self._auto_watch:
                self._next_odds_ts = now + config.DASHBOARD_AUTO_ODDS_MINUTES * 60
        self._update_status_bar()
        cache_note = " (cached features)" if payload.from_cache else ""
        quick_note = " — quick odds" if quick else ""
        self.meta_label.configure(
            text=f"{payload.generated_at or '—'}  |  {payload.event_label}  |  {config.normalize_profile(payload.profile)}{cache_note}"
        )
        if payload.errors:
            self.status.configure(text="Done with warnings: " + "; ".join(payload.errors[:2]) + quick_note)
        elif quick:
            self.status.configure(text=f"Quick odds updated at {payload.odds_updated_at or 'now'}.")
        else:
            self.status.configure(text=f"Refresh complete — {payload.event_label}")

        try:
            self._schedule_render_all_tabs(payload)
        except Exception as exc:
            tb = traceback.format_exc()
            _debug_log(f"Tab render error: {tb}")
            self._show_error(f"Tab render failed: {exc}")

    def _on_tab_selected(self, tab_name: str) -> None:
        """Lazy-render heavy tabs on first visit to reduce startup RAM/CPU."""
        if self._payload is None:
            return
        self._render_tab_lazy(tab_name)

    def _render_tab_lazy(self, tab_name: str) -> None:
        payload = self._payload
        if payload is None:
            return
        if tab_name == "Next Two Cards" and "next_two" not in self._rendered_sections:
            self._render_next_two_section(payload)
            self._rendered_sections.add("next_two")
        elif tab_name.startswith("Props") and "props" not in self._rendered_sections:
            self._render_props_section(payload)
            self._rendered_sections.add("props")
        elif tab_name == "Risk Analysis" and "risk" not in self._rendered_sections:
            self._render_risk_section(payload)
            self._rendered_sections.add("risk")
        elif tab_name == "Grok Analysis" and "grok" not in self._rendered_sections:
            self._render_grok_section()
            self._rendered_sections.add("grok")

    def _render_grok_section(self) -> None:
        from src.grok_analysis import grok_available

        self.grok_panel.render(self._grok_result, available=grok_available())

    def _on_grok_analysis(self) -> None:
        if self._grok_busy:
            self.status.configure(text="Grok analysis already running…")
            return
        if self._payload is None or not self._payload.books:
            self.status.configure(text="Run Refresh Next Two first — need card data for Grok.")
            return
        self._run_grok_analysis_async()

    def _run_grok_analysis_async(self) -> None:
        from src.grok_analysis import analyze_card_with_grok, grok_available

        if not grok_available():
            self.status.configure(text="Grok disabled — set GROK_ENABLED=true and API key in .env")
            self._render_grok_section()
            return

        self._grok_busy = True
        self.grok_panel.set_busy(True, "Running Grok analysis…")
        self.grok_btn.configure(state="disabled")
        self.status.configure(text="Grok analysis running (non-blocking)…")
        payload = self._payload
        budget = self._current_budget_state()
        event_label = payload.event_label if payload else ""

        def worker() -> None:
            try:
                result = analyze_card_with_grok(
                    payload.books if payload else {},
                    budget,
                    event_label=event_label,
                )
                self.after(0, lambda: self._apply_grok_result(result))
            except Exception as exc:
                self.after(0, lambda: self._apply_grok_result({"ok": False, "error": str(exc), "picks": []}))
            finally:
                self.after(0, self._finish_grok_busy)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_grok_result(self, result: dict[str, Any]) -> None:
        self._grok_result = result
        self._render_grok_section()
        if result.get("ok"):
            n = len(result.get("picks") or [])
            cache = " (cached)" if result.get("from_cache") else ""
            self.status.configure(text=f"Grok complete — {n} picks analyzed{cache}. Kelly sizing updated on Overview.")
            if self._payload is not None:
                self._render_overview_section(self._payload)
        else:
            self.status.configure(text=f"Grok failed: {str(result.get('error') or 'unknown')[:180]}")

    def _finish_grok_busy(self) -> None:
        self._grok_busy = False
        self.grok_panel.set_busy(False)
        try:
            self.grok_btn.configure(state="normal")
        except Exception:
            pass

    def _schedule_render_all_tabs(self, payload: DashboardPayload) -> None:
        """Render overview + books immediately; defer heavy tabs until visited."""
        self._render_token += 1
        token = self._render_token
        self._render_payload = payload
        self._rendered_sections.clear()
        _debug_log("Rendering overview + books (lazy tabs deferred)")
        props_enabled = _ensure_props_config()
        _debug_log(f"Rendering Props tabs: {'enabled' if props_enabled else 'disabled'}")
        self._render_overview_section(payload)
        self._rendered_sections.add("overview")
        self._render_books_section(payload)
        self._rendered_sections.add("books")
        if props_enabled:
            self._render_props_section(payload)
            self._rendered_sections.add("props")
        current = self.tabs.get() if hasattr(self, "tabs") else "Overview"
        self.after(1, lambda: self._render_tab_lazy(current))
        self.after(800, lambda t=token: self._render_idle_deferred(t))

    def _render_idle_deferred(self, token: int) -> None:
        """Warm props/risk in background if user has not opened them yet."""
        if token != self._render_token or self._payload is None:
            return
        if "props" not in self._rendered_sections:
            self._render_props_section(self._payload)
            self._rendered_sections.add("props")
        if "risk" not in self._rendered_sections:
            self._render_risk_section(self._payload)
            self._rendered_sections.add("risk")

    def _render_overview_section(self, payload: DashboardPayload) -> None:
        overview = payload.books.get("Overview", {})
        alerts = overview.get("alerts") or {}
        bankroll = float(
            self._budget_state.get("total_bankroll") or alerts.get("bankroll") or config.INITIAL_BANKROLL
        )
        strategy = strategy_from_profile(bankroll=bankroll)

        # Overview
        singles_n = int(alerts.get("singles_count") or len(alerts.get("singles") or []))
        parlays_n = int(alerts.get("parlays_count") or len(alerts.get("parlays") or []))
        self.overview_summary.configure(
            text=(
                f"{payload.event_label}  |  "
                f"{singles_n} qualifying singles  |  {parlays_n} parlays  |  "
                f"Profile: {config.normalize_profile(payload.profile)}"
            )
        )
        preds = overview.get("predictions", pd.DataFrame())
        if (preds is None or (isinstance(preds, pd.DataFrame) and preds.empty)) and not payload.combined.empty:
            preds = payload.combined
        self.overview_table.load_rows(_rows_for_table(preds, bankroll, strategy, compact=True))

        bs = self._budget_state
        from src.strategy import collect_dashboard_risk_warnings, format_risk_warnings

        self.top_bets_panel.render(self._overview_recommendations())

        overview_risks = collect_dashboard_risk_warnings(alerts, bs, bankroll=bankroll)
        risk_txt, risk_color = format_risk_warnings(overview_risks, max_lines=3)
        if risk_txt:
            self.overview_risk_box.configure(text=risk_txt, text_color=risk_color)
            self.overview_risk_box.pack(fill="x", padx=12, pady=(0, 6))
        else:
            self.overview_risk_box.pack_forget()

        try:
            from src.gane_foul_scenario import build_gane_foul_scenario

            foul_scenario = build_gane_foul_scenario(
                overview_predictions=preds,
                books=payload.books,
            )
            self.gane_foul_panel.render(foul_scenario)
        except Exception as exc:
            _debug_log(f"Gane foul scenario render failed: {exc}")
            self.gane_foul_panel.render({"found": False, "message": f"Scenario unavailable: {exc}"})

    def _book_tab_data(self, payload: DashboardPayload, book_key: str) -> dict[str, Any]:
        """Book payload for a tab, falling back to combined fights when predictions missing."""
        data = dict(payload.books.get(book_key) or {})
        preds = data.get("predictions", pd.DataFrame())
        if not isinstance(preds, pd.DataFrame) or preds.empty:
            if not payload.combined.empty:
                data = {
                    **data,
                    "predictions": payload.combined,
                    "odds_total": len(payload.combined),
                    "odds_matched": int(payload.combined.get("odds_matched", pd.Series(False)).sum())
                    if "odds_matched" in payload.combined.columns
                    else 0,
                }
        return data

    def _render_books_section(self, payload: DashboardPayload) -> None:
        ctx = payload.threshold_ctx or {}
        bs = self._budget_state
        profile = self._profile_from_menu(self.profile_var.get())
        self.betnow_tab.render(self._book_tab_data(payload, "BetNow.eu"), ctx, budget_state=bs, profile=profile)
        self.dk_tab.render(self._book_tab_data(payload, "DraftKings"), ctx, budget_state=bs, profile=profile)
        if config.MYBOOKIE_ENABLED:
            self.mybookie_tab.render(self._book_tab_data(payload, "MyBookie"), ctx, budget_state=bs, profile=profile)
        else:
            self.mybookie_tab.summary.configure(
                text="MyBookie disabled — set MYBOOKIE_ENABLED=true in .env and refresh."
            )

    def _render_next_two_section(self, payload: DashboardPayload) -> None:
        ctx = payload.threshold_ctx or {}
        bankroll = float(
            self._budget_state.get("total_bankroll") or config.INITIAL_BANKROLL
        )
        strategy = strategy_from_profile(bankroll=bankroll)

        for w in self.next_two_scroll.winfo_children():
            w.destroy()
        if not payload.cards:
            ctk.CTkLabel(
                self.next_two_scroll,
                text="No upcoming cards loaded — click Refresh Next Two.",
                anchor="w",
            ).pack(fill="x", padx=8, pady=8)
        for card in payload.cards:
            ev = card.get("event_name", "Card")
            cp = card.get("predictions", pd.DataFrame())
            ctk.CTkLabel(
                self.next_two_scroll,
                text=f"▸ {ev}  ({len(cp)} fights)",
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            ).pack(fill="x", pady=(8, 4))
            sub = DataTable(self.next_two_scroll, height=8)
            sub.pack(fill="x", padx=4, pady=4)
            dk = payload.books.get("DraftKings", {}).get("predictions", cp)
            if isinstance(dk, pd.DataFrame) and "event_name" in dk.columns:
                sub_df = dk[dk["event_name"] == ev]
            else:
                sub_df = cp
            sub.load_rows(_rows_for_table(sub_df, bankroll, strategy))

            try:
                from src.parlay_builder import ranked_parlays_for_card

                card_parlays = ranked_parlays_for_card(
                    sub_df,
                    bankroll=bankroll,
                    use_dynamic=ctx.get("use_dynamic"),
                    event_name=ev,
                )
                if card_parlays:
                    parlay_box = ctk.CTkFrame(self.next_two_scroll, fg_color="transparent")
                    parlay_box.pack(fill="x", padx=4, pady=(0, 8))
                    _render_ranked_parlays(
                        parlay_box,
                        card_parlays,
                        title=f"Same-card parlays — {ev}",
                        preds=sub_df,
                    )
            except Exception as exc:
                _debug_log(f"Parlay render skipped for {ev}: {exc}")

    def _render_props_section(self, payload: DashboardPayload) -> None:
        props_enabled = _ensure_props_config()
        _debug_log(f"Rendering Props tabs: {'enabled' if props_enabled else 'disabled'}")
        if not props_enabled:
            return
        bs = self._budget_state
        profile = self._profile_from_menu(self.profile_var.get())
        self.props_betnow_tab.render(payload, budget_state=bs, profile=profile)
        self.props_dk_tab.render(payload, budget_state=bs, profile=profile)
        self.props_mybookie_tab.render(payload, budget_state=bs, profile=profile)

    def _render_risk_section(self, payload: DashboardPayload) -> None:
        self._render_risk_tab(payload, budget_state=self._budget_state)

    def _render_all_tabs(self, payload: DashboardPayload) -> None:
        """Re-render all tabs (profile/budget changes) without blocking one long paint."""
        self._schedule_render_all_tabs(payload)

    def _render_model_insights_panel(self) -> None:
        """Show top discovered interaction features from the latest train run."""
        lines: list[str] = []
        try:
            import config

            paths = [
                config.DISCOVERED_INTERACTIONS_PATH,
                config.FEATURE_IMPORTANCE_PATH,
            ]
            discovered: dict[str, Any] = {}
            for path in paths:
                if path.is_file():
                    discovered = json.loads(path.read_text(encoding="utf-8"))
                    if discovered.get("top_interaction_importance") or discovered.get("insights"):
                        break

            top_ix = discovered.get("top_interaction_importance") or []
            if not top_ix and discovered.get("importance"):
                imp = discovered.get("importance") or {}
                top_ix = [
                    {"feature": k, "importance": v, "label": k}
                    for k, v in sorted(imp.items(), key=lambda kv: kv[1], reverse=True)
                    if str(k).startswith("ix_")
                ][:10]

            insights = discovered.get("insights") or discovered.get("interaction_insights") or []
            if top_ix:
                lines.append("Top interaction features (model importance):")
                for i, row in enumerate(top_ix[:10], start=1):
                    feat = row.get("feature", "")
                    label = row.get("label", feat)
                    imp = float(row.get("importance", 0.0)) * 100
                    lines.append(f"  {i:2}. {label} ({feat}) — {imp:.1f}% importance")
            if insights:
                lines.append("")
                lines.append("Discovered patterns (train split):")
                for row in insights[:8]:
                    lines.append(f"  • {row.get('message', row.get('label', ''))}")
            if not lines:
                lines.append(
                    "No interaction discoveries yet. Run: python main.py --train --backtest-2025"
                )
        except Exception as exc:
            lines = [f"Model insights unavailable: {exc}"]
        self.model_insights_box.configure(text="\n".join(lines))

    def _render_risk_tab(self, payload: DashboardPayload, *, budget_state: dict[str, Any] | None = None) -> None:
        try:
            from src.strategy import (
                available_card_budget_text,
                collect_dashboard_risk_warnings,
                format_risk_warnings,
            )

            rm = payload.risk_metrics or {}
            ctx = payload.threshold_ctx or {}
            overview = payload.books.get("Overview", {})
            alerts = overview.get("alerts") or {}
            bankroll = float(
                (budget_state or {}).get("total_bankroll")
                or alerts.get("bankroll")
                or config.INITIAL_BANKROLL
            )
            cp = rm.get("card_pnl") or {}
            lines = ["Monte Carlo card risk:"]
            if rm.get("available"):
                lines.append(
                    f"Mean PnL ${cp.get('mean_pnl', 0):+,.0f}  |  "
                    f"P(loss) {cp.get('prob_loss', 0):.0%}  |  "
                    f"P5 ${cp.get('p5_pnl', 0):+,.0f}  |  P95 ${cp.get('p95_pnl', 0):+,.0f}"
                )
                lines.append(
                    f"Suggested card cap {rm.get('suggested_max_risk_pct', 0):.1f}%  |  "
                    f"{rm.get('n_bets', 0)} value bets"
                )
            else:
                lines.append(rm.get("reason", "Run Refresh with odds-matched fights."))
            if budget_state:
                lines.append(available_card_budget_text(budget_state))
            if ctx.get("thresholds"):
                t = ctx["thresholds"]
                lines.append(
                    f"Active thresholds — edge {t.get('alert_min_edge', 0):.1%}, "
                    f"parlay leg {t.get('parlay_min_edge', 0):.1%}, "
                    f"combined {t.get('parlay_min_combined_prob', 0):.0%}"
                )
            if config.is_live_profile():
                cap = config.max_card_stake_cap(bankroll)
                live_cap = config.live_card_budget_cap_usd(bankroll)
                lines.append(
                    f"LIVE GUARDRAILS — Max ${cap:,.0f} total stake this card "
                    f"(${bankroll:,.0f} bankroll, ${live_cap:,.0f} hard cap). "
                    "Fewer bets; higher edge required."
                )
            elif config.is_paper_profile():
                lines.append(
                    "PAPER — Higher volume / lower edge thresholds for simulation and practice."
                )
            risk_warnings = collect_dashboard_risk_warnings(alerts, budget_state, bankroll=bankroll)
            warn_txt, _ = format_risk_warnings(risk_warnings, max_lines=5, separator="\n⚠ ")
            if warn_txt:
                lines.append(warn_txt)
            self.risk_summary.configure(text="\n".join(lines))
            self._render_model_insights_panel()

            self._risk_fig.clear()
            ax = self._risk_fig.add_subplot(111)
            ax.set_facecolor("#1a1a1a")
            staking = rm.get("staking_modes") or {}
            if staking:
                names = list(staking.keys())
                dds = [staking[m].get("expected_max_drawdown_pct", 0) for m in names]
                ax.bar(names, dds, color="#6366f1")
                ax.set_title("Expected Max Drawdown by Staking Mode", color="white")
                ax.set_ylabel("Drawdown %", color="#ccc")
            else:
                ax.text(0.5, 0.5, "Run Refresh for MC data", ha="center", color="#888", transform=ax.transAxes)
            ax.tick_params(colors="#aaa")
            for spine in ax.spines.values():
                spine.set_color("#444")
            self._risk_canvas.draw()

            self._thresh_fig.clear()
            ax2 = self._thresh_fig.add_subplot(111)
            ax2.set_facecolor("#1a1a1a")
            examples = example_threshold_table(profile=payload.profile)
            ax2.plot(examples["bankroll"], examples["min_edge"] * 100, marker="o", label="Min edge %", color="#3dd68c")
            ax2.plot(
                examples["bankroll"],
                examples["parlay_leg_edge"] * 100,
                marker="s",
                label="Parlay leg %",
                color="#60a5fa",
            )
            ax2.set_xscale("log")
            ax2.set_title("Dynamic Thresholds vs Bankroll", color="white")
            ax2.set_xlabel("Bankroll ($)", color="#ccc")
            ax2.set_ylabel("Threshold (%)", color="#ccc")
            ax2.legend(facecolor="#2b2b2b", labelcolor="white", fontsize=8)
            ax2.tick_params(colors="#aaa")
            for spine in ax2.spines.values():
                spine.set_color("#444")
            self._thresh_canvas.draw()
        except Exception as exc:
            _debug_log(f"Risk tab error: {exc}")
            self.risk_summary.configure(text=f"Risk tab error: {exc}")


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UFC Predictor Dashboard")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print startup diagnostics to the console",
    )
    # PyInstaller passes through unknown args; ignore extras when frozen
    args, _unknown = parser.parse_known_args()
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is not None:
        sys.argv = [sys.argv[0], *argv]

    global _DEBUG_MODE
    if "--debug" in sys.argv:
        _DEBUG_MODE = True
        _enable_debug_console()

    _parse_cli_args()

    if _DEBUG_MODE:
        _debug_log(
            "Python started. The ~280 MB onefile EXE unpacks before this line — "
            "first launch can take up to ~1 minute with no further console output."
        )

    splash = SplashScreen()
    splash.set_status("Starting UFC Predictor Dashboard…")
    splash.pump()

    try:
        _load_dependencies(progress=splash.set_status)
        splash.pump()
    except Exception as exc:
        splash.close()
        msg = str(exc)
        _show_fatal_error("UFC Dashboard — startup error", msg)
        return 1

    if _STARTUP_ERROR or ctk is None:
        return 1

    try:
        if config is not None:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            from src.logging_utils import setup_logging

            setup_logging(
                verbose=_DEBUG_MODE,
                log_dir=config.LOG_DIR,
                log_name="dashboard.log",
                console=_DEBUG_MODE,
            )
        splash.set_status("Building main window…")
        splash.pump()
        splash.close()
        app = UFCDashboardApp()
        _debug_log("Main window ready — entering event loop")
        app.mainloop()
        return 0
    except Exception as exc:
        splash.close()
        tb = traceback.format_exc()
        _debug_log(tb)
        _show_fatal_error("UFC Dashboard — runtime error", f"{exc}\n\n{tb}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
