"""Single 365d paper-aggressive run with post-dotenv config overrides.

Usage:
  python _run_single_ab_leg.py --label "ARIMA ON" --set ARIMA_ENABLED=true [--days 365]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("MARKOV_HMM_ENABLED", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

logging.disable(logging.INFO)
_orig = logging.Logger.callHandlers


def _quiet_handlers(self, record):
    if record.levelno < logging.WARNING:
        return
    return _orig(self, record)


logging.Logger.callHandlers = _quiet_handlers


def _parse_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default=os.environ.get("V154_TUNE_DAYS", "365"))
    ap.add_argument("--label", default="leg")
    ap.add_argument(
        "--export-json",
        default="",
        help="Optional path for backtester --export-json",
    )
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        help="KEY=VALUE overrides applied to config after import",
    )
    args = ap.parse_args()

    import config

    config.PAPER_DEPLOY_DEBUG = False
    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"Bad --set {item!r}, expected KEY=VALUE")
        key, val = item.split("=", 1)
        key = key.strip()
        val = val.strip()
        # Keep os.environ explicit so enforce_realistic_research_profile won't clobber.
        os.environ[key] = val
        if not hasattr(config, key):
            print(f"WARNING: config has no attr {key}", flush=True)
            continue
        cur = getattr(config, key)
        if isinstance(cur, bool):
            setattr(config, key, _parse_bool(val))
        elif isinstance(cur, int) and not isinstance(cur, bool):
            # float-looking ints
            if "." in val:
                setattr(config, key, float(val))
            else:
                setattr(config, key, int(val))
        elif isinstance(cur, float):
            setattr(config, key, float(val))
        else:
            setattr(config, key, val)
        print(f"OVERRIDE {key}={getattr(config, key)!r}", flush=True)

    print(f"=== LEG {args.label} days={args.days} ===", flush=True)
    print(
        f"ARIMA_ENABLED={config.ARIMA_ENABLED} "
        f"DYNAMIC_VTI_OPTIONAL_ENABLED={config.DYNAMIC_VTI_OPTIONAL_ENABLED} "
        f"DYNAMIC_VTI_ALLOW_ZERO={config.DYNAMIC_VTI_ALLOW_ZERO} "
        f"SPY_LIKE_BOOST_ENABLED={config.SPY_LIKE_BOOST_ENABLED}",
        flush=True,
    )

    sys.argv = [
        "backtester.py",
        "--days",
        str(args.days),
        "--paper-aggressive",
        "--no-thinking",
    ]
    if args.export_json:
        sys.argv.extend(["--export-json", str(args.export_json)])
    import runpy

    runpy.run_path(str(ROOT / "backtester.py"), run_name="__main__")


if __name__ == "__main__":
    main()
