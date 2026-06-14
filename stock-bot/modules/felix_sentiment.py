"""YouTube creator transcripts (Felix, Andrei Jikh, …): sync, store, wisdom/social blend."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
from modules.sentiment_keywords import (
    is_creator_channel,
    macro_bearish_keyword_hits,
    score_creator_transcript_sentiment,
    score_text_sentiment,
)

ROOT = Path(__file__).resolve().parents[1]


def _manifest_path(channel_id: str | None = None) -> Path:
    if channel_id:
        return ROOT / config.youtube_manifest_file(channel_id)
    return ROOT / config.FELIX_MANIFEST_FILE


def _transcripts_dir(channel_id: str) -> Path:
    path = ROOT / config.youtube_transcripts_dir(channel_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sync_state_path(channel_id: str) -> Path:
    path = ROOT / config.youtube_channel_dir(channel_id) / "sync_state.json"
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


def load_manifest(channel_id: str | None = None) -> list[dict]:
    path = _manifest_path(channel_id)
    if not path.is_file():
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                row = json.loads(line)
                if channel_id and "channel_id" not in row:
                    row["channel_id"] = channel_id
                rows.append(row)
    return rows


def load_all_manifests() -> list[dict]:
    rows: list[dict] = []
    for spec in config.youtube_channel_specs():
        for row in load_manifest(spec["id"]):
            tagged = dict(row)
            tagged.setdefault("channel_id", spec["id"])
            tagged.setdefault("channel_name", spec["name"])
            rows.append(tagged)
    return rows


def score_transcript_text(text: str, *, channel_name: str | None = None) -> float:
    score, _ = score_creator_transcript_sentiment(
        text, channel_name=channel_name, creator_boost=is_creator_channel(channel_name)
    )
    return score


def enrich_manifest_row(row: dict) -> dict:
    """Re-score from transcript/title; attach macro_bearish_hits for social sleeve."""
    out = dict(row)
    text = ""
    tf = row.get("transcript_file")
    if tf:
        path = ROOT / str(tf)
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
    if not text:
        text = str(row.get("title") or "")
    channel = row.get("channel_name") or row.get("channel_id")
    use_boost = config.paper_aggressive_context() and config.PAPER_SOCIAL_MACRO_BOOST_ENABLED
    if text.strip():
        if use_boost:
            score, hits = score_creator_transcript_sentiment(
                text,
                channel_name=str(channel) if channel else None,
            )
        else:
            score = score_text_sentiment(text, macro_weight=1.0)
            hits = macro_bearish_keyword_hits(text)
        out["sentiment"] = score
        out["macro_bearish_hits"] = hits
    else:
        out["macro_bearish_hits"] = macro_bearish_keyword_hits(str(row.get("title") or ""))
    return out


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


def _within_age(row: dict, *, max_age_days: int, ref: datetime) -> bool:
    if max_age_days <= 0:
        return True
    pub_dt = _parse_published(row.get("published"))
    if pub_dt is None and row.get("synced_at"):
        try:
            pub_dt = datetime.fromisoformat(
                str(row["synced_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            pub_dt = None
    if not pub_dt:
        return True
    pub_naive = pub_dt.replace(tzinfo=None) if pub_dt.tzinfo else pub_dt
    ref_naive = ref.replace(tzinfo=None) if ref.tzinfo else ref
    return (ref_naive - pub_naive).days <= max_age_days


def _latest_for_channel(
    channel_id: str,
    max_age_days: int | None = None,
    *,
    ref: datetime | None = None,
) -> dict | None:
    rows = load_manifest(channel_id)
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("published") or "", reverse=True)
    latest = rows[0]
    window = (
        max_age_days
        if max_age_days is not None
        else config.FELIX_SENTIMENT_MAX_AGE_DAYS
    )
    ref_dt = ref or datetime.now()
    if not _within_age(latest, max_age_days=window, ref=ref_dt):
        return None
    latest = dict(latest)
    latest.setdefault("channel_id", channel_id)
    return latest


def _blend_channel_rows(
    rows: list[dict],
    specs: list[dict],
) -> dict | None:
    if not rows:
        return None
    by_id = {s["id"]: s for s in specs}
    parts: list[tuple[float, float, dict]] = []
    for row in rows:
        cid = row.get("channel_id")
        spec = by_id.get(cid)
        if spec is None or row.get("sentiment") is None:
            continue
        parts.append((float(row["sentiment"]), float(spec["weight"]), row))
    if not parts:
        return None
    wsum = sum(w for _, w, _ in parts) or 1.0
    score = round(sum(s * w for s, w, _ in parts) / wsum, 4)
    newest = max(
        parts,
        key=lambda p: p[2].get("published") or p[2].get("synced_at") or "",
    )[2]
    macro_hits = max(int(r.get("macro_bearish_hits") or 0) for _, _, r in parts)
    return {
        "sentiment": score,
        "macro_bearish_hits": macro_hits,
        "video_id": newest.get("video_id"),
        "title": newest.get("title"),
        "published": newest.get("published"),
        "channel_id": newest.get("channel_id"),
        "channel_name": newest.get("channel_name"),
        "channels": [
            {
                "channel_id": r.get("channel_id"),
                "channel_name": r.get("channel_name") or by_id.get(r.get("channel_id"), {}).get("name"),
                "video_id": r.get("video_id"),
                "title": r.get("title"),
                "sentiment": r.get("sentiment"),
                "weight": by_id.get(r.get("channel_id"), {}).get("weight"),
            }
            for _, _, r in parts
        ],
    }


def felix_sentiment_as_of(ts, max_age_days: int | None = None) -> dict | None:
    """Creator mood available at a historical bar (no lookahead past ts)."""
    if not config.FELIX_SENTIMENT_ENABLED:
        return None
    specs = config.youtube_channel_specs()
    if not specs:
        return None
    window = (
        max_age_days
        if max_age_days is not None
        else config.FELIX_SENTIMENT_MAX_AGE_DAYS
    )
    bar = pd.Timestamp(ts)
    if bar.tz is not None:
        bar = bar.tz_convert(None)
    ref = bar.to_pydatetime()
    per_channel: list[dict] = []
    for spec in specs:
        row = _row_as_of(load_manifest(spec["id"]), ts)
        if not row:
            continue
        if not _within_age(row, max_age_days=window, ref=ref):
            continue
        tagged = dict(row)
        tagged = enrich_manifest_row(tagged)
        tagged["channel_id"] = spec["id"]
        tagged["channel_name"] = spec["name"]
        per_channel.append(tagged)
    return _blend_channel_rows(per_channel, specs)


def latest_felix_sentiment(max_age_days: int | None = None) -> dict | None:
    """Weighted blend of newest transcript per registered YouTube channel."""
    if not config.FELIX_SENTIMENT_ENABLED:
        return None
    specs = config.youtube_channel_specs()
    if not specs:
        return None
    per_channel: list[dict] = []
    for spec in specs:
        row = _latest_for_channel(spec["id"], max_age_days)
        if row:
            row = enrich_manifest_row(row)
            row["channel_name"] = spec["name"]
            per_channel.append(row)
    return _blend_channel_rows(per_channel, specs)


def apply_felix_web_blend(headline_web: float | None) -> tuple[float | None, dict | None]:
    """Blend headline web mood with latest creator transcript keyword sentiment."""
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
        "channel_id": felix.get("channel_id"),
        "channel_name": felix.get("channel_name"),
        "channels": felix.get("channels"),
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


def _resolve_channel_spec(
    *,
    channel_id: str | None = None,
    channel_url: str | None = None,
) -> dict | None:
    specs = config.youtube_channel_specs()
    if channel_id:
        for spec in specs:
            if spec["id"] == channel_id:
                return spec
        return None
    if channel_url:
        for spec in specs:
            if spec["url"].rstrip("/") == channel_url.rstrip("/"):
                return spec
        return {"id": "custom", "name": "Custom", "url": channel_url, "weight": 1.0}
    return None


def sync_channel_transcripts(
    channel_id: str,
    *,
    max_videos: int | None = None,
    channel_url: str | None = None,
) -> dict:
    """Pull new videos + captions for one registered channel."""
    spec = _resolve_channel_spec(channel_id=channel_id, channel_url=channel_url)
    if not spec:
        return {"ok": False, "error": f"unknown channel: {channel_id}"}
    cid = spec["id"]
    max_n = max_videos if max_videos is not None else config.FELIX_SYNC_MAX_VIDEOS
    channel = channel_url or spec["url"]
    transcripts_dir = _transcripts_dir(cid)
    manifest_path = _manifest_path(cid)
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
            sentiment = round(
                score_transcript_text(text, channel_name=spec.get("name")), 4
            )
            _, macro_hits = score_creator_transcript_sentiment(
                text, channel_name=spec.get("name")
            )
            published = entry.get("upload_date") or entry.get("release_date")
            if not published:
                published = _fetch_upload_date(vid)
            row = {
                "video_id": vid,
                "channel_id": cid,
                "channel_name": spec.get("name"),
                "title": entry.get("title", ""),
                "published": published,
                "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sentiment": sentiment,
                "macro_bearish_hits": macro_hits,
                "transcript_file": str(out_file.relative_to(ROOT)).replace("\\", "/"),
                "chars": len(text),
            }
            manifest.write(json.dumps(row) + "\n")
            known.add(vid)
            added += 1

    result = {
        "ok": True,
        "channel_id": cid,
        "channel_name": spec.get("name"),
        "added": added,
        "skipped": skipped,
        "manifest": str(manifest_path.relative_to(ROOT)),
        "errors": errors,
    }
    with open(_sync_state_path(cid), "w", encoding="utf-8") as f:
        json.dump(
            {
                "last_sync_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **result,
            },
            f,
            indent=2,
        )
    return result


def sync_felix_transcripts(
    *,
    max_videos: int | None = None,
    channel_url: str | None = None,
    channel_id: str | None = None,
) -> dict:
    """Pull new channel videos + captions into sentiment/sources/."""
    if channel_id or channel_url:
        spec = _resolve_channel_spec(channel_id=channel_id, channel_url=channel_url)
        if spec and spec["id"] != "custom":
            return sync_channel_transcripts(
                spec["id"], max_videos=max_videos, channel_url=channel_url
            )
        if channel_url:
            return sync_channel_transcripts(
                "felix_and_friends",
                max_videos=max_videos,
                channel_url=channel_url,
            )

    specs = config.youtube_channel_specs()
    if not specs:
        return {"ok": False, "error": "no youtube channels configured"}
    results = [
        sync_channel_transcripts(spec["id"], max_videos=max_videos)
        for spec in specs
    ]
    return {
        "ok": all(r.get("ok") for r in results),
        "added": sum(r.get("added", 0) for r in results),
        "skipped": sum(r.get("skipped", 0) for r in results),
        "channels": results,
    }


def backfill_manifest_published_dates(channel_id: str | None = None) -> dict:
    """Fill missing published (YYYYMMDD) on existing manifest rows."""
    targets = (
        [channel_id]
        if channel_id
        else [s["id"] for s in config.youtube_channel_specs()]
    )
    total = 0
    updated = 0
    for cid in targets:
        manifest_path = _manifest_path(cid)
        if not manifest_path.is_file():
            continue
        rows: list[dict] = []
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
        total += len(rows)
    return {"ok": True, "total": total, "updated": updated}


def maybe_sync_felix_transcripts(force: bool = False) -> dict | None:
    """Run channel sync when FELIX_SYNC_ENABLED and interval elapsed."""
    if not config.FELIX_SYNC_ENABLED:
        return None
    specs = config.youtube_channel_specs()
    if not specs:
        return None
    if not force:
        due = False
        for spec in specs:
            state_path = _sync_state_path(spec["id"])
            if not state_path.is_file():
                due = True
                break
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                last = state.get("last_sync_at")
                if not last:
                    due = True
                    break
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                age_h = (
                    datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)
                ).total_seconds() / 3600
                if age_h >= config.FELIX_SYNC_INTERVAL_HOURS:
                    due = True
                    break
            except (json.JSONDecodeError, OSError, ValueError):
                due = True
                break
        if not due:
            return None
    try:
        return sync_felix_transcripts()
    except FileNotFoundError:
        return {"ok": False, "error": "yt-dlp not installed (pip install yt-dlp)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
