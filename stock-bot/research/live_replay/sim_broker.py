"""In-memory broker for Live replay. It cannot submit, place, or transmit orders."""

from __future__ import annotations


class OrderSendBlocked(RuntimeError):
    """Raised when replay code attempts to send an order."""


class _DeadClient:
    """Stand-in for a trading client. Every attribute access is a hard stop."""

    def __getattr__(self, name: str):
        raise OrderSendBlocked(f"replay broker has no trading client ({name})")


class SimBroker:
    """Records intents and applies fills the caller already has. Never sends."""

    def __init__(self, *, cash: float = 0.0, equity: float = 0.0, positions: dict | None = None):
        self.cash = float(cash)
        self.equity = float(equity)
        self.positions = {str(k): dict(v) for k, v in (positions or {}).items()}
        self.intents: list[dict] = []
        self.client = _DeadClient()
        self.dry_run = True
        self.equity_session_open = True

    def submit_order(self, *_args, **_kwargs):
        raise OrderSendBlocked("replay broker cannot submit orders")

    def place_order(self, *_args, **_kwargs):
        raise OrderSendBlocked("replay broker cannot place orders")

    def cancel_order(self, *_args, **_kwargs):
        raise OrderSendBlocked("replay broker cannot cancel orders")

    def replace_order(self, *_args, **_kwargs):
        raise OrderSendBlocked("replay broker cannot replace orders")

    def execute_order(self, *_args, **_kwargs):
        raise OrderSendBlocked("replay broker cannot execute orders")

    def execute_full_exit(self, *_args, **_kwargs):
        raise OrderSendBlocked("replay broker cannot execute exits")

    def execute_reduce_notional(self, *_args, **_kwargs):
        raise OrderSendBlocked("replay broker cannot reduce positions")

    def record_intent(self, **fields) -> dict:
        """Store a decision locally. This does not fill and does not send."""
        intent = {"sent": False, **fields}
        self.intents.append(intent)
        return intent

    def apply_recorded_fill(self, *, side: str, notional: float, cash_after: float | None = None) -> None:
        """Move local cash from a fill that already happened. No network call."""
        signed = float(notional) if str(side).lower() == "sell" else -float(notional)
        self.cash = float(cash_after) if cash_after is not None else self.cash + signed
