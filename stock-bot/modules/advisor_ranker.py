"""Rank asset pairs by z-score mean-reversion potential."""

import numpy as np
import pandas as pd

def get_ranked_targets(candidates, combined_data):
    """
    Ranks potential pairs based on Z-Score mean reversion potential.
    """
    results = []

    valid_candidates = [c for c in candidates if c in combined_data.columns]
    if len(valid_candidates) < 2:
        return results

    prices = combined_data[valid_candidates]
    corr = prices.corr()

    for i in range(len(valid_candidates)):
        for j in range(i + 1, len(valid_candidates)):
            asset_a_name = valid_candidates[i]
            asset_b_name = valid_candidates[j]

            if corr.iloc[i, j] < 0.0:
                continue

            asset_a = prices[asset_a_name]
            asset_b = prices[asset_b_name]
            
            # Z-Score mean reversion on the price spread
            spread = asset_a - asset_b
            if len(spread) <= 30:
                spread_mean = spread.mean()
                spread_std = spread.std()
            else:
                spread_mean = spread.rolling(window=30).mean().iloc[-1]
                spread_std = spread.rolling(window=30).std().iloc[-1]

            if spread_std == 0 or np.isnan(spread_std):
                continue

            z_score = (spread.iloc[-1] - spread_mean) / spread_std
            results.append((asset_a_name, asset_b_name, z_score))

    # Highest |z| = strongest mean-reversion opportunity
    return sorted(results, key=lambda x: abs(x[2]), reverse=True)