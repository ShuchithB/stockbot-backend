# app/main.py
import os
import json
import time
import datetime
from urllib.parse import quote
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

import pymongo
from kiteconnect import KiteConnect
from pymongo import MongoClient


MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["stockbot"]


# Import your strategy implementations
from app.swing_strategy import run_backtest as run_swing_backtest

# --- quick test "momentum" strategy as a light fast alternative for dev/test ---
def run_momentum_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE):
    """
    Lightweight momentum backtest used for quick testing (single symbol).
    Returns dict {trades, equity}
    """
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)
    try:
        # Try RELIANCE only to be quick
        inst = kite.ltp("NSE:RELIANCE")
        token = list(inst.values())[0]["instrument_token"]
        raw = kite.historical_data(instrument_token=token,
                                   from_date=START_DATE, to_date=END_DATE, interval="day")
        df = None
        if raw:
            import pandas as pd
            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
        else:
            return {"trades": [], "equity": []}

        # compute a simple ema cross
        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()

        trades = []
        equity = 1_000_000
        position = 0
        entry_price = 0
        equity_curve = []
        for i in range(len(df)):
            row = df.iloc[i]
            price = row["close"]
            equity_curve.append({"date": str(row["date"].date()), "portfolio_equity": round(equity,2)})
            if i == 0 or (i < 50):
                continue
            if position == 0 and row["ema20"] > row["ema50"]:
                entry_price = price
                position = 1
                trades.append({"Symbol":"RELIANCE","Date":str(row["date"].date()),"Action":"BUY","Price":round(entry_price,2),"Qty":1})
            elif position > 0 and row["ema20"] < row["ema50"]:
                exit_price = price
                pnl = exit_price - entry_price
                equity += pnl
                trades.append({"Symbol":"RELIANCE","Date":str(row["date"].date()),"Action":"SELL","Price":round(exit_price,2),"Qty":1,"PnL":round(pnl,2)})
                position = 0

        return {"trades": trades, "equity": equity_curve}
    except Exception as e:
        print("Momentum run error:", e)
        return {"trades": [], "equity": []}

