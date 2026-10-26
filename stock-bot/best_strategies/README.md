# Best strategies — test, then deploy

Renaissance-style operating system for this shop: **data quality first**, many small
edges, pre-registered tests, kill faster than promote. Not a slogan folder.
Nothing here writes `.env`, restarts a bot, or touches live.

As of **2026-10-10**.

## Books (deploy ladder)

| Rung | Book | Who decides | Default |
|------|------|-------------|---------|
| 0 Research | scripts under `scripts/analysis` + `scripts/research` | anyone | run freely |
| 1 Aggressive | `alpaca_paper` (`PAPER_AGGRESSIVE`) | this agent | **HOLD** — Lab 8 portal; A2B1 on (walk CANDIDATE) |
| 2 Medium SoT | `alpaca_paper_v2` | owner only | locked look + locked strategy unless owner names it |
| 3 Live | `alpaca_live` Profile A | owner only | never from this folder |

A candidate must survive rung 0 with **PROTOCOL.md** gates before it is even
proposed for rung 1. Rungs 2–3 need an explicit owner phrase.

## Isolated NYSE walk SoT

S&P 500 membership by date, Mon–Fri session bars, interior ffill, 10 bps RT,
sleeve 0.95, marked-to-market DD. Coverage **97.4% / 95.4%** of index members.
`current` screener numbers are look-ahead. Windows are two disjoint 365-session
walks (recent 2025-04-11→2026-10-09, prior 2023-10-02→2025-04-10).

| Policy | 365d_recent | 365d_prior | Gate vs pick_15 |
|--------|-------------|------------|-----------------|
| **pick_15** | **+100.2% / 18.7 / 1.66** | **+6.2% / 22.8 / 0.27** | base |
| pick_8 | +167% / 26.7 | +1.6% / 29.0 | FAIL DD |
| Lab 8 | +96% / 16.0 | +24% / 24.8 | FAIL recent ret |
| A2B1 | +108% (+7.9 pp) | +6.7% (+0.5 pp) | **CANDIDATE** |
| risk_adj rank | +39% (−61 pp) | +23% (+17 pp) | FAIL recent ret |
| 12% vol target | +70% (−31 pp) | +11% (+5 pp) | FAIL recent ret |

## What is actually running (drift vs docs)

Docs still describe Realistic Research v1.5.4 Dynamic VTI 40–75%. Both paper
books currently have **VTI core = 0** and **NYSE cap ≈ 95%**.

| | Aggressive (`alpaca_paper`) | Medium SoT (`alpaca_paper_v2`) |
|--|--|--|
| Names | Lab 8 · 15%/name · trail (portal) | 15 cap · 2.0× ATR (portal; disk 10/2.5× is stale) |
| Stat-arb / ORB / scanners | ON | OFF |
| Fat-loser | OFF (portal) | OFF |
| A2B1 (≥8% next-open, skip Mon) | ON (portal; isolated walk CANDIDATE) | OFF |
| Daily bank / GARCH | ON | (profile default; not the v2 pick) |
| Isolated-walk keep | pick_15, 30d, 2× ATR, **no 1R** | same cousin |

Lab 8 is the aggressive *executor* because STRICT name-count beat 25, not
because the isolated PIT walk prefers 8.

## Read next

1. [PROTOCOL.md](PROTOCOL.md) — how a test becomes a deploy
2. [DATA.md](DATA.md) — what we trust
3. [findings.md](findings.md) — already-measured edges and rejects
4. [QUEUE.md](QUEUE.md) — ordered work
5. `python best_strategies/status.py` — same ledger from code

Ledger SoT: `ledger.py`. Edit that, not a side spreadsheet.
