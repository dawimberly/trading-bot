# h1d_trend_mix

Research sleeve. The backtest edge is **not proven**. The strategy is off until a book opts in. Defaults for Live and Medium are unchanged. Exclusive mode is also off until that book sets it.

## Which book is which

| Book id | Account | Portal |
| --- | --- | --- |
| `alpaca_live` | Real money. Profile A, `PAPER_TRADING=false`, `ALLOW_LIVE_TRADING=yes`. Keys in that book's `.env`. | Started. Role `live`. |
| `alpaca_paper_v2` | Medium paper. | Registered, **hidden**, `enabled` false, so the dropdown and `restart_all_bots` do not start it. `paper_chase` is false, so it does not inherit the Lab research profile. Role `medium`. |
| `alpaca_paper` | Legacy Lab paper. Profile B / Realistic Research. "Best Paper v2.2" is a strategy name on this book, not a second account. | Started. Role `lab`. Do not put h1d flags here. |

There is no separate account behind the words "Paper v2". Those words in the repo are the Lab strategy label.

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

Deltas below the dust threshold (`H1D_TREND_MIX_DUST_NOTIONAL`, default $1) are skipped.

If SPY history has fewer than 200 completed closes, a close is missing or non-finite, equity or positions cannot be read, or the state file is corrupt, the cycle **does not trade**.

## Exclusive mode (default off)

`H1D_TREND_MIX_EXCLUSIVE=true` makes this strategy **replace** the other entry sleeves on that book. It does nothing unless `H1D_TREND_MIX_ENABLED` is also on (and the book allowlist matches, when set).

While it is on:

- New entries from the other sleeves are not run, and `execute_order` blocks their buys and sell-to-open. A sell of a position they already hold can still pass. Kraken autopilot is left alone.
- At the first eligible open (same 7-minute window), every holding that is **not** in the strategy symbol list is sold. SSO or BIL leaving the target is sold by the normal rebalance, not a second time here.
- A non-target sell below the broker minimum is a full exit (close the position). A partial trim of an ETF that stays in the target is never a full exit.
- The 8% per-name cap, the active-name cap, and the auto-dust cleaner skip H1D ETFs so they do not sell the 50% SSO sleeve back down. Other names keep those guards until the wind-down sells them.
- State is saved for the initial alignment, the month start, and a trend flip, after the sized orders are accepted. A wind-down by itself does not rewrite state, and it is tried again at the next open if a sell does not go through.

With the flag off, names outside the list are left alone.

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

**7-minute entry freshness.** The repo does not have a separate global 7-minute freshness gate. Other equity sleeves wait `EQUITY_SCAN_AFTER_OPEN_MIN` (default 5) after the bell. This sleeve does not change that. It adds its own rule: submit only in the first 7 minutes after the regular open (`H1D_TREND_MIX_ENTRY_FRESHNESS_MIN`), and only if the decision itself is younger than that same window. Wind-down uses that same window. A missed open is not chased later in the day. The plan stays pending and is tried at the next open.

**Hard stops.** Book defaults are unchanged (`STOP_LOSS_PCT`, `PAPER_SMART_STOPS`, `SMART_STOPS_LIVE_ENABLED`).

- Lab / Medium paper runs smart stops (`PAPER_SMART_STOPS` default on). The hard exit is −10% (`smart_hard_exit`). At −5% the smart stop may tighten or cut size.
- Live Profile A leaves smart stops off unless `SMART_STOPS_LIVE_ENABLED` is set. The fixed stop is `STOP_LOSS_PCT` (5%).

`H1D_TREND_MIX_DISABLE_HARD_STOPS`:

- unset: H1D ETFs skip the fixed stop and the smart −10% hard exit **only when exclusive mode is on**. Stacking the sleeve (exclusive off) leaves the book stops in place.
- `true`: those ETFs are exempt even if exclusive is off.
- `false`: the book stops still apply, even in exclusive mode.

The exemption is the strategy's 200-day trend exit (SSO flips to BIL). It does not add a new stop price. ATR stops, the −5% smart reeval, trailing stops, and max-hold still apply to these ETFs. SPY and GLD are one Alpaca position, so an exemption covers that whole line.

**Live $10 minimum.** `LIVE_MIN_NOTIONAL` defaults to $10 and does not shrink with equity. On a small book:

