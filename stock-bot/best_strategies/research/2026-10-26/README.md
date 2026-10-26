# Research 2026-10-26 (box scratch runs, free data, not repo backtester)
- vti_spy/: VTI vs SPY variants in H1D -> no change (not pre-registered).
- cross_asset/: H1 stock-bond corr, H2 gold/real rates, H3 credit HYG/IEF, H4 dollar -> all REJECT. Hypotheses frozen + sha256 before running.
- small_edge/: H1 turn-of-month IWM, H2 pre-holiday IWM, H3 weekly reversal S&P600, H4 52wk-high S&P600 -> all REJECT. H3/H4 survivorship-biased (current S&P600). PEAD, index add/delete, insider Form-4 NOT tested (data) - not rejected. DIAG file is post-verdict no-cost diagnostic only.
Close-to-close daily simulation; no taxes; not STRICT PIT backtester.py.
