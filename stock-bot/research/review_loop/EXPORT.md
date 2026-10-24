# Read-only broker evidence bridge

## What runs
broker_export.py reads the three existing book credential files in memory, verifies the account ID hash against pins copied from the pre-existing local trade reports, and uses only GET requests to fixed Alpaca account, positions, open-orders, portfolio-history, activity, calendar, raw SIP daily-bar and corporate-action routes. Redirects and unrecognized hosts/routes are refused. Pins are never silently changed. The pin source is a previous local broker report, not independent reauthentication by the owner.

All raw responses, fills, account snapshots, portfolio history, corporate-action evidence, request timestamps and content hashes remain in private/. This path is ignored by git and is outside production. No production module is imported. The copied vendor_post_sale_review.py is the existing offline mathematical report builder; its source SHA is in VENDORED_SOURCE.json.

The collector checks identity before requesting fills and again before publishing a prepared report. Historical activity walks are capped and a full page at the cap means incomplete. Fills, fees, dividends and other activity types are retained privately. The collector does not calculate account investment P&L or apply missing corporate actions. Inventory remains explicitly unknown. Current account snapshots are not mislabeled as snapshots from a past trade decision. Raw SIP daily prices are for completed-session research only; no trading feed or freshness gate changes.

## Daily flow
The separate Windows task StockBotResearchEvidenceReview runs daily_run.py on weekday evenings, serializes with a local lock, refreshes all three books, writes a private evidence review and pushes only README.md and latest_review.json to the fixed branch codex/research-review-evidence-20261024 of dawimberly/trading-bot. It uses the machine's existing Git credential manager through normal git push; no GitHub token is extracted or stored in source. Prompts are disabled. Publication fails closed if authentication is unavailable, the remote/branch differs, unknown files are tracked or a push is not a fast-forward.

The public summary contains only source timestamps, purchase/sale/plan/path counts and evidence-gap flags. It excludes balances, position details, account IDs, account hashes, raw fills, order IDs, free-text broker errors, credentials and account-return figures. GitHub oversight can now read that branch without access to private files on the PC. No model is trained or deployed by this bridge.

The PC must be awake and Owner logged on. No wake timer, new service, administrator execution or trading-task modification is used. A task missed while the PC is off can run when available after logon. A stale daily.lock requires inspection; it is not automatically deleted on assumed PID reuse. A failed refresh retains prior evidence with its old as-of time and marks the latest refresh unsuccessful.

## Manual use

    python -B broker_export.py --source-root C:/Users/Owner/PythonTrading/stock-bot --bootstrap-pins
    python -B daily_run.py --source-root C:/Users/Owner/PythonTrading/stock-bot --publish

Bootstrap runs only once; the current pins already exist. Do not publish private/. Do not re-run bootstrap to bypass an account mismatch. The --reuse-run option is for a reviewed one-shot replay of an existing collection, not a fresh broker fetch.

## Boundaries
This completes fresh evidence collection and sanitized publication, not comprehensive strategy learning. Original plans missing historically remain missing. Only 80 recent sale symbols are priced per run, with the remainder explicitly deferred; expanding coverage or implementing incremental prioritization is later work. Corporate actions/initial inventory, in-trade path attribution, cash-flow-adjusted equity/benchmarks, rejected-opportunity labels, the experiment registry/worker and controlled shadow/paper promotion remain unfinished.

Alpaca API references:
- https://docs.alpaca.markets/us/reference/getaccountactivities-2
- https://docs.alpaca.markets/us/reference/getaccountportfoliohistory-1


## Verified installation receipt
The separate research task was registered for weekdays at 18:00 Central, limited interactive Owner logon, missed-start handling enabled, no wake timer, battery execution allowed and overlapping runs ignored. Its first scheduled time is Monday 2026-10-26. A manual launch through the actual hidden Task Scheduler path completed with exit code 0 on 2026-10-23 at 19:37 Central. It refreshed all three accounts, generated the private review and published the allowlisted summary. The next scheduled run is 2026-10-26 at 18:00 Central.


## Legacy journal correction
The original aggressive Lab journal and its July 29 backup are now integrated as decision-attribution sources. New purchase-plan counts are not used as a count of all historical decision evidence. See ATTRIBUTION.md for the conservative matching rule, preserved unmatched executions, regulatory/crypto fee separation and the still-blocked whole-book profit.
