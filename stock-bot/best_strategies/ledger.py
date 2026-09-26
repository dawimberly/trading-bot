"""Strategy ledger — SoT for best_strategies/.

Statuses:
  deployed  running on a book
  hold      running; do not change
  test      next scientific work
  watch     measured, not enough to deploy
  reject    measured fail; do not revive without a new window + new knob
  blocked   cannot test until data/code exists

This module never deploys.
"""

from __future__ import annotations

from typing import Any

AS_OF = "2026-09-20"

# Deploy ladder. Lower rung_order = earlier capital.
BOOKS: dict[str, dict[str, Any]] = {
    "research": {
        "rung": 0,
        "id": None,
        "owner": "scripts",
        "deploy": False,
    },
    "aggressive": {
        "rung": 1,
        "id": "alpaca_paper",
        "owner": "agent",
        "deploy": "pass + named knob in chat",
        "now": "HOLD - VTI 0, NYSE ~95%, Lab 8 via portal, scanners ON, fat-loser OFF, A2B1 ON",
    },
    "medium": {
        "rung": 2,
        "id": "alpaca_paper_v2",
        "owner": "owner",
        "deploy": "explicit owner phrase",
        "now": "locked look; 10 names, 2.5x ATR, scanners OFF, fat-loser OFF, VTI 0",
    },
    "live": {
        "rung": 3,
        "id": "alpaca_live",
        "owner": "owner",
        "deploy": "explicit owner phrase",
        "now": "Profile A; never from this folder",
    },
}

