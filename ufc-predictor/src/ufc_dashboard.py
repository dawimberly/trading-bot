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
    except Exception as exc:
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

_enable_debug_console()


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

    _ROOT = bootstrap(entry_file=_ENTRY)
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
        self.profile = "research"
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
    p.profile = data.get("profile", "research")
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
    )
    data["profile"] = profile
    return _result_to_payload(data)


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
    if not pick or not row.get("odds_matched"):
        return "—"
    f1 = str(row.get("fighter_1", ""))
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


def _model_prob_for_row(row: pd.Series) -> float:
    """Model win probability for the predicted pick (used for table sorting)."""
    from src.strategy import _pick_model_prob

    _pick, prob, _fight = _pick_model_prob(row)
    return float(prob) if pd.notna(prob) else 0.0


def _sort_preds_by_model_prob(preds: pd.DataFrame) -> pd.DataFrame:
    if preds is None or preds.empty:
        return preds
    scored = preds.copy()
    scored["_sort_prob"] = scored.apply(_model_prob_for_row, axis=1)
    scored = scored.sort_values("_sort_prob", ascending=False).drop(columns="_sort_prob")
    return scored.reset_index(drop=True)


def _rows_for_table(preds: pd.DataFrame, bankroll: float, strategy) -> list[tuple]:
    rows: list[tuple] = []
    for _, row in _sort_preds_by_model_prob(preds).iterrows():
        edge, pick = _pick_edge(row)
        f1, f2 = str(row.get("fighter_1", "")), str(row.get("fighter_2", ""))
        prob = row.get("predicted_prob", row.get("prob_f1_win"))
        if pd.notna(prob) and pick == f2 and pd.notna(row.get("prob_f2_win")):
            prob = row["prob_f2_win"]
        edge_txt = f"{edge * 100:+.1f}%" if edge is not None else "—"
        prob_txt = f"{float(prob):.0%}" if pd.notna(prob) else "—"
        brief = build_fight_brief(row, edge_pct=edge * 100 if edge else None)[:120]
        sort_prob = _model_prob_for_row(row)
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
    rows.sort(key=lambda r: r[8], reverse=True)
    return [(r[:8]) for r in rows]


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

    def __init__(self, master, *, height: int = 12, **kwargs) -> None:
        super().__init__(master, **kwargs)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Dash.Treeview",
            background="#1e1e1e",
            foreground="#e8e8e8",
            fieldbackground="#1e1e1e",
            rowheight=26,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Dash.Treeview.Heading",
            background="#2b2b2b",
            foreground="#ffffff",
            font=("Segoe UI", 10, "bold"),
        )
        self.tree = ttk.Treeview(
            self,
            columns=self.COLUMNS,
            show="headings",
            height=height,
            style="Dash.Treeview",
        )
        widths = (220, 130, 58, 58, 72, 68, 300, 140)
        for col, w in zip(self.COLUMNS, widths):
            self.tree.heading(col, text=col, anchor="w")
            self.tree.column(col, width=w, minwidth=48, anchor="w", stretch=(col == "Fight"))
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("pos", foreground="#3dd68c")
        self.tree.tag_configure("neg", foreground="#f87171")
        self.tree.tag_configure("neutral", foreground="#b0b0b0")
        self._bind_mousewheel()

    def _bind_mousewheel(self) -> None:
        def _on_wheel(event) -> None:
            if event.delta:
                self.tree.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.tree.bind("<MouseWheel>", _on_wheel)
        self.tree.bind("<Enter>", lambda _e: self.tree.focus_set())

    def load_rows(self, rows: list[tuple]) -> None:
        self.tree.delete(*self.tree.get_children())
        if not rows:
            self.tree.insert("", "end", values=("No fights loaded — click Refresh", "", "", "", "", "", "", ""))
            return
        for row in rows:
            edge_txt = row[4]
            tag = "neutral"
            if edge_txt not in ("—", ""):
                try:
                    tag = "pos" if float(edge_txt.replace("%", "").replace("+", "")) > 0 else "neg"
                except ValueError:
                    pass
            self.tree.insert("", "end", values=row, tags=(tag,))


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
        self.summary = ctk.CTkLabel(self, text="Run Refresh to load data.", anchor="w", justify="left")
        self.summary.pack(fill="x", padx=12, pady=(10, 4))
        self.threshold_box = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
        )
        self.threshold_box.pack(fill="x", padx=12, pady=(0, 6))
        self.table = DataTable(self, height=14)
        self.table.pack(fill="both", expand=True, padx=10, pady=6)
        self.bets_frame = ctk.CTkScrollableFrame(self, height=200, label_text="Top Singles & Ranked Parlays")
        self.bets_frame.pack(fill="x", padx=10, pady=(4, 10))

    def render(self, book_data: dict[str, Any], threshold_ctx: dict[str, Any]) -> None:
        preds: pd.DataFrame = book_data.get("predictions", pd.DataFrame())
        alerts: dict = book_data.get("alerts") or {}
        matched = book_data.get("odds_matched", 0)
        total = book_data.get("odds_total", 0)
        source = book_data.get("source", self.title)
        warning = book_data.get("warning") or book_data.get("error") or ""

        if warning:
            self.warning_box.configure(text=f"⚠ {warning}")
            self.warning_box.pack(fill="x", padx=12, pady=(8, 0))
        else:
            self.warning_box.pack_forget()

        bankroll = float(alerts.get("bankroll") or config.INITIAL_BANKROLL)
        strategy = strategy_from_profile(bankroll=bankroll)

        self.summary.configure(
            text=(
                f"{source}  |  Odds matched {matched}/{total}  |  "
                f"{alerts.get('risk_summary', 'Run Refresh for picks and parlays.')}"
            )
        )
        td = threshold_ctx.get("thresholds") or alerts.get("threshold_detail")
        if td:
            max_legs = threshold_ctx.get("parlay_max_legs", 5 if not config.is_live_profile() else 3)
            self.threshold_box.configure(
                text=(
                    f"Dynamic thresholds: edge {td.get('alert_min_edge', 0):.1%}  "
                    f"leg {td.get('parlay_min_edge', 0):.1%}  "
                    f"prob {td.get('parlay_min_combined_prob', 0):.0%}  "
                    f"EV {td.get('parlay_min_ev', 0):.0%}  "
                    f"max {max_legs}-leg parlays"
                )
            )
        else:
            ps = config.profile_settings()
            max_legs = 5 if not config.is_live_profile() else 3
            self.threshold_box.configure(
                text=(
                    f"Static thresholds: edge {ps['alert_min_edge']:.1%}  "
                    f"leg {ps['parlay_min_edge']:.1%}  "
                    f"prob {ps['parlay_min_combined_prob']:.0%}  "
                    f"max {max_legs}-leg parlays"
                )
            )

        self.table.load_rows(_rows_for_table(preds, bankroll, strategy))

        for w in self.bets_frame.winfo_children():
            w.destroy()
        singles = alerts.get("singles") or []
        parlays = alerts.get("parlays") or []
        if singles:
            ctk.CTkLabel(self.bets_frame, text="Top Singles (by edge)", anchor="w").pack(fill="x")
            for s in singles[:5]:
                prob_txt = f", prob {s['prob']:.0%}" if s.get("prob") is not None else ""
                ctk.CTkLabel(
                    self.bets_frame,
                    text=f"• {s.get('fight', '')} — {s.get('pick', '')}{prob_txt} ({s.get('edge_pct', 0):+.1f}%)",
                    anchor="w",
                    text_color="#3dd68c",
                ).pack(fill="x")
        _render_ranked_parlays(self.bets_frame, parlays, preds=preds)


