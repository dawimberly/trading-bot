def get_market_regime(current_sentiment, current_volatility):
    """
    Classifies the current market 'Rhyme' (Regime).
    This logic helps the AI Observer understand the context of your trades.
    """
    if current_sentiment > 0.5 and current_volatility == "High":
        return "RHYME_A: Euphoric_Volatility"
    elif current_sentiment < -0.5 and current_volatility == "High":
        return "RHYME_B: Panic_Volatility"
    elif current_sentiment > 0.5 and current_volatility == "Low":
        return "RHYME_C: Steady_Bullish_Growth"
    else:
        return "RHYME_D: Range_Bound_Neutral"