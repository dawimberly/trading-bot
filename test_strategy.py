import pandas as pd
from strategy_module.logic import check_signal

# Create dummy data: 50 days of prices where the last price is clearly above the MA
data = pd.Series([100] * 45 + [105, 110, 115, 120, 125]) 
signal, ma = check_signal(data, "stock", 45)

print(f"Signal: {signal}, MA: {ma}")
# Expected: Signal should be 'BUY' because 125 > average