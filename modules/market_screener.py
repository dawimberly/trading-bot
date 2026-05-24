import json
import alpaca_trade_api as tradeapi
import config # Importing from your config file

api = tradeapi.REST(config.API_KEY, config.SECRET_KEY, config.BASE_URL, api_version='v2')

def run_screener():
    targets = {"VTI": 0.33, "VXUS": 0.66}
    # Goal: Deploy between $100 and $250
    deployment_size = 150.00 
    
    plan = {}
    for symbol, target_weight in targets.items():
        amount = deployment_size * target_weight
        plan[symbol] = {
            "action": "buy",
            "amount_needed": amount
        }
    
    with open('plan.json', 'w') as f:
        json.dump(plan, f, indent=4)
    print(f"Intelligence Layer: Plan generated for ${deployment_size:.2f}")

if __name__ == "__main__":
    run_screener()