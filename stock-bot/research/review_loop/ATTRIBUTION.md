# Legacy aggressive Lab decision attribution

alpaca_paper is the old aggressive paper account, now owned by Lab Next. Its historical journal exists. Missing new trade_plans files never establish that historical decisions or logs were deleted.

## Sources and verified scope
- paper_journal.csv: 33,219 parsed CSV records, including 501 fills, 350 signals and 201 exits.
- paper_journal.csv.bak_pre_quarantine_20260729T221617Z: 30,238 parsed CSV records, dated history but no additional fill rows. It remains intact. CSV records differ from physical lines because quoted errors contain newlines.
- The quarantine and July 18 backups remain source evidence; this patch neither deletes nor rewrites any journal, backup or bot.log. It does not infer dated trades from undated broken lines or treat bot.log as a broker blotter.
- Sources are read using the local modules/csv_utils.py read_csv_file helper via an isolated importlib load. No production package initializer, configuration or trading engine is imported. The existing local pandas dependency is used; no package installation was performed.

## Frozen observational matching rule
The policy in journal_match_policy.json was written before inspecting match results. Symbol and side must agree; absolute quantity tolerance is 0.00000001; timestamps must be within 120 seconds. Broker price must round to the journal's recorded decimal precision (half-even). Broker timestamps require offsets; naive journal timestamps use America/Chicago, the documented host convention. The originals did not store offsets, so that assumption stays explicit.

Candidates are single execution IDs or a completed order's complete fill group. No arbitrary partial subset sums or greedy nearest match. Only one compatible candidate with no competing journal claimant is attributed. Identical observations across source snapshots collapse; repeated identical rows within one source stay ambiguous. Activity IDs deduplicate broker executions and conflicting duplicates are refused. No rule is loosened after seeing results.

Matches preserve sleeve, notes, exit_reason, regime, source CSV record references and original row content in private evidence. Nearby signal/exit rows are retained as context, explicitly not a confirmed recorded-order link. A unique evidence match is not proof that the journal recorded the broker order ID. Original purchase plans are not reconstructed from these rows.

## Measured result on the existing broker snapshot
165 of 501 journal fill rows have unique conservative broker matches (165 equity executions). The other 336 journal fill records remain unmatched. All 350 signal and 201 exit records remain in the private decision evidence.

15,657 equity executions and 85 crypto executions remain without a matching saved journal decision. Of the equity executions, 140 precede the June 8 journal start DATE; 149 precede its first timestamp, because nine more occur earlier on June 8. Those are different boundaries, not conflicting history counts. No unmatched execution is converted into an invented plan or ignored.

The source broker snapshot as-of is 2026-10-24 00:34:36 UTC (October 23 Central). This patch processes that collected evidence offline; it makes no new broker request and does not label its later processing time as a fresh broker read.

## Costs and accounting
Posted FEE rows with REG, CAT and TAF subtypes are separate equity regulatory cost lines. CFEE is a separate crypto pair transaction-fee bucket, excluded from stock costs. Signed amounts/credits remain signed and currencies are never summed together. Unknown activity types or fee subtypes remain unresolved rather than becoming commissions or trade profit.

Lab's collected ledger has no posted DIV cash. Add dividend cash only when an actual posted broker activity exists. No dividend strategy, dividend sleeve or reinvestment rule is added. No stock commission is inferred from regulatory pass-throughs.

Whole-book profit remains BLOCKED: unknown opening inventory/cost basis, positive equity-history observations on May 22 and May 23 before the May 24 activity start, and corporate actions not applied to the position/cost ledger. Sales proceeds and current broker average price are not a replacement for that missing basis. Fee categorization or incomplete original plans are not the opening-cost-basis blocker.

## Daily integration and privacy
The separate research daily runner calls refresh_lab_attribution.py after collection, offline, before reviewing/publishing. Only allowlisted counts of legacy journal evidence and matches reach the public summary; raw journal text, notes, quantities, prices, execution/order IDs and fee amounts stay under private/. Errors block legacy coverage rather than reuse stale attributed counts. Initial inventory and original_plan fields are not filled in.

The last published observation still contains the small number of newly recorded purchase plans; that count is now explicitly distinct from the 501-row historical fill decision record. The reporting limitation is missing ORIGINAL plans, not missing all local historical decision evidence.

Run offline against a completed private collection:

    python -B refresh_lab_attribution.py --source-root C:/Users/Owner/PythonTrading/stock-bot --run COMPLETED_RUN_NAME --update-prepared-report

The CLI refuses network use and writes outside the private research root. All changes are research/reporting only. No engine, .env, trading rule, order or task schedule is changed.
