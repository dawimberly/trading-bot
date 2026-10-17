# Purchase plans and post-sale review — observational patch

Audit: modules/game_plan.py is sleeve allocation, not a per-order plan. modules/trade_journal.py log_fill records fills/FIFO metadata and trade_closed; modules/strategy_performance.py sync_from_journal summarizes closed-trade returns. Neither supplies a 30-session post-sale path. modules/decision_tape.py captures NYSE ranks but not an explicit exit plan for each submitted purchase. The executor notification path has known partial-fill/dedup limits, so broker FILL activity remains authoritative for post-sale reports.

This staged patch adds a pre-submit plan for each equity purchase through AlpacaExecutor._submit_order, a broker-order link after submission, and an OFFLINE report generator from broker fills plus explicit calendar/daily-price inputs. It does not fetch prices, call brokers, send messages, schedule work, or activate a dashboard. No production files or running processes are changed by staging it.

30 means 30 exchange trading sessions STRICTLY AFTER the last fill's Eastern sale date; sale-day pre-sale highs/lows cannot leak in. Partial broker executions of one order aggregate into one sale; separate sell orders remain separate. Brokerage partial fill status is not position partial/full status. Inventory classification is unknown without a verified initial inventory. Buy covers are not new long purchases when a cached short position is visible. Historical trades have original_plan_unavailable unless linked to a contemporaneous saved plan; do not fabricate intent.

Milestones 1/5/10/20/30 use the exact session, not the Nth available price. Missing intermediate bars leave completeness false; never ffill a missing/delisted tape. Forward high/low use available OHLC after sale day; with close-only source, label them close extrema. Raw broker sale prices and price series must share declared units; corporate-action and dividend uncertainty is explicit. Price-only follow-through is not total return, a causal timing verdict, or a replacement-capital strategy. Return, dollars on sold quantity, fees, distributions and replacement-capital attribution remain separate. No assumed transaction cost is silently added. Actual redeployment attribution is unavailable unless cash flows can be traced; do not pair a sale with the next buy merely by time.

Plans use current effective config functions, not book-file claims. Paper smart ATR is dynamic; the exact future stop/ATR is unknown until that stop module computes it. Live fixed5% is recorded as the existing rule, NOT the rejected expected-drawdown rule. Lab stop/trail/partial/hold inputs are recorded. Position caches are marked cached, never called fresh broker evidence. Add impact at fill is not inferred from a pre-submit quote. Plan failure logs visibly but cannot block or alter orders/exits.

Integration requires Cursor to review/apply the patch on disk; no restart is authorized. Existing engines will not load it until their normal separately authorized launch. The offline reporter can run without engine activation. Actual Monday recording and 30-session maturation are not yet verified.

## Cursor integration contract

The PR is based on GitHub main 0d86e92 (September12), NOT the later local checkout. Do not reset, checkout-over, or copy the complete executor from this branch onto the local bot. Add the new modules/tests/CLI/docs and apply ONLY the two small _submit_order hooks to the existing local executor. Preserve its newer error handling, journal/accounting/recorder/sizing/supervisor work. Re-run the focused tests and strip-hook semantic comparison against that local pre-change file. No engine restart or flag edits are authorized by the PR. Do not merge automatically.

Plan fields are observational and incomplete until linked broker fills and decision/exit evidence exist. The code does not enforce having a plan, modify order IDs, or block a trade on capture failure. Short-cover buys and non-NYSE strategies are distinguished or labeled unverified; no universal strategy-specific exit is invented.

## Usage (offline, no .env/config imports)

python scripts/analysis/post_sale_review.py --book alpaca_live --fills fills.json --calendar calendar.json --prices prices.json --as-of 2026-10-17T20:00:00Z --output reports/research/post_sale_live_20261017.json

Optional --plans file1.jsonl file2.jsonl joins contemporaneous pre-submit plans through order-link IDs. Optional --initial-inventory inventory.json must be a verified map of symbol to quantity at the start of the supplied fill history. An explicit {} asserts flat; omit when unknown. Overlapping same-symbol orders are classified conservatively because aggregated fill groups do not establish inventory between interleaved executions. Reports refuse to overwrite existing files or write into book/tape directories.

fills.json: array of broker FILL activity objects with id,order_id,symbol,side,qty,price,transaction_time. Timestamp must include timezone; each execution must have its own broker ID. No API keys or .env data are inputs. Caller verifies account provenance; the report explicitly cannot authenticate the caller-supplied book.

calendar.json: exchange-session array [{"date":"2026-10-19","close":"2026-10-19T20:00:00Z"},...]. Include actual session closes (holidays/early closes), not generated weekdays. Only closes completed by --as-of mature a milestone.

prices.json: per symbol {"ABC":{"basis":"raw_same_units_as_broker","unit_alignment_verified":true,"corporate_actions_checked_through":"2026-11-30","uncertain_action_dates":[],"bars":[{"date":"2026-10-19","close":100,"high":102,"low":98}]}}. This is a provenance contract, not a magic validation switch: source adapters must establish raw same-unit prices and action coverage before setting it. Adjusted series cannot be compared directly to historical raw fills. If missing/unverified, price results stay unavailable. A future price-provider/rename/dividend adapter is NOT implemented in this PR; no production history was imported and no historical return claim is made. Close-only input is allowed but extrema are labeled daily_close_only.

Results are JSON for review, not a dashboard panel. Replacement-capital performance remains explicitly unassigned until cash/fills attribution is available. Fees and dividends are excluded from this price-only diagnostic, not silently assumed zero in net P&L. A price increase after sale is not itself evidence of a bad decision.

## Validation

Focused stdlib suite: 24 tests passed on isolated Python3.11. Full GitHub executor AST comparison against the fetched original passed after removing observational hooks. No trading imports/network/notifications were used. CI runs only the focused suite plus compilation, not profitability tests. Actual plan capture after a normal authorized engine launch and end-to-end price-provider integration are still unverified.
