# Protocol — test then deploy

One knob. Two windows. Costs on. Kill on fail. Never promote from a story.

## 1. Pre-register

Before looking at results, write in the run notes:

- **Hypothesis** (one sentence)
- **Book** (aggressive / Medium / live — almost always aggressive)
- **Baseline** (the exact stack the book runs today)
- **Knob** (one change)
- **Windows** (365d + a disjoint second window, or 365d + 1000d)
- **Pass rule** (copy from below; do not invent after)
- **Kill rule**

If the pass rule is missing, the run is research theater.

## 2. Data

- Price path: SQLite daily (`load_close_matrix`) is SoT for close-to-close.
- Overnight / open: Alpaca OHLC **gap** (`open[t] / close[t-1]`), never mix
  Alpaca dollars with sqlite dollars.
- Session calendar: Alpaca RTH majority dates. Drop sqlite weekend/junk bars.
- Journals: Alpaca positions/fills beat `paper_journal.csv`. Journals may be
  ragged — use `modules.csv_utils.read_csv_file` (pad), do not skip.
- STRICT PIT for full-stack `backtester.py`. FULL overlays are diagnostic only.

Fail closed: stale bars, mixed adjusted/unadjusted, or grade C weekly equity
→ the week cannot promote anything.

## 3. Pass (rung 0 → propose rung 1)

All required:

| Gate | NYSE walk (`one_r_hit` family) | Full stack (`backtester.py --paper-aggressive`) |
|------|--------------------------------|--------------------------------------------------|
| Return | > baseline | > baseline |
| Sharpe | — | ≥ baseline |
| MaxDD | not worse by **> 2.0 pp** | not worse by **> 1.0 pp** |
| Costs | print 10 bps round-trip | execution costs ON |
| Second window | 365d and 1000d (or two disjoint 365d) | same |
| Folds | — | walk-forward ≥ 3 **or** two calendar years |
| Knobs | one | one |
| Lookahead | none | STRICT PIT |

Monte Carlo (even 200/200) does **not** pass a candidate. It can only kill one
(left tail too fat).

## 4. Kill

Kill (status `reject`) if any:

- Fails pass on the second window
- Median MC return ≪ mean (lottery path)
- Turnover explodes without costed edge
- The edge is Monday beta (short winners overnight, Friday flatten)
- STRICT loses to a dumber baseline (hold, VTI, 15×30×2)

Rejected ideas stay in the ledger. Do not re-run them with a new story.

## 5. Deploy

`status.py` and this folder **never deploy**.

| Rung | How |
|------|-----|
| Aggressive | Agent may change book `.env` **only after** pass + a sentence in chat that names the knob. Restart that book only. |
| Medium SoT | Owner phrase required. Never `--paper-book`. Reinstall desktop launcher if UI/layout changed. |
| Live | Owner phrase required. Separate lock. |

One book, one knob, one restart. No “while we’re in there.”

## 6. After deploy

- Shadow / weekly_review on **Medium SoT** (`PAPER_SOT_BOOK_ID=alpaca_paper_v2`)
  is attribution, not a promote engine.
- If 14d forward paper is worse than the costed backtest envelope by more than
  noise, revert the knob. Do not add a second knob to “fix” it.
