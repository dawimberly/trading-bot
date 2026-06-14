"""Print Alpaca account status via alpaca-py.

Run: python scripts/account/check_account.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_diagnostics import alpaca_env_status, fetch_alpaca_account, format_missing_env_message
from modules.console_output import safe_print

st = alpaca_env_status()
book = "paper" if st["paper_mode"] else "live"
safe_print(f"--- Alpaca {book} account ({st['base_url']}) ---")
safe_print(f".env: {'found' if st['env_file_exists'] else 'NOT FOUND'} at {Path('.env').resolve()}")
safe_print(
    f"Credentials: key={'yes' if st['has_api_key'] else 'NO'} | "
    f"secret={'yes' if st['has_api_secret'] else 'NO'}"
)

if not st["credentials_ready"]:
    safe_print(format_missing_env_message())
    sys.exit(1)

account, err = fetch_alpaca_account()
if err or account is None:
    safe_print(f"ERROR: {err}")
    sys.exit(1)

safe_print(f"Status: {account.status}")
safe_print(f"Cash Available: ${account.cash}")
safe_print(f"Buying Power: ${account.buying_power}")
safe_print(f"Equity: ${account.equity}")
