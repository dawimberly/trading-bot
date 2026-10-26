# Staging v2 report: live snapshot + PR23 (h1d trend mix) + PR24 (exit reconcile), 2026-10-25

Worktree: C:\workspace\staging_v2_20261025. Branch: staging/livesnap-h1d-exitfix-20261025 (local, not pushed).

## Commits
- 1de29ba7: snapshot of the live working tree code (222 files: 87 modified tracked + 135 untracked code). SHA-256 matches live for every copied file. Paths are in STAGING_COPIED_PATHS.txt. Excluded: .env*, data/, reports/, sentiment/, logs, jsonl, heartbeat json, *.db/csv, caches, repo-root extras.
- 2008f4a2: merged PR23.
- c8c61fb0: merged PR24.
- a3e0a483: cherry-picked the two test updates.
- cf0ca06c: fix for the dust-close regression the merge caused.

## Conflicts and resolutions
PR23:
- alpaca_executor.execute_order: kept live's lab_forward_trail buy block and added PR23's h1d exclusive block after it.
- portal_bot.user_bot_env: kept live's version.
- trading_books: kept live's registry (v2 enabled, chase on, no kraken) and added role=medium.
- run_all.py (6 hunks): kept live's VTI unwind and live's _paper_research_scan_allowed gates on options, ORB, sector rotation and vol breakout, then added `not h1d_pause` to each. NYSE: added PR23's h1d cycle call before live's equity_scans block (live's SPY-sleeve retirement kept; PR23's re-added SPY calls dropped).

PR24:
- alpaca_client: auth check uses PR24's 403 held_for_orders case. Kept live's is_not_fractionable_error and TLS retry, using broker_error_text in the raises. Added held_for_orders to is_skippable_order_error.
- alpaca_executor: kept live's _broker_order_id. execute_full_exit and execute_reduce_notional use PR24's reconciled path with live's exit_meta. The dust path keeps exit_meta and adds skip_dust=True. exit_meta is threaded through _submit_reconciled_exit to _submit_order, including its retries.
- position_exits: kept live's a2b1 helpers and PR24's journal helpers. The exit call keeps live's reason/exit_meta and adds PR24's _journal_pending_exit.

Fixed after the merge: PR24's "non-fill status" guard in order_fill_details stopped live's polling of dust closes that have an order_id. The guard now applies only when there is no order id.

## Tests (tests/, venv311; scripts/ excluded)
- Snapshot-only baseline: 429 passed, 34 failed, 8 errors.
- Final: 469 passed, 34 failed, 8 errors. The failure list is identical to the baseline.

## Dry runs (forced dry-run; read-only broker; flags set per command; DB snapshot)
Both books target SSO 50% and 12 funds at 4.17% each (SPY TLT GLD XLB XLE XLF XLI XLK XLP XLU XLV XLY).
- Live: 9 wind-down sells, about 95% of the book; 13 buys. No errors and no skips under the minimum.
- Medium: 13 wind-down sells, about 96% of the book; 13 buys. No errors.

## Smoke check
run_all and run_paper_bot both import cleanly. Their main loops are guarded by __main__, so importing them does not start a trading loop.
