# Paqinhaüs poster look — PythonTrading monitor

Look only. Do not change strategy, journal, Alpaca, orders, sleeves, or bot process logic.

Reference: the Grok web preview the owner signed off — giant blackletter **Stock-bot** wordmark, cyan **NYSE 100** stamp, cream-on-near-black, cyan tape.

## Palette (mandatory)

| Token | Hex | Use |
|---|---|---|
| bg | `#0b0b0e` | Window / page |
| surface | `#121214` | Header / tab chrome |
| card | `#16161a` / `#1a1a1f` | Metric cards, tables |
| ink | `#f2ebe0` | Primary text, numbers |
| muted | `#a89f91` | Labels, captions |
| cyan | `#1fa8ef` | Primary accent, paper tab, tape, stamp |
| cyan-2 | `#4fbcf5` | Chart stroke |
| green | `#7ec13a` | Gains, running |
| red | `#e23a3a` | Losses only |
| live | `#c81e1e` | LIVE banner bg |
| live-fg | `#fff5f5` | LIVE banner text |
| amber | `#f4d21e` | Warnings / small account |
| magenta | `#e653a4` | Atmosphere only, never CTAs |

Red is **live money + losses + errors**. Never decorative.

## Type

- Display / wordmark / section titles: **Georgia** on Windows (Pirata One if installed). Huge.
- Body / tables / metrics: **Segoe UI**, tabular numbers.
- Wordmark text: `Stock-bot`
- Kicker above wordmark (cyan, tracked uppercase): `PYTHONTRADING · {BOOK}`
- Stamp next to wordmark: `NYSE 100` — cyan fill, near-black text, slight rotation if the toolkit allows, otherwise a tight cyan pill.

## Layout (top → bottom)

1. 6px cyan hairline across the full window.
2. Header: kicker + giant **Stock-bot** + NYSE stamp on the left. Running/halted chip + Paper/Live book switcher on the right.
3. Cyan tape row with strategy facts: `NYSE 100% · VTI CORE OFF · SPY SLEEVE 0% · CRYPTO 0% · STAT-ARB 0% · JOURNAL = FILL · ATR COOLDOWN ON · SURVIVAL NOT P95`
4. **LIVE TRADING — REAL MONEY ACCOUNT** banner only on the live book. Loud red. Do not size options on ~$300.
5. Status pills: book, NYSE 100%, regime, gates, health, bot, heartbeat, conviction.
6. Metric row: Equity, Cash %, Invested %, Unrealized P&L, Next market. Equity is the largest cream number.
7. Tabs: Positions / Overview / Trades / Wisdom / Charts.
8. Active tab is cyan fill + near-black label. Inactive is cream on surface.

Paper book ≈ $97k. Live book ≈ $300. Switching books must change the stack, banner, and title kicker.

## Do not

- Touch `modules/`, `run_all.py`, `run_paper_bot.py`, journal writers, Alpaca clients.
- Use navy `#0a0e17` / `#2563eb` anywhere on the monitor.
- Add a hamburger, new always-on widgets, or a second accent color for primary buttons.
- Make the live banner subtle.
