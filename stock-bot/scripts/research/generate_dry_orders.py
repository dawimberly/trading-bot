"""Generate synthetic dry-run order payloads and write mock responses to data/dry_orders.jsonl

This script does not require Alpaca or other services. It writes JSONL records with the mock order response
format used by the DRY_RUN path (id, status, filled_qty, symbol, request).
"""
import json
import time
from pathlib import Path

OUT = Path("data") / "dry_orders.jsonl"
OUT.parent.mkdir(parents=True, exist_ok=True)

samples = [
    {"symbol": "AAPL", "side": "buy", "qty": 1.0, "type": "market"},
    {"symbol": "TSLA", "side": "sell", "qty": 0.9999999, "type": "limit", "limit_price": 199.99},
    {"symbol": "SKY-USD", "side": "buy", "notional": 50.0, "type": "market"},
    {"symbol": "BTCUSD", "side": "buy", "notional": 10.0, "type": "market"},
    {"symbol": "SMALL", "side": "buy", "qty": 9.101098725, "type": "market"},
    {"symbol": "SMALL", "side": "sell", "qty": 9.10109334, "type": "market"},
]

records = []
for i, req in enumerate(samples):
    now_ms = int(time.time() * 1000)
    mock = {
        "id": f"DRY-{now_ms}-{i}",
        "status": "new",
        "filled_qty": 0,
        "symbol": req.get("symbol"),
        "request": req,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    records.append(mock)

with OUT.open("w", encoding="utf-8") as f:
    for r in records:
        f.write(json.dumps(r) + "\n")

print(f"Wrote {len(records)} mock orders to {OUT}")
