"""Freeze-safe ops reports: hygiene checks + notify/open helpers.

Measure-only. Never edits .env, strategy knobs, or live Profile A.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]

Severity = Literal["info", "cleanup", "anomaly"]


@dataclass
class Finding:
    id: str
    severity: Severity
    title: str
    detail: str
    default_action: str = "HOLD"  # CONFIRM | DENY | HOLD
    evidence: str = ""


@dataclass
class HygienePack:
    generated_at: str
    findings: list[Finding] = field(default_factory=list)
    equity: float | None = None
    regime: str | None = None
    heartbeat_age_min: float | None = None
    notes: list[str] = field(default_factory=list)

    def cleanups(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "cleanup"]

    def anomalies(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "anomaly"]


def _truthy(raw: str | None, default: str = "false") -> bool:
    return (raw if raw is not None else default).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def freeze_ops_enabled() -> bool:
    return _truthy(os.getenv("FREEZE_OPS_ENABLED"), "true")


def freeze_daily_enabled() -> bool:
    return freeze_ops_enabled() and _truthy(os.getenv("FREEZE_DAILY_HYGIENE_ENABLED"), "true")


def freeze_weekly_enabled() -> bool:
    return freeze_ops_enabled() and _truthy(os.getenv("FREEZE_WEEKLY_PLAN_ENABLED"), "true")


def freeze_open_enabled(*, weekly: bool = False) -> bool:
    if weekly:
        return _truthy(os.getenv("FREEZE_WEEKLY_OPEN"), "true")
    return _truthy(os.getenv("FREEZE_DAILY_OPEN"), "false")


def freeze_telegram_enabled() -> bool:
    return _truthy(os.getenv("FREEZE_OPS_TELEGRAM"), "true")


def freeze_ollama_enabled() -> bool:
    return _truthy(os.getenv("FREEZE_OPS_OLLAMA"), "false")


def open_report(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        print(f"[freeze_ops] Opened {path}", flush=True)
    except Exception as exc:
        print(f"[freeze_ops] Could not open report: {exc}", flush=True)


def notify_owner(subject: str, body: str, out_path: Path | None = None) -> bool:
    """Email + Telegram (truncated). Falls back to data/freeze_ops_pending.txt."""
    if not freeze_telegram_enabled() and not _truthy(os.getenv("FREEZE_OPS_EMAIL"), "true"):
        return False

    emailed = False
    telegrammed = False
    try:
        if _truthy(os.getenv("FREEZE_OPS_EMAIL"), "true"):
            from modules.alerts import send_email

            emailed = bool(send_email(subject, body))
            print(f"[freeze_ops] Email {'sent' if emailed else 'failed/not configured'}.", flush=True)
    except Exception as exc:
        print(f"[freeze_ops] Email failed: {exc}", flush=True)

    try:
        if freeze_telegram_enabled():
            from modules.alerts import send_telegram

            path_line = f"\nFile: {out_path}" if out_path else ""
            # Prefer executive section before "## Full detail"
            cut = body.find("## Full detail")
            if cut < 0:
                cut = body.find("## How to respond")
            snippet = body[: cut if cut > 0 else 2800]
            if len(snippet) > 3200:
                snippet = snippet[:3000] + "\n...(truncated)"
            telegrammed = bool(send_telegram(f"{subject}{path_line}\n\n{snippet}"))
            print(
                f"[freeze_ops] Telegram {'sent' if telegrammed else 'failed/not configured'}.",
                flush=True,
            )
    except Exception as exc:
        print(f"[freeze_ops] Telegram failed: {exc}", flush=True)

    if emailed or telegrammed:
        return True
    pending = ROOT / "data" / "freeze_ops_pending.txt"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(f"{subject}\n\n{body}", encoding="utf-8")
    print(f"[freeze_ops] Wrote pending notice to {pending}", flush=True)
    return False


def _paper_book() -> Path:
    return ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper"


def _journal_candidates() -> list[Path]:
    # Prefer active freeze book (run_paper_bot) over portal historical dump.
    return [
        ROOT / "paper_chase_journal.csv",
        ROOT / "paper_journal.csv",
        _paper_book() / "paper_journal.csv",
    ]


def _load_heartbeat() -> dict[str, Any] | None:
    for path in (
        _paper_book() / "heartbeat.json",
        ROOT / "paper_chase_heartbeat.json",
        ROOT / "bot_heartbeat.json",
        ROOT / Path(os.getenv("HEARTBEAT_FILE", "bot_heartbeat.json")),
    ):
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def _heartbeat_age_min(hb: dict[str, Any] | None) -> float | None:
    if not hb:
        return None
    for key in ("timestamp", "ts", "updated_at", "last_cycle_at"):
        raw = hb.get(key)
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 60.0
        except Exception:
            continue
    return None


def gather_hygiene(*, lookback_days: int = 1) -> HygienePack:
    """Deterministic freeze hygiene findings (no LLM required)."""
    import pandas as pd

    now = datetime.now(timezone.utc)
    pack = HygienePack(generated_at=now.strftime("%Y-%m-%d %H:%M UTC"))
    pack.notes.append("Freeze-safe: recommendations are CONFIRM/DENY/HOLD for humans only.")
    pack.notes.append("No auto .env / strategy / live changes.")

    hb = _load_heartbeat()
    pack.heartbeat_age_min = _heartbeat_age_min(hb)
    if hb:
        pack.regime = str(hb.get("regime") or hb.get("rhyme") or "") or None
        try:
            pack.equity = float(hb.get("equity") or hb.get("portfolio_value") or 0) or None
        except (TypeError, ValueError):
            pack.equity = None

    # Heartbeat stale
    age = pack.heartbeat_age_min
    if age is None:
        pack.findings.append(
            Finding(
                "heartbeat_missing",
                "anomaly",
                "Paper heartbeat missing",
                "No readable heartbeat.json — paper bot may be down.",
                default_action="CONFIRM",
                evidence="check run_paper_bot / portal book heartbeat",
            )
        )
    elif age > 180:
        pack.findings.append(
            Finding(
                "heartbeat_stale",
                "anomaly",
                f"Paper heartbeat stale ({age:.0f} min)",
                "Restart paper bot if unexpected overnight halt.",
                default_action="CONFIRM",
                evidence=f"age_min={age:.1f}",
            )
        )

    # Verify SPY-off from actual paper fills. Raw config defaults are shared with
    # live Profile A and are overridden only when the paper profile initializes.
    cutoff = now - timedelta(days=max(1, lookback_days))
    spy_fills = 0
    journal_path = ""
    try:
        for path in _journal_candidates():
            if not path.is_file():
                continue
            try:
                df = pd.read_csv(path, low_memory=False)
            except Exception:
                # Historical portal dumps sometimes embed stacktraces; skip bad lines.
                df = pd.read_csv(path, engine="python", on_bad_lines="skip")
                pack.findings.append(
                    Finding(
                        "journal_parse_error",
                        "cleanup",
                        "Journal had bad CSV lines (skipped)",
                        f"Used on_bad_lines=skip for {path.name}",
                        default_action="HOLD",
                        evidence=str(path),
                    )
                )
            if df.empty:
                continue
            journal_path = str(path)
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
                df = df[df["timestamp"] >= cutoff]
            sleeve = df["sleeve"].astype(str).str.lower() if "sleeve" in df.columns else None
            event = df["event"].astype(str).str.lower() if "event" in df.columns else None
            symbol = (
                df.get("symbol", df.get("ticker", pd.Series(dtype=str)))
                .astype(str)
                .str.upper()
            )
            buy_mask = event.isin({"buy", "entry", "open", "fill", "signal"}) if event is not None else False
            if sleeve is not None:
                spy_fills = int(((sleeve == "spy") & buy_mask).sum())
            if spy_fills == 0 and buy_mask is not False:
                spy_fills = int((symbol.isin({"SPY", "SPYUSD"}) & buy_mask).sum())
            break
        if not journal_path:
            pack.findings.append(
                Finding(
                    "journal_missing",
                    "cleanup",
                    "Paper journal not found",
                    "Expected portal/paper_chase journal for attribution.",
                    default_action="HOLD",
                    evidence="searched portal + root journal paths",
                )
            )
        elif spy_fills > 0:
            pack.findings.append(
                Finding(
                    "spy_fills_while_off",
                    "anomaly",
                    f"SPY fills in last {lookback_days}d: {spy_fills}",
                    "Paper SPY satellite should be OFF — restart paper bot after SPY-off lock.",
                    default_action="CONFIRM",
                    evidence=journal_path,
                )
            )
        else:
            pack.findings.append(
                Finding(
                    "spy_fills_ok",
                    "info",
                    "No SPY sleeve fills in window",
                    "Consistent with SPY-off freeze lock.",
                    default_action="HOLD",
                    evidence=journal_path or "n/a",
                )
            )
    except Exception as exc:
        pack.findings.append(
            Finding(
                "journal_parse_error",
                "cleanup",
                "Journal parse failed",
                str(exc),
                default_action="HOLD",
            )
        )

    # Daily errors file — only flag when count > 0 (empty template is noise)
    err_path = ROOT / "logs" / f"daily_errors_{date.today().isoformat()}.md"
    if err_path.is_file():
        err_txt = err_path.read_text(encoding="utf-8", errors="replace")
        count_today = 0
        for line in err_txt.splitlines():
            if line.lower().startswith("**count today:**"):
                try:
                    count_today = int("".join(ch for ch in line.split(":", 1)[1] if ch.isdigit()) or "0")
                except ValueError:
                    count_today = 0
                break
        has_errors = count_today > 0 or (
            "No errors logged today" not in err_txt and "_None._" not in err_txt and len(err_txt) > 200
        )
        if has_errors and count_today > 0:
            pack.findings.append(
                Finding(
                    "daily_errors_present",
                    "cleanup",
                    f"Today's error log has {count_today} error(s)",
                    "Review obvious repeats; do not retune strategy from errors alone.",
                    default_action="HOLD",
                    evidence=str(err_path),
                )
            )

    # Attribution freshness
    attr = ROOT / "scripts" / "analysis" / "forward_sleeve_attr_last.md"
    if attr.is_file():
        age_h = (now.timestamp() - attr.stat().st_mtime) / 3600.0
        if age_h > 48:
            pack.findings.append(
                Finding(
                    "attribution_stale",
                    "cleanup",
                    f"Sleeve attribution stale ({age_h:.0f}h)",
                    "Run: python scripts/analysis/forward_sleeve_attribution.py",
                    default_action="CONFIRM",
                    evidence=str(attr),
                )
            )
    else:
        pack.findings.append(
            Finding(
                "attribution_missing",
                "cleanup",
                "No forward_sleeve_attr_last.md",
                "Run attribution once this week for freeze measurement.",
                default_action="CONFIRM",
                evidence="scripts/analysis/forward_sleeve_attribution.py",
            )
        )

    return pack


def optional_ollama_narrative(context: str) -> str | None:
    """Analyst-only Ollama memo. Never returns config edits."""
    if not freeze_ollama_enabled():
        return None
    try:
        from modules.ollama_client import ollama_available, ollama_complete

        if not ollama_available():
            return None
        prompt = (
            "You are a research analyst, not a portfolio manager.\n"
            "Output: (1) what the locked paper book did, (2) any ops anomalies, "
            "(3) macro context only.\n"
            "Do NOT recommend new indicators, risk increases, or live changes.\n"
            "Do NOT invent fills or metrics not in the inputs.\n"
            "Do NOT propose .env edits.\n\n"
            f"INPUTS:\n{context[:12000]}"
        )
        text, _model = ollama_complete(prompt, temperature=0.2)
        return (text or "").strip() or None
    except Exception as exc:
        return f"(ollama narrative skipped: {exc})"


def pack_to_dict(pack: HygienePack) -> dict[str, Any]:
    return {
        "generated_at": pack.generated_at,
        "equity": pack.equity,
        "regime": pack.regime,
        "heartbeat_age_min": pack.heartbeat_age_min,
        "notes": pack.notes,
        "findings": [asdict(f) for f in pack.findings],
    }


def last_saturday(today: date | None = None) -> date:
    d = today or date.today()
    return d - timedelta(days=(d.weekday() - 5) % 7)
