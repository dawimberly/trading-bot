"""Trading books — multiple platforms/accounts under one portal login."""

from __future__ import annotations

from typing import TypedDict


class BookSpec(TypedDict, total=False):
    label: str
    platform: str
    enabled: bool
    coming_soon: bool
    default_paper: bool
    paper_chase: bool
    allow_live_default: bool


BOOKS: dict[str, BookSpec] = {
    "alpaca_live": {
        "label": "Alpaca Live",
        "platform": "alpaca",
        "enabled": True,
        "default_paper": False,
        "paper_chase": False,
        "allow_live_default": True,
    },
    "alpaca_paper": {
        "label": "Alpaca Paper",
        "platform": "alpaca",
        "enabled": True,
        "default_paper": True,
        "paper_chase": True,
        "allow_live_default": False,
    },
    "kraken": {
        "label": "Kraken",
        "platform": "kraken",
        "enabled": False,
        "coming_soon": True,
        "default_paper": False,
        "paper_chase": False,
        "allow_live_default": False,
    },
}

DEFAULT_BOOK_ID = "alpaca_live"


def list_books() -> list[tuple[str, BookSpec]]:
    return [(book_id, BOOKS[book_id]) for book_id in BOOKS]


def book_label(book_id: str) -> str:
    spec = BOOKS.get(book_id)
    return spec["label"] if spec else book_id


def book_enabled(book_id: str) -> bool:
    spec = BOOKS.get(book_id)
    return bool(spec and spec.get("enabled"))


def default_env_prefs(book_id: str) -> dict[str, bool]:
    spec = BOOKS.get(book_id) or BOOKS[DEFAULT_BOOK_ID]
    return {
        "paper": bool(spec.get("default_paper", True)),
        "allow_live": bool(spec.get("allow_live_default", False)),
    }


def book_dropdown_entries() -> list[tuple[str, str, bool]]:
    """(menu label, book_id, selectable) — extensible for future platforms."""
    entries: list[tuple[str, str, bool]] = []
    for book_id, spec in list_books():
        label = spec["label"]
        enabled = bool(spec.get("enabled")) and not spec.get("coming_soon")
        if spec.get("coming_soon"):
            label = f"{label} (coming soon)"
        entries.append((label, book_id, enabled))
    return entries


def book_id_for_dropdown_label(label: str) -> str | None:
    for menu_label, book_id, _ in book_dropdown_entries():
        if menu_label == label:
            return book_id
    return None


def dropdown_label_for_book(book_id: str) -> str:
    for menu_label, bid, _ in book_dropdown_entries():
        if bid == book_id:
            return menu_label
    return book_label(book_id)
