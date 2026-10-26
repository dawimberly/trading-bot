"""Modular chaotic market scenarios for stress-testing backtests.

Register new chaos types with ``@register_scenario`` — each scenario transforms
a close-price matrix (warmup preserved) into a stressed path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

ChaosFn = Callable[[pd.DataFrame, np.random.Generator, int], pd.DataFrame]


@dataclass(frozen=True)
class ChaosScenario:
    id: str
    name: str
    description: str
    apply: ChaosFn
    # Preferred historical window hint (used when deep history is available).
    era_hint: str = ""


_REGISTRY: dict[str, ChaosScenario] = {}


def register_scenario(
    scenario_id: str,
    *,
    name: str,
    description: str,
    era_hint: str = "",
):
    """Decorator to register a chaos transform."""

    def _wrap(fn: ChaosFn) -> ChaosFn:
        _REGISTRY[scenario_id] = ChaosScenario(
            id=scenario_id,
            name=name,
            description=description,
            apply=fn,
            era_hint=era_hint,
        )
        return fn

    return _wrap


def list_scenarios() -> list[ChaosScenario]:
    return list(_REGISTRY.values())


def get_scenario(scenario_id: str) -> ChaosScenario:
    if scenario_id not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown chaos scenario '{scenario_id}'. Known: {known}")
    return _REGISTRY[scenario_id]


# --- helpers ------------------------------------------------------------------


def _split_warmup(data: pd.DataFrame, min_history: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    mh = max(2, min(int(min_history), len(data) - 2))
    return data.iloc[:mh].copy(), data.iloc[mh:].copy()


def _returns(sim: pd.DataFrame) -> pd.DataFrame:
    rets = sim.pct_change().fillna(0.0)
    return rets.clip(lower=-0.95, upper=3.0)


def _from_returns(
    warmup: pd.DataFrame,
    sim: pd.DataFrame,
    rets: pd.DataFrame,
) -> pd.DataFrame:
    """Rebuild sim prices from returns anchored at last warmup row."""
    if sim.empty:
        return warmup.copy()
    anchor = warmup.iloc[-1].astype(float) if len(warmup) else sim.iloc[0].astype(float)
    level = anchor.copy()
    rows: list[pd.Series] = []
    # First sim bar: apply first return from prior close (anchor).
    for i in range(len(sim)):
        r = rets.iloc[i].astype(float).fillna(0.0)
        if i == 0:
            # pct_change first row is 0; keep near prior path start.
            level = sim.iloc[0].astype(float).where(np.isfinite(sim.iloc[0]), level)
        else:
            level = level * (1.0 + r)
        level = level.where(np.isfinite(level) & (level > 0), np.nan)
        # Fill any broken symbols from previous level.
        level = level.fillna(anchor)
        rows.append(level.copy())
        anchor = level
    synth = pd.DataFrame(rows, index=sim.index, columns=sim.columns)
    return pd.concat([warmup, synth])


def _scale_vol(rets: pd.DataFrame, mult: float) -> pd.DataFrame:
    return (rets * float(mult)).clip(lower=-0.95, upper=3.0)


def _inject_crash_window(
    rets: pd.DataFrame,
    *,
    start: int,
    length: int,
    daily_shock: float,
    vol_boost: float,
    corr_blend: float,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Apply a correlated crash over [start, start+length)."""
    out = rets.copy()
    n = len(out)
    if n < 5:
        return out
    start = int(_clamp(start, 1, max(1, n - 2)))
    length = int(_clamp(length, 1, n - start))
    end = start + length
    market = out.mean(axis=1)
    for i in range(start, end):
        progress = (i - start) / max(1, length - 1)
        # Front-loaded crash then grind.
        shock = daily_shock * (1.2 - 0.4 * progress)
        noise = rng.normal(0, abs(daily_shock) * 0.35, size=out.shape[1])
        crash_row = shock + noise
        blended = (
            (1.0 - corr_blend) * out.iloc[i].to_numpy(dtype=float)
            + corr_blend * (float(market.iloc[i]) * vol_boost + crash_row)
        )
        out.iloc[i] = np.clip(blended * vol_boost, -0.95, 3.0)
    return out


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _place_shock_start(n: int, *, frac: float = 0.35, rng: np.random.Generator | None = None) -> int:
    """Start shock roughly mid-window with light jitter."""
    if n < 10:
        return 1
    base = int(n * frac)
    if rng is None:
        return int(_clamp(base, 1, n - 5))
    jitter = int(rng.integers(-max(1, n // 20), max(2, n // 20)))
    return int(_clamp(base + jitter, 1, n - 5))


# --- scenarios ----------------------------------------------------------------


@register_scenario(
    "normal",
    name="Normal (baseline)",
    description="Unperturbed historical path — control arm.",
    era_hint="recent",
)
def chaos_normal(data: pd.DataFrame, rng: np.random.Generator, min_history: int) -> pd.DataFrame:
    del rng, min_history
    return data.copy()


@register_scenario(
    "crash_2008",
    name="2008-style crash",
    description="Multi-week cascade, vol ~2.5x, correlation spike toward 1.",
    era_hint="2008",
)
def chaos_crash_2008(data: pd.DataFrame, rng: np.random.Generator, min_history: int) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    rets = _scale_vol(rets, 1.35)
    start = _place_shock_start(len(rets), frac=0.30, rng=rng)
    length = max(40, len(rets) // 6)
    rets = _inject_crash_window(
        rets,
        start=start,
        length=length,
        daily_shock=-0.028,
        vol_boost=2.5,
        corr_blend=0.85,
        rng=rng,
    )
    # Slow bleed after crash.
    bleed_end = min(len(rets), start + length + max(20, length // 2))
    for i in range(start + length, bleed_end):
        rets.iloc[i] = np.clip(rets.iloc[i].to_numpy(dtype=float) * 1.4 - 0.004, -0.95, 3.0)
    return _from_returns(warmup, sim, rets)


@register_scenario(
    "covid_2020",
    name="2020 COVID vol spike",
    description="Sharp ~30% crash over ~15 sessions then violent rebound.",
    era_hint="2020",
)
def chaos_covid_2020(data: pd.DataFrame, rng: np.random.Generator, min_history: int) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    start = _place_shock_start(len(rets), frac=0.40, rng=rng)
    crash_len = 15
    rets = _inject_crash_window(
        rets,
        start=start,
        length=crash_len,
        daily_shock=-0.045,
        vol_boost=3.2,
        corr_blend=0.9,
        rng=rng,
    )
    # V-shaped recovery.
    rec_end = min(len(rets), start + crash_len + 25)
    for i in range(start + crash_len, rec_end):
        bounce = 0.018 + float(rng.normal(0, 0.01))
        row = rets.iloc[i].to_numpy(dtype=float) * 1.8 + bounce
        rets.iloc[i] = np.clip(row, -0.95, 3.0)
    return _from_returns(warmup, sim, rets)


@register_scenario(
    "inflation_2022",
    name="2022 inflation bear",
    description="Grinding bear: elevated vol, persistent negative drift, weak rallies.",
    era_hint="2022",
)
def chaos_inflation_2022(
    data: pd.DataFrame, rng: np.random.Generator, min_history: int
) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    rets = _scale_vol(rets, 1.55)
    start = _place_shock_start(len(rets), frac=0.20, rng=rng)
    for i in range(start, len(rets)):
        # Fade up-days; amplify down-days.
        row = rets.iloc[i].to_numpy(dtype=float)
        row = np.where(row > 0, row * 0.55, row * 1.35)
        row = row - 0.0018 + rng.normal(0, 0.003, size=row.shape)
        rets.iloc[i] = np.clip(row, -0.95, 3.0)
    return _from_returns(warmup, sim, rets)


@register_scenario(
    "flash_crash",
    name="Flash crash",
    description="1–2 day liquidity air-pocket (−12% to −20%) then partial snap-back.",
    era_hint="synthetic",
)
def chaos_flash_crash(data: pd.DataFrame, rng: np.random.Generator, min_history: int) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    start = _place_shock_start(len(rets), frac=0.55, rng=rng)
    depth = float(rng.uniform(-0.20, -0.12))
    # Day 1 crash.
    market = float(rets.iloc[start].mean()) if start < len(rets) else 0.0
    crash = np.full(rets.shape[1], depth * 0.7 + market * 0.3) + rng.normal(
        0, 0.02, size=rets.shape[1]
    )
    rets.iloc[start] = np.clip(crash, -0.95, 3.0)
    if start + 1 < len(rets):
        rets.iloc[start + 1] = np.clip(
            np.full(rets.shape[1], depth * 0.35) + rng.normal(0, 0.015, size=rets.shape[1]),
            -0.95,
            3.0,
        )
    # Partial recovery next 3 days (~50% of drop).
    recover = abs(depth) * 0.5 / 3.0
    for j in range(2, 5):
        if start + j >= len(rets):
            break
        rets.iloc[start + j] = np.clip(
            rets.iloc[start + j].to_numpy(dtype=float) * 0.5 + recover,
            -0.95,
            3.0,
        )
    return _from_returns(warmup, sim, rets)


@register_scenario(
    "rate_hike_inflation",
    name="High inflation + rate hikes",
    description="Stagflation-ish: rising discount-rate drag, sector dispersion, sticky vol.",
    era_hint="synthetic",
)
def chaos_rate_hike_inflation(
    data: pd.DataFrame, rng: np.random.Generator, min_history: int
) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    rets = _scale_vol(rets, 1.45)
    start = _place_shock_start(len(rets), frac=0.15, rng=rng)
    n_cols = rets.shape[1]
    # Per-name beta to a rising-rate factor (higher = more hurt).
    betas = rng.uniform(0.4, 1.6, size=n_cols)
    for i in range(start, len(rets)):
        t = (i - start) / max(1, len(rets) - start)
        rate_drag = -0.0012 - 0.0025 * t
        idio = rng.normal(0, 0.006, size=n_cols)
        row = rets.iloc[i].to_numpy(dtype=float) * 1.2 + betas * rate_drag + idio
        rets.iloc[i] = np.clip(row, -0.95, 3.0)
    return _from_returns(warmup, sim, rets)


@register_scenario(
    "corr_breakdown",
    name="Correlation breakdown",
    description="Cross-asset structure collapses — idiosyncratic chaos, hedges fail.",
    era_hint="synthetic",
)
def chaos_corr_breakdown(
    data: pd.DataFrame, rng: np.random.Generator, min_history: int
) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    start = _place_shock_start(len(rets), frac=0.25, rng=rng)
    n_cols = rets.shape[1]
    for i in range(start, len(rets)):
        base = rets.iloc[i].to_numpy(dtype=float)
        # Replace shared factor with independent shocks.
        idio = rng.normal(0, 0.025, size=n_cols)
        signs = rng.choice([-1.0, 1.0], size=n_cols)
        row = 0.25 * base + 0.75 * idio * signs * (1.0 + abs(float(np.mean(base))) * 8)
        rets.iloc[i] = np.clip(row, -0.95, 3.0)
    return _from_returns(warmup, sim, rets)


@register_scenario(
    "liquidity_gaps",
    name="Liquidity gaps",
    description="Random multi-day gaps / air pockets — discontinuous price jumps.",
    era_hint="synthetic",
)
def chaos_liquidity_gaps(
    data: pd.DataFrame, rng: np.random.Generator, min_history: int
) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    n = len(rets)
    n_gaps = max(3, n // 40)
    for _ in range(n_gaps):
        i = int(rng.integers(1, max(2, n - 1)))
        width = int(rng.integers(1, 4))
        mag = float(rng.choice([-1, 1])) * float(rng.uniform(0.04, 0.12))
        # Hit a random subset of names (liquidity is name-specific).
        mask = rng.random(rets.shape[1]) < 0.45
        for j in range(width):
            if i + j >= n:
                break
            row = rets.iloc[i + j].to_numpy(dtype=float)
            row = np.where(mask, row + mag / width + rng.normal(0, 0.01, size=row.shape), row)
            rets.iloc[i + j] = np.clip(row, -0.95, 3.0)
    return _from_returns(warmup, sim, rets)


@register_scenario(
    "vol_regime_shift",
    name="Vol regime shift",
    description="Sudden jump from calm to crisis vol (GARCH-like step), no mean crash.",
    era_hint="synthetic",
)
def chaos_vol_regime_shift(
    data: pd.DataFrame, rng: np.random.Generator, min_history: int
) -> pd.DataFrame:
    warmup, sim = _split_warmup(data, min_history)
    rets = _returns(sim)
    start = _place_shock_start(len(rets), frac=0.45, rng=rng)
    # Calm first half of shock window already in data; after start amplify.
    for i in range(start, len(rets)):
        mult = 2.8 + float(rng.uniform(-0.3, 0.5))
        rets.iloc[i] = np.clip(rets.iloc[i].to_numpy(dtype=float) * mult, -0.95, 3.0)
    return _from_returns(warmup, sim, rets)


DEFAULT_CATALOG_ORDER: tuple[str, ...] = (
    "normal",
    "crash_2008",
    "covid_2020",
    "inflation_2022",
    "flash_crash",
    "rate_hike_inflation",
    "corr_breakdown",
    "liquidity_gaps",
    "vol_regime_shift",
)


def select_scenarios(n: int | None = None, ids: list[str] | None = None) -> list[ChaosScenario]:
    """Pick by ids, or first N from DEFAULT_CATALOG_ORDER."""
    if ids:
        return [get_scenario(i) for i in ids]
    ordered = [get_scenario(i) for i in DEFAULT_CATALOG_ORDER if i in _REGISTRY]
    # Append any custom registrations not in the default order.
    for sid, sc in _REGISTRY.items():
        if sid not in DEFAULT_CATALOG_ORDER:
            ordered.append(sc)
    if n is None or n <= 0:
        return ordered
    return ordered[: min(n, len(ordered))]
