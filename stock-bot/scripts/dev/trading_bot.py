"""Legacy strategy loop using advisor ranker and shared data loader.

Run: python scripts/dev/trading_bot.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import time

import config
import watchlist
from modules.advisor_ranker import get_ranked_targets
from modules.data_loader import load_close_matrix
from modules.risk_management import RiskManager

risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)


def run_trading_loop():
    print("Bot initialized. Starting strategy loop...")
    while True:
        try:
            assets = watchlist.get_full_watchlist()
            data = load_close_matrix()
            if data.empty:
                print("No data found. Retrying in 60s...")
                time.sleep(60)
                continue
            rankings = get_ranked_targets(assets, data)
            if rankings:
                top_pair = rankings[0]
                print(
                    f"Top target: {top_pair[0]} vs {top_pair[1]} | Score: {top_pair[2]:.4f}"
                )
                if risk_manager.check_drawdown(100000):
                    print("Risk check passed. Proceeding with execution...")
            else:
                print("No viable pairs found in this cycle.")
            time.sleep(60)
        except Exception as e:
            print(f"Critical error in loop: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run_trading_loop()
