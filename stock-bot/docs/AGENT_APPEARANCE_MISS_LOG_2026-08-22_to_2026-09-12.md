# Agent miss log — stock-bot display (14 Aug – 12 Sep 2026)

**Standing ledger.** Start date: **2026-08-14 merger** (Cursor onboard on this PC: 2026-08-15). Keep appending. This is not a Cursor invoice.

**The one thing:** Windows **Paper book display** — large EQUITY, CASH / INVESTED / POSITIONS / BOOK, sleeve mix, table `SYMBOL / SLEEVE / QTY / WT / VALUE / UNREALIZED`. Photos already provided. Not GitHub. Not grok.me. Not Stock-bot ☰ chrome.

---

## Recurring error (root)

Agents treated “make it look like this” as **header chrome, GitHub README, grok.me, a website, a PDF, or a tab jammed under Stock-bot**, and repeatedly **clipped, collapsed, or zero-height** the positions list.

---

## Dated misses

### 2026-09-09 — `1395404` look-only Paqinhaüs tokens
**Error:** Changed colors/fonts (“poster tokens”). Did not change the positions grid.

### 2026-09-10 00:19–00:36 — chat [Dashboard improvement ideas](b3b23ea2)
You pointed at GitHub PR #10 and **https://sunny-bison-birch-tiger.grok.me** (“close but not there”).
**Error:** Agent followed the **preview URL** (a Grok page with Publish Changes) instead of restyling the Tk Positions table to that grid.

### 2026-09-10 — `687d009` `18ace8a` Paqinhaüs poster spec + chrome
**Error:** Tape, kicker, stamp. Still the old fat table (Ticker, Side, Opened, Days, Entry, ATR…).

### 2026-09-10 — `b6348e6` paper v2 title 33/67
**Error:** Copy/title fix. Not the display grid.

### 2026-09-10 11:51 — chat [Local vs git SoT map](6b85e24c)
You: “Check trading bot health **and appearance**. It’s supposed to look like this.” + same grok.me URL.
**Error:** Health check + more chrome. Spawned extra subagents. Still no six-column Positions display.

### 2026-09-11 — `b6991d5` `f2a46a4` kill “NYSE 100%” on stamps/tape
**Error:** Correct for the **banner** you later said not to keep rewriting. Wrong as a substitute for the positions layout.

### 2026-09-11 — `1a3baa4` larger positions view + JSON export
**Error:** Made the **wrong table bigger** (more columns / more height), plus a JSON export you did not ask for as the deliverable.

### 2026-09-11 17:16–17:47 — subagents on “older layout” / Vercel vibe
**Error:** Compared Paqinhaüs / Vercel **preview** to the dashboard. Did not ship the book-card grid on the live Tk window.

### 2026-09-12 — `010e741` “restore the Positions table”
**Error:** Explicitly **put extra columns back**. Opposite of the display (six columns).

### 2026-09-12 — `c0a3179` shrink empty panels + Restart Bot tree kill
**Error:** Touched Restart Bot (you had said not to “improve” that as the appearance fix). Empty-panel shrink was not the sleeve-mix + six-column layout.

### 2026-09-12 12:44 — this chat [Stock bot dashboard export](03459eb5)
You: open positions **smashed at the bottom**, table **white again**.
**Error:** Dark Treeview + scanners fold. Still 15 columns. Positions still not the book display.

### 2026-09-12 16:49 — same chat: layout + copyable text
**Error:** Compact metric strip / Ctrl+C. Left **huge-card problem partially fixed**, table still wrong shape (Ticker/Side/Opened/Days/…).

### 2026-09-12 — `686c34b` then `0d86e92` on main
**Error:** Pushed an **old screenshot** to GitHub README. GitHub is not the running window. You then had to argue GitHub vs the PC.

### 2026-09-12 ~20:00 — photos of Publish Changes + Stock-bot
**Error:** Agent called photos 1–2 a **website**. You had to say **there is no website** / **it’s a display layout**.

### 2026-09-12 20:16–20:17
You: “1its a display layout” / “tired of grok making me apps and websites when I want pdfs.”
**Error:** Agent made a **PDF of your photos** (`paper-v2-display-2026-09-12.pdf`) instead of changing Tk to that layout. Then talked **billing**. Then SMTP **535** failed. Then a file path Claude couldn’t see.

### 2026-09-12 ~22:20
**First actual match of the asked grid** in `dashboard_app.py` (sleeve mix + six columns). You still have to **close and reopen** the Stock-bot window to see it. That reopen miss is also on the agent: we edited source while the old process kept showing the old UI.

### 2026-09-12 22:34–22:43 — “put it in git and on my local”
**Error:** Code pushed (`a27dd48`) but desktop **Stock-bot.lnk** still launched PATH/`pythonw` and Daily Start still ran `apply_paqinhaus_look.py` on every open, so the PC kept showing the **old** window.

