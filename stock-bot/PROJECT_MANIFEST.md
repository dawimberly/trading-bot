# Structural Symmetry Trading Engine - Project Manifest

## 1. Core Modules
- **`modules/advisor_ranker.py`**: The "Brain". Calculates the geometric coupling (Symmetry Score) between assets using inverse standard deviation of normalized price spreads.
- **`run_all.py`**: The "Orchestrator". 
    - **Scanner**: Uses `ThreadPoolExecutor` for concurrent `yfinance` data fetching.
    - **Filter**: Trend-following logic (Price > 20-period SMA).
    - **Runner**: Executes the top-ranked structural invariant every hour.

## 2. Key Mathematical Principles
- **Normalization**: Assets are scaled to their initial value ($P_t / P_0$) to allow cross-asset comparison (e.g., VTI vs BTC).
- **Manifold Stability**: The system optimizes for the *Inverse* of volatility ($\frac{1}{\sigma_{spread}}$). A higher score = lower structural variance.

## 3. Current Status
- **Architecture**: Asynchronous/Concurrent.
- **Data Handling**: Multi-Index aware (handles single and multi-ticker frames).
- **Execution**: Operational; currently logging signals to console.