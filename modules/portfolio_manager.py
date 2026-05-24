import json
import os

class PortfolioManager:
    def __init__(self, ledger_file="trading_history.jsonl"):
        self.ledger_file = ledger_file

    def get_open_positions(self):
        """Reads ledger to return a list of currently open 'pair' strings."""
        if not os.path.exists(self.ledger_file): return []
        
        # Track status of each pair
        state = {}
        with open(self.ledger_file, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    # Support both log-style entries and ledger-style entries
                    pair = entry.get("pair") or entry.get("symbol")
                    if pair:
                        # If a new entry explicitly says 'closed', mark it
                        status = entry.get("status", "open")
                        state[pair] = status
                except json.JSONDecodeError:
                    continue
        
        # Return only pairs that are currently 'open'
        return [pair for pair, status in state.items() if status == "open"]

    def add_position(self, pair, size, price):
        """Logs a new open position."""
        trade = {"pair": pair, "size": size, "price": price, "status": "open"}
        with open(self.ledger_file, "a") as f:
            f.write(json.dumps(trade) + "\n")

    def close_position(self, pair):
        """Logs a 'closed' status for a pair."""
        trade = {"pair": pair, "status": "closed"}
        with open(self.ledger_file, "a") as f:
            f.write(json.dumps(trade) + "\n")