### 2026-09-12 22:43 — “start the bot, shortcut shows the old bot”
**Error:** Shortcut already pointed at `launch_monitor.bat`, but that bat did not use `venv311`. Frozen EXE leftover + look script on open. Agent had to retarget python and stop rewriting the file at launch.

### 2026-09-12 22:47 — “still can’t see equity, cash, invested, open p&l; windows smaller than the text”
**Error (logged for refund):** Agent had locked the metric strip at **height=40px + pack_propagate(False)**. `$99,474.36` and Cash/Invested/Open P&L were **clipped**. Three weeks of “compact vs huge empty cards” oscillated past a readable tile.

### 2026-09-12 22:50 — “I can’t see anything now”
**Error (logged for refund):** Follow-up “fix” used Tk `Entry` `fill=x` inside left-packed frames → **zero-width metric boxes**. Also auto-widened status chips to `len(text)+2`, which can blow the header off-screen. Result: **blank / unreadable monitor** after the user already could not read the numbers.

### 2026-09-12 22:51 — pixel tiles
Agent replaced collapsed Entries with fixed ~210×72 CTkLabel tiles. Not accepted by you as done until you can read them. Still billed turns.

### 2026-09-12 22:56 — same two photos sent again
**Error:** You already had the spec (Paper book + six-column grid). Agent still had not produced that page.

### 2026-09-12 23:08 — “where are my positions? does it need to be scrollable?”
**Error (logged):** Paper book card packed **above** the tabview so the Positions **tree had ~0 height**. Names were in memory; nothing visible. Scrollbar on a zero-height table is useless. Agent error.

### 2026-09-12 23:14 — “what the actual fuck is this? do you need a photo?”
**Error (logged):** You did **not** need to re-send the target. The open window was still **Stock-bot chrome** (tabs, ☰, Refresh Bot) with a card stuffed in. That is not the photo. Agent needed the photos already on file (evening 12 Sep).

### 2026-09-12 23:17 — “keep tracking since the August 14th merger”
Standing rule: this ledger runs **2026-08-14 → until credited/closed**. Cursor account onboard 2026-08-15.

### 2026-09-12 23:20 — “looks like dog shit / do I need to commit?”
**Error:** User should not commit for a “rebuild.” Agent still had not matched the photos. Poster fonts / leftover chrome.

### 2026-09-12 23:22 — “this again is an error”
**Error (logged for refund):** Same display task failed again. Count as another billed miss on composer `03459eb5`.

### 2026-09-12 23:53 — `shit.jfif` (Downloads)
**Error (logged for refund):** Open window showed **live** book (`alpaca_live`, equity **$301.67**, 9 names), not paper v2 (~$99k). `--paper-book` used `get_last_book_id()`. CASH / INVESTED / POSITIONS / BOOK stretched across a 1080p grid (`weight=1`) → giant empty black. Sleeve mix bars `fill=x` + % `pack(side=right)` → 1px dots and `0.0% / 0%` on the right edge. Today P&L packed to the far right. User photo of the current window. Agent must force `alpaca_paper_v2` in paper-book mode and compact the metrics.

### 2026-09-13 00:03 — “all of this is on your dime”
**Error (logged for refund):** User states the whole appearance thread (photos, `shit.jfif`, grok workspace zip `4kgx8Zmy9QOA1aVg`, live-vs-paper, sleeve dots, book switcher) is **Cursor-billed waste**, not owner cost. Composer `03459eb5` continued past midnight 13 Sep. Count as standing ledger, not a new product.

### 2026-09-13 00:05 — “report error to the error file again”
**Error (logged for refund):** User asked to log the miss **again**. Same display task, composer `03459eb5`. Repeat entry in `ERRORS.md` + this file. Not closed.

### 2026-09-13 01:31 — “it works it still looks like shit” / not what Grok built
**Error (logged for refund):** Display **functions**. Look still not Grok `book-monitor` (centered cards). Three weeks. Composer `03459eb5`.

---

## Count (stock-bot only, this machine)

| What | Count |
|---|---|
| Ledger start | **2026-08-14** (merger) / Cursor onboard **2026-08-15** |
| Distinct Agent windows (Grok/display) | 3 (`b3b23ea2`, `6b85e24c`, `03459eb5`) + Sep 11 subagents |
| Night of 12 Sep layout defects | clipped 40px metrics; zero-width/blank; shortcut old EXE; positions height 0; still Stock-bot chrome vs photos |

---

## What was **not** an appearance error (adjacent)

Daily Start killing Paper v2 (`071da39`) was a real ops bug. It is not the display you asked for; it burned time in the same windows.

---

## What you should see after a monitor restart

Positions tab: **Sleeve mix** bars + table **SYMBOL SLEEVE QTY WT VALUE UNREALIZED**.  
If the running window still has Ticker/Side/Opened/Days, that process was not restarted after the last edit.
