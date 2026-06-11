# Cloud Bot deploy assets

**Canonical guide:** [`../README_CLOUD.md`](../README_CLOUD.md) — VPS setup, systemd, logrotate, monitoring.

## Files in this folder

| Path | Purpose |
|------|---------|
| `systemd/cloud-bot.service` | systemd unit (`python -m cloud_bot.runtime.main --run`) |
| `logrotate/cloud-bot` | Daily rotation for `data/logs/cloud_bot.log` |

## Quick install

```bash
sudo cp cloud_bot/deploy/systemd/cloud-bot.service /etc/systemd/system/
sudo cp cloud_bot/deploy/logrotate/cloud-bot /etc/logrotate.d/cloud-bot
sudo systemctl daemon-reload
sudo systemctl enable cloud-bot
sudo systemctl start cloud-bot
```

Edit `User=`, `WorkingDirectory=`, and `EnvironmentFile=` in the unit if not using `/opt/PythonTrading`.
