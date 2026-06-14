"""Cycle scheduler stub for cloud deployment (cron, ECS, Cloud Run, etc.)."""


def should_run_cycle(*, market_open: bool, last_cycle_utc: str | None) -> bool:
    """Placeholder gate — wire to market hours + min interval when live."""
    _ = (market_open, last_cycle_utc)
    return False
