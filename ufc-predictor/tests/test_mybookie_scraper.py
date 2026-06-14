"""Tests for MyBookie odds scraper."""

from __future__ import annotations

import pandas as pd
import pytest

import config
from src.odds_providers.mybookie_scraper import (
    _map_method_prop_description,
    _normalize_fighter_name,
    _parse_moneyline_rows,
    _parse_prop_buttons,
    _parse_totals_props,
    fetch_mybookie_odds,
    fetch_mybookie_prop_odds,
)


@pytest.fixture(autouse=True)
def enable_mybookie(monkeypatch):
    monkeypatch.setattr(config, "MYBOOKIE_ENABLED", True)


def test_normalize_fighter_name_last_first():
    assert _normalize_fighter_name("Lopes, Diego") == "Diego Lopes"
    assert _normalize_fighter_name("Michael Chandler") == "Michael Chandler"


def test_parse_moneyline_rows_from_sample_html():
    from bs4 import BeautifulSoup

    html = """
    <div class="game-line">
      <div class="container-fluid">
        <div class="game-line__home-team">
          <p class="game-line__home-team__name" title="Lopes, Diego">Lopes, Diego</p>
        </div>
        <div class="game-line__visitor-team">
          <p class="game-line__visitor-team__name" title="Garcia, Steve">Garcia, Steve</p>
        </div>
        <button class="lines-odds" data-markettype="ml" data-team="Lopes, Diego" data-odd="-145"></button>
        <button class="lines-odds" data-markettype="ml" data-team="Garcia, Steve" data-odd="118"></button>
      </div>
    </div>
    """
    soup = BeautifulSoup(html, "lxml")
    rows = _parse_moneyline_rows(soup)
    assert len(rows) == 1
    assert rows[0]["fighter_1"] == "Diego Lopes"
    assert rows[0]["fighter_2"] == "Steve Garcia"
    assert rows[0]["f1_odds"] > 1
    assert rows[0]["bookmaker"] == "MyBookie"


def test_parse_totals_props_over_under():
    from bs4 import BeautifulSoup

    html = """
    <div class="game-line">
      <div class="container-fluid">
        <div class="game-line__home-team">
          <p class="game-line__home-team__name" title="Lopes, Diego">Lopes, Diego</p>
        </div>
        <div class="game-line__visitor-team">
          <p class="game-line__visitor-team__name" title="Garcia, Steve">Garcia, Steve</p>
        </div>
        <button class="lines-odds" data-markettype="to" data-team="Garcia, Steve"
                data-team-vs="Lopes, Diego" data-odd="-156" data-points="1.5"
                data-gameid="123">O 1.5 -156</button>
        <button class="lines-odds" data-markettype="to" data-team="Lopes, Diego"
                data-team-vs="Garcia, Steve" data-odd="122" data-points="1.5"
                data-gameid="123">U 1.5 +122</button>
      </div>
    </div>
    """
    soup = BeautifulSoup(html, "lxml")
    props = _parse_totals_props(soup)
    keys = {p["prop_key"] for p in props}
    assert "over_1_5_rounds" in keys
    assert "round_1_finish" in keys


def test_fetch_mybookie_disabled_raises(monkeypatch):
    monkeypatch.setattr(config, "MYBOOKIE_ENABLED", False)
    with pytest.raises(Exception, match="disabled"):
        fetch_mybookie_odds(force_refresh=True)


def test_map_method_prop_description():
    mapped = _map_method_prop_description("Lopes, Diego by ko")
    assert mapped == ("fighter_ko", "Diego Lopes")
    mapped = _map_method_prop_description("Garcia, Steve by submission")
    assert mapped == ("fighter_sub", "Steve Garcia")
    assert _map_method_prop_description("Lopes, Diego by decision") is None
    assert _map_method_prop_description("Draw") is None


def test_parse_method_prop_buttons_from_sample_html():
    from bs4 import BeautifulSoup

    html = """
    <div class="game-line__home-team">
      <p class="game-line__home-team__name" title="Lopes, Diego">Lopes, Diego</p>
    </div>
    <div class="game-line__visitor-team">
      <p class="game-line__visitor-team__name" title="Garcia, Steve">Garcia, Steve</p>
    </div>
    <button class="lines-odds" data-markettype="ml" data-gameid="123"
            data-description="Lopes, Diego by ko" data-odd="3.48"
            data-team="Lopes, Diego" data-team-vs="Garcia, Steve">Lopes, Diego by ko +3</button>
    <button class="lines-odds" data-markettype="ml" data-gameid="123"
            data-description="Garcia, Steve by submission" data-odd="23.00"
            data-team="Garcia, Steve" data-team-vs="Lopes, Diego">Garcia, Steve by submission +23</button>
    """
    soup = BeautifulSoup(html, "lxml")
    props = _parse_prop_buttons(soup)
    keys = {(p["prop_key"], p["fighter_1"], p["fighter_2"]) for p in props}
    assert ("fighter_ko", "Diego Lopes", "Steve Garcia") in keys
    assert ("fighter_sub", "Diego Lopes", "Steve Garcia") in keys


def test_live_prop_scrape_returns_method_lines(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROPS", True)
    monkeypatch.setattr(config, "ODDS_CACHE_TTL_HOURS", 0)
    try:
        df = fetch_mybookie_prop_odds(force_refresh=True)
    except Exception as exc:
        pytest.skip(f"MyBookie live prop scrape unavailable: {exc}")
    assert not df.empty
    assert "fighter_ko" in set(df["prop_key"])


def test_live_scrape_returns_rows(monkeypatch):
    """Integration smoke: live MyBookie page when network available."""
    monkeypatch.setattr(config, "ODDS_CACHE_TTL_HOURS", 0)
    try:
        df = fetch_mybookie_odds(force_refresh=True)
    except Exception as exc:
        pytest.skip(f"MyBookie live scrape unavailable: {exc}")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert "fighter_1" in df.columns
