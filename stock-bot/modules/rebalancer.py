"""Strategic rebalancer — Operating Layer sleeve bands and hybrid rebalance triggers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import config


@dataclass
class SleeveSnapshot:
    equity: float
    core_value: float
    tactical_value: float
    cash_value: float
    core_pct: float
    tactical_pct: float
    cash_pct: float

    @property
    def core_symbol(self) -> str:
        return config.VTI_CORE_SYMBOL


@dataclass
class RebalanceOrder:
    sleeve: str
    action: str
    symbol: str
    notional: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class StrategicRebalancer:
    """Hybrid rebalance: drift beyond band OR first trading day of month."""

    def __init__(
        self,
        *,
        core_target: float | None = None,
        band_width: float | None = None,
    ) -> None:
        self.core_target = float(
            core_target if core_target is not None else config.REBALANCE_CORE_TARGET
        )
        self.band_width = float(
            band_width if band_width is not None else config.REBALANCE_BAND_WIDTH
        )
        self.core_min = float(config.REBALANCE_CORE_MIN)
        self.core_max = float(config.REBALANCE_CORE_MAX)
        self.tactical_min = float(config.REBALANCE_TACTICAL_MIN)
        self.tactical_max = float(config.REBALANCE_TACTICAL_MAX)
        self.cash_min = float(config.REBALANCE_CASH_MIN)
        self.cash_max = float(config.REBALANCE_CASH_MAX)
        self._last_rebalance_date: date | None = None

    @staticmethod
    def _pct(value: float, equity: float) -> float:
        if equity <= 0:
            return 0.0
        return float(value) / float(equity)

    @classmethod
    def snapshot_from_values(
        cls,
        *,
        equity: float,
        core_value: float,
        tactical_value: float,
        cash_value: float,
    ) -> SleeveSnapshot:
        eq = max(1e-9, float(equity))
        return SleeveSnapshot(
            equity=eq,
            core_value=float(core_value),
            tactical_value=float(tactical_value),
            cash_value=float(cash_value),
            core_pct=cls._pct(core_value, eq),
            tactical_pct=cls._pct(tactical_value, eq),
            cash_pct=cls._pct(cash_value, eq),
        )

    def snapshot_from_executor(self, executor) -> SleeveSnapshot:
        account = executor._get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        from modules.vti_core import vti_core_value

        core = float(vti_core_value(executor))
        tactical = float(
            executor.spy_sleeve_value()
            + executor.nyse_sleeve_value()
            + executor.crypto_sleeve_value()
        )
        return self.snapshot_from_values(
            equity=equity,
            core_value=core,
            tactical_value=tactical,
            cash_value=cash,
        )

    def snapshot_from_portfolio(self, portfolio, prices) -> SleeveSnapshot:
        eq = float(portfolio.equity(prices))
        cash = float(portfolio.cash)
        core_sym = config.VTI_CORE_SYMBOL
        core = float(portfolio.positions.get(core_sym, 0.0)) * float(
            prices.get(core_sym, 0.0) or 0.0
        )
        tactical = 0.0
        for symbol, qty in portfolio.positions.items():
            if symbol == core_sym:
                continue
            price = prices.get(symbol)
            if price is not None and float(price) > 0:
                tactical += float(qty) * float(price)
        return self.snapshot_from_values(
            equity=eq,
            core_value=core,
            tactical_value=tactical,
            cash_value=cash,
        )

    def snapshot_from_backtest(self, portfolio, prices, executor=None) -> SleeveSnapshot:
        if executor is not None:
            eq = float(portfolio.equity(prices))
            cash = float(portfolio.cash)
            core_sym = config.VTI_CORE_SYMBOL
            core = float(portfolio.positions.get(core_sym, 0.0)) * float(
                prices.get(core_sym, 0.0) or 0.0
            )
            tactical = float(
                executor.spy_sleeve_value()
                + executor.nyse_sleeve_value()
                + executor.crypto_sleeve_value()
            )
            return self.snapshot_from_values(
                equity=eq,
                core_value=core,
                tactical_value=tactical,
                cash_value=cash,
            )
        return self.snapshot_from_portfolio(portfolio, prices)

    def check_drift(self, snapshot: SleeveSnapshot) -> dict[str, Any]:
        core_drift = abs(snapshot.core_pct - self.core_target)
        tactical_target = max(
            self.tactical_min,
            min(self.tactical_max, 1.0 - self.core_target - snapshot.cash_pct),
        )
        cash_target = max(
            self.cash_min,
            min(self.cash_max, 1.0 - self.core_target - tactical_target),
        )
        return {
            "core_drift": round(core_drift, 4),
            "core_target": round(self.core_target, 4),
            "core_pct": round(snapshot.core_pct, 4),
            "tactical_drift": round(abs(snapshot.tactical_pct - tactical_target), 4),
            "tactical_target": round(tactical_target, 4),
            "tactical_pct": round(snapshot.tactical_pct, 4),
            "cash_drift": round(abs(snapshot.cash_pct - cash_target), 4),
            "cash_target": round(cash_target, 4),
            "cash_pct": round(snapshot.cash_pct, 4),
            "max_drift": round(
                max(
                    core_drift,
                    abs(snapshot.tactical_pct - tactical_target),
                    abs(snapshot.cash_pct - cash_target),
                ),
                4,
            ),
            "core_out_of_band": not (self.core_min <= snapshot.core_pct <= self.core_max),
            "tactical_out_of_band": not (
                self.tactical_min <= snapshot.tactical_pct <= self.tactical_max
            ),
            "cash_out_of_band": not (
                self.cash_min <= snapshot.cash_pct <= self.cash_max
            ),
        }

    def _is_month_start(
        self,
        bar_date: date | datetime,
        prev_bar_date: date | datetime | None = None,
    ) -> bool:
        if isinstance(bar_date, datetime):
            bar_date = bar_date.date()
        if prev_bar_date is not None:
            if isinstance(prev_bar_date, datetime):
                prev_bar_date = prev_bar_date.date()
            return (
                bar_date.month != prev_bar_date.month
                or bar_date.year != prev_bar_date.year
            )
        if self._last_rebalance_date is None:
            return False
        return (
            bar_date.month != self._last_rebalance_date.month
            or bar_date.year != self._last_rebalance_date.year
        )

    def needs_rebalance(
        self,
        snapshot: SleeveSnapshot,
        bar_date: date | datetime,
        drift: dict[str, Any] | None = None,
        *,
        prev_bar_date: date | datetime | None = None,
    ) -> bool:
        if not config.REBALANCE_ENABLED:
            return False
        drift = drift or self.check_drift(snapshot)
        if drift["max_drift"] >= self.band_width:
            return True
        if self._is_month_start(bar_date, prev_bar_date):
            return True
        return bool(
            drift.get("core_out_of_band")
            or drift.get("tactical_out_of_band")
            or drift.get("cash_out_of_band")
        )

    def clamp_core_target(self, core_target: float) -> float:
        return round(
            max(self.core_min, min(self.core_max, float(core_target))),
            4,
        )

    def generate_rebalance_orders(
        self,
        snapshot: SleeveSnapshot,
        core_target: float,
        *,
        min_notional: float | None = None,
    ) -> list[RebalanceOrder]:
        core_target = self.clamp_core_target(core_target)
        min_n = float(min_notional if min_notional is not None else config.MIN_NOTIONAL)
        orders: list[RebalanceOrder] = []
        target_core_value = round(snapshot.equity * core_target, 2)
        delta = round(target_core_value - snapshot.core_value, 2)
        if abs(delta) >= min_n:
            orders.append(
                RebalanceOrder(
                    sleeve="core",
                    action="buy" if delta > 0 else "sell",
                    symbol=snapshot.core_symbol,
                    notional=abs(delta),
                    reason=f"core drift to {core_target:.0%}",
                    metadata={
                        "target_pct": core_target,
                        "current_pct": snapshot.core_pct,
                    },
                )
            )
        return orders

    def mark_rebalanced(self, bar_date: date | datetime) -> None:
        if isinstance(bar_date, datetime):
            bar_date = bar_date.date()
        self._last_rebalance_date = bar_date
