"""Print non-secret strategy knobs from paper book .env files."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYS = (
    "MAX_ACTIVE_TICKERS",
    "ATR_STOP_MULTIPLIER",
    "PAPER_MEDIUM_STRATEGY",
    "PAPER_LAB_CONCENTRATED",
    "PAPER_POSITION_MAX_HOLD_BARS",
    "EXIT_OPTIMIZATION_ENABLED",
    "PAPER_SMART_STOPS",
    "PAPER_NYSE_FAT_LOSER_ENABLED",
    "PAPER_NYSE_A2B1_ENABLED",
    "PAPER_NYSE_GAIN_EXIT_PCT",
    "PAPER_NYSE_GAIN_EXIT_MODE",
    "PAPER_NYSE_PER_NAME_MAX_PCT",
    "PAPER_MAX_POSITION_PCT",
    "PAPER_DYNAMIC_VTI",
    "PAPER_VTI_CORE_PCT",
    "PAPER_NYSE_SLEEVE_CAP_PCT",
    "PAPER_STAT_ARB_ENABLED",
    "PAPER_ORB_ENABLED",
    "PAPER_CHASE_MODE",
    "PAPER_AGGRESSIVE",
)
MISSING = "<unset>"

for book in ("alpaca_paper_v2", "alpaca_paper"):
    path = ROOT / "data/portal/users/dawimberly/books" / book / ".env"
    vals: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k in KEYS:
            vals[k] = v
    print("==", book, "disk .env ==")
    for k in KEYS:
        print(f"  {k}={vals.get(k, MISSING)}")
