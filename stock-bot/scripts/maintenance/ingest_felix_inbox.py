#!/usr/bin/env python3
"""Ingest manual VidScript TXT drops into Felix/Andrei transcript store.

VidScript (https://www.vidscript.co/) free tier = 3 TXT/day — human download only.
Do NOT scrape the site (ToS forbids bots).

  1. Paste YouTube URL on VidScript → Download TXT
  2. Save as:
       sentiment/sources/youtube/felix_and_friends/inbox/{video_id}.txt
     or:
       sentiment/sources/youtube/felix_and_friends/inbox/{Title}__{video_id}.txt
  3. Run:
       python scripts/maintenance/ingest_felix_inbox.py
       python scripts/maintenance/ingest_felix_inbox.py --channel andrei_jikh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.felix_sentiment import ingest_inbox_transcripts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--channel",
        default="felix_and_friends",
        help="Channel id (felix_and_friends, andrei_jikh)",
    )
    ap.add_argument(
        "--published",
        default=None,
        help="Optional YYYYMMDD publish date (default: today)",
    )
    args = ap.parse_args()
    result = ingest_inbox_transcripts(args.channel, published=args.published)
    if not result.get("ok"):
        print(f"FAIL: {result.get('error', result)}")
        return 1
    print(
        f"{result.get('channel_name', args.channel)}: "
        f"added {result.get('added', 0)}, skipped {result.get('skipped', 0)}"
    )
    for vid in result.get("ingested") or []:
        print(f"  ingested {vid}")
    for err in result.get("errors") or []:
        print(f"  warn: {err}")
    print(f"inbox: {result.get('inbox')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
