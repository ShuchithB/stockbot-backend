# app/fast_swing_strategy.py
# Fully self-contained file. No missing pieces.
# Drop into /app folder.

import math
import pandas as pd
import numpy as np
from datetime import timedelta

# -----------------------------------------------------
# Helper: add indicators
# -----------------------------------------------------
def add_indicators(df,
                   MA_SHORT=21, MA_LONG=200,
                   RSI_PERIOD=21,
                   MACD_FAST=12, MACD_SLOW=26, MACD_SIGNAL=9,
                   ATR_PERIOD=14):

    df = df.copy()

    # MA
    df["ma_short"] = df["close"].rolling(MA_SHORT).mean()
    df["ma_long"] = df["close"].rolling(MA_LONG).mean()

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()

    rs = avg_gain / (avg_loss.replace(0, np.nan))
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema_fast = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # ATR
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    # Volume 20
    df["vol20"] = df["volume"].rolling(20).mean()

    return df

# -----------------------------------------------------
# Sanitizer for MongoDB / JSON
# -----------------------------------------------------
def safe(val):
    """Convert numpy types to pure Python + remove NaN/inf."""
    if val is None:
        return None
    try:
        if isinstance(val, (np.floating, np.float32, np.float64)):
            val = float(val)
        if isinstance(val, (np.integer, np.int32, np.int64)):
            val = int(val)
    except:
        pass
    # remove nan / inf
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
    return val

