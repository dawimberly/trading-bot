# Cloud Bot

The **cloud bot** lives in [`cloud_bot/`](cloud_bot/) — a 24/7 VPS-ready wrapper around the **Best Paper Bot** stack (Dynamic VTI, Stat Arb, Dynamic Risk, Vol Overlay, Options, advanced flags).

**Full deployment guide:** [`cloud_bot/README_CLOUD.md`](cloud_bot/README_CLOUD.md)

## Quick commands

```bash
cd cloud_bot
python runtime/main.py --backtest --days 365
python runtime/main.py --dry-run
```

Laptop paper bot (`run_paper_bot.py`, `run_all.py`) stays in the repo root and receives lightweight changes only.