"""Sync Felix & Friends YouTube transcripts into sentiment/sources/.

Requires: pip install yt-dlp youtube-transcript-api

Run:
  python scripts/maintenance/sync_felix_transcripts.py
  python scripts/maintenance/sync_felix_transcripts.py --max 50
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.felix_sentiment import backfill_manifest_published_dates, sync_felix_transcripts


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Felix YouTube transcripts")
    parser.add_argument("--max", type=int, default=None, help="Max videos to scan")
    parser.add_argument("--channel", default=None)
    parser.add_argument(
        "--backfill-dates",
        action="store_true",
        help="Fill missing upload dates on manifest rows (no new transcripts)",
    )
    args = parser.parse_args()

    if args.backfill_dates:
        try:
            result = backfill_manifest_published_dates()
        except Exception as exc:
            print(f"FAIL: {exc}")
            sys.exit(1)
        print(f"Updated {result.get('updated', 0)} / {result.get('total', 0)} manifest rows")
        return

    try:
        result = sync_felix_transcripts(
            max_videos=args.max,
            channel_url=args.channel or config.FELIX_YOUTUBE_CHANNEL_URL,
        )
    except FileNotFoundError:
        print("FAIL: yt-dlp not found. Run: pip install yt-dlp youtube-transcript-api")
        sys.exit(1)
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

    if not result.get("ok"):
        print(f"FAIL: {result.get('error', result)}")
        sys.exit(1)
    print(f"Added {result.get('added', 0)}, skipped {result.get('skipped', 0)}")
    print(f"Manifest: {result.get('manifest')}")


if __name__ == "__main__":
    main()
