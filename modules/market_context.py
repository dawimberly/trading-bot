"""Market regime, volatility, and sentiment helpers for the trading pipeline."""

import config


def get_volatility(data):
    """Classify cross-asset volatility as High or Low."""
    vol = data.pct_change().dropna().std().mean()
    return "High" if vol > 0.02 else "Low"


def get_price_sentiment(data):
    """Price-momentum sentiment (free; used by backtests and live default)."""
    if len(data) < 20:
        return 0.0
    recent = data.iloc[-5:].mean()
    older = data.iloc[-20:-5].mean()
    return float((recent / older).mean() - 1.0)


def _get_tavily_sentiment():
    """Optional paid news search — only when SENTIMENT_SOURCE=tavily."""
    import tavily

    api_key = config.get_tavily_api_key()
    if not api_key:
        raise ValueError("TAVILY_API_KEY not set")
    client = tavily.TavilyClient(api_key=api_key)
    results = client.search("stock market crypto sentiment today", max_results=5)
    text = " ".join(
        r.get("content", "") for r in results.get("results", [])
    ).lower()
    bullish = (
        text.count("bullish")
        + text.count("rally")
        + text.count("surge")
        + text.count("gains")
        + text.count("upbeat")
    )
    bearish = (
        text.count("bearish")
        + text.count("crash")
        + text.count("plunge")
        + text.count("decline")
        + text.count("fear")
    )
    total = bullish + bearish
    if total == 0:
        return 0.0
    return round((bullish - bearish) / total, 2)


def get_sentiment(data):
    """Regime sentiment: price momentum by default (free, unlimited)."""
    source = config.SENTIMENT_SOURCE
    if source == "price":
        return get_price_sentiment(data)
    if source == "tavily":
        try:
            return _get_tavily_sentiment()
        except Exception as e:
            print("Tavily error: " + str(e))
            return get_price_sentiment(data)
    print(f"Unknown SENTIMENT_SOURCE={source!r}; using price sentiment")
    return get_price_sentiment(data)


def get_market_regime(sentiment, volatility):
    """Classify market into one of five regime 'rhymes'."""
    if sentiment > 0.5 and volatility == "High":
        return "RHYME_A: Euphoric_Volatility"
    if sentiment < -0.5 and volatility == "High":
        return "RHYME_B: Panic_Volatility"
    if sentiment > 0.5 and volatility == "Low":
        return "RHYME_C: Steady_Bullish_Growth"
    if sentiment < -0.5 and volatility == "Low":
        return "RHYME_E: Steady_Bearish_Decline"
    return "RHYME_D: Range_Bound_Neutral"
