"""Market-hours-aware refresh scheduling for live bots."""

from __future__ import annotations

import config

from modules.data_loader import clear_close_matrix_cache
from modules.market_hours import is_equity_market_open
from modules.safe_io import safe_print


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
        try:
            from modules.dynamic_universe import live_equity_refresh_symbols

            symbols = list(live_equity_refresh_symbols())
        except Exception:
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

    @staticmethod
    def _refresh_symbols(symbols: list[str]) -> None:
        """Fetch yfinance data and invalidate in-memory price matrices."""
        if not symbols:
            return
        from fetch_data import fetch_and_store

        fetch_and_store(symbols)
        clear_close_matrix_cache()

    def sync(self, trading_client, now_ts, *, equity_prep=False):
        """Run due refreshes. Returns whether the equity session is open."""
        market_open = is_equity_market_open(trading_client)
        equity_symbols = self._equity_symbols()
        refresh_equities = market_open or equity_prep

        if (
            self.refresh_equity
            and equity_symbols
            and self._market_was_open is False
            and refresh_equities
        ):
            label = "Market open" if market_open else "Open prep"
            safe_print(f"--- {label}: refreshing {len(equity_symbols)} equity tickers ---")
            self._refresh_symbols(equity_symbols)
            self.last_equity_refresh = now_ts

        if self.refresh_crypto and config.crypto_sleeve_enabled() and self._due(self.last_crypto_refresh, now_ts):
            crypto_symbols = config.crypto_universe()
            if crypto_symbols:
                safe_print(f"--- Refreshing {len(crypto_symbols)} crypto tickers ---")
                self._refresh_symbols(crypto_symbols)
                self.last_crypto_refresh = now_ts

        if (
            self.refresh_equity
            and refresh_equities
            and equity_symbols
            and self._due(self.last_equity_refresh, now_ts)
        ):
            safe_print(f"--- Refreshing {len(equity_symbols)} equity tickers ---")
            self._refresh_symbols(equity_symbols)
            self.last_equity_refresh = now_ts

        self._market_was_open = market_open
        return market_open
