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
# Macro unwind / crash language (weighted heavier in creator transcripts)
MACRO_BEARISH_UNWIND = (
    "unwind",
    "crash",
    "collapse",
    "correction",
    "meltdown",
    "blow off top",
    "blow-off top",
    "overvalued",
    "bubble",
    "recession",
    "bear market",
    "hard landing",
)
_CREATOR_CHANNEL_HINTS = ("felix", "andrei", "jikh")

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


def macro_bearish_keyword_hits(text: str) -> int:
    """Count distinct macro unwind/crash terms present in text."""
    low = text.lower()
    return sum(1 for w in MACRO_BEARISH_UNWIND if w in low)


def is_creator_channel(name: str | None) -> bool:
    low = (name or "").lower()
    return any(h in low for h in _CREATOR_CHANNEL_HINTS)


def score_text_sentiment(text: str, *, macro_weight: float = 2.0) -> float:
    text = text.lower()
    bull = sum(text.count(w) for w in BULLISH)
    bear = sum(text.count(w) for w in BEARISH)
    macro = sum(text.count(w) for w in MACRO_BEARISH_UNWIND) * macro_weight
    bear += macro
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 4)


def score_creator_transcript_sentiment(
    text: str,
    *,
    channel_name: str | None = None,
    creator_boost: bool | None = None,
) -> tuple[float, int]:
    """Score transcript with macro unwind terms; extra bearish pull for Felix/Andrei."""
    boost = is_creator_channel(channel_name) if creator_boost is None else creator_boost
    hits = macro_bearish_keyword_hits(text)
    base = score_text_sentiment(text, macro_weight=2.5 if boost else 2.0)
    if boost and hits > 0:
        adjusted = max(-1.0, round(base - min(0.35, 0.07 * hits), 4))
        return adjusted, hits
    return base, hits


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
