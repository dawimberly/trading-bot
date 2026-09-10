"""Apply Paqinhaüs poster look to stock-bot dashboards. Look only. Idempotent."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard_app.py"
STREAMLIT = ROOT / "dashboard.py"

NEW_COLORS = '''COLORS = {
    # Paqinhaüs poster tokens — look only; live banner stays loud red
    "bg": "#0b0b0e",
    "surface": "#121214",
    "surface2": "#16161a",
    "card": "#1a1a1f",
    "card_hover": "#24242a",
    "border": "#2e2c28",
    "muted": "#a89f91",
    "text": "#f2ebe0",
    "text_dim": "#d4cbbd",
    "green": "#7ec13a",
    "green_dim": "#3d6a18",
    "red": "#e23a3a",
    "red_dim": "#7f1d1d",
    "amber": "#f4d21e",
    "amber_dim": "#8a7610",
    "blue": "#4fbcf5",
    "accent": "#1fa8ef",
    "accent_hover": "#1688c4",
    "live": "#c81e1e",
    "live_bg": "#3a0a0a",
    "small": "#b45309",
    "small_bg": "#451a03",
    "paper_ok": "#3d6a18",
    "paper_ok_bg": "#1a3310",
    "chart_grid": "#2e2c28",
    "magenta": "#e653a4",
}'''

NEW_FONTS = '''FONTS = {
    "hero": ("Georgia", 42, "bold"),
    "hero_sub": ("Georgia", 26, "bold"),
    "title": ("Georgia", 22, "bold"),
    "heading": ("Georgia", 16, "bold"),
    "body": ("Segoe UI", 12),
    "body_sm": ("Segoe UI", 11),
    "caption": ("Segoe UI", 10),
    "metric": ("Segoe UI", 22, "bold"),
    "metric_sm": ("Segoe UI", 15, "bold"),
}'''

ACCENT_OLD = '''        # Header bar
        header_bar = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=16,
            border_width=1,
            border_color=COLORS["border"],
        )
        header_bar.pack(fill="x", padx=14, pady=(12, 6))'''

ACCENT_NEW = '''        accent_bar = ctk.CTkFrame(self, fg_color=COLORS["accent"], height=6, corner_radius=0)
        accent_bar.pack(fill="x")
        accent_bar.pack_propagate(False)

        # Header bar
        header_bar = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=16,
            border_width=1,
            border_color=COLORS["border"],
        )
        header_bar.pack(fill="x", padx=14, pady=(12, 6))'''


def _replace_block(text: str, name: str, new_block: str) -> str:
    pattern = rf"^{re.escape(name)} = \{{.*?\n\}}"
    compiled = re.compile(pattern, re.M | re.S)
    if compiled.search(text):
        return compiled.sub(new_block, text, count=1)
    return text


def _swap(s: str, old: str, new: str) -> str:
    if new.strip() in s and old not in s:
        return s
    if old not in s:
        return s
    return s.replace(old, new, 1)


def patch_app(text: str) -> str:
    text = _replace_block(text, "COLORS", NEW_COLORS)
    text = _replace_block(text, "FONTS", NEW_FONTS)
    text = text.replace(
        'ctk.set_default_color_theme("blue")',
        'ctk.set_default_color_theme("dark-blue")',
        1,
    )
    text = _swap(
        text,
        '''            fg_color="#1e3a5f",
            button_color="#334155",
            button_hover_color="#475569",''',
        '''            fg_color=COLORS["surface2"],
            button_color=COLORS["card"],
            button_hover_color=COLORS["accent"],''',
    )
    text = _swap(
        text,
        '''            fg_color="#374151",
            hover_color="#4b5563",''',
        '''            fg_color=COLORS["surface2"],
            hover_color=COLORS["card_hover"],''',
    )
    text = _swap(
        text,
        '''                fg_color="#166534",
                hover_color="#14532d",''',
        '''                fg_color=COLORS["green_dim"],
                hover_color=COLORS["paper_ok"],''',
    )
    text = text.replace('specs.append(("GLD", "#fbbf24"))', 'specs.append(("GLD", COLORS["amber"]))')
    text = text.replace(
        'accent_bar = ctk.CTkFrame(self, fg_color=COLORS["accent"], height=4, corner_radius=0)',
        'accent_bar = ctk.CTkFrame(self, fg_color=COLORS["accent"], height=6, corner_radius=0)',
    )
    if 'accent_bar = ctk.CTkFrame(self, fg_color=COLORS["accent"]' not in text:
        text = _swap(text, ACCENT_OLD, ACCENT_NEW)
    return text


def patch_streamlit(text: str) -> str:
    if "background: #0b0b0e !important" in text:
        return text
    needle = """        <style>
        .live-trading-banner {"""
    insert = """        <style>
        html, body, [data-testid=\"stAppViewContainer\"], .stApp {
            background: #0b0b0e !important;
            color: #f2ebe0 !important;
        }
        [data-testid=\"stHeader\"] { background: #0b0b0e !important; }
        h1, h2, h3 { color: #f2ebe0 !important; font-family: Georgia, serif !important; }
        [data-testid=\"stMetricValue\"] { color: #f2ebe0 !important; font-variant-numeric: tabular-nums; }
        [data-testid=\"stMetricLabel\"] { color: #a89f91 !important; }
        .stButton>button {
            background: #1fa8ef !important;
            color: #0b0b0e !important;
            border: 0 !important;
            font-weight: 700 !important;
        }
        .live-trading-banner {"""
    text = text.replace(needle, insert, 1)
    text = text.replace('return "color: #198754; font-weight: 600"', 'return "color: #7ec13a; font-weight: 600"')
    text = text.replace('return "color: #dc3545; font-weight: 600"', 'return "color: #e23a3a; font-weight: 600"')
    text = text.replace('line=dict(color="#fd7e14", width=1.5)', 'line=dict(color="#f4d21e", width=1.5)')
    text = text.replace('line=dict(color="#0d6efd", width=1.5)', 'line=dict(color="#1fa8ef", width=1.5)')
    old_layout = '''        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )'''
    new_layout = '''        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        paper_bgcolor="#0b0b0e",
        plot_bgcolor="#121214",
        font=dict(color="#f2ebe0"),
        xaxis=dict(gridcolor="#2e2c28", zerolinecolor="#2e2c28"),
        yaxis=dict(gridcolor="#2e2c28", zerolinecolor="#2e2c28"),
    )'''
    if old_layout in text:
        text = text.replace(old_layout, new_layout, 1)
    return text


def main() -> int:
    if APP.is_file():
        before = APP.read_text(encoding="utf-8")
        after = patch_app(before)
        if after != before:
            APP.write_text(after, encoding="utf-8")
            print(f"look: patched {APP.name}", flush=True)
        else:
            print(f"look: {APP.name} already Paqinhaüs", flush=True)
    if STREAMLIT.is_file():
        before = STREAMLIT.read_text(encoding="utf-8")
        after = patch_streamlit(before)
        if after != before:
            STREAMLIT.write_text(after, encoding="utf-8")
            print(f"look: patched {STREAMLIT.name}", flush=True)
        else:
            print(f"look: {STREAMLIT.name} already Paqinhaüs", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
