"""Felix & Friends YouTube transcripts: sync, store, optional wisdom blend."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
from modules.sentiment_keywords import score_text_sentiment

ROOT = Path(__file__).resolve().parents[1]
SYNC_STATE_FILE = "sentiment/sources/youtube/felix_and_friends/sync_state.json"


def _manifest_path() -> Path:
    return ROOT / config.FELIX_MANIFEST_FILE


def _transcripts_dir() -> Path:
    path = ROOT / config.FELIX_TRANSCRIPTS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sync_state_path() -> Path:
    path = ROOT / SYNC_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _parse_published(raw) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) == 8 and s.isdigit():
        return datetime.strptime(s, "%Y%m%d")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_manifest() -> list[dict]:
    path = _manifest_path()
    if not path.is_file():
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def score_transcript_text(text: str) -> float:
    return score_text_sentiment(text)


def _row_as_of(rows: list[dict], ts) -> dict | None:
    """Newest manifest row known at backtest bar time (published or synced_at)."""
    if not rows:
        return None
    bar = pd.Timestamp(ts)
    if bar.tz is not None:
        bar = bar.tz_convert(None)
    eligible: list[tuple[datetime, dict]] = []
    for row in rows:
        dt = _parse_published(row.get("published"))
        if dt is None and row.get("synced_at"):
            try:
                dt = datetime.fromisoformat(
                    str(row["synced_at"]).replace("Z", "+00:00")
                )
            except ValueError:
                dt = None
        if dt is None:
            continue
        known = dt.replace(tzinfo=None) if dt.tzinfo else dt
        if known <= bar.to_pydatetime():
            eligible.append((known, row))
    if not eligible:
        return None
    eligible.sort(key=lambda x: x[0], reverse=True)
    return eligible[0][1]


def felix_sentiment_as_of(ts, max_age_days: int | None = None) -> dict | None:
    """Felix mood available at a historical bar (no lookahead past ts)."""
    if not config.FELIX_SENTIMENT_ENABLED:
        return None
    rows = load_manifest()
    row = _row_as_of(rows, ts)
    if not row:
        return None
    window = (
        max_age_days
        if max_age_days is not None
        else config.FELIX_SENTIMENT_MAX_AGE_DAYS
    )
    if window > 0:
        bar = pd.Timestamp(ts)
        if bar.tz is not None:
            bar = bar.tz_convert(None)
        pub_dt = _parse_published(row.get("published"))
        if pub_dt is None and row.get("synced_at"):
            try:
                pub_dt = datetime.fromisoformat(
                    str(row["synced_at"]).replace("Z", "+00:00")
                )
            except ValueError:
                pub_dt = None
        if pub_dt:
            pub_naive = pub_dt.replace(tzinfo=None) if pub_dt.tzinfo else pub_dt
            age_days = (bar.to_pydatetime() - pub_naive).days
            if age_days > window:
                return None
    return row


def latest_felix_sentiment(max_age_days: int | None = None) -> dict | None:
    """Newest manifest entry within max_age_days (default config)."""
    if not config.FELIX_SENTIMENT_ENABLED:
        return None
    rows = load_manifest()
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("published") or "", reverse=True)
    latest = rows[0]
    window = (
        max_age_days
        if max_age_days is not None
        else config.FELIX_SENTIMENT_MAX_AGE_DAYS
    )
    pub_dt = _parse_published(latest.get("published"))
    if pub_dt and window > 0:
        now = datetime.now(pub_dt.tzinfo) if pub_dt.tzinfo else datetime.now()
        pub_naive = pub_dt.replace(tzinfo=None) if pub_dt.tzinfo else pub_dt
        age_days = (now.replace(tzinfo=None) - pub_naive).days
        if age_days > window:
            return None
    return latest


def apply_felix_web_blend(headline_web: float | None) -> tuple[float | None, dict | None]:
    """Blend headline web mood with latest Felix transcript keyword sentiment."""
    if not config.FELIX_SENTIMENT_ENABLED:
        return headline_web, None
    felix = latest_felix_sentiment()
    if not felix:
        return headline_web, None

    felix_score = float(felix.get("sentiment", 0))
    w = config.FELIX_SENTIMENT_BLEND_WEIGHT
    meta = {
        "video_id": felix.get("video_id"),
        "title": felix.get("title"),
        "published": felix.get("published"),
        "sentiment": felix_score,
        "blend_weight": w,
    }
    if headline_web is None:
        meta["blended_web"] = felix_score
        return felix_score, meta

    blended = round((1 - w) * float(headline_web) + w * felix_score, 4)
    meta["headline_web"] = round(float(headline_web), 4)
    meta["blended_web"] = blended
    return blended, meta


def _existing_ids(manifest: Path) -> set[str]:
    if not manifest.is_file():
        return set()
    ids: set[str] = set()
    with open(manifest, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                ids.add(json.loads(line).get("video_id", ""))
    return ids


def _list_videos(channel_url: str, max_videos: int) -> list[dict]:
    import sys

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        str(max_videos),
        channel_url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "yt-dlp failed")
    entries: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload.get("entries"), list):
            entries.extend(e for e in payload["entries"] if isinstance(e, dict))
        else:
            entries.append(payload)
    if not entries and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
            if isinstance(payload.get("entries"), list):
                entries = [e for e in payload["entries"] if isinstance(e, dict)]
            elif isinstance(payload, dict):
                entries = [payload]
        except json.JSONDecodeError:
            pass
    return entries[:max_videos]


def _fetch_upload_date(video_id: str) -> str | None:
    """YYYYMMDD from yt-dlp metadata (flat playlist omits dates)."""
    import sys

    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--no-warnings",
        "--print",
        "upload_date",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    return raw if len(raw) == 8 and raw.isdigit() else None


def _fetch_transcript(video_id: str) -> str | None:
    from youtube_transcript_api import YouTubeTranscriptApi

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id)
        snippets = getattr(fetched, "snippets", None) or fetched
        return " ".join(
            s.text if hasattr(s, "text") else s["text"] for s in snippets
        )
    except Exception:
        return None


def sync_felix_transcripts(
    *,
    max_videos: int | None = None,
    channel_url: str | None = None,
) -> dict:
    """Pull new channel videos + captions into sentiment/sources/."""
    max_n = max_videos if max_videos is not None else config.FELIX_SYNC_MAX_VIDEOS
    channel = channel_url or config.FELIX_YOUTUBE_CHANNEL_URL
    transcripts_dir = _transcripts_dir()
    manifest_path = _manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    known = _existing_ids(manifest_path)
    entries = _list_videos(channel, max_n)

    added = 0
    skipped = 0
    errors: list[str] = []
    with open(manifest_path, "a", encoding="utf-8") as manifest:
        for entry in entries:
            vid = entry.get("id") or entry.get("url", "").split("v=")[-1]
            if not vid or vid in known:
                skipped += 1
                continue
            try:
                text = _fetch_transcript(vid)
            except ImportError:
                raise RuntimeError(
                    "youtube-transcript-api not installed (pip install youtube-transcript-api)"
                ) from None
            if not text:
                skipped += 1
                continue
            out_file = transcripts_dir / f"{vid}.txt"
            out_file.write_text(text, encoding="utf-8")
            sentiment = round(score_transcript_text(text), 4)
            published = entry.get("upload_date") or entry.get("release_date")
            if not published:
                published = _fetch_upload_date(vid)
            row = {
                "video_id": vid,
                "title": entry.get("title", ""),
                "published": published,
                "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sentiment": sentiment,
                "transcript_file": str(out_file.relative_to(ROOT)).replace("\\", "/"),
                "chars": len(text),
            }
            manifest.write(json.dumps(row) + "\n")
            known.add(vid)
            added += 1

    result = {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "manifest": str(manifest_path.relative_to(ROOT)),
        "errors": errors,
    }
    with open(_sync_state_path(), "w", encoding="utf-8") as f:
        json.dump(
            {
                "last_sync_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **result,
            },
            f,
            indent=2,
        )
    return result


def backfill_manifest_published_dates() -> dict:
    """Fill missing published (YYYYMMDD) on existing manifest rows."""
    manifest_path = _manifest_path()
    if not manifest_path.is_file():
        return {"ok": False, "error": "manifest missing"}
    rows: list[dict] = []
    updated = 0
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    for row in rows:
        if row.get("published"):
            continue
        vid = row.get("video_id")
        if not vid:
            continue
        pub = _fetch_upload_date(vid)
        if pub:
            row["published"] = pub
            updated += 1
    with open(manifest_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return {"ok": True, "total": len(rows), "updated": updated}


def maybe_sync_felix_transcripts(force: bool = False) -> dict | None:
    """Run channel sync when FELIX_SYNC_ENABLED and interval elapsed."""
    if not config.FELIX_SYNC_ENABLED:
        return None
    state_path = _sync_state_path()
    if not force and state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            last = state.get("last_sync_at")
            if last:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                age_h = (
                    datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)
                ).total_seconds() / 3600
                if age_h < config.FELIX_SYNC_INTERVAL_HOURS:
                    return None
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    try:
        return sync_felix_transcripts()
    except FileNotFoundError:
        return {"ok": False, "error": "yt-dlp not installed (pip install yt-dlp)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
