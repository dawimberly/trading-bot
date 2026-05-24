"""Sync daily yfinance bars into SQLite."""

import sqlite3

import yfinance as yf

import config


def update_database(tickers):
    conn = sqlite3.connect(config.DB_PATH)
    for ticker in tickers:
        print(f"Syncing {ticker}...")
        df = yf.download(ticker, period='1y', interval='1d', progress=False)
        df.to_sql(ticker, conn, if_exists='replace')
    conn.close()