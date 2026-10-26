"""Filesystem locations. Frozen research files are read-only inputs."""

import os
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
RESEARCH = LAB.parent
STOCK_BOT = RESEARCH.parent
ABC = RESEARCH / "nyse_cash_abc"
WALK = RESEARCH / "walk_forward_select"
BARS = ABC / "data" / "bars.sqlite"
ABC_MANIFEST = ABC / "data" / "manifest.json"
ABC_RESULTS = ABC / "results" / "abc_results.json"
WALK_CONFIG = WALK / "config.json"
PAYABLE_EVAL = WALK / "results" / "payment_timing" / "evaluation.json"
PAYABLE_SELECTIONS = WALK / "results" / "payment_timing" / "selections.json"
PAYMENTS = WALK / "data" / "dividend_payments.json"
LAB_DATA = LAB / "lab_data"
EXPORTS = LAB_DATA / "exports"
DB = LAB_DATA / "lab.sqlite"
SNAPSHOT_DIR = LAB_DATA / "snapshots"
SPAN_BUNDLE = SNAPSHOT_DIR / "span_2021_2026"
SIM_V1 = SNAPSHOT_DIR / "sim_2021_2026_v1"
SIM_V2 = SNAPSHOT_DIR / "sim_2021_2026_v2"
SIM_BUNDLE = SIM_V1
SNAPSHOT_REGISTRY = SNAPSHOT_DIR / "registry.json"
MEMBERSHIP_FILE = STOCK_BOT / "data" / "sp500_ticker_start_end.csv"
UNAPPLIED_ACTIONS = ABC / "data" / "unapplied_actions.json"


def database_path() -> Path:
    override = os.environ.get("STRATEGY_LAB_DB")
    return Path(override) if override else DB
SNAPSHOT_CACHE = LAB_DATA / "snapshot.json"

FROZEN_READ_ONLY = (BARS, ABC_MANIFEST, ABC_RESULTS, WALK_CONFIG, PAYABLE_EVAL, PAYABLE_SELECTIONS, PAYMENTS)
