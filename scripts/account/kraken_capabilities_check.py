"""Print what Kraken API key can trade (crypto / xStocks / equity).

  python scripts/account/kraken_capabilities_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.kraken_capabilities import probe_kraken_capabilities

if __name__ == "__main__":
    cap = probe_kraken_capabilities(force=True)
    print("Kraken API capabilities:")
    print(f"  crypto:      {cap.get('crypto_ok')}  {cap.get('crypto_error') or ''}")
    print(f"  xStocks:     {cap.get('xstock_ok')}  {cap.get('xstock_error') or ''}")
    print(f"  equity spot: {cap.get('equity_spot_ok')}  {cap.get('equity_error') or ''}")
    if not cap.get("xstock_ok"):
        print("\nTo automate stocks: Kraken API key settings - enable tokenized/xStocks trading.")
        print("Your .EQ holdings may need a one-time move to xStocks in the app.")
