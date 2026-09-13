from pathlib import Path

p = Path(r"c:\Users\Owner\PythonTrading\stock-bot\run_all.py")
t = p.read_text(encoding="utf-8")

old_hb = """    try:
        from modules.time_of_day import heartbeat_tod_payload

        tod_hb = heartbeat_tod_payload()
        if tod_hb is not None:
            payload["time_of_day"] = tod_hb
    except Exception:
        pass
    if thinking_engine:"""

new_hb = """    try:
        from modules.time_of_day import heartbeat_tod_payload

        tod_hb = heartbeat_tod_payload()
        if tod_hb is not None:
            payload["time_of_day"] = tod_hb
    except Exception:
        pass
    try:
        from modules.daily_profit_banking import heartbeat_daily_bank_payload

        bank_hb = heartbeat_daily_bank_payload()
        if bank_hb is not None:
            payload["daily_bank"] = bank_hb
    except Exception:
        pass
    if thinking_engine:"""

if old_hb not in t:
    raise SystemExit("heartbeat anchor missing")
t = t.replace(old_hb, new_hb, 1)

# Insert daily bank update after daily loss circuit block (after set_entry_block)
old_eq = """    set_entry_block_for_cycle(dl_reason if dl_tripped else None)
    if dl_tripped:
"""
new_eq = """    set_entry_block_for_cycle(dl_reason if dl_tripped else None)

    if config.effective_daily_bank_enabled():
        try:
            from modules.daily_profit_banking import (
                format_daily_bank_banner,
                update_daily_bank,
            )

            update_daily_bank(equity)
            bank_banner = format_daily_bank_banner()
            if bank_banner and _main_cycle_count <= 2:
                print(f"--- {bank_banner} ---")
        except Exception as exc:
            _warn_nonfatal("Daily profit banking", exc)

    if dl_tripped:
"""
if old_eq not in t:
    raise SystemExit("equity update anchor missing")
t = t.replace(old_eq, new_eq, 1)

# Print banner near regime line when banked mid-session
old_reg = """    print(
        f"--- Regime: {display_regime} | Vol: {vol} | "
"""
new_reg = """    if config.effective_daily_bank_enabled():
        try:
            from modules.daily_profit_banking import (
                format_daily_bank_banner,
                is_banked,
                update_daily_bank,
            )

            update_daily_bank(equity)
            if is_banked():
                bb = format_daily_bank_banner()
                if bb:
                    print(f"--- {bb} ---")
        except Exception as exc:
            _warn_nonfatal("Daily profit banking refresh", exc)

    print(
        f"--- Regime: {display_regime} | Vol: {vol} | "
"""
if old_reg not in t:
    raise SystemExit("regime print anchor missing")
t = t.replace(old_reg, new_reg, 1)

p.write_text(t, encoding="utf-8")
print("run_all.py patched")
