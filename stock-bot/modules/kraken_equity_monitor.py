"""Detect SpaceX (SPCX) on Kraken Pro: xStocks API + pair scan."""

from __future__ import annotations

import re

import requests

import config

KRAKEN_ASSET_PAIRS = "https://api.kraken.com/0/public/AssetPairs"
# Space (SPC) is unrelated; SPCEx is a different xStock — match SPCX only
_SPCX_RE = re.compile(r"^SPCX", re.I)


def _pair_online(info: dict) -> bool:
    status = (info.get("status") or "").lower()
    return status in ("online", "reduce_only")


def _classify_pair(info: dict) -> str:
    base_class = (info.get("aclass_base") or "").lower()
    if base_class == "tokenized_asset":
        return "xstock"
    return "spot_or_other"


def scan_kraken_spcx_pairs() -> list[dict]:
    """Return Kraken pairs whose symbol/wsname/base looks like SPCX (not SPC alone)."""
    found: list[dict] = []
    seen: set[str] = set()

    for params in (None, {"aclass_base": "tokenized_asset"}):
        try:
            resp = requests.get(
                KRAKEN_ASSET_PAIRS,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            pairs = resp.json().get("result") or {}
        except requests.RequestException:
            continue

        for altname, info in pairs.items():
            wsname = info.get("wsname") or ""
            base = info.get("base") or ""
            if not (_SPCX_RE.search(altname) or _SPCX_RE.search(wsname) or _SPCX_RE.search(base)):
                continue
            key = wsname or altname
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "altname": altname,
                    "wsname": wsname,
                    "pair": altname,
                    "base": base,
                    "quote": info.get("quote"),
                    "kind": _classify_pair(info),
                    "online": _pair_online(info),
                    "status": info.get("status"),
                }
            )

    found.sort(key=lambda p: (not p["online"], p["kind"] != "xstock", p["altname"]))
    return found


def check_kraken_spcx_tradable() -> dict:
    """
    Live check whether SPCX (or SPCXx) exists and is online on Kraken.
    Does not require API keys.
    """
    symbol = config.SPACEX_IPO_TICKER
    override = (config.KRAKEN_SPCX_PAIR or "").strip()
    pairs = scan_kraken_spcx_pairs()

    chosen = None
    if override:
        for p in pairs:
            if p["altname"].upper() == override.upper() or p["wsname"].upper() == override.upper():
                chosen = p
                break
        if chosen is None:
            chosen = {
                "altname": override,
                "wsname": override,
                "pair": override,
                "kind": "configured",
                "online": False,
                "status": "not_found_in_scan",
            }
    elif pairs:
        chosen = pairs[0]

    tradable = bool(chosen and chosen.get("online"))
    return {
        "symbol": symbol,
        "found": bool(pairs) or bool(override),
        "tradable": tradable,
        "pair": chosen.get("pair") if chosen else None,
        "wsname": chosen.get("wsname") if chosen else None,
        "kind": chosen.get("kind") if chosen else None,
        "status": chosen.get("status") if chosen else None,
        "candidates": pairs,
    }
