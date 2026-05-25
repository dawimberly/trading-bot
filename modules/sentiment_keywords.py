"""Keyword sentiment scoring (shared by Wayback research and live web fetch)."""

BULLISH = (
    "bullish",
    "rally",
    "surge",
    "gains",
    "upbeat",
    "record high",
    "soar",
    "boom",
    "recovery",
    "optimism",
)
BEARISH = (
    "bearish",
    "crash",
    "plunge",
    "decline",
    "fear",
    "recession",
    "selloff",
    "sell-off",
    "panic",
    "worries",
    "slump",
    "tumble",
)


def score_text_sentiment(text: str) -> float:
    text = text.lower()
    bull = sum(text.count(w) for w in BULLISH)
    bear = sum(text.count(w) for w in BEARISH)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 4)
