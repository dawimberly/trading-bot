# h1d_trend_mix

Research sleeve. The backtest edge is **not proven**. The strategy is off until a book opts in. It does not change Live (`alpaca_live`) or Medium paper (`alpaca_paper`, Paper v2 / Realistic Research) defaults.

This repo has no book id `alpaca_paper_v2`. Medium paper is `alpaca_paper`.

## Rules

Monthly mix, two sleeves, 50% each. Account size in the research note is about $300, so orders are fractional notional orders, not whole shares.

**Sleeve A — trend.** After each completed session, compare SPY's close with its 200-day simple moving average of completed closes (the signal day is included in the average).

- Prior close **above** the SMA: hold SSO.
- Otherwise, including a tie: hold BIL (T-bill proxy).

The signal is that close. The trade is the **next regular-session open**. The bot checks every day so a flip is eligible at the next open. It does not use the session it is about to trade.

**Sleeve B — diversified.** Equal weight:

`XLB XLE XLF XLI XLK XLP XLU XLV XLY SPY TLT GLD`

**Rebalance.** Put both sleeves back to 50/50 and to those targets:

- the first open caught in a new calendar month
- a trend flip
- the first run on a book with no saved state (otherwise a mid-month enable would wait until the next flip or month)

Nothing else is traded. Names outside this list are left alone. Deltas below the dust threshold (`H1D_TREND_MIX_DUST_NOTIONAL`, default $1) are skipped.

If SPY history has fewer than 200 completed closes, a close is missing or non-finite, equity or strategy positions cannot be read, or the state file is corrupt, the cycle **does not trade**.

## Backtest (not a proven edge)

1999–2026, pre-tax, realistic costs:

| | h1d_trend_mix | VTI |
| --- | ---: | ---: |
| CAGR | 9.35% | 8.78% |
| Max drawdown | -38.6% | -55.5% |
| Worst year | -17.5% | -37% |

A bootstrap confidence interval of the edge **includes zero**. Treat the sleeve as research, not as a demonstrated edge.

## Safety already in the bot

**Market hours.** Orders are sent only when this cycle's clock check and `run_all`'s equity-session flag both say the regular session is open. `AlpacaExecutor` still refuses equity orders when the session is closed. A failed clock check fails closed (no trades).

**7-minute entry freshness.** The repo does not have a separate global 7-minute freshness gate. Other equity sleeves wait `EQUITY_SCAN_AFTER_OPEN_MIN` (default 5) after the bell. This sleeve does not change that. It adds its own rule: submit only in the first 7 minutes after the regular open (`H1D_TREND_MIX_ENTRY_FRESHNESS_MIN`), and only if the decision itself is younger than that same window. A missed open is not chased later in the day. The plan stays pending and is tried at the next open.

**Hard stops.** Defaults are unchanged.

- Medium paper runs smart stops (`PAPER_SMART_STOPS` default on). At the hard threshold (default **−10%**) `evaluate_smart_stop` exits the whole position. At −5% it may tighten the stop or cut size. `run_position_exits` applies that to non-crypto names other than the VTI core, which includes these ETFs if the sleeve holds them.
- Live Profile A leaves smart stops off unless `SMART_STOPS_LIVE_ENABLED` is set. The fixed stop is `STOP_LOSS_PCT` (**5%**, not 10%). A −10% loss on Live would already have passed that 5% stop. If smart stops are later turned on for Live, the same −10% hard exit applies.

**Proposal, strategy config only.** `H1D_TREND_MIX_DISABLE_HARD_STOPS` defaults to the strategy proposal (on) **only after the sleeve itself is enabled**. While `H1D_TREND_MIX_ENABLED` is off, `hard_stop_exempt()` is false and Medium/Live stops behave as they do today. `STOP_LOSS_PCT`, `PAPER_SMART_STOPS`, and `SMART_STOPS_LIVE_ENABLED` are not modified.

When the sleeve is on and the proposal is left on, the fixed stop and the smart −10% hard exit are skipped for this sleeve's ETFs only (SSO, BIL, the sector ETFs, SPY, TLT, GLD). The −5% smart reeval, ATR stops, trailing stops, and max-hold are **not** skipped. On Medium those can still sell these ETFs.

SPY and GLD are also used by the existing book. Alpaca keeps one position per symbol, so an exemption covers the whole SPY or GLD line, not a separate lot. Set `H1D_TREND_MIX_DISABLE_HARD_STOPS=false` on that book to keep the book's hard stops.

## How to enable it on one book

Do not put the flag in the shared `stock-bot/.env`. That file is filled in for every book. Set it only in the book env the portal loads (`PYTHONTRADING_ENV_FILE`). The portal also sets `TRADING_BOOK_ID` to `alpaca_paper` or `alpaca_live`.

Medium paper, dry-run first:

```
H1D_TREND_MIX_ENABLED=true
H1D_TREND_MIX_DRY_RUN=true
H1D_TREND_MIX_BOOKS=alpaca_paper
```

Restart **that book only**. In the first 7 minutes after the open the log prints target weights (sum 100%) and the notionals it would send, tagged `DRY-RUN` and `not sent`. It does not submit those orders and does not write state.

After several weeks of paper prints look right, set `H1D_TREND_MIX_DRY_RUN=false` on `alpaca_paper` and restart that book again.

Live (`alpaca_live`) stays off until the owner says so. Do not copy the flag into the live book env before that.

Optional preview without enabling the book: from `stock-bot/`, `python -m modules.h1d_trend_mix` is dry-run only and never submits. That command sizes a flat $300 illustration and does not read an account. The book dry-run uses the book equity.

If `H1D_TREND_MIX_BOOKS` is set and the process has no `TRADING_BOOK_ID`, the sleeve does not trade.

## Rollout

1. Medium paper (`alpaca_paper`) in dry-run for several weeks. Confirm signals, the open window, and that other sleeves were not retuned.
2. Medium paper with dry-run off, still several weeks. Watch overlap with the existing SPY / GLD / sector holdings and with trailing or max-hold exits.
3. Live only with an explicit owner OK, on `alpaca_live`, preferably after deciding whether hard stops on shared ETFs stay on (`H1D_TREND_MIX_DISABLE_HARD_STOPS=false`).

## Risks

- Bootstrap interval on the edge includes zero.
- Targets are fractions of **that book's whole equity**. On a book that already runs the 33/67 stack, this sleeve adds another full allocation instead of replacing it.
- A trend flip sells then buys. If a later buy is rejected, the book can sit half-turned until the next open. State is written only after the sized orders are accepted.
- Live's minimum notional (default $10) can skip a small sleeve-B delta even when it is above the $1 dust threshold.
- The 7-minute window is easy to miss if the process is down at the open. The bot does not send a late market order to catch up.
