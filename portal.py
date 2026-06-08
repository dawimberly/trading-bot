"""Web portal: login → Alpaca keys → dashboard → bot control.

Friends (after git clone):
    friend_setup.bat        # Windows
    ./friend_setup.sh       # Mac/Linux

Or: streamlit run portal.py

Set PORTAL_INVITE_CODE in the environment to require an invite for registration.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import config
from modules.portal_auth import authenticate, init_db, register_user
from modules.portal_bot import bot_running, start_bot, stop_bot
from modules.portal_paths import (
    has_alpaca_config,
    user_env_path,
    user_heartbeat_path,
    user_journal_path,
    write_user_env,
)

st.set_page_config(
    page_title="PythonTrading Portal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _activate_user(username: str) -> None:
    config.reload_from_env(str(user_env_path(username)))


def _logout() -> None:
    st.session_state.pop("portal_user", None)


def _login_page() -> None:
    st.title("PythonTrading Portal")
    st.caption("Sign in to connect Alpaca and run the systematic fund bot.")
    init_db()
    tab_login, tab_register = st.tabs(["Log in", "Register"])
    with tab_login:
        with st.form("login"):
            user = st.text_input("Username")
            pwd = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", type="primary"):
                ok, name = authenticate(user, pwd)
                if ok and name:
                    st.session_state.portal_user = name
                    st.rerun()
                st.error("Invalid username or password.")
    with tab_register:
        invite_required = bool(os.getenv("PORTAL_INVITE_CODE", "").strip())
        with st.form("register"):
            new_user = st.text_input("Choose username")
            new_pwd = st.text_input("Choose password", type="password")
            new_pwd2 = st.text_input("Confirm password", type="password")
            invite = st.text_input(
                "Invite code" + (" (required)" if invite_required else " (optional)"),
                type="password",
            )
            if st.form_submit_button("Create account"):
                if new_pwd != new_pwd2:
                    st.error("Passwords do not match.")
                else:
                    ok, msg = register_user(new_user, new_pwd, invite_code=invite)
                    if ok:
                        st.success(msg + " Log in above.")
                    else:
                        st.error(msg)


def _alpaca_setup_page(username: str) -> None:
    st.title("Connect Alpaca")
    st.info(
        "Your API keys are stored only on this server in "
        f"`data/portal/users/{username}/.env` — not shared with other users."
    )
    with st.form("alpaca"):
        key = st.text_input("API Key ID", type="password")
        secret = st.text_input("API Secret", type="password")
        paper = st.checkbox("Paper trading", value=True)
        allow_live = st.checkbox("Allow live trading (ALLOW_LIVE_TRADING=yes)", value=False)
        st.divider()
        st.caption("Telegram alerts (optional)")
        tg_token = st.text_input("Telegram bot token", type="password")
        tg_chat = st.text_input("Telegram chat ID")
        if st.form_submit_button("Save & continue", type="primary"):
            if not key or not secret:
                st.error("API Key ID and Secret are required.")
            elif not paper and not allow_live:
                st.error("Enable paper trading or allow live trading.")
            else:
                write_user_env(
                    username,
                    api_key=key,
                    api_secret=secret,
                    paper=paper,
                    allow_live=allow_live,
                    telegram_token=tg_token,
                    telegram_chat=tg_chat,
                )
                st.success("Alpaca credentials saved.")
                st.rerun()


def _dashboard_page(username: str) -> None:
    _activate_user(username)
    heartbeat = _load_json(user_heartbeat_path(username))
    st.title("Dashboard")
    if heartbeat is None:
        st.warning("No heartbeat yet — start the bot from the **Bot** page.")
        return

    equity = float(heartbeat.get("equity") or 0)
    cash = float(heartbeat.get("cash") or 0)
    if equity > 0:
        config.configure_account_profile(equity)
    small = equity > 0 and config.is_small_account(equity)

    if small:
        st.error("SMALL ACCOUNT MODE — 1% risk · 90% VTI · $10 max order")
    if not config.PAPER_TRADING:
        st.error(f"LIVE TRADING — Equity ${equity:,.2f}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${equity:,.2f}")
    c2.metric("Cash", f"${cash:,.2f}")
    c3.metric("Regime", str(heartbeat.get("regime", "—")).split(":")[-1].strip())
    c4.metric("Status", "HALTED" if heartbeat.get("halted") else "Running")

    exposure = heartbeat.get("sleeve_exposure") or {}
    caps = heartbeat.get("sleeve_caps") or {}
    if exposure:
        st.subheader("Allocation")
        rows = []
        for label, key in (
            ("VTI", "vti_core"),
            ("SPY", "spy"),
            ("Crypto", "crypto"),
            ("NYSE", "nyse"),
        ):
            val = float(exposure.get(f"{key}_value") or 0)
            cap = float(exposure.get(f"{key}_cap") or 0)
            rows.append(
                {
                    "Sleeve": label,
                    "Value": f"${val:,.2f}",
                    "Cap": f"${cap:,.2f}",
                    "Target %": f"{float(caps.get(key) or 0) * 100:.1f}%",
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.caption(f"Last cycle: {heartbeat.get('timestamp', '—')}")


def _run_fetch_data() -> tuple[bool, str]:
    import subprocess
    import sys

    from modules.portal_paths import PROJECT_ROOT

    if sys.platform == "win32":
        python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        python = Path(sys.executable)
    try:
        subprocess.run(
            [str(python), str(PROJECT_ROOT / "fetch_data.py")],
            cwd=str(PROJECT_ROOT),
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        return True, "Market data downloaded to market_data.db"
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or exc.stdout or str(exc))[-500:]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _bot_page(username: str) -> None:
    _activate_user(username)
    st.title("Trading bot")
    running = bot_running(username)
    st.metric("Bot status", "Running" if running else "Stopped")

    st.subheader("First-time setup")
    st.caption("Download price data once before starting the bot.")
    if st.button("Download market data"):
        with st.spinner("Fetching tickers (may take 1–2 minutes)..."):
            ok, msg = _run_fetch_data()
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Start bot", type="primary", disabled=running):
            ok, msg = start_bot(username)
            if ok:
                st.success(msg)
            else:
                st.warning(msg)
            st.rerun()
    with col2:
        if st.button("Stop bot", disabled=not running):
            ok, msg = stop_bot(username)
            if ok:
                st.success(msg)
            else:
                st.warning(msg)
            st.rerun()

    st.divider()
    st.markdown(
        """
        **What the bot does**
        - Runs `run_all.py` with **your** Alpaca keys and isolated logs
        - VTI core + SPY / crypto / NYSE sleeves (recommended stack)
        - 10% max drawdown halt; small accounts auto-use 1% risk

        **Before live:** run `python scripts/account/preflight.py` with your keys in the portal.
        """
    )


def _settings_page(username: str) -> None:
    st.title("Settings")
    if st.button("Update Alpaca keys"):
        user_env_path(username).unlink(missing_ok=True)
        st.rerun()
    if user_env_path(username).is_file():
        st.caption(f"Config file: `{user_env_path(username)}`")
    journal = user_journal_path(username)
    if journal.is_file():
        try:
            df = pd.read_csv(journal).tail(15).iloc[::-1]
            st.subheader("Recent journal")
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as exc:
            st.warning(f"Could not load journal: {exc}")


def main() -> None:
    init_db()
    user = st.session_state.get("portal_user")
    if not user:
        _login_page()
        return

    if not has_alpaca_config(user):
        _alpaca_setup_page(user)
        return

    st.sidebar.title(f"Hi, {user}")
    page = st.sidebar.radio(
        "Navigate",
        ["Dashboard", "Bot", "Settings"],
        label_visibility="collapsed",
    )
    st.sidebar.caption(f"Updated {datetime.now().strftime('%H:%M:%S')}")
    if st.sidebar.button("Log out"):
        _logout()
        st.rerun()

    if page == "Dashboard":
        _dashboard_page(user)
    elif page == "Bot":
        _bot_page(user)
    else:
        _settings_page(user)


if __name__ == "__main__":
    main()