# -----------------------------------------------------
# Main strategy
# -----------------------------------------------------
def run_fast_swing_backtest(
        API_KEY=None,
        ACCESS_TOKEN=None,
        START_DATE="2024-01-01",
        END_DATE="2025-01-01",
        NIFTY100_FILE="nifty100.csv",
        fetch_historical=None,
        universe=None,
        INTERVAL="day",
        INITIAL_CAPITAL=1_000_000,
        MAX_POSITIONS=5,
        RISK_PER_TRADE=0.0075,
        ATR_MULT=2.5,
        SLIPPAGE=0.0005,
        TRANSACTION_COST=0.0015,
        RSI_ENTER=55,
        RSI_EXIT=45,
        REQUIRE_TREND=False,
        MAX_HOLD_DAYS=7
):
    # -------- Load symbol universe --------
    if universe is None:
        try:
            uni_df = pd.read_csv(NIFTY100_FILE)
            universe = uni_df["Symbol"].tolist()
        except Exception:
            universe = ["RELIANCE", "INFY", "HDFCBANK", "ICICIBANK"]

    # -------- Load historical data --------
    data_cache = {}
    for sym in universe:
        df = None

        if callable(fetch_historical):
            df = fetch_historical(sym, start=START_DATE, end=END_DATE, interval=INTERVAL)

        if df is None:
            continue

        # Convert list → DataFrame
        if isinstance(df, list):
            df = pd.DataFrame(df)

        if "date" not in df.columns:
            continue

        df = df.sort_values("date").reset_index(drop=True)
        df = add_indicators(df)
        data_cache[sym] = df

    if not data_cache:
        return {"trades": [], "equity": []}

    # -------- Build date calendar --------
    all_dates = sorted({d.date() for df in data_cache.values() for d in df["date"]})
    all_dates = [pd.Timestamp(d) for d in all_dates]

    equity = INITIAL_CAPITAL
    positions = {}  # {sym: {...}}
    trades = []
    equity_curve = []

    # -------- Helper: close position --------
    def close_position(sym, price, date):
        nonlocal equity
        if sym not in positions:
            return

        pos = positions.pop(sym)
        qty = pos["qty"]
        entry_price = pos["entry_price"]

        pnl = (price - entry_price) * qty
        cost = (abs(price) + abs(entry_price)) * qty * TRANSACTION_COST
        net = pnl - cost
        equity += net

        trades.append({
            "Symbol": sym,
            "Date": str(date.date()),
            "Action": "SELL",
            "Price": safe(price),
            "Qty": int(qty),
            "PnL": safe(net)
        })

    # ---------------------------------------------------
    # DAILY LOOP
    # ---------------------------------------------------
    for current_date in all_dates:

        # ---- Update equity mark-to-market ----
        unreal = 0
        for sym, pos in positions.items():
            df = data_cache[sym]
            row = df[df["date"].dt.date == current_date.date()]
            if not row.empty:
                price = float(row.iloc[0]["close"])
                unreal += (price - pos["entry_price"]) * pos["qty"]

        equity_curve.append({
            "date": str(current_date.date()),
            "portfolio_equity": safe(equity + unreal)
        })

        # ---- Exit rules ----
        for sym in list(positions.keys()):
            df = data_cache[sym]
            row = df[df["date"].dt.date == current_date.date()]
            if row.empty:
                continue

            row = row.iloc[0]
            price = float(row["close"])

            # Update trailing stop
            atr = safe(row["atr"])
            if atr:
                new_trail = price - ATR_MULT * atr
                if new_trail > positions[sym]["trail_stop"]:
                    positions[sym]["trail_stop"] = new_trail

            # Exit conditions
            exit_trail = price < positions[sym]["trail_stop"]
            exit_rsi = safe(row["rsi"]) is not None and row["rsi"] < RSI_EXIT
            exit_macd = safe(row["macd"]) is not None and row["macd"] < row["macd_signal"]
            exit_time = (current_date - positions[sym]["entry_date"]).days >= MAX_HOLD_DAYS

            if exit_trail or exit_rsi or exit_macd or exit_time:
                close_position(sym, price * (1 - SLIPPAGE), current_date)

        # ---- Entry rules ----
        free_slots = MAX_POSITIONS - len(positions)
        if free_slots > 0:

            candidates = []
            for sym, df in data_cache.items():
                if sym in positions:
                    continue

                row = df[df["date"].dt.date == current_date.date()]
                if row.empty:
                    continue
                row = row.iloc[0]

                price = safe(row["close"])
                atr = safe(row["atr"])
                rsi = safe(row["rsi"])
                ma_short = safe(row["ma_short"])
                macd = safe(row["macd"])
                macd_signal = safe(row["macd_signal"])

                if None in (price, atr, rsi, macd, macd_signal, ma_short):
                    continue

                if REQUIRE_TREND:
                    if safe(row["ma_long"]) is None or ma_short < row["ma_long"]:
                        continue

                cond_ma = price > ma_short
                cond_rsi = rsi > RSI_ENTER
                cond_macd = macd > macd_signal

                # Volume filter
                vol_ok = True
                if safe(row.get("vol20", None)):
                    vol_ok = row["volume"] > 1.2 * row["vol20"]

                if cond_ma and cond_rsi and cond_macd and vol_ok:
                    candidates.append((sym, atr, price))

            # Sort by ATR (high volatility first)
            candidates.sort(key=lambda x: x[1], reverse=True)

            for sym, atr, price in candidates[:free_slots]:

                stop_dist = atr * ATR_MULT
                risk_amt = equity * RISK_PER_TRADE
                qty = max(int(risk_amt // stop_dist), 1)

                entry_price = price * (1 + SLIPPAGE)
                trail_stop = entry_price - ATR_MULT * atr

                positions[sym] = {
                    "qty": qty,
                    "entry_price": entry_price,
                    "entry_date": current_date,
                    "trail_stop": trail_stop
                }

                trades.append({
                    "Symbol": sym,
                    "Date": str(current_date.date()),
                    "Action": "BUY",
                    "Price": safe(entry_price),
                    "Qty": int(qty)
                })

    # Close all at end
    last_date = all_dates[-1]
    for sym in list(positions.keys()):
        df = data_cache[sym]
        price = float(df.iloc[-1]["close"])
        close_position(sym, price * (1 - SLIPPAGE), last_date)

    return {
        "trades": trades,
        "equity": equity_curve
    }
