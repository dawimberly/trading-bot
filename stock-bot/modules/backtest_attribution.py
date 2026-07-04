"""Per-sleeve trade attribution for backtests (SPY / MA50 / stat arb / crypto)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import config

STRATEGIES = ("spy", "ma50_momentum", "stat_arb", "crypto", "opportunistic_short")
SLEEVE_REPORT_KEYS = ("spy", "ma50_momentum", "stat_arb", "crypto", "opportunistic_short")


def infer_strategy(order: dict | None, **kwargs) -> str | None:
    """Classify a fill into spy, ma50_momentum, stat_arb, or crypto."""
    if kwargs.get("strategy"):
        strat = str(kwargs["strategy"])
        if strat == "stat_arb":
            sym = config.normalize_symbol((order or {}).get("symbol") or "")
            if config.is_crypto(sym):
                return "crypto"
        if strat in STRATEGIES:
            return strat
    if order is None:
        return None
    if order.get("naked_short") or order.get("naked_cover"):
        return "opportunistic_short"
    sym = config.normalize_symbol(order.get("symbol") or "")
    if config.is_crypto(sym):
        return "crypto"
    if order.get("pair_short") or order.get("pair_cover"):
        return "stat_arb"
    sleeve = (kwargs.get("sleeve") or order.get("sleeve") or "").upper()
    reason = str(kwargs.get("reason") or order.get("reason") or "")
    if sleeve in ("CRYPTO", "CRYPTO_V2"):
        return "crypto"
    if sleeve == "SPY" or sym == config.SPY_BOT_SYMBOL:
        return "spy"
    if "/MA50" in reason.upper() or reason.endswith("/MA50"):
        return "ma50_momentum"
    if "/" in reason and "MA" not in reason.split("/")[-1]:
        return "stat_arb"
    if kwargs.get("pair_key") or order.get("pair_key"):
        return "stat_arb"
    return None


def normalize_crypto_reject(reason: str) -> str:
    """Map vol-gate / sizing reasons to stable funnel tokens."""
    r = (reason or "unknown").strip().lower()
    if r in (
        "vol_low",
        "vol_high",
        "vol_gate",
        "regime_paused",
        "bearish_regime",
        "crypto_sleeve_disabled",
        "crypto_disabled",
    ):
        return "vol_gate" if r in ("vol_low", "vol_high") else r.replace("crypto_sleeve_disabled", "crypto_disabled")
    if r in ("no_room", "leg_notional", "min_notional", "atomic_fail", "in_book", "cooldown", "max_pairs"):
        return r
    if "vol" in r:
        return "vol_gate"
    if "room" in r or r == "no_room":
        return "no_room"
    if "notional" in r or "min_n" in r:
        return "min_notional"
    return r.split(":")[0][:32] or "unknown"


class BacktestAttribution:
    """Accumulates signals, fills, round-trip PnL, and sleeve overlap."""

    def __init__(self) -> None:
        self.signals: dict[str, int] = {s: 0 for s in STRATEGIES}
        self.intents: dict[str, int] = {s: 0 for s in STRATEGIES}
        self.entry_fills: dict[str, int] = {s: 0 for s in STRATEGIES}
        self.exit_fills: dict[str, int] = {s: 0 for s in STRATEGIES}
        self.pair_entries: dict[str, int] = {s: 0 for s in STRATEGIES}
        self.pair_exits: dict[str, int] = {s: 0 for s in STRATEGIES}
        self.round_trips: list[dict[str, Any]] = []
        self.realized_pnl: dict[str, float] = {s: 0.0 for s in STRATEGIES}
        self._lots: dict[str, list[dict]] = defaultdict(list)
        self._short_lots: dict[str, list[dict]] = defaultdict(list)
        self.overlap_bars: int = 0
        self.symbol_overlap_bars: int = 0
        self.stat_arb_rejects: dict[str, int] = defaultdict(int)
        self.crypto_vol_gate_pass: int = 0
        self.crypto_scan_signals: int = 0
        self.crypto_intents: int = 0
        self.crypto_entries: int = 0
        self.crypto_rejects: dict[str, int] = defaultdict(int)
        self._open_pairs: set[str] = set()
        self._ma50_symbols: set[str] = set()
        self._stat_arb_symbols: set[str] = set()
        self._stat_arb_entry_z: list[float] = []
        self._stat_arb_exposure_samples: list[tuple[float, float]] = []
        self._crypto_symbols: set[str] = set()
        self._crypto_pair_trips: list[dict[str, Any]] = []
        self.short_trigger_scans: int = 0
        self.short_trigger_fires: int = 0
        self.short_trigger_rejects: dict[str, int] = defaultdict(int)
        self.short_entry_triggers: dict[str, int] = defaultdict(int)

    def record_short_trigger(self, trigger: dict, *, opened: bool = False) -> None:
        if opened:
            reason = str(trigger.get("trigger_reason") or "allowed")
            self.short_entry_triggers[reason] += 1
            return
        self.short_trigger_scans += 1
        if trigger.get("allowed"):
            self.short_trigger_fires += 1
        else:
            reject = str(trigger.get("reject") or "unknown")
            self.short_trigger_rejects[reject] += 1

    def record_short_entry(self, trigger_reason: str) -> None:
        if trigger_reason:
            self.short_entry_triggers[trigger_reason] += 1

    def record_signals(self, strategy: str, count: int = 1) -> None:
        if strategy in self.signals and count > 0:
            self.signals[strategy] += int(count)

    def record_intents(self, strategy: str, count: int = 1) -> None:
        if strategy in self.intents and count > 0:
            self.intents[strategy] += int(count)

    def record_stat_arb_reject(self, reason: str) -> None:
        if reason:
            self.stat_arb_rejects[str(reason)] += 1

    def record_crypto_vol_gate_pass(self, count: int = 1) -> None:
        if count > 0:
            self.crypto_vol_gate_pass += int(count)

    def record_crypto_scan_signals(self, count: int = 1) -> None:
        if count > 0:
            self.crypto_scan_signals += int(count)

    def record_crypto_intents(self, count: int = 1) -> None:
        if count > 0:
            self.crypto_intents += int(count)

    def record_crypto_reject(self, reason: str) -> None:
        if reason:
            key = normalize_crypto_reject(reason)
            self.crypto_rejects[key] += 1

    def on_crypto_entry(
        self,
        pair_key: str = "",
        *,
        symbol: str = "",
        regime: str = "",
        entry_bar: int | None = None,
    ) -> None:
        self.crypto_entries += 1
        if symbol:
            self._crypto_symbols.add(config.normalize_symbol(symbol))
        if pair_key and "/" in pair_key:
            for part in pair_key.split("/"):
                self._crypto_symbols.add(config.normalize_symbol(part))

    def on_crypto_pair_exit(self, pair_key: str) -> None:
        self.pair_exits["crypto"] = self.pair_exits.get("crypto", 0) + 1

    def record_crypto_pair_realized(
        self,
        pnl: float,
        pair_key: str,
        *,
        leg_notional: float,
        regime: str = "",
        entry_bar: int | None = None,
        exit_bar: int | None = None,
    ) -> None:
        pnl = float(pnl)
        self.realized_pnl["crypto"] += pnl
        hold_bars = None
        if entry_bar is not None and exit_bar is not None:
            hold_bars = max(0, int(exit_bar) - int(entry_bar))
        base = max(float(leg_notional) * 2.0, 1.0)
        trip = {
            "strategy": "crypto",
            "symbol": pair_key,
            "side": "pair_exit",
            "pnl_usd": round(pnl, 2),
            "return_pct": round(100.0 * pnl / base, 3),
            "pair_key": pair_key,
            "regime": regime or "unknown",
            "hold_bars": hold_bars,
        }
        self.round_trips.append(trip)
        self._crypto_pair_trips.append(trip)

    def on_fill(
        self,
        order: dict,
        prices: dict,
        portfolio,
        **meta,
    ) -> None:
        strategy = infer_strategy(order, **meta)
        if not strategy:
            return
        side = str(order.get("side") or "").lower()
        sym = config.normalize_symbol(order.get("symbol") or "")
        qty = float(order.get("qty") or 0)
        notional = float(order.get("notional") or 0)
        price = prices.get(order.get("symbol")) or prices.get(sym)
        if price is None or not float(price):
            price = notional / qty if qty else 0.0
        price = float(price)

        if side == "buy":
            if order.get("naked_cover"):
                self.exit_fills[strategy] += 1
                pnl = self._cover_short(sym, qty, price, strategy)
                if pnl is not None:
                    self.realized_pnl[strategy] += pnl
                    entry_bar = meta.get("entry_bar") or order.get("entry_bar")
                    exit_bar = meta.get("exit_bar") or order.get("exit_bar")
                    hold_bars = None
                    if entry_bar is not None and exit_bar is not None:
                        hold_bars = max(0, int(exit_bar) - int(entry_bar))
                    self.round_trips.append(
                        {
                            "strategy": strategy,
                            "symbol": sym,
                            "side": "cover",
                            "pnl_usd": round(pnl, 2),
                            "return_pct": round(100.0 * pnl / max(notional, 1.0), 3),
                            "pair_key": meta.get("pair_key") or order.get("pair_key"),
                            "exit_reason": meta.get("exit_reason") or order.get("exit_reason"),
                            "hold_bars": hold_bars,
                            "entry_bar": entry_bar,
                            "exit_bar": exit_bar,
                        }
                    )
            elif order.get("pair_cover"):
                self.exit_fills[strategy] += 1
                cover_strat = "crypto" if config.is_crypto(sym) else "stat_arb"
                pnl = self._cover_short(sym, qty, price, cover_strat)
                if pnl is not None:
                    self.realized_pnl[cover_strat] += pnl
                    self.round_trips.append(
                        {
                            "strategy": cover_strat,
                            "symbol": sym,
                            "side": "cover",
                            "pnl_usd": round(pnl, 2),
                            "return_pct": round(100.0 * pnl / max(notional, 1.0), 3),
                            "pair_key": meta.get("pair_key") or order.get("pair_key"),
                        }
                    )
            else:
                self.entry_fills[strategy] += 1
                if strategy == "ma50_momentum":
                    self._ma50_symbols.add(sym)
                elif strategy == "stat_arb":
                    self._stat_arb_symbols.add(sym)
                elif strategy == "crypto":
                    self._crypto_symbols.add(sym)
                self._lots[sym].append(
                    {
                        "qty": qty,
                        "price": price,
                        "strategy": strategy,
                        "pair_key": meta.get("pair_key") or order.get("pair_key"),
                    }
                )
        elif side == "sell" and order.get("naked_short"):
            self.entry_fills[strategy] += 1
            self._short_lots[sym].append(
                {
                    "qty": qty,
                    "price": price,
                    "strategy": strategy,
                    "pair_key": meta.get("pair_key") or order.get("pair_key"),
                    "entry_bar": meta.get("entry_bar") or order.get("entry_bar"),
                }
            )
        elif side == "sell" and order.get("pair_short"):
            self.entry_fills[strategy] += 1
            if strategy == "crypto":
                self._crypto_symbols.add(sym)
            else:
                self._stat_arb_symbols.add(sym)
            self._short_lots[sym].append(
                {
                    "qty": qty,
                    "price": price,
                    "strategy": strategy,
                    "pair_key": meta.get("pair_key") or order.get("pair_key"),
                }
            )
        elif side == "sell" or order.get("pair_cover"):
            self.exit_fills[strategy] += 1
            sell_qty = qty
            if order.get("pair_cover"):
                strategy = "stat_arb"
            pnl = self._consume_lots(sym, sell_qty, price, strategy)
            if pnl is not None:
                self.realized_pnl[strategy] += pnl
                self.round_trips.append(
                    {
                        "strategy": strategy,
                        "symbol": sym,
                        "side": side,
                        "pnl_usd": round(pnl, 2),
                        "return_pct": round(100.0 * pnl / max(notional, 1.0), 3),
                        "pair_key": meta.get("pair_key") or order.get("pair_key"),
                    }
                )

    def on_stat_arb_pair_entry(
        self, pair_key: str, long_sym: str, short_sym: str, *, entry_z: float = 0.0
    ) -> None:
        self._open_pairs.add(pair_key)
        self._stat_arb_symbols.add(config.normalize_symbol(long_sym))
        self._stat_arb_symbols.add(config.normalize_symbol(short_sym))
        if entry_z:
            self._stat_arb_entry_z.append(abs(float(entry_z)))
        self.pair_entries["stat_arb"] += 1

    def on_stat_arb_pair_exit(
        self, pair_key: str, *, long_sym: str = "", short_sym: str = ""
    ) -> None:
        self._open_pairs.discard(pair_key)
        self.pair_exits["stat_arb"] += 1
        for sym in (long_sym, short_sym):
            if sym:
                norm = config.normalize_symbol(sym)
                self._lots.pop(norm, None)
                self._short_lots.pop(norm, None)

    def _position_qty(self, portfolio, symbol: str) -> float:
        norm = config.normalize_symbol(symbol)
        for key, qty in portfolio.positions.items():
            if config.normalize_symbol(key) == norm:
                return float(qty)
        return 0.0

    def _price_for(self, prices: dict, symbol: str) -> float:
        if hasattr(prices, "to_dict"):
            prices = prices.to_dict()
        if symbol in prices:
            return float(prices[symbol])
        norm = config.normalize_symbol(symbol)
        for key, val in prices.items():
            if config.normalize_symbol(str(key)) == norm:
                return float(val)
        return 0.0

    def pair_mark_pnl(self, executor, position: dict, prices: dict) -> float | None:
        long_sym = position.get("long_symbol")
        short_sym = position.get("short_symbol")
        if not long_sym or not short_sym:
            return None
        long_qty = self._position_qty(executor.portfolio, long_sym)
        short_qty = self._position_qty(executor.portfolio, short_sym)
        if long_qty <= 0 or short_qty >= 0:
            return None
        long_entry = float(
            position.get("long_filled_notional", position.get("leg_notional", 0))
        )
        short_entry = float(
            position.get("short_filled_notional", position.get("leg_notional", 0))
        )
        long_px = self._price_for(prices, long_sym)
        short_px = self._price_for(prices, short_sym)
        if long_px <= 0 or short_px <= 0:
            return None
        long_pnl = long_qty * long_px - long_entry
        short_pnl = short_entry - abs(short_qty) * short_px
        return long_pnl + short_pnl

    def record_stat_arb_realized(
        self,
        pnl: float,
        pair_key: str,
        *,
        leg_notional: float,
        exit_reason: str = "",
        entry_bar: int | None = None,
        exit_bar: int | None = None,
    ) -> None:
        pnl = float(pnl)
        self.realized_pnl["stat_arb"] += pnl
        base = max(float(leg_notional) * 2.0, 1.0)
        hold_bars = None
        if entry_bar is not None and exit_bar is not None:
            hold_bars = max(0, int(exit_bar) - int(entry_bar))
        self.round_trips.append(
            {
                "strategy": "stat_arb",
                "symbol": pair_key,
                "side": "pair_exit",
                "pnl_usd": round(pnl, 2),
                "return_pct": round(100.0 * pnl / base, 3),
                "pair_key": pair_key,
                "exit_reason": exit_reason or "mean_revert",
                "hold_bars": hold_bars,
                "exit_bar": exit_bar,
            }
        )

    def _consume_lots(
        self, symbol: str, sell_qty: float, sell_price: float, strategy: str
    ) -> float | None:
        lots = self._lots.get(symbol) or []
        if not lots:
            return None
        remaining = sell_qty
        cost = 0.0
        matched = 0.0
        while remaining > 1e-9 and lots:
            lot = lots[0]
            if lot.get("strategy") != strategy:
                break
            take = min(remaining, float(lot["qty"]))
            cost += take * float(lot["price"])
            matched += take
            lot["qty"] = float(lot["qty"]) - take
            if lot["qty"] < 1e-9:
                lots.pop(0)
            remaining -= take
        if matched <= 0:
            return None
        proceeds = matched * sell_price
        return proceeds - cost

    def _cover_short(
        self, symbol: str, cover_qty: float, cover_price: float, strategy: str
    ) -> float | None:
        lots = self._short_lots.get(symbol) or []
        if not lots:
            return None
        remaining = cover_qty
        entry_proceeds = 0.0
        matched = 0.0
        while remaining > 1e-9 and lots:
            lot = lots[0]
            if lot.get("strategy") != strategy:
                break
            take = min(remaining, float(lot["qty"]))
            entry_proceeds += take * float(lot["price"])
            matched += take
            lot["qty"] = float(lot["qty"]) - take
            if lot["qty"] < 1e-9:
                lots.pop(0)
            remaining -= take
        if matched <= 0:
            return None
        cover_cost = matched * cover_price
        return entry_proceeds - cover_cost

    def _signed_position_value(self, executor, prices) -> dict[str, float]:
        book = getattr(executor, "_stat_arb_open", None) or {}
        pair_syms: set[str] = set()
        for pos in book.values():
            pair_syms.add(config.normalize_symbol(pos.get("long_symbol", "")))
            pair_syms.add(config.normalize_symbol(pos.get("short_symbol", "")))
        pair_syms.discard("")

        out = {s: 0.0 for s in STRATEGIES}
        for symbol, qty in executor.portfolio.positions.items():
            sym = config.normalize_symbol(symbol)
            p = prices.get(symbol)
            if p is None or not float(p):
                continue
            mv = float(qty) * float(p)
            if sym == config.SPY_BOT_SYMBOL:
                out["spy"] += mv
            elif sym in pair_syms:
                out["stat_arb"] += mv
            elif self._is_nyse_single(sym):
                out["ma50_momentum"] += mv
        return out

    @staticmethod
    def _is_nyse_single(sym: str) -> bool:
        if config.is_crypto(sym):
            return False
        if sym in (config.SPY_BOT_SYMBOL, "VTI"):
            return False
        if config.is_metal_symbol(sym):
            return False
        return True

    def _unrealized_pnl(self, prices: dict) -> dict[str, float]:
        out = {s: 0.0 for s in STRATEGIES}
        for sym, lots in self._lots.items():
            p = prices.get(sym)
            if p is None:
                continue
            px = float(p)
            for lot in lots:
                strat = lot.get("strategy")
                if strat not in out:
                    continue
                out[strat] += float(lot["qty"]) * (px - float(lot["price"]))
        for sym, lots in self._short_lots.items():
            p = prices.get(sym)
            if p is None:
                continue
            px = float(p)
            for lot in lots:
                strat = lot.get("strategy")
                if strat not in out:
                    continue
                out[strat] += float(lot["qty"]) * (float(lot["price"]) - px)
        return out

    def snapshot_mtm(self, executor, prices) -> None:
        """Track sleeve overlap exposure (not PnL)."""
        mtm = self._signed_position_value(executor, prices)

        ma50_active = abs(mtm["ma50_momentum"]) > 1.0
        stat_active = bool(getattr(executor, "_stat_arb_open", {})) or abs(
            mtm["stat_arb"]
        ) > 1.0
        if ma50_active and stat_active:
            self.overlap_bars += 1

        ma50_syms = {
            config.normalize_symbol(s)
            for s, q in executor.portfolio.positions.items()
            if q > 0 and self._is_nyse_single(config.normalize_symbol(s))
            and config.normalize_symbol(s) not in self._stat_arb_symbols
        }
        stat_syms = {
            config.normalize_symbol(s)
            for s, q in executor.portfolio.positions.items()
            if config.normalize_symbol(s) in self._stat_arb_symbols and abs(q) > 0
        }
        if ma50_syms & stat_syms:
            self.symbol_overlap_bars += 1

        if config.effective_stat_arb_sleeve_cap_enabled() and hasattr(
            executor, "stat_arb_sleeve_value"
        ):
            equity = float(executor.portfolio.equity(prices))
            if equity > 0:
                exp = float(executor.stat_arb_sleeve_value())
                cap_usd = equity * config.effective_stat_arb_cap()
                self._stat_arb_exposure_samples.append((exp, cap_usd))

    def finalize(self, prices: dict | None = None) -> dict[str, Any]:
        px = prices
        if hasattr(px, "to_dict"):
            px = px.to_dict()
        unrealized = self._unrealized_pnl(px or {})

        def _trade_stats(strategy: str) -> dict:
            trips = [t for t in self.round_trips if t["strategy"] == strategy]
            pnls = [float(t["pnl_usd"]) for t in trips]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gross_win = sum(wins)
            gross_loss = abs(sum(losses))
            pf = (
                round(gross_win / gross_loss, 2)
                if gross_loss > 1e-9
                else (round(gross_win, 2) if gross_win else 0.0)
            )
            unreal = round(unrealized.get(strategy, 0.0), 2)
            realized = round(self.realized_pnl[strategy], 2)
            return {
                "round_trips": len(trips),
                "win_rate_pct": round(100.0 * len(wins) / len(pnls), 1) if pnls else 0.0,
                "avg_return_pct": round(sum(t["return_pct"] for t in trips) / len(trips), 3)
                if trips
                else 0.0,
                "profit_factor": pf,
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": unreal,
                "total_pnl_usd": round(realized + unreal, 2),
            }

        stat = _trade_stats("stat_arb")
        crypto = _trade_stats("crypto")
        stat_trips = [
            t
            for t in self.round_trips
            if t.get("strategy") == "stat_arb" and t.get("side") == "pair_exit"
        ]
        stat_holds = [t["hold_bars"] for t in stat_trips if t.get("hold_bars") is not None]
        avg_hold = round(sum(stat_holds) / len(stat_holds), 1) if stat_holds else 0.0
        avg_entry_z = (
            round(sum(self._stat_arb_entry_z) / len(self._stat_arb_entry_z), 2)
            if self._stat_arb_entry_z
            else 0.0
        )
        reject_total = sum(self.stat_arb_rejects.values())
        no_room = int(self.stat_arb_rejects.get("no_room", 0))
        reject_rates: dict[str, float] = {}
        if reject_total > 0:
            reject_rates = {
                k: round(100.0 * v / reject_total, 1)
                for k, v in self.stat_arb_rejects.items()
            }
        sleeve_util_pct = 0.0
        sleeve_peak_usd = 0.0
        sleeve_cap_usd = 0.0
        if self._stat_arb_exposure_samples:
            utils = [
                exp / cap if cap > 0 else 0.0
                for exp, cap in self._stat_arb_exposure_samples
            ]
            sleeve_util_pct = round(100.0 * sum(utils) / len(utils), 1)
            sleeve_peak_usd = round(
                max(exp for exp, _cap in self._stat_arb_exposure_samples), 2
            )
            sleeve_cap_usd = round(
                sum(cap for _exp, cap in self._stat_arb_exposure_samples)
                / len(self._stat_arb_exposure_samples),
                2,
            )
        crypto_analytics = self._crypto_pair_analytics()
        crypto_total = float(crypto.get("total_pnl_usd", 0.0))
        crypto_realized = float(crypto.get("realized_pnl_usd", 0.0))
        if abs(crypto_total) > 1e-9:
            crypto_contrib = 100.0 * crypto_realized / crypto_total
        elif abs(crypto_realized) > 1e-9:
            crypto_contrib = 100.0
        else:
            crypto_contrib = 0.0
        short_stats = _trade_stats("opportunistic_short")
        short_analytics = self._opportunistic_short_analytics()
        return {
            "signals": dict(self.signals),
            "intents": dict(self.intents),
            "entry_fills": dict(self.entry_fills),
            "exit_fills": dict(self.exit_fills),
            "sleeves": {
                "spy": _trade_stats("spy"),
                "ma50_momentum": _trade_stats("ma50_momentum"),
                "stat_arb": stat,
                "crypto": crypto,
                "opportunistic_short": short_stats,
            },
            "opportunistic_short": {
                **short_stats,
                **short_analytics,
                "signals": self.signals["opportunistic_short"],
                "intents": self.intents["opportunistic_short"],
                "entry_fills": self.entry_fills["opportunistic_short"],
                "exit_fills": self.exit_fills["opportunistic_short"],
                "trigger_scans": self.short_trigger_scans,
                "trigger_fires": self.short_trigger_fires,
                "trigger_rejects": dict(self.short_trigger_rejects),
                "entry_triggers": dict(self.short_entry_triggers),
            },
            "stat_arb": {
                **stat,
                "signals": self.signals["stat_arb"],
                "intents": self.intents["stat_arb"],
                "pair_entries": self.pair_entries["stat_arb"],
                "pair_exits": self.pair_exits["stat_arb"],
                "leg_fills": self.entry_fills["stat_arb"],
                "fill_rate_pct": round(
                    100.0 * self.pair_entries["stat_arb"] / max(self.intents["stat_arb"], 1),
                    1,
                ),
                "avg_entry_z": avg_entry_z,
                "avg_hold_bars": avg_hold,
                "reject_total": reject_total,
                "reject_rates_pct": reject_rates,
                "no_room_rate_pct": round(
                    100.0 * no_room / max(no_room + self.intents["stat_arb"], 1),
                    1,
                ),
                "dedicated_cap_enabled": config.effective_stat_arb_sleeve_cap_enabled(),
                "sleeve_cap_pct": round(config.effective_stat_arb_cap(), 4),
                "avg_sleeve_util_pct": sleeve_util_pct,
                "peak_sleeve_exposure_usd": sleeve_peak_usd,
                "avg_sleeve_cap_usd": sleeve_cap_usd,
            },
            "crypto": {
                **crypto,
                **crypto_analytics,
                "vol_gate_pass": self.crypto_vol_gate_pass,
                "scan_signals": self.crypto_scan_signals,
                "intents": self.crypto_intents,
                "entries": self.crypto_entries,
                "entry_fills": self.entry_fills.get("crypto", 0),
                "fill_rate_pct": round(
                    100.0 * self.crypto_entries / max(self.crypto_intents, 1),
                    1,
                ),
                "sleeve_pnl_contrib_pct": round(crypto_contrib, 0),
            },
            "overlap_bars": self.overlap_bars,
            "symbol_overlap_bars": self.symbol_overlap_bars,
            "stat_arb_rejects": dict(self.stat_arb_rejects),
            "crypto_rejects": dict(self.crypto_rejects),
            "round_trips": self.round_trips,
        }

    def _opportunistic_short_analytics(self) -> dict[str, Any]:
        trips = [
            t
            for t in self.round_trips
            if t.get("strategy") == "opportunistic_short" and t.get("side") == "cover"
        ]
        by_symbol: dict[str, float] = defaultdict(float)
        by_reason: dict[str, list[float]] = defaultdict(list)
        for trip in trips:
            sym = str(trip.get("symbol") or "")
            if sym:
                by_symbol[sym] += float(trip.get("pnl_usd", 0))
            reason = str(trip.get("exit_reason") or "unknown")
            by_reason[reason].append(float(trip.get("pnl_usd", 0)))
        best = sorted(by_symbol.items(), key=lambda x: -x[1])[:3]
        worst = sorted(by_symbol.items(), key=lambda x: x[1])[:3]
        holds = [t["hold_bars"] for t in trips if t.get("hold_bars") is not None]
        pnls = [float(t.get("pnl_usd", 0)) for t in trips]
        wins = [p for p in pnls if p > 0]
        exit_breakdown = {
            reason: {"count": len(vals), "pnl": round(sum(vals), 2)}
            for reason, vals in sorted(by_reason.items())
        }
        return {
            "cover_exits": len(trips),
            "avg_hold_bars": round(sum(holds) / len(holds), 1) if holds else 0.0,
            "win_rate_pct": round(100.0 * len(wins) / len(pnls), 1) if pnls else 0.0,
            "best_shorts": [(sym, round(pnl, 2)) for sym, pnl in best if pnl > 0],
            "worst_shorts": [(sym, round(pnl, 2)) for sym, pnl in worst if pnl < 0],
            "symbol_pnl": {k: round(v, 2) for k, v in sorted(by_symbol.items())},
            "exit_breakdown": exit_breakdown,
        }

    def _crypto_pair_analytics(self) -> dict[str, Any]:
        trips = list(self._crypto_pair_trips)
        holds = [t["hold_bars"] for t in trips if t.get("hold_bars") is not None]
        avg_hold = round(sum(holds) / len(holds), 1) if holds else 0.0

        by_regime: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "total": 0})
        for trip in trips:
            reg = str(trip.get("regime") or "unknown")
            by_regime[reg]["total"] += 1
            if float(trip.get("pnl_usd", 0)) > 0:
                by_regime[reg]["wins"] += 1

        pair_pnl: dict[str, float] = defaultdict(float)
        for trip in trips:
            pk = str(trip.get("pair_key") or trip.get("symbol") or "")
            if pk:
                pair_pnl[pk] += float(trip.get("pnl_usd", 0))

        win_by_regime = {
            reg: round(100.0 * stats["wins"] / stats["total"], 1)
            for reg, stats in sorted(by_regime.items())
            if stats["total"] > 0
        }
        top_losers = [
            (pair, round(pnl, 2))
            for pair, pnl in sorted(pair_pnl.items(), key=lambda x: x[1])[:5]
            if pnl < 0
        ]
        return {
            "pair_exits": len(trips),
            "avg_hold_bars": avg_hold,
            "win_rate_by_regime": win_by_regime,
            "top_losing_pairs": top_losers,
        }


def record_crypto_vol_gate(executor, gate: dict) -> None:
    """Record vol-gate pass or reject for crypto funnel attribution."""
    att = getattr(executor, "_attribution", None)
    if not att:
        return
    if gate.get("allowed"):
        att.record_crypto_vol_gate_pass()
    else:
        att.record_crypto_reject(str(gate.get("reason") or "vol_gate"))


def _crypto_tuning_hint(attribution: dict, fill_rate: float) -> str:
    if fill_rate >= 70.0:
        return ""
    rejects = attribution.get("crypto_rejects") or {}
    top_reason = (
        max(rejects.items(), key=lambda x: x[1])[0] if rejects else "in_book"
    )
    knobs = {
        "in_book": "PAPER_CRYPTO_Z_EXIT=1.0 or PAPER_CRYPTO_MAX_HOLD_BARS=8",
        "max_pairs": "PAPER_CRYPTO_MAX_PAIRS=6",
        "vol_gate": "PAPER_CRYPTO_VOL_ONLY=false",
        "bearish_regime": "PAPER_CRYPTO_REGIME_FILTER=true (default)",
        "no_room": "PAPER_VTI_CORE_PCT=0.20 with PAPER_DYNAMIC_VTI=false",
        "min_notional": "raise crypto sleeve cap or account equity",
        "atomic_fail": "check crypto notional / leg sizing",
    }
    knob = knobs.get(top_reason, "PAPER_CRYPTO_MAX_PAIRS=6")
    return f" | fill<{70:.0f}%: try {knob}"


def format_crypto_banner(attribution: dict | None) -> str | None:
    """One-line crypto sleeve summary for the top of the backtest report."""
    if not attribution or not attribution.get("crypto_enabled", True):
        return None
    cr = attribution.get("crypto") or {}
    entries = int(cr.get("entries", 0))
    fill = float(cr.get("fill_rate_pct", 0.0))
    contrib = float(cr.get("sleeve_pnl_contrib_pct", 0.0))
    line = (
        f"Crypto: {entries} entries ({fill:.0f}% fill rate) "
        f"— contributing {contrib:.0f}% of crypto sleeve PnL"
    )
    line += _crypto_tuning_hint(attribution, fill)
    return line


def format_short_trigger_log(trigger: dict) -> str:
    """Human-readable line when a protective short gate passes."""
    if not trigger.get("allowed"):
        return ""
    raw = str(trigger.get("trigger_reason") or "")
    regime = "RHYME_B" if "RHYME_B" in raw else "RHYME_E" if "RHYME_E" in raw else "SHORT"
    vix = str(trigger.get("vix_reason") or "VIX confirm")
    exh = str(trigger.get("exhaustion_reason") or "exhaustion")
    bubble = float(trigger.get("bubble_score") or 0.0)
    return f"Short triggered: {regime} + {vix} + {exh} (bubble={bubble:.2f})"


def format_short_trigger_summary(os_data: dict) -> str:
    """Compact fires/scans + top reject and fire reasons for reports."""
    scans = int(os_data.get("trigger_scans", 0) or 0)
    fires = int(os_data.get("trigger_fires", 0) or 0)
    if not scans:
        return ""
    parts = [f"Short fires: {fires}/{scans} scans"]
    rejects = os_data.get("trigger_rejects") or {}
    if rejects:
        top_rej = sorted(rejects.items(), key=lambda x: -x[1])[:3]
        parts.append("Rejects: " + ", ".join(f"{k}={v}" for k, v in top_rej))
    triggers = os_data.get("entry_triggers") or {}
    if triggers:
        top_fire = sorted(triggers.items(), key=lambda x: -x[1])[:3]
        parts.append("Reasons: " + ", ".join(f"{k}={v}" for k, v in top_fire))
    return " | ".join(parts)


def format_opportunistic_short_banner(attribution: dict | None = None) -> str | None:
    """One-line protective short summary for backtest report or startup."""
    if attribution is None:
        return config.format_opportunistic_short_banner()
    os_data = attribution.get("opportunistic_short") or {}
    sleeves = attribution.get("sleeves") or {}
    sleeve = sleeves.get("opportunistic_short") or os_data
    trips = int(sleeve.get("round_trips", 0) or 0)
    pnl = float(sleeve.get("total_pnl_usd", 0) or 0)
    if trips == 0 and pnl == 0 and not config.effective_opportunistic_short_enabled():
        return None
    lo = config.effective_protective_short_min_pct()
    hi = config.effective_protective_short_max_pct()
    line = f"Protective Shorts: ON ({lo:.0%}-{hi:.0%}, selective triggers)"
    if trips or abs(pnl) > 1e-9:
        line += f" | {trips} trips PnL ${pnl:+.2f}"
    wr = float(os_data.get("win_rate_pct", 0) or sleeve.get("win_rate_pct", 0) or 0)
    avg_hold = float(os_data.get("avg_hold_bars", 0) or 0)
    fires = int(os_data.get("trigger_fires", 0) or 0)
    scans = int(os_data.get("trigger_scans", 0) or 0)
    entries = int(os_data.get("entry_fills", 0) or 0)
    if trips:
        line += f" | win {wr:.0f}% | hold {avg_hold:.0f}b"
    if scans:
        line += f" | triggers {fires}/{scans} | entries {entries}"
        summary = format_short_trigger_summary(os_data)
        if summary:
            line += f" | {summary}"
    exit_bd = os_data.get("exit_breakdown") or {}
    if exit_bd:
        top = sorted(exit_bd.items(), key=lambda x: -x[1].get("count", 0))[:2]
        parts = [f"{k}×{v['count']}" for k, v in top if v.get("count")]
        if parts:
            line += f" | exits {', '.join(parts)}"
    best = os_data.get("best_shorts") or []
    worst = os_data.get("worst_shorts") or []
    if best:
        line += f" | best {best[0][0]} ${best[0][1]:+.2f}"
    if worst:
        line += f" | worst {worst[0][0]} ${worst[0][1]:+.2f}"
    return line


def format_stat_arb_banner(attribution: dict | None) -> str | None:
    """One-line stat arb summary for the top of the backtest report."""
    if not attribution or not attribution.get("stat_arb_enabled", True):
        return None
    sa = attribution.get("stat_arb") or {}
    pairs = int(sa.get("pair_entries", 0))
    fill = float(sa.get("fill_rate_pct", 0.0))
    sleeves = attribution.get("sleeves") or {}
    stat_pnl = float((sleeves.get("stat_arb") or {}).get("total_pnl_usd", 0.0))
    realized = float((sleeves.get("stat_arb") or {}).get("realized_pnl_usd", 0.0))
    ma50_pnl = float((sleeves.get("ma50_momentum") or {}).get("total_pnl_usd", 0.0))
    equity_pnl = stat_pnl + ma50_pnl
    if abs(equity_pnl) > 1e-9:
        contrib = 100.0 * stat_pnl / equity_pnl
    elif abs(stat_pnl) > 1e-9:
        contrib = 100.0
    else:
        contrib = 0.0
    stat_sleeve = sleeves.get("stat_arb") or {}
    line = (
        f"Stat Arb v1.3: {pairs} pairs ({fill:.0f}% fill, {contrib:.0f}% equity PnL) "
        f"PnL ${stat_pnl:+.2f} (real ${realized:+.2f}) | win {stat_sleeve.get('win_rate_pct', 0):.0f}% "
        f"| avg Z {sa.get('avg_entry_z', 0):.2f} | avg hold {sa.get('avg_hold_bars', 0):.0f}b"
    )
    if sa.get("dedicated_cap_enabled"):
        line += (
            f" | dedicated cap {sa.get('sleeve_cap_pct', 0):.0%} "
            f"util {sa.get('avg_sleeve_util_pct', 0):.0f}%"
        )
    rejects = attribution.get("stat_arb_rejects") or {}
    reject_total = int(sa.get("reject_total", 0)) or sum(rejects.values())
    if reject_total:
        rates = sa.get("reject_rates_pct") or {}
        top = sorted(rates.items(), key=lambda x: -x[1])[:3]
        parts = ", ".join(f"{k} {v:.0f}%" for k, v in top)
        line += f" | rejects ({reject_total}): {parts}"
    overlap = int(attribution.get("symbol_overlap_bars", 0))
    if overlap:
        line += f" | MA50 overlap bars={overlap}"
    return line


def print_attribution_report(attribution: dict | None) -> None:
    if not attribution:
        return
    print("--- SLEEVE ATTRIBUTION ---")
    sleeves = attribution.get("sleeves") or {}
    labels = (
        ("spy", "SPY MA200"),
        ("ma50_momentum", "NYSE MA50 momentum"),
        ("stat_arb", "Stat arb pairs (NYSE)"),
        ("crypto", "Crypto sleeve"),
        ("opportunistic_short", "Opportunistic shorts"),
    )
    print(
        f"{'Sleeve':<22} {'PnL $':>9} {'Realized':>9} {'Unreal':>9} "
        f"{'Trips':>6} {'Win%':>6} {'AvgRet%':>8} {'PF':>5}"
    )
    print("-" * 72)
    for key, label in labels:
        s = sleeves.get(key) or {}
        print(
            f"{label:<22} "
            f"{s.get('total_pnl_usd', 0):>9.2f} "
            f"{s.get('realized_pnl_usd', 0):>9.2f} "
            f"{s.get('unrealized_pnl_usd', 0):>9.2f} "
            f"{s.get('round_trips', 0):>6} "
            f"{s.get('win_rate_pct', 0):>5.1f}% "
            f"{s.get('avg_return_pct', 0):>7.3f}% "
            f"{s.get('profit_factor', 0):>5.2f}"
        )

    sa = attribution.get("stat_arb") or {}
    print("Stat arb funnel:")
    print(
        f"  scan_signals={attribution.get('signals', {}).get('stat_arb', 0)} "
        f"intents={sa.get('intents', 0)} "
        f"pairs_opened={sa.get('pair_entries', 0)} "
        f"pairs_closed={sa.get('pair_exits', 0)} "
        f"leg_fills={sa.get('leg_fills', 0)} "
        f"fill_rate={sa.get('fill_rate_pct', 0):.1f}%"
    )
    if sa.get("avg_entry_z"):
        cap_note = ""
        if sa.get("dedicated_cap_enabled"):
            cap_note = (
                f" | dedicated cap {sa.get('sleeve_cap_pct', 0):.0%} "
                f"avg_util={sa.get('avg_sleeve_util_pct', 0):.0f}% "
                f"peak=${sa.get('peak_sleeve_exposure_usd', 0):.0f}"
            )
        stat_sleeve = sleeves.get("stat_arb") or {}
        print(
            f"  avg_Z_entry={sa.get('avg_entry_z', 0):.2f} "
            f"win_rate={stat_sleeve.get('win_rate_pct', 0):.1f}% "
            f"avg_hold={sa.get('avg_hold_bars', 0):.1f} bars "
            f"fill_rate={sa.get('fill_rate_pct', 0):.1f}% "
            f"no_room_rate={sa.get('no_room_rate_pct', 0):.1f}%"
            f"{cap_note}"
        )
        stat_pnl = float(stat_sleeve.get("total_pnl_usd", 0.0))
        ma50_pnl = float((sleeves.get("ma50_momentum") or {}).get("total_pnl_usd", 0.0))
        equity_pnl = stat_pnl + ma50_pnl
        if abs(equity_pnl) > 1e-9:
            print(f"  Stat arb contribution: {100.0 * stat_pnl / equity_pnl:.1f}% of NYSE+stat PnL")
    mom_sig = attribution.get("signals", {}).get("ma50_momentum", 0)
    mom_fill = attribution.get("entry_fills", {}).get("ma50_momentum", 0)
    spy_fill = attribution.get("entry_fills", {}).get("spy", 0)
    print(
        f"  MA50 signals={mom_sig} fills={mom_fill} | "
        f"SPY entry_fills={spy_fill}"
    )
    print(
        f"  Overlap: {attribution.get('overlap_bars', 0)} bars with MA50+stat arb exposure | "
        f"{attribution.get('symbol_overlap_bars', 0)} bars same-symbol overlap"
    )
    rejects = attribution.get("stat_arb_rejects") or {}
    if rejects:
        rates = sa.get("reject_rates_pct") or {}
        parts = ", ".join(
            f"{k}={v} ({rates.get(k, 0):.0f}%)"
            for k, v in sorted(rejects.items(), key=lambda x: -x[1])
        )
        print(f"  Stat arb rejects: {parts}")

    trips = [
        t
        for t in (attribution.get("round_trips") or [])
        if t.get("strategy") == "stat_arb" and t.get("side") == "pair_exit"
    ]
    if trips:
        by_reason: dict[str, list[float]] = defaultdict(list)
        for t in trips:
            by_reason[str(t.get("exit_reason") or "unknown")].append(
                float(t.get("pnl_usd", 0))
            )
        reason_parts = []
        for reason, pnls in sorted(by_reason.items()):
            reason_parts.append(f"{reason}={len(pnls)} (${sum(pnls):+.2f})")
        print(f"  Stat arb exits by reason: {', '.join(reason_parts)}")
        holds = [t["hold_bars"] for t in trips if t.get("hold_bars") is not None]
        if holds:
            print(
                f"  Stat arb avg hold: {sum(holds) / len(holds):.1f} bars "
                f"(max {max(holds)})"
            )

    os_data = attribution.get("opportunistic_short") or {}
    if os_data.get("entry_fills") or os_data.get("round_trips") or os_data.get("cover_exits"):
        print("Opportunistic short funnel:")
        print(
            f"  signals={os_data.get('signals', 0)} "
            f"intents={os_data.get('intents', 0)} "
            f"entries={os_data.get('entry_fills', 0)} "
            f"covers={os_data.get('cover_exits', 0)} "
            f"trips={os_data.get('round_trips', 0)} "
            f"win={os_data.get('win_rate_pct', 0):.0f}% "
            f"avg_hold={os_data.get('avg_hold_bars', 0):.0f}b"
        )
        breakdown = os_data.get("exit_breakdown") or {}
        if breakdown:
            parts = [
                f"{k}={v['count']}(${v['pnl']:+.0f})"
                for k, v in sorted(breakdown.items())
            ]
            print(f"  Exit breakdown: {', '.join(parts)}")
        triggers = os_data.get("entry_triggers") or {}
        if triggers:
            parts = [f"{k}={v}" for k, v in sorted(triggers.items(), key=lambda x: -x[1])[:5]]
            print(f"  Entry triggers: {', '.join(parts)}")
        rejects = os_data.get("trigger_rejects") or {}
        if rejects and int(os_data.get("trigger_scans", 0) or 0) > 0:
            top = sorted(rejects.items(), key=lambda x: -x[1])[:4]
            print(
                f"  Trigger rejects ({os_data.get('trigger_scans', 0)} scans, "
                f"{os_data.get('trigger_fires', 0)} fires): "
                + ", ".join(f"{k}={v}" for k, v in top)
            )
        best = os_data.get("best_shorts") or []
        worst = os_data.get("worst_shorts") or []
        if best:
            print(
                "  Best shorts: "
                + ", ".join(f"{sym} ${pnl:+.2f}" for sym, pnl in best[:3])
            )
        if worst:
            print(
                "  Worst shorts: "
                + ", ".join(f"{sym} ${pnl:+.2f}" for sym, pnl in worst[:3])
            )

    cr = attribution.get("crypto") or {}
    if attribution.get("crypto_enabled") or cr.get("vol_gate_pass", 0) or cr.get("intents", 0):
        print("Crypto funnel:")
        print(
            f"  vol_gate_pass={cr.get('vol_gate_pass', 0)} "
            f"scan_signals={cr.get('scan_signals', 0)} "
            f"intents={cr.get('intents', 0)} "
            f"entries={cr.get('entries', 0)} "
            f"entry_fills={cr.get('entry_fills', 0)} "
            f"fill_rate={cr.get('fill_rate_pct', 0):.1f}%"
        )
        crypto_rejects = attribution.get("crypto_rejects") or {}
        if crypto_rejects:
            parts = ", ".join(
                f"{k}={v}" for k, v in sorted(crypto_rejects.items(), key=lambda x: -x[1])
            )
            print(f"  Crypto rejects: {parts}")
        if cr.get("pair_exits") or cr.get("entries") or cr.get("avg_hold_bars"):
            print("Crypto pair detail:")
            print(f"  pair_exits={cr.get('pair_exits', 0)} avg_hold_bars={cr.get('avg_hold_bars', 0):.1f}")
            win_reg = cr.get("win_rate_by_regime") or {}
            if win_reg:
                reg_line = ", ".join(f"{k}={v:.0f}%" for k, v in win_reg.items())
                print(f"  win_rate_by_regime: {reg_line}")
            losers = cr.get("top_losing_pairs") or []
            if losers:
                loser_line = ", ".join(f"{k} ${v:+.2f}" for k, v in losers[:3])
                print(f"  top_losing_pairs: {loser_line}")
    if (
        sa.get("intents", 0) == 0
        and sa.get("scan_signals", sa.get("signals", 0)) == 0
        and not attribution.get("stat_arb_enabled", True)
    ):
        print("  Note: stat arb off for this run (e.g. --fast-mode disables it)")
