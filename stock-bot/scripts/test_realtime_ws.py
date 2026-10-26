"""Smoke-test Alpaca WebSocket feed (reconnect, batching, status). No trading."""

from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402
from modules.real_time_data import (  # noqa: E402
    MAX_RETRIES_PER_SESSION,
    format_status_line,
    get_latest_price,
    get_status,
    resolve_subscription_symbols,
    start_realtime_feed,
    stop_realtime_feed,
)


def main() -> int:
    if not config.effective_real_time_websocket_enabled():
        print("REAL_TIME_WEBSOCKET_ENABLED is off (set true in .env or use paper mode).")
        return 1

    stocks, crypto = resolve_subscription_symbols()
    print(f"Configured: {len(stocks)} stocks, {len(crypto)} crypto")
    print(f"  stocks: {', '.join(stocks[:8])}{'...' if len(stocks) > 8 else ''}")
    if crypto:
        print(f"  crypto: {', '.join(crypto)}")
    print(f"Max retries per session: {MAX_RETRIES_PER_SESSION}")

    start_realtime_feed()
    seconds = int(os.getenv("REALTIME_WS_TEST_SEC", "20"))
    print(f"Listening {seconds}s (watch logs for connect / reconnect messages)...")
    for i in range(seconds):
        time.sleep(1)
        if i > 0 and i % 5 == 0:
            print(f"  [{i}s] {format_status_line()}")

    print()
    print("--- Final status ---")
    print(format_status_line())
    st = get_status()
    print(
        f"connected={st.get('connected')} degraded={st.get('degraded')} "
        f"subscribed={st.get('subscribed_stock')} stock "
        f"streak={st.get('disconnect_streak')}"
    )
    for sym in stocks[:5]:
        px = get_latest_price(sym)
        if px:
            print(f"  {sym}: {px:.4f}")
    stop_realtime_feed()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
