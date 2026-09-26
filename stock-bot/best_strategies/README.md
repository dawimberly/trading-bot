# Best strategies — test, then deploy

Renaissance-style operating system for this shop: **data quality first**, many small
edges, pre-registered tests, kill faster than promote. Not a slogan folder.
Nothing here writes `.env`, restarts a bot, or touches live.

As of **2026-09-20**.

## Books (deploy ladder)

| Rung | Book | Who decides | Default |
|------|------|-------------|---------|
| 0 Research | scripts under `scripts/analysis` + `scripts/research` | anyone | run freely |
| 1 Aggressive | `alpaca_paper` (`PAPER_AGGRESSIVE`) | this agent | **HOLD** — no overlay/1R/Friday flatten |
| 2 Medium SoT | `alpaca_paper_v2` | owner only | locked look + locked strategy unless owner names it |
| 3 Live | `alpaca_live` Profile A | owner only | never from this folder |

A candidate must survive rung 0 with **PROTOCOL.md** gates before it is even
proposed for rung 1. Rungs 2–3 need an explicit owner phrase.

## What is actually running (drift vs docs)

Docs still describe Realistic Research v1.5.4 Dynamic VTI 40–75%. Both paper
books currently have **VTI core = 0** and **NYSE cap ≈ 95%**.

| | Aggressive (`alpaca_paper`) | Medium SoT (`alpaca_paper_v2`) |
|--|--|--|
| Names | Lab 8 · 15%/name · trail (portal) | 15 cap · 2.0× ATR (portal; disk 10/2.5× is stale) |
| Stat-arb / ORB / scanners | ON | OFF |
| Fat-loser | OFF (portal) | OFF |
| A2B1 (≥8% next-open, skip Mon) | ON (portal) | OFF |
| Daily bank / GARCH | ON | (profile default; not the v2 pick) |
| NYSE walk that won the last panel | 15 names, 30d, 2× ATR, **no 1R** | closest cousin; **10 names + 2.5× ATR** is drift |

NYSE-only 365d panel (`paper_v2_best_backtest`): **8 names +59.9%**, **15 +49.2%**,
**25 +41.2%**. Full-stack STRICT A/B: 8 beats 25 both windows; 15 fails. Portal
already launches Lab 8. That test is done — not the weekday overlay.

## Read next

1. [PROTOCOL.md](PROTOCOL.md) — how a test becomes a deploy
2. [DATA.md](DATA.md) — what we trust
3. [findings.md](findings.md) — already-measured edges and rejects
4. [QUEUE.md](QUEUE.md) — ordered work
5. `python best_strategies/status.py` — same ledger from code

Ledger SoT: `ledger.py`. Edit that, not a side spreadsheet.
