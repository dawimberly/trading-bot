from cloud_bot.config.profile import BEST_PAPER_ENV, apply_best_paper_profile
from cloud_bot.config.settings import load_settings


def test_load_settings_paths():
    s = load_settings()
    assert s.data_dir.name == "data"
    assert s.heartbeat_file.name == "cloud_bot_heartbeat.json"


def test_best_paper_profile_keys():
    env = apply_best_paper_profile({})
    assert env["PAPER_STAT_ARB_ENABLED"] == "true"
    assert env["PAPER_VOL_TRADING_ENABLED"] == "true"
    assert env["PAPER_DYNAMIC_RISK_ENABLED"] == "true"
    assert "PAPER_DYNAMIC_VTI" in BEST_PAPER_ENV
