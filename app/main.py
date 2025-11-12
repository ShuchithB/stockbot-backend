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

# === FastAPI App ===
app = FastAPI(title="📈 StockBot Backend", version="3.0.0")

# === CORS for frontend ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # You can later restrict this to your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Environment Config ===
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")  # e.g. https://stockbot-backend.onrender.com/kite/callback

# === Mongo Helper ===
def get_latest_access_token():
    """Fetch latest Kite Access Token from MongoDB."""
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
        print("❌ MongoDB error:", e)
        return None


# === Strategy Parameters ===
EMA_FAST, EMA_MID, EMA_SLOW = 20, 50, 100
RSI_PERIOD, ATR_PERIOD = 14, 14
ATR_MULT, MIN_ATR_PCT = 2.5, 0.012
RISK_PER_TRADE, TRANSACTION_COST, SLIPPAGE = 0.0075, 0.0015, 0.0005
INITIAL_CAPITAL = 1_000_000


# === Indicators & Backtest Logic ===
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


# === Backtest Trigger ===
class RunRequest(BaseModel):
    api_key: str = None
    access_token: str = None
    start_date: str = "2024-01-01"
    end_date: str = "2025-01-01"


def run_backtest(api_key, access_token, start_date, end_date):
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    try:
        data = kite.historical_data(
            instrument_token=kite.ltp("NSE:RELIANCE")["NSE:RELIANCE"]["instrument_token"],
            from_date=start_date, to_date=end_date, interval="day"
        )
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        trades, equity = backtest_symbol(df, "RELIANCE")
        summary = summarize_backtest(trades)
        print("✅ Backtest Completed:", summary)
        return summary
    except Exception as e:
        print("❌ Error during backtest:", e)
        raise


# === Manual Backtest Endpoint ===
@app.post("/run_once")
def run_once(data: RunRequest, background_tasks: BackgroundTasks):
    try:
        token = data.access_token or get_latest_access_token()
        if not token:
            raise HTTPException(status_code=400, detail="No valid Kite Access Token found. Please generate it first.")

        background_tasks.add_task(run_backtest, data.api_key or KITE_API_KEY, token, data.start_date, data.end_date)
        return {"status": "started", "message": "Backtest running in background."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# === Kite Access Token Manual Generation ===
@app.get("/generate_token_url")
def generate_token_url():
    """Return Zerodha login URL for user-triggered token generation."""
    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        login_url = kite.login_url()
        print("🔗 Kite login URL generated.")
        return {"login_url": login_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate login URL: {e}")


@app.get("/kite/callback")
def kite_callback(request_token: str):
    """Kite redirect handler."""
    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = data["access_token"]

        client = pymongo.MongoClient(MONGO_URI)
        db = client[DB_NAME]
        db["config"].update_one(
            {"name": "kite_access_token"},
            {"$set": {"access_token": access_token, "updated_at": datetime.datetime.utcnow()}},
            upsert=True
        )

        print("✅ Access Token saved to MongoDB.")
        return RedirectResponse(url="https://stockbot-dashboard.onrender.com?token_success=true")
    except Exception as e:
        print("❌ Token generation failed:", e)
        return RedirectResponse(url="https://stockbot-dashboard.onrender.com?token_error=true")


# === Health Check ===
@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}


# === Scheduler ===
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: print("📊 Daily check:", datetime.datetime.now()), "interval", hours=24)
scheduler.start()

@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)
    print("🛑 Scheduler stopped cleanly.")
