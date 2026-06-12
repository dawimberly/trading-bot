# Cloud bot architecture (draft)

## Separation from laptop bot

| | Laptop (repo root) | Cloud (`cloud_bot/`) |
|---|-------------------|----------------------|
| Entry | `run_all.py`, `run_paper_bot.py` | `cloud_bot/runtime/main.py` |
| Config | `config.py` | `cloud_bot/config/settings.py` |
| State | Root JSON/DB files | `cloud_bot/data/` |
| Deploy | Local process / portal | `cloud_bot/deploy/` |

## Planned enhancements (cloud-only)

- Stat arb, vol overlay, dynamic risk — port from `modules/` when stable
- Centralized logging & alerting
- Horizontal scale / redundant scheduler (single-writer for orders)
- Secrets from cloud provider, not `.env` on disk

## Non-goals (for now)

- No changes to live small-account bot on laptop
- No shared heartbeat files with portal paper user until explicitly wired