class BookPropsTab(_CTK_FRAME):
    """Prop bets for one book — singles; DraftKings also shows parlays."""

    def __init__(
        self,
        master,
        *,
        book_name: str,
        book_note: str,
        show_parlays: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self.book_name = book_name
        self.book_note = book_note
        self.show_parlays = show_parlays
        self.summary = ctk.CTkLabel(
            self,
            text="Prop betting disabled. Set ENABLE_PROPS=true in .env and refresh.",
            anchor="w",
            justify="left",
        )
        self.summary.pack(fill="x", padx=12, pady=(10, 4))
        self.backtest_box = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color="#9ca3af",
        )
        self.backtest_box.pack(fill="x", padx=12, pady=(0, 6))
        self.scroll = ctk.CTkScrollableFrame(self, label_text="Top prop bets by edge")
        self.scroll.pack(fill="both", expand=True, padx=10, pady=8)

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
            else:
                self.backtest_box.configure(text="")
        else:
            self.backtest_box.configure(
                text="Run holdout backtest with ENABLE_PROPS=true to populate historical prop stats."
            )

    def _format_single_line(self, s: dict[str, Any]) -> tuple[str, str]:
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

    def render(self, payload: "DashboardPayload") -> None:
        for w in self.scroll.winfo_children():
            w.destroy()

        if not config.ENABLE_PROPS:
            self.summary.configure(text="Prop betting disabled — set ENABLE_PROPS=true in .env")
            self.backtest_box.configure(text="")
            ctk.CTkLabel(
                self.scroll,
                text="Enable ENABLE_PROPS=true in .env, then click Refresh Next Two.",
                anchor="w",
                text_color="#9ca3af",
            ).pack(fill="x", padx=8, pady=8)
            return

        if self.book_name == "MyBookie" and not config.MYBOOKIE_ENABLED:
            self.summary.configure(text="MyBookie disabled — set MYBOOKIE_ENABLED=true in .env")
            self.backtest_box.configure(text="")
            ctk.CTkLabel(
                self.scroll,
                text="Enable MYBOOKIE_ENABLED=true in .env, then click Refresh Next Two.",
                anchor="w",
                text_color="#9ca3af",
            ).pack(fill="x", padx=8, pady=8)
            return

        has_cards = bool(payload.cards) or not payload.combined.empty
        if not has_cards:
            self.summary.configure(
                text=f"{self.book_name} — {self.book_note}"
            )
            self.backtest_box.configure(text="")
            ctk.CTkLabel(
                self.scroll,
                text="No props available yet — run full Refresh Next Two.",
                anchor="w",
                text_color="#fbbf24",
                font=ctk.CTkFont(size=13),
            ).pack(fill="x", padx=8, pady=12)
            return

        self.summary.configure(
            text=(
                f"{self.book_name} — {self.book_note}. "
                "Live book lines when available; synthetic model odds otherwise."
            )
        )
        self._render_backtest(payload)

        book = payload.books.get(self.book_name, {})
        props = book.get("props") or {}
        singles = props.get("singles") or []
        parlays = props.get("parlays") or [] if self.show_parlays else []
        book_warning = str(book.get("warning") or book.get("error") or "").strip()

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
            ctk.CTkLabel(
                self.scroll,
                text=(
                    "No prop bets ranked for this card "
                    f"(model prob below {config.PROP_MIN_MODEL_PROB:.0%} or no fights loaded)."
                ),
                anchor="w",
                text_color="#9ca3af",
            ).pack(fill="x", padx=8, pady=8)
            return

        if singles:
            ctk.CTkLabel(self.scroll, text="Top singles", anchor="w").pack(fill="x", padx=4)
            for s in singles[:10]:
                text, color = self._format_single_line(s)
                ctk.CTkLabel(self.scroll, text=text, anchor="w", text_color=color).pack(fill="x", padx=8)

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

        config.UFC_PROFILE = "research"
        config.apply_profile_overrides()

        self._build_top_bar()
        self._build_tabs()
        self._build_status_area()
        self._schedule_status_tick()
        self.after(200, self._load_background_cache_on_startup)

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

    def _build_top_bar(self) -> None:
        bar = ctk.CTkFrame(self, height=52)
        bar.pack(fill="x", padx=12, pady=(12, 6))
        bar.pack_propagate(False)

        ctk.CTkLabel(bar, text="Profile", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(8, 4))
        self.profile_var = ctk.StringVar(value="research")
        self.profile_menu = ctk.CTkOptionMenu(
            bar,
            variable=self.profile_var,
            values=["research", "live"],
            width=120,
            command=self._on_profile_change,
        )
        self.profile_menu.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(bar, text="Event", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(8, 4))
        self.event_var = ctk.StringVar(value="Next Two Cards")
        self.event_menu = ctk.CTkOptionMenu(
            bar,
            variable=self.event_var,
            values=["Next Two Cards", "Next Card", "Freedom 250"],
            width=180,
        )
        self.event_menu.pack(side="left", padx=(0, 16))

        self.refresh_btn = ctk.CTkButton(bar, text="Refresh Next Two", width=130, command=self._on_refresh)
        self.refresh_btn.pack(side="left", padx=(8, 4))

        self.quick_odds_btn = ctk.CTkButton(
            bar,
            text="Quick Odds Refresh",
            width=150,
            fg_color="#2d6a4f",
            hover_color="#40916c",
            command=self._on_quick_odds,
        )
        self.quick_odds_btn.pack(side="left", padx=4)

        self.new_card_btn = ctk.CTkButton(
            bar,
            text="Process New Card",
            width=140,
            fg_color="#7c3aed",
            hover_color="#8b5cf6",
            command=self._on_process_new_card,
        )
        self.new_card_btn.pack(side="left", padx=4)

        self.auto_watch_var = ctk.BooleanVar(value=False)
        self.auto_watch_switch = ctk.CTkSwitch(
            bar,
            text="Enable Auto Watch",
            variable=self.auto_watch_var,
            command=self._on_auto_watch_toggle,
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

        self.overview_summary = ctk.CTkLabel(
            self.tab_overview, text="Combined cross-book summary.", anchor="w", justify="left"
        )
        self.overview_summary.pack(fill="x", padx=12, pady=10)
        self.overview_table = DataTable(self.tab_overview)
        self.overview_table.pack(fill="both", expand=True, padx=10, pady=6)

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
        )
        self.props_betnow_tab.pack(fill="both", expand=True)
        self.props_dk_tab = BookPropsTab(
            self.tab_props_dk,
            book_name="DraftKings",
            book_note="Singles + 2–3 leg prop/mixed parlays (correlation-adjusted)",
            show_parlays=True,
        )
        self.props_dk_tab.pack(fill="both", expand=True)
        self.props_mybookie_tab = BookPropsTab(
            self.tab_props_mybookie,
            book_name="MyBookie",
            book_note="Singles + 2–3 leg prop/mixed parlays (live method/round props when scraped)",
            show_parlays=True,
        )
        self.props_mybookie_tab.pack(fill="both", expand=True)

        self.risk_summary = ctk.CTkLabel(self.tab_risk, text="", anchor="w", justify="left")
        self.risk_summary.pack(fill="x", padx=12, pady=8)
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

    def _on_profile_change(self, _value: str) -> None:
        config.UFC_PROFILE = self.profile_var.get()
        config.apply_profile_overrides()

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
        self.refresh_btn.configure(state=state)
        self.quick_odds_btn.configure(state=state)
        self.new_card_btn.configure(state=state)

    def _on_refresh(self) -> None:
        """Full prediction + odds for the two soonest upcoming cards."""
        if self._busy:
            return
        self._set_busy(True)
        self._on_progress("Refresh: next two upcoming cards (predictions + odds)…", 0.02)

        def worker() -> None:
            try:
                payload = run_dashboard_analysis(
                    event_mode="Next Two Cards",
                    profile=self.profile_var.get(),
                    force_refresh_odds=True,
                    explain=True,
                    use_cache=True,
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
                    profile=self.profile_var.get(),
                    force_refresh_odds=True,
                    explain=True,
                    use_cache=True,
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
            text=f"{payload.generated_at or '—'}  |  {payload.event_label}  |  {payload.profile}{cache_note}"
        )
        if payload.errors:
            self.status.configure(text="Done with warnings: " + "; ".join(payload.errors[:2]) + quick_note)
        elif quick:
            self.status.configure(text=f"Quick odds updated at {payload.odds_updated_at or 'now'}.")
        else:
            self.status.configure(text=f"Refresh complete — {payload.event_label}")

        try:
            self._render_all_tabs(payload)
        except Exception as exc:
            tb = traceback.format_exc()
            _debug_log(f"Tab render error: {tb}")
            self._show_error(f"Tab render failed: {exc}")

    def _render_all_tabs(self, payload: DashboardPayload) -> None:
        ctx = payload.threshold_ctx or {}
        overview = payload.books.get("Overview", {})
        alerts = overview.get("alerts") or {}
        bankroll = float(alerts.get("bankroll") or config.INITIAL_BANKROLL)
        strategy = strategy_from_profile(bankroll=bankroll)

        # Overview
        self.overview_summary.configure(
            text=(
                f"Events: {payload.event_label}\n"
                f"{alerts.get('risk_summary', '')}\n"
                f"Singles: {alerts.get('singles_count', 0)}  |  "
                f"Parlays: {alerts.get('parlays_count', 0)}  |  "
                f"Best-edge overview across BetNow + DraftKings"
                + (" + MyBookie" if config.MYBOOKIE_ENABLED else "")
                + " + consensus"
            )
        )
        preds = overview.get("predictions", pd.DataFrame())
        self.overview_table.load_rows(_rows_for_table(preds, bankroll, strategy))

        # Book tabs
        self.betnow_tab.render(payload.books.get("BetNow.eu", {}), ctx)
        self.dk_tab.render(payload.books.get("DraftKings", {}), ctx)
        if config.MYBOOKIE_ENABLED:
            self.mybookie_tab.render(payload.books.get("MyBookie", {}), ctx)
        else:
            self.mybookie_tab.summary.configure(
                text="MyBookie disabled — set MYBOOKIE_ENABLED=true in .env and refresh."
            )

        # Next Two Cards (per-event tables; same-card parlays stay within each event)
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

        self.props_betnow_tab.render(payload)
        self.props_dk_tab.render(payload)
        self.props_mybookie_tab.render(payload)
        self._render_risk_tab(payload)
        _debug_log("All tabs rendered")

    def _render_risk_tab(self, payload: DashboardPayload) -> None:
        try:
            rm = payload.risk_metrics or {}
            ctx = payload.threshold_ctx or {}
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
            if ctx.get("thresholds"):
                t = ctx["thresholds"]
                lines.append(
                    f"Active thresholds — edge {t.get('alert_min_edge', 0):.1%}, "
                    f"parlay leg {t.get('parlay_min_edge', 0):.1%}, "
                    f"combined {t.get('parlay_min_combined_prob', 0):.0%}"
                )
            self.risk_summary.configure(text="\n".join(lines))

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
    elif _FROZEN:
        try:
            print("[dashboard] UFC Predictor starting…", flush=True)
        except Exception:
            pass

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
