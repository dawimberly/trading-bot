"""Audit how much Monte Carlo runs actually move regime mixes.

Freeze-safe research helper. Example:
  python scripts/analysis/audit_mc_regime_diversity.py \\
    --jsonl scripts/research/exhaustive_campaign/runs/artifacts/.../mc/runs.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="MC regime_counts diversity audit")
    ap.add_argument("--jsonl", type=Path, required=True)
    args = ap.parse_args()
    path: Path = args.jsonl
    if not path.is_file():
        print(f"Missing {path}")
        return 1

    sigs: list[tuple] = []
    rets: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        rc = o.get("regime_counts") or {}
        sigs.append(tuple(sorted((str(k), int(v)) for k, v in rc.items())))
        try:
            rets.append(float(o.get("total_return_pct") or 0))
        except (TypeError, ValueError):
            rets.append(0.0)

    c = Counter(sigs)
    n = len(sigs)
    uniq = len(c)
    top_n = c.most_common(1)[0][1] if c else 0
    print(f"runs={n} unique_regime_signatures={uniq}")
    print(f"top_signature_share={top_n}/{n} ({100.0 * top_n / max(1, n):.1f}%)")
    if rets:
        s = sorted(rets)
        print(f"return_median={s[len(s) // 2]:.2f}%")
    print("top signatures:")
    for sig, cnt in c.most_common(5):
        print(f"  {cnt}x {dict(sig)}")
    if uniq <= max(3, n // 10):
        print(
            "VERDICT: regime mix barely moves — use --regime-stress "
            "(separate export-dir) for the next MC leg."
        )
    else:
        print("VERDICT: regime mix diversity looks healthier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
