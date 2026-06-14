# Migration checklist (laptop → cloud)

When the cloud bot is ready to replace or mirror paper research:

- [ ] Freeze laptop paper profile revision (git tag)
- [ ] Copy or submodule `modules/` sleeves into `cloud_bot/modules/`
- [ ] Point `market_data.db` sync or cloud-native data feed
- [ ] Dedicated Alpaca paper API keys for cloud
- [ ] Run `backtester.py --paper-aggressive` parity on cloud image CI
- [ ] Enable `CLOUD_BOT_DRY_RUN=false` only after paper soak period
- [ ] Keep laptop bot running until cloud metrics match for N days
