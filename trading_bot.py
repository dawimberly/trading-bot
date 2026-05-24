import time
import watchlist
import fetch_data
import config
from modules.advisor_ranker import get_ranked_targets
from modules.risk_management import RiskManager

# Initialize
risk_manager = RiskManager(max_drawdown_pct=0.10)

def run_trading_loop():
    print("Bot initialized. Starting strategy loop...")
    
    while True:
        try:
            # 1. Get assets and load data
            assets = watchlist.get_full_watchlist()
            data = fetch_data.get_data_for_bot()
            
            if data.empty:
                print("No data found. Retrying in 60s...")
                time.sleep(60)
                continue

            # 2. Get ranked targets
            rankings = get_ranked_targets(assets, data)
            
            # 3. Handle results
            if rankings:
                top_pair = rankings[0]
                print(f"Top target: {top_pair[0]} vs {top_pair[1]} | Score: {top_pair[2]:.4f}")
                
                # Risk Check
                current_equity = 100000 
                if risk_manager.check_drawdown(current_equity):
                    print("Risk check passed. Proceeding with execution...")
            else:
                # This 'else' block must be indented to match the 'if' above it
                print("No viable pairs found in this cycle.")
            
            time.sleep(60) 
            
        except Exception as e:
            print(f"Critical error in loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_trading_loop()