# priority: 10 = do next, 90 = later, 100 = parked
STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "nyse_ma70_hold",
        "name": "NYSE MA70 rank · 15 names · 30d · 2x ATR · no 1R",
        "status": "deployed",
        "book": "medium",
        "priority": 20,
        "evidence": "paper_v2_best 365d +49.2%/DD 4.9%; 1000d +91.8%/10.0%",
        "next": "Audit Medium drift (running 10 names + 2.5x ATR vs this pick)",
        "knob": None,
    },
    {
        "id": "aggressive_stack",
        "name": "Aggressive employed stack (VTI 0, NYSE 95%, 25 names, scanners)",
        "status": "hold",
        "book": "aggressive",
        "priority": 15,
        "evidence": "Agent HOLD 2026-09-20 - weekday overlay does not transfer; take_1r family lost",
        "next": "Stay put. Name-count 8 already live. STRICT/FULL: do not cut scanners.",
        "knob": None,
    },
    {
        "id": "name_count_8_15_25",
        "name": "NYSE name count 8 vs 15 vs 25",
        "status": "hold",
        "book": "aggressive",
        "priority": 10,
        "evidence": "STRICT 365d+1000d 2026-09-20: 15 loses. 8 beats 25. 6 vs 8 2026-09-21: FAIL both (365d -0.12pp; 1000d -15.4pp / -0.34 Sh). Stay Lab 8. No .env write.",
        "next": "Leave aggressive on portal 8-name Lab. Do not cut to 6 or set 15.",
        "knob": "MAX_ACTIVE_TICKERS / NYSE slots",
        "pass": "15 or 8 beats 25 on return+Sharpe, MaxDD not worse >1pp, both windows",
    },
    {
        "id": "medium_drift",
        "name": "Medium SoT spec vs running env",
        "status": "hold",
        "book": "medium",
        "priority": 12,
        "evidence": "Disk .env 10 names/2.5x is stale. portal_bot + heartbeat = 15 names/2.0x pick. See MEDIUM_DRIFT.md. No edit.",
        "next": "Optional later: sync disk .env to launch overlay so the file matches. Not a strategy change.",
        "knob": None,
    },
    {
        "id": "overlay_a2b1",
        "name": "Flatten >=8% at next open, skip Monday (hold A2B1)",
        "status": "deployed",
        "book": "aggressive",
        "priority": 40,
        "evidence": "NYSE walk hold +9.0pp / DD better, 242 RTH bars. Chat 2026-09-26: wired Lab 8 portal PAPER_NYSE_A2B1_ENABLED. take_1r/wide/long all fail.",
        "next": "Collect full-stack second window. Kill if book misses hold baseline or DD +2pp. Not Medium/live.",
        "knob": "PAPER_NYSE_A2B1_ENABLED / gain_exit_pct=8, gain_exit_mode=skip_monday",
        "pass": "PROTOCOL full-stack gates vs current aggressive",
    },
    {
        "id": "tod_entry",
        "name": "NYSE entry midday / last hour vs open-chase",
        "status": "reject",
        "book": "aggressive",
        "priority": 100,
        "evidence": "5m MA50 cooldown-only, 30-name PIT, 10bps RT, 2026-09-20: last 365d PASS (+3.09pp / +0.20 Sh / DD better) but prior 365d FAIL (-0.25pp / -0.04 Sh). See TOD_ENTRY.md.",
        "next": "Dead as a default 9:30-10:00 block. Do not flip PAPER_MOMENTUM_QUALITY_FIXES from this.",
        "knob": "block_open_chase 9:30-10:00",
    },
    {
        "id": "strict_vs_full_now",
        "name": "STRICT vs FULL on the employed VTI=0 stack",
        "status": "hold",
        "book": "aggressive",
        "priority": 25,
        "evidence": "Lab 8 VTI=0 365d 2026-09-20: STRICT +1.60%/0.16/-11.90 vs FULL +7.66%/0.57/-12.38 (dDD +0.48pp). FULL is not PIT. Do not promote or cut scanners. Old STRICT>FULL was a different VTI mix.",
        "next": "Leave overlays as portal launches them (ON). No second-window deploy from FULL.",
        "knob": None,
    },
    {
        "id": "fat_loser_ab",
        "name": "Fat-loser drip (open −1%)",
        "status": "reject",
        "book": "aggressive",
        "priority": 100,
        "evidence": "NYSE hold 8-name 10bps RT 2026-09-20: 365d OFF +36.9%/DD 13.3 vs ON +3.9%/5.9 (dRet -33pp); 1000d OFF +66.9%/18.6 vs ON +8.0%/9.9 (dRet -59pp). Portal already OFF.",
        "next": "Leave OFF. Do not restore from disk .env. Not Medium.",
        "knob": "PAPER_NYSE_FAT_LOSER_ENABLED",
    },
    {
        "id": "lab_concentrated",
        "name": "Lab 8 names · 15% · trail from peak",
        "status": "watch",
        "book": "aggressive",
        "priority": 70,
        "evidence": "Walk +123%/DD 12% 365d. PAPER_LAB_CONCENTRATED is not set on alpaca_paper .env.",
        "next": "Do not enable. Compare only as a sleeve alternative after name-count A/B.",
        "knob": "PAPER_LAB_CONCENTRATED",
    },
    {
        "id": "stat_arb_pairs",
        "name": "Equity stat-arb pairs",
        "status": "hold",
        "book": "aggressive",
        "priority": 60,
        "evidence": "ON aggressive, OFF Medium. v1.5.4 quality lock. Not a calendar fade.",
        "next": "Leave on aggressive until STRICT-now says overlays drag on VTI=0 tape",
        "knob": None,
    },
    {
        "id": "dynamic_vti_40_75",
        "name": "Smart Dynamic VTI 40–75%",
        "status": "watch",
        "book": "aggressive",
        "priority": 80,
        "evidence": "Documented v1.5.4 lock. Both paper books currently VTI=0.",
        "next": "STRICT 365d+1000d VTI=0 vs floor 40% on today's NYSE stack. Do not silently restore.",
        "knob": "PAPER_DYNAMIC_VTI / floors",
    },
    {
        "id": "take_1r",
        "name": "Full exit at 1R",
        "status": "reject",
        "book": "research",
        "priority": 100,
        "evidence": "one_r_hit: book +81% vs hold +66% but DD 11.0 vs 6.6 (>+2pp)",
        "next": "Dead unless a new 1000d window passes the DD gate",
        "knob": "take_1r / EXIT_OPTIMIZATION full-exit",
    },
    {
        "id": "friday_flatten",
        "name": "Flatten leftovers Friday close",
        "status": "reject",
        "book": "research",
        "priority": 100,
        "evidence": "365d −19pp vs hold; 1000d −33pp. Weekend continuation.",
        "next": "Do not run again as a default",
        "knob": "friday_flatten",
    },
    {
        "id": "thursday_only_fade",
        "name": "Thursday-only next-open flatten (B2)",
        "status": "reject",
        "book": "research",
        "priority": 100,
        "evidence": "Event study Thursday gap is real; overlay on the long book lost all 12 cells",
        "next": "Not a long-NYSE overlay. Residual short book is Monday beta — also reject.",
        "knob": "gain_exit_mode=thu_only",
    },
    {
        "id": "monday_unique_dump",
        "name": "Monday-only first-hour dump",
        "status": "reject",
        "book": "research",
        "priority": 100,
        "evidence": "WOP Mon 0.98 vs Tue 1.02; ~48% down-hour every weekday",
        "next": "Use five-day first-hour stats, not a Monday special",
        "knob": None,
    },
    {
        "id": "open_predict",
        "name": "Predict next open vs prior close",
        "status": "reject",
        "book": "research",
        "priority": 100,
        "evidence": "Ridge and gap-persist lose MAE to pred=prior_close on GOLD/FCX/...",
        "next": "Dead as an entry model",
        "knob": None,
    },
    {
        "id": "vti_tactical_5k",
        "name": "VTI tactical $5k shadow",
        "status": "watch",
        "book": "live",
        "priority": 90,
        "evidence": "PROMOTE_CRITERIA.md not passed; freeze language still applies",
        "next": "Owner-only. Do not mix with aggressive NYSE.",
        "knob": None,
    },
    {
        "id": "weekly_review_sot",
        "name": "Saturday weekly_review on Medium SoT",
        "status": "watch",
        "book": "medium",
        "priority": 18,
        "evidence": "Re-run 2026-09-20: grade A, 7d +5.66%, 90d A/B APPROVE 0.675 (+0.22pp) on paper-aggressive not Medium stack. Not applied (owner Medium; tiny; employed cap 0.95).",
        "next": "Owner-only if they want 0.675. Do not copy to live.",
        "knob": None,
    },
]


def by_status(status: str) -> list[dict[str, Any]]:
    return [s for s in STRATEGIES if s["status"] == status]


def queue() -> list[dict[str, Any]]:
    rows = [s for s in STRATEGIES if s["status"] in ("test", "watch")]
    return sorted(rows, key=lambda s: (s["priority"], s["id"]))
