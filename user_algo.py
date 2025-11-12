import os
import pandas as pd
import numpy as np
from typing import List, Tuple, Dict, Any
from .config import settings

# Strategy parameters
EMA_FAST = 20
EMA_MID  = 50
EMA_SLOW = 100
RSI_PERIOD = 14
ATR_PERIOD = 14
ATR_MULT = 2.5
MIN_ATR_PCT = 0.012
RISK_PER_TRADE = 0.0075
TRANSACTION_COST = 0.0015
SLIPPAGE = 0.0005
INITIAL_CAPITAL = 1_000_000

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_mid"] = df["close"].ewm(span=EMA_MID, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta = df["close"].diff()
    up = np.where(delta > 0, delta, 0)
    down = np.where(delta < 0, -delta, 0)
    roll_up = pd.Series(up).rolling(RSI_PERIOD).mean()
    roll_down = pd.Series(down).rolling(RSI_PERIOD).mean()
    rs = roll_up / roll_down
    df["rsi"] = 100 - (100 / (1 + rs))

    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()
    return df

def fetch_historical(symbol: str, start_date: str, end_date: str, kite_creds: Dict[str, str] = None) -> pd.DataFrame:
    """Fetch OHLC data via Kite API (if creds) or fallback CSV."""
    try:
        if kite_creds and kite_creds.get("api_key") and kite_creds.get("access_token"):
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=kite_creds["api_key"])
            kite.set_access_token(kite_creds["access_token"])
            inst = kite.ltp(f"NSE:{symbol}")
            token = list(inst.values())[0]["instrument_token"]
            data = kite.historical_data(
                instrument_token=token,
                from_date=start_date,
                to_date=end_date,
                interval="day"
            )
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"])
            df.sort_values("date", inplace=True)
            return df
    except Exception as e:
        print(f"⚠️ Kite fetch failed for {symbol}: {e}")

    csv_file = f"{symbol}.csv"
    if os.path.exists(csv_file):
        df = pd.read_csv(csv_file, parse_dates=["date"])
        df.sort_values("date", inplace=True)
        return df
    return pd.DataFrame()

def backtest_symbol(df: pd.DataFrame, symbol: str) -> Tuple[pd.DataFrame, float]:
    df = compute_indicators(df)
    equity = INITIAL_CAPITAL
    position = 0
    entry_price, trail_stop = 0, None
    trades = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        if pd.isna(row["atr"]) or row["atr"] == 0:
            continue
        atr_ratio = row["atr"] / row["close"]
        if atr_ratio < MIN_ATR_PCT:
            continue

        price, rsi = row["close"], row["rsi"]
        in_uptrend = row["ema_mid"] > row["ema_slow"]
        entry_cond = in_uptrend and rsi > 60 and price > row["ema_fast"]
        exit_cond = (rsi < 45 or price < row["ema_fast"])

        # Entry
        if position == 0 and entry_cond:
            risk_amt = equity * RISK_PER_TRADE
            stop_dist = row["atr"] * ATR_MULT
            qty = max(int(risk_amt / stop_dist), 1)
            entry_price = price * (1 + SLIPPAGE)
            trail_stop = entry_price - ATR_MULT * row["atr"]
            position = qty
            trades.append({
                "Symbol": symbol, "Date": row["date"], "Action": "BUY",
                "Qty": qty, "Price": entry_price
            })

        # Manage position
        elif position > 0:
            new_trail = price - ATR_MULT * row["atr"]
            if new_trail > trail_stop:
                trail_stop = new_trail
            if exit_cond or price < trail_stop:
                exit_price = price * (1 - SLIPPAGE)
                pnl = (exit_price - entry_price) * position
                cost = (entry_price + exit_price) * position * TRANSACTION_COST
                net_pnl = pnl - cost
                equity += net_pnl
                trades.append({
                    "Symbol": symbol, "Date": row["date"], "Action": "SELL",
                    "Qty": position, "Price": exit_price, "PnL": net_pnl
                })
                position = 0
                trail_stop = None

    return pd.DataFrame(trades), equity

def summarize_backtest(trades_df: pd.DataFrame) -> Dict[str, Any]:
    if trades_df.empty:
        return {"Total PnL": 0, "Win Rate %": 0, "Trades": 0}
    trades_df["PnL"] = trades_df.get("PnL", pd.Series([0]*len(trades_df))).fillna(0)
    wins = trades_df[trades_df["PnL"] > 0]["PnL"]
    losses = trades_df[trades_df["PnL"] < 0]["PnL"]
    total_pnl = trades_df["PnL"].sum()
    win_rate = (len(wins) / len(trades_df)) * 100 if len(trades_df)>0 else 0
    avg_win, avg_loss = wins.mean() if len(wins)>0 else 0, losses.mean() if len(losses)>0 else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else None
    return {"Total PnL": round(total_pnl,2), "Win Rate %": round(win_rate,2), "Trades": len(trades_df),
            "Avg Win": round(avg_win,2), "Avg Loss": round(avg_loss,2), "Reward:Risk": round(rr,2) if rr else None}

def run_once(symbols: List[str]=None, start_date=None, end_date=None, kite_creds: Dict[str,str]=None):
    if symbols is None:
        symbols = ["RELIANCE", "TCS", "INFY", "SBIN"]
    start_date = start_date or settings.START_DATE
    end_date = end_date or settings.END_DATE

    all_trades, equities = [], []
    for sym in symbols:
        df = fetch_historical(sym, start_date, end_date, kite_creds)
        if df.empty:
            continue
        trades_df, eq = backtest_symbol(df, sym)
        if not trades_df.empty:
            all_trades.append(trades_df)
            equities.append(eq)

    if not all_trades:
        return {"trades": [], "summary": {"Total PnL": 0, "Trades": 0}, "final_equity_avg": 0}

    all_trades_df = pd.concat(all_trades, ignore_index=True)
    summary = summarize_backtest(all_trades_df)
    avg_final_eq = float(np.mean(equities)) if equities else 0
    trades_list = all_trades_df.fillna("").to_dict(orient="records")
    return {"trades": trades_list, "summary": summary, "final_equity_avg": avg_final_eq}