- Each diversified name is about 4.17% ($12.50) and SSO is 50% ($150), so the first alignment clears $10.
- Later, same-side adjustments below $10 are merged into the largest of those small orders when the sum reaches $10. A batched sell is capped at that name's position value.
- If the sum (after the cap) is still under $10, those adjustments wait. A deferred name can sit just under $10 off target, about 3% of a $300 book.
- Closing a name that is not in the target, when the position itself is under $10, uses a full exit instead of waiting forever.

## How to enable it on one book

Do not put the flags in the shared `stock-bot/.env`. That file is filled in for every book. Set them only in the book env the portal loads (`data/portal/users/<user>/books/<book>/.env`). The portal sets `TRADING_BOOK_ID` to the book id.

Leave `alpaca_paper` (Lab) unchanged.

Live, only in `books/alpaca_live/.env`. Keep dry-run until the owner says otherwise:

```
H1D_TREND_MIX_ENABLED=true
H1D_TREND_MIX_EXCLUSIVE=true
H1D_TREND_MIX_DRY_RUN=true
H1D_TREND_MIX_BOOKS=alpaca_live
H1D_TREND_MIX_DISABLE_HARD_STOPS=true
```

Medium, only in `books/alpaca_paper_v2/.env`:

```
H1D_TREND_MIX_ENABLED=true
H1D_TREND_MIX_EXCLUSIVE=true
H1D_TREND_MIX_DRY_RUN=true
H1D_TREND_MIX_BOOKS=alpaca_paper_v2
H1D_TREND_MIX_DISABLE_HARD_STOPS=true
```

`alpaca_paper_v2` stays hidden and disabled in `trading_books.py`, so the portal will not launch Medium until that entry is turned on. The dry-run command below can still read the book env.

Restart **that book only** after the env is in place and the book is actually running. In the first 7 minutes after the open the log prints target weights (sum 100%) and the notionals, tagged `DRY-RUN` and `not sent`. It does not submit those orders and does not write state.

After several weeks of paper prints look right, set `H1D_TREND_MIX_DRY_RUN=false` on `alpaca_paper_v2` only. Live stays dry-run until the owner says so.

If `H1D_TREND_MIX_BOOKS` is set and the process has no `TRADING_BOOK_ID`, the sleeve does not trade.

## Dry-run command

From `stock-bot/`. This command has no submit flag. It reads positions and equity and prints the orders for the next open. A failed read prints the error and exits 0 with no orders.

```
python -m modules.h1d_trend_mix --dry-run --book alpaca_live --user <portal user>
python -m modules.h1d_trend_mix --dry-run --book alpaca_paper_v2 --user <portal user>
```

`--env-file` can point at the book `.env` instead of `--user`. `--exclusive` lists wind-down sells even when the book env has exclusive off. If the strategy flag is off, the report is labeled `preview-only`. Outside the 7-minute window the orders are labeled `next open` and `not sent`.

Before 16:00 ET on a weekday the signal drops today's bar, because that close is not finished. After the cash close, the next weekday is the session and today's close is included. The preview uses weekdays; it does not skip exchange holidays.

`python -m modules.h1d_trend_mix` with no book still prints a flat $300 illustration and does not read an account.

## Status (2026-10-25)

Live and Medium were switched to this sleeve with exclusive mode on 2026-10-25 at the owner's explicit request, skipping the multi-week paper rollout below. The first cycle after the switch ran with `H1D_TREND_MIX_DRY_RUN=true` and printed the plan with no orders; dry-run was then turned off. Medium is enabled in `trading_books.py` on this branch. Lab is unchanged. Rollback steps are in the top-level `README.md`.

## Original rollout plan

1. Medium (`alpaca_paper_v2`) dry-run for several weeks. Confirm the signal, the open window, and the wind-down list.
2. Medium with `H1D_TREND_MIX_DRY_RUN=false`, still several weeks.
3. Live only with an explicit owner OK, on `alpaca_live`, still dry-run until that OK includes sending orders.

## Risks

- Bootstrap interval on the edge includes zero.
- With exclusive off, targets are a second allocation on top of the book. With exclusive on, other sleeves stop opening names and non-target holdings are sold at the next eligible open.
- A trend flip sells then buys. If a later buy is rejected, the book can sit half-turned until the next open. State is written only after the sized orders are accepted.
- Deferred sub-$10 adjustments drift until a later open can batch them. On a $300 book that gap is just under $10 per name.
- ATR, the −5% smart reeval, trailing stops, and max-hold can still sell an H1D ETF. The 8% trim can still sell a non-H1D name before the wind-down open.
- The 7-minute window is easy to miss if the process is down at the open. The bot does not send a late market order to catch up.
- Medium does not start from the portal while `alpaca_paper_v2` is hidden and disabled.
