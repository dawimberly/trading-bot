"""Dashboard close/logout: title-bar X quits; Account menu has Log out / Close."""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "dashboard_app.py"


def _src() -> str:
    return APP.read_text(encoding="utf-8")


def _book_menu_src() -> str:
    src = _src()
    start = src.index("class BookMenu")
    end = src.index("class AlpacaKeysDialog")
    return src[start:end]


def _on_close_src() -> str:
    src = _src()
    start = src.index("    def _on_close(self)")
    end = src.index("\ndef main(")
    return src[start:end]


def test_dashboard_app_parses():
    ast.parse(_src())


def test_book_menu_does_not_modal_grab():
    text = _book_menu_src()
    assert "grab_set(" not in text
    assert 'text="Close dashboard"' in text
    assert 'text="Log out"' in text


def test_account_dropdown_has_logout_and_close():
    src = _src()
    assert 'values=["Account", "Log out", "Close dashboard"]' in src
    assert "def _on_account_menu(" in src


def test_visible_close_and_logout_buttons():
    src = _src()
    assert 'text="Close"' in src
    assert 'text="Log out"' in src
    assert "command=self._on_close" in src
    assert "command=self._on_logout_click" in src


def test_titlebar_x_always_shuts_down_not_tray():
    body = _on_close_src()
    assert "self._shutdown()" in body
    assert "withdraw(" not in body
    assert "_start_tray" not in body


def test_wm_delete_and_alt_f4_bound():
    src = _src()
    assert 'self.protocol("WM_DELETE_WINDOW", self._on_close)' in src
    assert 'self.bind("<Alt-F4>", lambda _e: self._on_close())' in src
