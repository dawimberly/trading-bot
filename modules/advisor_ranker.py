"""Rank asset pairs by z-score mean-reversion potential."""

import numpy as np
import pandas as pd

def get_ranked_targets(candidates, combined_data):
    """
    Ranks potential pairs based on Z-Score mean reversion potential.
    """
    results = []
    
    # 1. Filter candidates: Only use assets that exist in the database data
    valid_candidates = [c for c in candidates if c in combined_data.columns]
    
    # 2. Iterate through all unique pairs
    for i in range(len(valid_candidates)):
        for j in range(i + 1, len(valid_candidates)):
            asset_a_name = valid_candidates[i]
            asset_b_name = valid_candidates[j]
            
            asset_a = combined_data[asset_a_name]
            asset_b = combined_data[asset_b_name]
            
            # Correlation filter - Using 0.0 for testing purposes
            if asset_a.corr(asset_b) < 0.0:
                continue
            
            # Z-Score Mean Reversion calculation
            spread = asset_a - asset_b
            mean = spread.rolling(window=30).mean()
            std = spread.rolling(window=30).std()
            
            # Ensure we have valid data before calculating
            if std.iloc[-1] == 0 or np.isnan(std.iloc[-1]):
                continue
                
            z_score = (spread.iloc[-1] - mean.iloc[-1]) / std.iloc[-1]
            results.append((asset_a_name, asset_b_name, abs(z_score)))
    
    # Sort by the highest absolute Z-score
    return sorted(results, key=lambda x: x[2], reverse=True)