# app/swing_strategy.py

import os
import math
import time
import numpy as np
import pandas as pd
from kiteconnect import KiteConnect
from datetime import datetime, timedelta

# ------------- Strategy Parameters -------------
MA_SHORT = 21
MA_LONG = 200
RSI_PERIOD = 21
RSI_ENTER = 55
RSI_EXIT = 45
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_MULT = 1.2
ATR_PERIOD = 14
ATR_MULT = 2.5
RISK_PER_TRADE = 0.0075
TRANSACTION_COST = 0.0015
SLIPPAGE = 0.0005
INITIAL_CAPITAL = 1_000_000
MAX_HOLD_DAYS = 15
REQUIRE_MA_LONG = True
USE_PIVOT_CONFIRM = False
# -------------------------------------------------


def run_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE):

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

    # ------------------- Load Symbols -------------------
    if os.path.exists(NIFTY100_FILE):
        symbols = pd.read_csv(NIFTY100_FILE)["Symbol"].tolist()
    else:
        symbols = ["RELIANCE", "INFY", "TCS", "HDFCBANK", "ICICIBANK", "SBIN"]

    # -----------------------------------------------------
    def fetch_historical(symbol):
        try:
            inst = kite.ltp(f"NSE:{symbol}")
            token = list(inst.values())[0]["instrument_token"]

            raw = kite.historical_data(
                instrument_token=token,
                from_date=START_DATE,
                to_date=END_DATE,
                interval="day"
            )
            if not raw:
                return pd.DataFrame()

            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
            return df

        except Exception:
            return pd.DataFrame()

    # -----------------------------------------------------
    def add_indicators(df):
        df = df.copy()
        df["ma_short"] = df["close"].rolling(MA_SHORT).mean()
        df["ma_long"] = df["close"].rolling(MA_LONG).mean()

        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(RSI_PERIOD).mean()
        avg_loss = loss.rolling(RSI_PERIOD).mean()
        rs = avg_gain / (avg_loss.replace(0, np.nan))
        df["rsi"] = 100 - (100 / (1 + rs))

        ema_fast = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
        ema_slow = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
        df["macd"] = ema_fast - ema_slow
        df["macd_signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()

        high = df["high"]
        low = df["low"]
        prev = df["close"].shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev).abs(),
            (low - prev).abs()
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(ATR_PERIOD).mean()

        df["vol20"] = df["volume"].rolling(20).mean()

        return df

    # -----------------------------------------------------
    def backtest_symbol(df, symbol):
        df = add_indicators(df)
        equity = INITIAL_CAPITAL
        position = 0
        entry = None
        trades = []
        eq_curve = []

        for i in range(len(df)):
            row = df.iloc[i]
            date = row["date"]
            price = row["close"]

            eq_curve.append({"date": date, "equity": equity})

            if np.isnan(row["atr"]): 
                continue

            if position == 0:
                cond1 = price > row["ma_short"]
                cond2 = row["rsi"] > RSI_ENTER
                cond3 = row["macd"] > row["macd_signal"]
                cond4 = row["volume"] > VOL_MULT * row["vol20"]

                if cond1 and cond2 and cond3 and cond4:
                    entry = price * (1 + SLIPPAGE)
                    position = max(int((equity * RISK_PER_TRADE) / (row["atr"] * ATR_MULT)), 1)
                    trades.append({
                        "Symbol": symbol, "Date": date, "Action": "BUY",
                        "Price": entry, "Qty": position
                    })

            else:
                exit_cond = (
                    price < row["ma_short"] or
                    row["rsi"] < RSI_EXIT or
                    row["macd"] < row["macd_signal"]
                )

                if exit_cond:
                    exit_price = price * (1 - SLIPPAGE)
                    pnl = (exit_price - entry) * position
                    equity += pnl

                    trades.append({
                        "Symbol": symbol, "Date": date, "Action": "SELL",
                        "Price": exit_price,
                        "Qty": position,
                        "PnL": pnl
                    })

                    position = 0
                    entry = None

        eq_df = pd.DataFrame(eq_curve)
        return pd.DataFrame(trades), eq_df

    # ------------- Run full portfolio -------------
    all_trades = []
    ecurves = []

    for s in symbols:
        df = fetch_historical(s)
        if df.empty: 
            continue

        t, e = backtest_symbol(df, s)
        if not t.empty:
            all_trades.append(t)

        e = e.rename(columns={"equity": s})
        ecurves.append(e[["date", s]])

    if not ecurves:
        return {"error": "No data"}

    # merge equity curves
    port = ecurves[0]
    for e in ecurves[1:]:
        port = port.merge(e, on="date", how="outer")

    cols = [c for c in port.columns if c != "date"]
    port["portfolio_equity"] = port[cols].mean(axis=1)

    trades = pd.concat(all_trades, ignore_index=True)

    return {
        "trades": trades.to_dict(orient="records"),
        "equity": port[["date", "portfolio_equity"]].to_dict(orient="records")
    }
