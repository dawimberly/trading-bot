# Cursor prompt — paste this into Cursor on the trading-bot repo

Copy everything below the line.

---

Restyle the PythonTrading **CustomTkinter** monitor to the Paqinhaüs poster look. LOOK ONLY.

Repo: `dawimberly/trading-bot`
Primary file: `stock-bot/dashboard_app.py`
Backup: `stock-bot/dashboard.py` (Streamlit)
Spec: `stock-bot/docs/PAQINHAUS_LOOK.md`
Helper already in repo: `stock-bot/scripts/apply_paqinhaus_look.py` (owner_reset runs it before launch)

## Hard rules

- Do **not** change strategy, journal, Alpaca, orders, sleeve caps, PID files, heartbeat writers, or anything under `modules/`.
- Do **not** change bot start/stop behavior.
- Live banner stays loud red (`#c81e1e` / `#fff5f5`).
- Paper ≈ $97k, Live ≈ $300 — do not invent new balances.
- Keep existing tabs: Positions, Overview, Trades, Wisdom, Charts.
- Keep Paper / Live book switcher working.
- Windows-safe fonts: **Georgia** for display, **Segoe UI** for body. Do not require Google Fonts.

## What “done” looks like

When I open `dashboard_app.py` I should immediately see:

1. Window background `#0b0b0e`, cream text `#f2ebe0`.
2. A **6px cyan (`#1fa8ef`) bar** across the top of the window.
3. Header left:
   - Small cyan tracked kicker: `PYTHONTRADING · {active book}`
   - Giant Georgia wordmark: **Stock-bot** (hero ~42pt)
   - Cyan stamp/pill: `LIVE` or `PAPER` (active book). Never `NYSE 100` or `VTI 85`.
4. Header right: running/halted chip + Paper / Live segmented control. Active Paper = cyan fill + `#0b0b0e` text. Active Live = live red fill + cream text.
5. A cyan “tape” strip under the header from `dashboard_header.header_tape_text` (paper research / mixed holdings). Never hardcode `NYSE 100% · VTI CORE OFF · …`.
6. Live book only: full-width banner `LIVE TRADING — REAL MONEY ACCOUNT` plus “Do not size options on ~$300.”
7. Status pills then a 5-up metric row (Equity / Cash / Invested / UPL / Next market). Equity is the biggest cream number. Gains `#7ec13a`, losses `#e23a3a`.
8. Tab labels in Georgia. Active tab cyan. Tables cream-on-card with `#2e2c28` grid.
9. Charts: cyan `#1fa8ef` / `#4fbcf5` stroke, dark plot, cream ticks. No navy.

## How to implement

1. Replace the `COLORS` dict in `dashboard_app.py` with the tokens in `stock-bot/docs/PAQINHAUS_LOOK.md` (same keys the file already uses: bg, surface, surface2, card, card_hover, border, muted, text, text_dim, green, green_dim, red, red_dim, amber, amber_dim, blue, accent, accent_hover, live, live_bg, small, small_bg, paper_ok, paper_ok_bg, chart_grid). Add `magenta` if missing.
2. Replace `FONTS` so hero/title/heading are Georgia and larger (hero 42 bold, heading 16 bold, metric 22 bold). Body stays Segoe UI.
3. Add the cyan hairline if missing; bump height to 6 if a 4px bar already exists.
4. Rebuild the **header left cluster** to kicker + Stock-bot + LIVE/PAPER stamp via `dashboard_header.py`. Do not leave a generic “PythonTrading” 20pt title as the hero. Do not restore `NYSE 100`.
5. Add the cyan tape row from `header_tape_text`. Static text is fine (no animation required on Tk).
6. Sweep leftover navy/blue hex (`#0a0e17`, `#111827`, `#152238`, `#2563eb`, `#1e3a5f`, `#334155`, `#60a5fa`, `#1d4ed8`) in `dashboard_app.py` and `dashboard.py` — map them to `COLORS[...]`.
7. Run `python stock-bot/scripts/apply_paqinhaus_look.py` after edits so the helper stays in sync.
8. Do not commit secrets. Do not touch `.env`.

## Verify

- Open the monitor, Paper book: dark near-black, giant Stock-bot, cyan **PAPER** stamp (not NYSE 100), cyan research tape, ~$97k equity, no live banner.
- Click Live: red banner appears, equity ~$300, cyan **LIVE** stamp, title kicker shows live.
- Positions / Overview / Trades / Wisdom / Charts still populate from existing data.
- `rg "0a0e17|2563eb|1e3a5f" stock-bot/dashboard_app.py` returns nothing.

When finished: summarize the files you changed and nothing else.
