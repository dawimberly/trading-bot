"""Isolated Live replay. Missing historical inputs stay missing."""

from __future__ import annotations

from dataclasses import dataclass, field

from research.live_replay.sim_broker import SimBroker

# Same comparison as modules/position_exits.py: pnl_pct <= -disaster, then plpc.
FIXED_STOP_PCT = 0.05


@dataclass
class PointInTimeBundle:
    """Frozen inputs for one decision. Absent fields must not be backfilled."""

    closes: dict[str, list[float]]
    universe: list[str]
    as_of: str
    cash: float | None = None
    equity: float | None = None
    positions: dict | None = None
    cooldown: dict | None = None
    regime: str | None = None
    source: str = ""


@dataclass
class ReplayResult:
    faithful: bool
    status: str
    ranked: list[str] = field(default_factory=list)
    intents: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def fixed_stop_hit(entry: float, mark: float, plpc: float | None = None, *, disaster: float = FIXED_STOP_PCT) -> bool:
    """Live fixed-stop branch. Smart stops and exit optimization are off on this book."""
    if entry <= 0 or mark <= 0:
        return False
    pnl_pct = (mark - entry) / entry
    hit = pnl_pct <= -disaster
    if plpc is not None and float(plpc) <= -disaster:
        hit = True
    return hit


def base_ma50_rank(closes: dict[str, list[float]], universe: list[str], *, tolerance: float = 0.0) -> list[str]:
    """MA50 momentum order used when Live scanner boosts are off.

    Mirrors the base sort in modules/pipeline_strategies.py _equity_momentum_candidates:
    keep names above MA50 (minus tolerance) and sort by close/MA - 1. Does not call
    scanners, the network, or the current market database.
    """
    floor = 1.0 - max(0.0, tolerance)
    rows: list[tuple[float, str]] = []
    allowed = list(universe)
    for symbol in allowed:
        prices = [float(px) for px in closes.get(symbol, []) if px is not None]
        if len(prices) < 20:
            continue
        window = prices[-50:]
        ma = sum(window) / len(window)
        current = prices[-1]
        if ma > 0 and current > ma * floor:
            rows.append((current / ma - 1.0, symbol))
    rows.sort(reverse=True)
    return [symbol for _score, symbol in rows]


def replay_decision(bundle: PointInTimeBundle | None, broker: SimBroker | None = None) -> ReplayResult:
    """Replay one decision from a frozen bundle. Refuses when the bundle is absent."""
    sim = broker or SimBroker()
    if bundle is None:
        return ReplayResult(
            faithful=False,
            status="incomplete",
            gaps=[
                "no point-in-time close matrix",
                "no recorded candidate ranking",
                "no recorded account, cooldown, or regime snapshot for this decision",
            ],
        )
    gaps: list[str] = []
    if bundle.positions is None or bundle.cash is None or bundle.equity is None:
        gaps.append("account state was not frozen with the price matrix")
    if bundle.cooldown is None:
        gaps.append("pair cooldown was not frozen")
    if bundle.regime is None:
        gaps.append("regime was not frozen")
    ranked = base_ma50_rank(bundle.closes, bundle.universe)
    if ranked:
        sim.record_intent(kind="rank", symbols=ranked, as_of=bundle.as_of, sent=False)
    if not gaps:
        gaps.append("fill, cash, and equity are not replayed from this rank alone")
    return ReplayResult(
        faithful=False,
        status="incomplete",
        ranked=ranked,
        intents=list(sim.intents),
        gaps=gaps,
    )
