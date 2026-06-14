# Kraken phone playbook (text alerts)

Short **what to do on Kraken Pro** messages, timed for a late schedule (up after 10am, up late).

Uses the **same macro logic** as the paper bot (stress, yield gate) plus your holdings.

## Delivery (pick one or both)

### Option A — Telegram (recommended, pings your phone)

1. Message [@BotFather](https://t.me/BotFather) → create bot → copy token.
2. Message your bot once, then open  
   `https://api.telegram.org/bot<TOKEN>/getUpdates`  
   and copy your `chat_id`.
3. In `.env`:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Option B — Email to SMS

Many carriers deliver email as texts, e.g. `yournumber@vtext.com`. Use the same SMTP block as alerts:

```env
SMTP_HOST=smtp.gmail.com
SMTP_USER=...
SMTP_PASSWORD=...   # app password
ALERT_EMAIL_TO=yourphone@vtext.com
```

## Kraken holdings

- **Crypto balances:** read automatically if `KRAKEN_API_KEY` + `KRAKEN_SECRET_KEY` are in `.env`.
- **Stocks/ETFs on Kraken:** edit `reference/kraken_positions.json` when your app holdings change (API often won’t list equities).

## Commands

```powershell
cd PythonTrading
.\.venv\Scripts\Activate.ps1

# Preview
python scripts/kraken_send_playbook.py --dry-run --slot morning

# Send now
python scripts/kraken_send_playbook.py --force --slot morning
python scripts/kraken_send_playbook.py --force --slot evening
```

## Windows Task Scheduler (late-day schedule)

| Task | Time (example) | Command |
|------|----------------|---------|
| Morning playbook | **11:00 AM** | `python scripts\kraken_send_playbook.py --slot morning` |
| Evening playbook | **9:30 PM** | `python scripts\kraken_send_playbook.py --slot evening` |

- Start in: `C:\Users\Owner\PythonTrading`
- Program: full path to `.venv\Scripts\python.exe`

Adjust times in Task Scheduler to your timezone.

## What each message contains

- **Morning:** max 1–2 actions (sell leverage, trim duplicate QQQ/VOO, simplify tiny positions).
- **Evening:** review only — avoid big trades after midnight.

Not auto-trading — you execute in Kraken Pro yourself.
