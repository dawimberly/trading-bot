"""Run Dynamic VTI ≥40% leg for 1000d and merge with Fixed 20% baseline."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import config
from backtester import _ensure_daily_data, run_backtest


def main() -> int:
    config.enforce_realistic_research_profile()
    config.DYNAMIC_VTI_PAPER_FLOOR = 0.40
    config.DYNAMIC_VTI_PAPER_CEILING = 0.75
    config.DYNAMIC_VTI_DEFAULT_PCT = 0.65
    config.DYNAMIC_VTI_CALM_PCT = 0.50
    config.DYNAMIC_VTI_STRESS_PCT = 0.75
    config.DYNAMIC_VTI_FLOOR_MIN = 0.40
    config.DYNAMIC_VTI_ALLOW_ZERO = False

    data = _ensure_daily_data(1000, refresh=False, use_max=False)
    print("--- Dynamic VTI only 1000d ---", flush=True)
    print(
        f"Tiers: stress={config.DYNAMIC_VTI_STRESS_PCT:.0%} "
        f"default={config.DYNAMIC_VTI_DEFAULT_PCT:.0%} "
        f"calm={config.DYNAMIC_VTI_CALM_PCT:.0%} "
        f"floor={config.DYNAMIC_VTI_PAPER_FLOOR:.0%}-"
        f"{config.DYNAMIC_VTI_PAPER_CEILING:.0%}",
        flush=True,
    )
    result = run_backtest(
        data,
        track_active_exposure=True,
        paper_aggressive=True,
        paper_dynamic_vti=True,
    )
    vti_avg = float(result.get("vti_core_pct") or 0.0)
    if 0.0 < vti_avg <= 1.5:
        vti_avg *= 100.0
    row = {
        "label": "Dynamic VTI >=40% (paper)",
        "return_pct": result["total_return_pct"],
        "sharpe": result["sharpe"],
        "max_dd_pct": result["max_drawdown_pct"],
        "avg_active_pct": result["avg_active_exposure_pct"],
        "avg_vti_pct": round(vti_avg, 1),
    }
    print(
        f"Dynamic: ret={row['return_pct']:+.2f}% sharpe={row['sharpe']:.2f} "
        f"maxdd={row['max_dd_pct']:.2f}% avgAct={row['avg_active_pct']:.1f}% "
        f"vtiAvg={row['avg_vti_pct']:.1f}%",
        flush=True,
    )
    fixed = {
        "label": "Fixed 20% VTI (legacy)",
        "return_pct": 28.53,
        "sharpe": 0.67,
        "max_dd_pct": -17.85,
        "avg_active_pct": 80.0,
        "avg_vti_pct": 20.0,
    }
    out = ROOT / "scripts" / "analysis" / "dynamic_vti_ab_1000.json"
    out.write_text(
        json.dumps(
            {
                "days": 1000,
                "tiers": {
                    "stress": 0.75,
                    "default": 0.65,
                    "calm": 0.5,
                    "floor": 0.4,
                    "ceiling": 0.75,
                },
                "rows": [fixed, row],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Saved: {out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
