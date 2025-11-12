from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import pandas as pd
import numpy as np
import pymongo
import time, os, datetime
from kiteconnect import KiteConnect

# === FastAPI App ===
app = FastAPI(title="📈 StockBot Backend", version="2.0.0")

# === CORS (Frontend Access) ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change later for security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === MongoDB Config ===
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")

def get_latest_access_token():
    """Fetch the latest Kite access token stored in MongoDB."""
    try:
        client = pymongo.MongoClient(MONGO_URI)
        db = client[DB_NAME]
        cfg = db["config"].find_one({"name": "kite_access_token"})
        if cfg and "access_token" in cfg:
            print("✅ Access Token loaded from MongoDB.")
            return cfg["access_token"]
        else:
            print("⚠️ No access token found in MongoDB.")
            return None
    except Exception as e:
        print("❌ Error fetching token:", e)
        return None


# === Strategy Parameters ===
EMA_FAST, EMA_MID, EMA_SLOW = 20, 50, 100
RSI_PERIOD, ATR_PERIOD = 14, 14
ATR_MULT, MIN_ATR_PCT = 2.5, 0.012
RISK_PER_TRADE, TRANSACTION_COST, SLIPPAGE = 0.0075, 0.0015, 0.0005
INITIAL_CAPITAL = 1_000_000


# === Utility Functions ===
def compute_indicators(df):
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_mid"] = df["close"].ewm(span=EMA_MID, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    delta = df["close"].diff()
    up, down = np.where(delta > 0, delta, 0), np.where(delta < 0, -delta, 0)
    roll_up, roll_down = pd.Series(up).rolling(RSI_PERIOD).mean(), pd.Series(down).rolling(RSI_PERIOD).mean()
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

        # Entry
        if position == 0 and entry_cond:
            risk_amt = equity * RISK_PER_TRADE
            stop_dist = row["atr"] * ATR_MULT
            qty = max(int(risk_amt / stop_dist), 1)
            entry_price = price * (1 + SLIPPAGE)
            trail_stop = entry_price - ATR_MULT * row["atr"]
            position = qty
            trades.append({"Symbol": symbol, "Date": row["date"], "Action": "BUY", "Qty": qty, "Price": entry_price})

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
        "Avg Win": round(avg_win, 2),
        "Avg Loss": round(avg_loss, 2),
        "Reward:Risk": round(rr, 2) if not np.isnan(rr) else None,
        "Expectancy": round(expectancy, 2)
    }


# === FastAPI Models ===
class RunRequest(BaseModel):
    api_key: str = None
    access_token: str = None
    start_date: str = "2024-01-01"
    end_date: str = "2025-01-01"


# === Backtest Executor ===
def run_backtest(api_key, access_token, start_date, end_date):
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    print(f"📡 Fetching NIFTY100 symbols data from {start_date} → {end_date}")

    try:
        df = kite.historical_data(
            instrument_token=kite.ltp("NSE:RELIANCE")["NSE:RELIANCE"]["instrument_token"],
            from_date=start_date,
            to_date=end_date,
            interval="day"
        )
        df = pd.DataFrame(df)
        df["date"] = pd.to_datetime(df["date"])
        trades, equity = backtest_symbol(df, "RELIANCE")
        summary = summarize_backtest(trades)
        print("✅ Backtest Completed:", summary)
        return summary
    except Exception as e:
        print("❌ Backtest Error:", e)
        raise


# === API Endpoints ===
@app.get("/")
def home():
    return {"message": "✅ StockBot backend live!", "version": "2.0.0"}

@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}

@app.post("/run_once")
def run_once(data: RunRequest, background_tasks: BackgroundTasks):
    try:
        token = data.access_token or get_latest_access_token()
        if not token:
            raise HTTPException(status_code=400, detail="No valid Kite Access Token found. Please refresh it.")

        background_tasks.add_task(run_backtest, data.api_key or os.getenv("KITE_API_KEY"), token, data.start_date, data.end_date)
        return {"status": "started", "message": "Backtest running in background.", "auto_token": not bool(data.access_token)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Background Scheduler (Optional) ===
scheduler = BackgroundScheduler()
def scheduled_task():
    print("📊 Daily Auto Backtest Triggered:", datetime.datetime.now())

scheduler.add_job(scheduled_task, "interval", hours=24)
scheduler.start()

@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)
    print("🛑 Scheduler stopped cleanly.")
