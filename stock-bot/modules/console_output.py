"""Windows-safe console output with optional Rich rendering."""

from __future__ import annotations

import sys

_BLANK = "-"


def ascii_safe(text: str) -> str:
    """Replace Unicode punctuation that breaks cp1252 consoles."""
    return (
        text.replace("\u2014", _BLANK)
        .replace("\u2013", _BLANK)
        .replace("\u00b1", "+/-")
        .replace("\u2192", "->")
    )


def safe_print(text: str = "", *, end: str = "\n", file=None) -> None:
    stream = file or sys.stdout
    enc = getattr(stream, "encoding", None) or ""
    if enc.lower() not in ("utf-8", "utf8", "cp65001"):
        text = ascii_safe(text)
    try:
        print(text, end=end, file=stream)
    except UnicodeEncodeError:
        print(ascii_safe(text), end=end, file=stream)


def print_table(text: str, *, title: str | None = None) -> None:
    """Print a pre-formatted text table; uses Rich when installed."""
    body = ascii_safe(text)
    try:
        from rich.console import Console

        console = Console(force_terminal=True, soft_wrap=False)
        if title:
            console.print(f"[bold]{title}[/bold]")
        console.print(body)
    except ImportError:
        if title:
            safe_print(title)
        safe_print(body)
