# Audit of the 2026-10-21 prototype

The workspace prototype's 19 tests passed. These defects were still in the source. They are fixed in this directory. Live, Medium, and the current Lab were not modified.

## Causal learning

Training counted a closed trade only when both the entry session and the exit session sat inside the last 100 complete sessions. A trade that matured inside the window but was opened earlier never became a label. A future `exit_time` was excluded only by a timestamp check, and the session ids in the test did not sort as dates.

Labels are now baseline virtual round trips whose exit time is before the decision and whose exit session falls inside the last 100 complete sessions. The entry may be earlier. Learner trades are not labels. The chronological split still keeps an exit date entirely in training or entirely in validation. Acceptance is validation MAE against a constant fit on the earlier dates. The refit is frozen for 30 further complete sessions. Each update stores the window, sample definition, date split, feature hash, parameters, MAE, the constant MAE, the fallback reason, the activation time, and the freeze count. `portfolio_promotion` is false.

## Time and fills

A decision could fill on any later frame that happened to contain the symbol. A missing five-minute bar became a silent "next available" open.

A buy now fills only when the frame is the next scheduled session bar. Otherwise the buy is cancelled and the skip is stored. A latched sell still fills, and a missed bar is stored as `gap_fill`. The same bar cannot decide and fill. `as_of` must be at least five minutes after the bar start, so an unfinished bar is refused.

## Indicators

History was the last 51 closes with no timestamps. A symbol that skipped a bar was treated as if those closes were adjacent five-minute returns.

History is now `(timestamp, close)`. A gap resets that symbol's chain. Features stay empty until 51 contiguous session bars exist again, including the junction from the prior session's last bar to 09:30. A held symbol with no price still rejects the whole frame.

## Calendar

Any weekday from 09:30 through 15:55 could count, and a session was complete only with all 78 of those stamps. Holidays would have counted. Early closes could never count.

Bars must fall on a listed exchange session and inside that session's close. Thanksgiving 2026-11-26 is rejected. The verified 13:00 close on 2026-11-27 expects 42 bars, from 09:30 through 12:55, and a 13:00 stamp is rejected. Dates that have no verified close cannot become complete training sessions.

## Corporate actions

The feed was tradable when the caller set `actions_checked` to true. Only simple splits were applied. Dividends, mergers, renames, and spin-offs were either a generic error or absent if the caller omitted them.

The boolean is rejected as an audit. Synthetic events must be labeled synthetic and are not an independent audit. Cash dividends increase cash and a dividend ledger. Splits and stock dividends adjust quantity, entry, peak, and indicator history. Mergers, renames, and spin-offs stop the frame and name the event. The local cache is split-adjusted, so the reader will not emit it as raw prices.

## Accounting and review

The cash identity held for trades, but the report did not separate realized and unrealized P&L, exposure, turnover, or external flows. Post-sale paths followed observed complete sessions rather than the exchange calendar.

The report now checks cash plus cost basis against starting cash, realized trade P&L, dividends, and external flows, and it checks marked equity against those same pieces plus unrealized P&L. External flows are zero. SPY and VTI are unavailable, not zero. Post-sale horizons are 1, 5, 10, 20, and 30 later exchange sessions. Incomplete sessions, unverified closes, and missing prices stay labeled. The price change is not a verdict on the sale. Dividends are excluded from that price path.

## What the passing prototype tests did not show

They did not show a market feed, an exchange holiday, an early close, a missing per-symbol bar, a dividend, a restart of the cooldown, or a portfolio result. The synthetic smoke run is plumbing. Six sessions cannot train the ranker, so the two books match. That match is not a profitability result.