# === App ===
app = FastAPI(title="StockBot Backend (No-schedule test)", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to your frontend domain when locking down
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Environment ===
MONGO_URI = os.getenv("MONGO_URI")  # e.g. mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
DB_NAME = os.getenv("DB_NAME", "stockbot")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
# Keep redirect URL in your Zerodha app and env (must match)
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")  # e.g. https://stockbot-backend-39ec.onrender.com/kite/callback

if not MONGO_URI:
    print("⚠️ MONGO_URI not set - DB operations will fail until you set it.")

# === Mongo helpers ===
def get_db():
    client = pymongo.MongoClient(MONGO_URI)
    return client[DB_NAME]

def save_access_token(access_token: str):
    try:
        db = get_db()
        db["config"].update_one(
            {"name": "kite_access_token"},
            {"$set": {"access_token": access_token, "updated_at": datetime.datetime.utcnow()}},
            upsert=True
        )
        print("✅ Saved Kite access token to Mongo.")
    except Exception as e:
        print("❌ Error saving token to Mongo:", e)

def get_latest_access_token():
    try:
        db = get_db()
        cfg = db["config"].find_one({"name":"kite_access_token"})
        if cfg and "access_token" in cfg:
            return cfg["access_token"]
    except Exception as e:
        print("❌ Mongo read error:", e)
    return None

def save_backtest_result(record: dict):
    """
    Save the backtest result dictionary to 'backtests' collection.
    record should include keys: strategy, summary (optional), equity_curve (list), trades (list)
    """
    try:
        db = get_db()
        rec = {
            "timestamp": datetime.datetime.utcnow(),
            "strategy": record.get("strategy","unknown"),
            "symbols": record.get("symbols", []),
            "summary": record.get("summary", {}),
            "equity_curve": record.get("equity", []),
            "trades": record.get("trades", [])
        }
        db["backtests"].insert_one(rec)
        print("✅ Backtest saved to Mongo.")
        # strip ObjectId before returning
        rec_out = rec.copy()
        rec_out.pop("_id", None)
        return rec_out
    except Exception as e:
        print("❌ Save backtest failed:", e)
        raise

# === Strategy registry (multi-strategy switching) ===
STRATEGIES = {
    "swing": run_swing_backtest,
    "momentum": run_momentum_backtest,
    # add more strategies: "other": run_other_backtest
}

# === Models ===
class StrategyRequest(BaseModel):
    strategy: str = "swing"
    start_date: str = "2024-03-01"
    end_date: str = "2025-11-10"
    symbols_file: str = "nifty100.csv"
    # notify_callback: Optional[str] = None  # keep for future webhook use

# === Health + root ===
@app.get("/health")
def health():
    return {"status":"ok", "time": time.time()}

@app.get("/")
def root():
    return {"message":"StockBot Backend (test) - use /generate_token_url, /kite/callback, /run_strategy"}

# === Kite OAuth helpers ===
@app.get("/generate_token_url")
def generate_token_url():
    if not KITE_API_KEY:
        raise HTTPException(status_code=500, detail="Missing KITE_API_KEY env var")
    kite = KiteConnect(api_key=KITE_API_KEY)
    try:
        login_url = kite.login_url()
        return {"login_url": login_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kite login_url error: {e}")

@app.get("/kite/callback")
def kite_callback(request_token: str = None, action: str = None, status: str = None):
    """
    Zerodha will redirect here with ?request_token=...&status=success
    We save the access token in DB. We do NOT automatically run backtest here to keep flow manual for testing.
    """
    try:
        if not request_token:
            return JSONResponse(status_code=400, content={"detail":"Missing request_token in callback URL"})
        kite = KiteConnect(api_key=KITE_API_KEY)
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = data.get("access_token")
        if not access_token:
            return JSONResponse(status_code=500, content={"detail":"No access_token returned from Kite"})
        save_access_token(access_token)
        # redirect back to frontend with success flag
        return RedirectResponse(url=f"https://stockbot-dashboard.onrender.com?token_success=true")
    except Exception as e:
        print("Callback error:", e)
        return RedirectResponse(url=f"https://stockbot-dashboard.onrender.com?token_error=true")

# === Run a strategy endpoint (manual testing) ===
@app.post("/run_strategy")
def run_strategy(req: StrategyRequest, background_tasks: BackgroundTasks):
    """
    Launches the chosen strategy in background and stores results in MongoDB.
    Returns immediately with started status.
    """
    if req.strategy not in STRATEGIES:
        raise HTTPException(status_code=400, detail="Unknown strategy")

    access_token = get_latest_access_token()
    if not access_token:
        raise HTTPException(status_code=400, detail="No Kite access token found. Generate via /generate_token_url and login first.")

    def _runner():
        try:
            print(f"🔁 Starting {req.strategy} backtest: {req.start_date} -> {req.end_date}")
            strategy_fn = STRATEGIES[req.strategy]
            # strategy functions MAY return different shaped data. normalize.
            result = strategy_fn(API_KEY=KITE_API_KEY,
                                 ACCESS_TOKEN=access_token,
                                 START_DATE=req.start_date,
                                 END_DATE=req.end_date,
                                 NIFTY100_FILE=req.symbols_file)
            # normalize result into dict with trades + equity
            trades = result.get("trades", []) if isinstance(result, dict) else []
            equity = result.get("equity", []) if isinstance(result, dict) else []
            # try to compute basic summary (simple metrics)
            summary = {}
            try:
                # compute total pnl and win rate if trades provided
                import pandas as pd
                if trades:
                    tdf = pd.DataFrame(trades)
                    if "PnL" in tdf.columns:
                        total_pnl = float(tdf["PnL"].fillna(0).sum())
                        wins = (tdf["PnL"] > 0).sum()
                        losses = (tdf["PnL"] <= 0).sum()
                        win_rate = float(wins / (wins + losses) * 100) if (wins+losses)>0 else 0.0
                        summary = {"Total PnL": round(total_pnl,2), "Win Rate %": round(win_rate,2), "Trades": int(len(tdf))}
            except Exception as e:
                print("Summary calc error:", e)

            record = {
                "strategy": req.strategy,
                "symbols": [],  # optional: fill if strategy returns symbol info
                "summary": summary,
                "equity": equity,
                "trades": trades
            }
            saved = save_backtest_result(record)
            print(f"✅ Backtest finished and saved. strategy={req.strategy}")
        except Exception as e:
            print("Run strategy failed:", e)

    background_tasks.add_task(_runner)
    return {"status":"started", "message": f"{req.strategy} backtest started in background"}

# === Fetch saved backtests ===
@app.get("/backtests")
def list_backtests(limit: int = 50):
    try:
        db = get_db()
        docs = list(db["backtests"].find({}, {"_id":0}).sort("timestamp", -1).limit(limit))
        # convert datetimes to iso strings
        for d in docs:
            if isinstance(d.get("timestamp"), datetime.datetime):
                d["timestamp"] = d["timestamp"].isoformat()
        return {"backtests": docs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

