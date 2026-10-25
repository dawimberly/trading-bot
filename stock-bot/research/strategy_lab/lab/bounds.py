"""Compare a rule at both ends of a documented valuation range."""


def conclusion_changes(low: float | None, high: float | None, predicate) -> bool:
    """True when `predicate` disagrees at the two bounds, or either bound is missing."""
    if low is None or high is None:
        return True
    return bool(predicate(low)) != bool(predicate(high))
