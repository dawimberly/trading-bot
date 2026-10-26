"""Frozen 27-candidate grid and the selection rule."""

from __future__ import annotations


def candidates(cfg: dict) -> list[dict]:
    rows = []
    order = 0
    for ma in cfg["grid"]["ma_win"]:
        for hold in cfg["grid"]["max_hold"]:
            for mult in cfg["grid"]["atr_mult"]:
                rows.append(
                    {
                        "order": order,
                        "ma_win": int(ma),
                        "max_hold": int(hold),
                        "atr_mult": float(mult),
                    }
                )
                order += 1
    if len(rows) != 27:
        raise RuntimeError(f"grid has {len(rows)} candidates")
    base = cfg["baseline_A"]
    if not any(
        row["ma_win"] == base["ma_win"] and row["max_hold"] == base["max_hold"] and row["atr_mult"] == base["atr_mult"]
        for row in rows
    ):
        raise RuntimeError("baseline A is missing from the grid")
    return rows


def eligible(row: dict, cfg: dict) -> bool:
    rules = cfg["eligibility"]
    if not row.get("reconciled") or row.get("distribution_unpriced"):
        return False
    return (
        float(row["max_drawdown"]) <= float(rules["max_drawdown_at_most"]) + 1e-12
        and int(row["trades"]) >= int(rules["min_closed_trades"])
    )


def select_winner(rows: list[dict], cfg: dict) -> dict:
    """Pick from already scored rows. Does not resimulate."""
    ranked = []
    for row in rows:
        item = dict(row)
        item["eligible"] = eligible(item, cfg)
        if not item.get("reconciled", False):
            item["reject_reason"] = "unreconciled"
        elif item.get("distribution_unpriced"):
            item["reject_reason"] = "unpriced_distribution"
        elif float(item["max_drawdown"]) > float(cfg["eligibility"]["max_drawdown_at_most"]) + 1e-12:
            item["reject_reason"] = "drawdown"
        elif int(item["trades"]) < int(cfg["eligibility"]["min_closed_trades"]):
            item["reject_reason"] = "trade_count"
        else:
            item["reject_reason"] = None
        ranked.append(item)
    pool = [row for row in ranked if row["eligible"]]
    pool.sort(key=lambda row: (-float(row["net_return"]), float(row["max_drawdown"]), float(row["turnover"]), int(row["order"])))
    if not pool:
        return {"choice": "cash", "selected": None, "candidates": ranked}
    winner = pool[0]
    return {
        "choice": "parameters",
        "selected": {
            "order": winner["order"],
            "ma_win": winner["ma_win"],
            "max_hold": winner["max_hold"],
            "atr_mult": winner["atr_mult"],
            "net_return": winner["net_return"],
            "max_drawdown": winner["max_drawdown"],
            "turnover": winner["turnover"],
            "trades": winner["trades"],
        },
        "candidates": ranked,
    }
