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

# SpaceX IPO ↔ crypto narrative (S-1 disclosed ~18,712 BTC treasury)
SPACEX_IPO_TOPICS = (
    "spacex",
    "starlink",
    "spcx",
    "s-1",
    "ipo",
    "going public",
    "nasdaq",
)
SPACEX_CRYPTO_LINK = (
    "bitcoin",
    "btc",
    "crypto",
    "digital asset",
    "treasury",
    "corporate holder",
    "balance sheet",
)
SPACEX_IPO_BULLISH = (
    "largest ipo",
    "record",
    "trillion",
    "institutional",
    "legitimacy",
    "surge",
    "rally",
    "front-run",
    "demand",
)
SPACEX_IPO_BEARISH = (
    "delay",
    "postponed",
    "withdraw",
    "sec scrutiny",
    "loss",
    "burn",
    "lockup",
    "selloff",
    "overvalued",
    "bubble",
)
# Synthetic SPCX pre-IPO perp on Hyperliquid (not Alpaca-tradable; narrative proxy)
SPCX_PERP_TOPICS = (
    "spcx",
    "hyperliquid",
    "hypurrscan",
    "pre-ipo",
    "perpetual",
    "perp",
    "trade.xyz",
    "whale",
    "million long",
    "usdc",
)


def score_text_sentiment(text: str) -> float:
    text = text.lower()
    bull = sum(text.count(w) for w in BULLISH)
    bear = sum(text.count(w) for w in BEARISH)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 4)


def count_topic_mentions(text: str, keywords: tuple[str, ...]) -> int:
    text = text.lower()
    return sum(text.count(w) for w in keywords)


def score_topic_sentiment(
    text: str,
    *,
    bullish: tuple[str, ...] = SPACEX_IPO_BULLISH,
    bearish: tuple[str, ...] = SPACEX_IPO_BEARISH,
) -> float:
    text = text.lower()
    bull = sum(text.count(w) for w in bullish)
    bear = sum(text.count(w) for w in bearish)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 4)


def spacex_crypto_relevance(text: str) -> dict:
    """Score how much text ties SpaceX IPO narrative to crypto/BTC."""
    text = text.lower()
    spacex_hits = count_topic_mentions(text, SPACEX_IPO_TOPICS)
    crypto_hits = count_topic_mentions(text, SPACEX_CRYPTO_LINK)
    spcx_perp_hits = count_topic_mentions(text, SPCX_PERP_TOPICS)
    linked = spacex_hits > 0 and crypto_hits > 0
    spcx_perp = spcx_perp_hits > 0 and (
        spacex_hits > 0 or "spcx" in text or "spacex" in text
    )
    return {
        "spacex_hits": spacex_hits,
        "crypto_hits": crypto_hits,
        "spcx_perp_hits": spcx_perp_hits,
        "linked": linked,
        "spcx_perp": spcx_perp,
        "sentiment": score_topic_sentiment(text),
    }
