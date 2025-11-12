from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import pandas as pd
import numpy as np
import pymongo
import time, os, datetime
from kiteconnect import KiteConnect

app = FastAPI(title="📈 StockBot Backend", version="3.5.0")

# === Middleware ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update later for security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Environment Variables ===
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")  # Must match Zerodha developer app

# === Mongo Helpers ===
def save_access_token(access_token):
    client = pymongo.MongoClient(MONGO_URI)
    db = client[DB_NAME]
    db["config"].update_one(
        {"name": "kite_access_token"},
        {"$set": {"access_token": access_token, "updated_at": datetime.datetime.utcnow()}},
        upsert=True
    )
    print("✅ Token saved to MongoDB.")


def get_latest_access_token():
    try:
        client = pymongo.MongoClient(MONGO_URI)
        db = client[DB_NAME]
        cfg = db["config"].find_one({"name": "kite_access_token"})
        if cfg and "access_token" in cfg:
            return cfg["access_token"]
    except Exception as e:
        print("❌ Mongo Error:", e)
    return None


# === Strategy Parameters ===
EMA_FAST, EMA_MID, EMA_SLOW = 20, 50, 100
RSI_PERIOD, ATR_PERIOD = 14, 14
ATR_MULT, MIN_ATR_PCT = 2.5, 0.012
RISK_PER_TRADE, TRANSACTION_COST, SLIPPAGE = 0.0075, 0.0015, 0.0005
INITIAL_CAPITAL = 1_000_000


def compute_indicators(df):
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


def backtest_symbol(df, symbol):
    df = compute_indicators(df)
    equity, position, entry_price, trail_stop = INITIAL_CAPITAL, 0, 0, None
    trades = []
    for i in range(1, len(df)):
        row = df.iloc[i]
        if np.isnan(row["atr"]) or row["atr"] == 0:
            continue
        atr_ratio = row["atr"] / row["close"]
        if atr_ratio < MIN_ATR_PCT:
            continue
        price, rsi = row["close"], row["rsi"]
        in_uptrend = row["ema_mid"] > row["ema_slow"]
        entry_cond = in_uptrend and rsi > 60 and price > row["ema_fast"]
        exit_cond = (rsi < 45 or price < row["ema_fast"])
        if position == 0 and entry_cond:
            risk_amt = equity * RISK_PER_TRADE
            stop_dist = row["atr"] * ATR_MULT
            qty = max(int(risk_amt / stop_dist), 1)
            entry_price = price * (1 + SLIPPAGE)
            trail_stop = entry_price - ATR_MULT * row["atr"]
            position = qty
            trades.append({"Symbol": symbol, "Date": row["date"], "Action": "BUY", "Qty": qty, "Price": entry_price})
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
                position, trail_stop = 0, None
    return pd.DataFrame(trades), equity


def summarize_backtest(trades_df):
    if trades_df.empty:
        return {"TotalPnL": 0, "WinRate": 0, "Trades": 0}
    trades_df["PnL"] = trades_df["PnL"].fillna(0)
    wins, losses = trades_df[trades_df["PnL"] > 0]["PnL"], trades_df[trades_df["PnL"] < 0]["PnL"]
    total_pnl, win_rate = trades_df["PnL"].sum(), (len(wins) / len(trades_df)) * 100
    avg_win, avg_loss = wins.mean() if len(wins) else 0, losses.mean() if len(losses) else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else np.nan
    expectancy = (win_rate / 100) * avg_win + (1 - (win_rate / 100)) * avg_loss
    return {
        "Total PnL": round(total_pnl, 2),
        "Win Rate %": round(win_rate, 2),
        "Trades": len(trades_df),
        "Reward:Risk": round(rr, 2) if not np.isnan(rr) else None,
        "Expectancy": round(expectancy, 2)
    }


def run_backtest(api_key, access_token):
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    try:
        data = kite.historical_data(
            instrument_token=kite.ltp("NSE:RELIANCE")["NSE:RELIANCE"]["instrument_token"],
            from_date="2024-01-01", to_date="2025-01-01", interval="day"
        )
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        trades, _ = backtest_symbol(df, "RELIANCE")
        summary = summarize_backtest(trades)
        print("✅ Backtest completed:", summary)
        return summary
    except Exception as e:
        print("❌ Error running backtest:", e)
        raise


@app.get("/generate_token_url")
def generate_token_url():
    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        return {"login_url": kite.login_url()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kite API Error: {e}")


@app.get("/kite/callback")
def kite_callback(request_token: str):
    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = data["access_token"]
        save_access_token(access_token)

        # Run backtest automatically
        summary = run_backtest(KITE_API_KEY, access_token)

        # Redirect to frontend with encoded summary
        summary_params = "&".join([f"{k}={v}" for k, v in summary.items()])
        return RedirectResponse(url=f"https://stockbot-dashboard.onrender.com?login_success=true&{summary_params}")
    except Exception as e:
        print("❌ Callback error:", e)
        return RedirectResponse(url="https://stockbot-dashboard.onrender.com?login_error=true")


@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}
