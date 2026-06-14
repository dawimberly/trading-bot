"""Fighter and event name normalization for odds joins."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd


def clean_fighter_name(name: Any) -> str:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    text = str(name).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"\s*\(.*?\)\s*", " ", text).strip()
    return re.sub(r"\s+", " ", text)


def clean_date(value: Any) -> pd.Timestamp | pd.NaT:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return pd.NaT
    parsed = pd.to_datetime(value, errors="coerce", utc=False)
    if pd.isna(parsed):
        return pd.NaT
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert(None)
    return pd.Timestamp(parsed).normalize()


def normalize_event_name(event: Any) -> str:
    text = str(event or "").lower().strip()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fighters_same_person(a: str, b: str) -> bool:
    ka, kb = clean_fighter_name(a).lower(), clean_fighter_name(b).lower()
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    a_parts = set(ka.split())
    b_parts = set(kb.split())
    if len(a_parts.intersection(b_parts)) >= 2:
        return True
    a_last, b_last = ka.split()[-1], kb.split()[-1]
    return len(a_last) > 3 and a_last == b_last and (
        a_parts.intersection(b_parts) or ka.split()[0][0] == kb.split()[0][0]
    )
