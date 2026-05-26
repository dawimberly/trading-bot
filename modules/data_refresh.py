"""Market-hours-aware refresh scheduling for live bots."""

import config
from fetch_data import fetch_and_store
from modules.market_hours import is_equity_market_open


class RefreshScheduler:
    """Refresh crypto 24/7; refresh equities only when the US session is open."""

    def __init__(self, *, refresh_crypto=True, refresh_equity=True, equity_tickers=None):
        self.refresh_crypto = refresh_crypto
        self.refresh_equity = refresh_equity
        self.equity_tickers = equity_tickers
        self.last_crypto_refresh = None
        self.last_equity_refresh = None
        self._market_was_open = None

    def _equity_symbols(self):
        if self.equity_tickers is not None:
            return list(self.equity_tickers)
        symbols = list(config.equity_universe())
        if config.GAME_PLAN_ENABLED:
            for sym in config.live_metal_universe():
                if sym not in symbols:
                    symbols.append(sym)
        return symbols

    def _due(self, last_refresh, now_ts):
        if last_refresh is None:
            return True
        return (now_ts - last_refresh).total_seconds() >= config.REFRESH_INTERVAL

    def sync(self, trading_client, now_ts):
        """Run due refreshes. Returns whether the equity session is open."""
        market_open = is_equity_market_open(trading_client)
        equity_symbols = self._equity_symbols()

        if (
            self.refresh_equity
            and equity_symbols
            and self._market_was_open is False
            and market_open
        ):
            print(f"--- Market open: refreshing {len(equity_symbols)} equity tickers ---")
            fetch_and_store(equity_symbols)
            self.last_equity_refresh = now_ts

        if self.refresh_crypto and self._due(self.last_crypto_refresh, now_ts):
            crypto_symbols = config.crypto_universe()
            if crypto_symbols:
                print(f"--- Refreshing {len(crypto_symbols)} crypto tickers ---")
                fetch_and_store(crypto_symbols)
                self.last_crypto_refresh = now_ts

        if (
            self.refresh_equity
            and market_open
            and equity_symbols
            and self._due(self.last_equity_refresh, now_ts)
        ):
            print(f"--- Refreshing {len(equity_symbols)} equity tickers ---")
            fetch_and_store(equity_symbols)
            self.last_equity_refresh = now_ts

        self._market_was_open = market_open
        return market_open
