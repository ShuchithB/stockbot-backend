# app/main.py
import os
import json
import time
import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

import pandas as pd
from pymongo import MongoClient
from kiteconnect import KiteConnect

# ==============================
# ENV / DB INIT
# ==============================
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")

if not MONGO_URI:
    print("❌ ERROR: MONGO_URI not set!")

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client[DB_NAME]


# ==============================
# Import strategies
# ==============================
from app.swing_strategy import run_backtest as run_swing_backtest
from app.fast_swing_strategy import run_fast_swing_backtest


# ==============================
# Helper to save & load token
# ==============================
def save_access_token(access_token: str, expiry_minutes: int = 9):
    expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=expiry_minutes)
    mongo_db["config"].update_one(
        {"name": "kite_access_token"},
        {"$set": {"access_token": access_token, "expiry": expiry.isoformat()}},
        upsert=True
    )
    print("✅ Saved access token until:", expiry)


def get_saved_token():
    cfg = mongo_db["config"].find_one({"name": "kite_access_token"})
    if not cfg:
        return None
    return cfg.get("access_token")


def is_token_valid():
    try:
        cfg = mongo_db["config"].find_one({"name": "kite_access_token"})
        if not cfg:
            return False
        expiry = cfg.get("expiry")
        if not expiry:
            return False
        expiry = datetime.datetime.fromisoformat(expiry)
        return expiry > datetime.datetime.utcnow()
    except:
        return False


# ==============================
# Historical Data Fetch (Kite)
# ==============================
def fetch_historical_kite(symbol: str, start: str, end: str, interval="day"):
    """Fetch candles for each symbol safely."""
    try:
        token = get_saved_token()
        if not token:
            print("⚠ No access token")
            return []

        kite = KiteConnect(api_key=KITE_API_KEY)
        kite.set_access_token(token)

        # Resolve instrument token
        ltp = kite.ltp(f"NSE:{symbol}")
        if not ltp:
            return []
        inst_token = list(ltp.values())[0]["instrument_token"]

        raw = kite.historical_data(inst_token, start, end, interval)

        if not raw:
            return []

        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])
        return df

    except Exception as e:
        print("❌ fetch_historical_kite:", e)
        return []


# ==============================
# Wrapper for FAST SWING Strategy
# ==============================
def run_fast_swing_wrapper(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE):
    return run_fast_swing_backtest(
        API_KEY=API_KEY,
        ACCESS_TOKEN=ACCESS_TOKEN,
        START_DATE=START_DATE,
        END_DATE=END_DATE,
        NIFTY100_FILE=NIFTY100_FILE,
        fetch_historical=lambda sym, start, end, interval="day": fetch_historical_kite(sym, start, end, interval)
    )


# ==============================
# Strategy registry
# ==============================
STRATEGIES = {
    "swing": run_swing_backtest,
    "momentum": lambda *a, **k: {"trades": [], "equity": []},   # minimal placeholder
    "fast_swing": run_fast_swing_wrapper,
}


# ==============================
# API Models
# ==============================
class StrategyRequest(BaseModel):
    strategy: str
    start_date: str
    end_date: str
    symbols_file: str = "nifty100.csv"


# ==============================
# FASTAPI SETUP
# ==============================
app = FastAPI(title="StockBot Backend", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True
)


# ==============================
# BASIC ROUTES
# ==============================
@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": time.time(),
        "token_valid": is_token_valid()
    }


@app.get("/")
def root():
    return {"message": "StockBot Backend Running"}


# ==============================
# KITE LOGIN
# ==============================
@app.get("/generate_token_url")
def generate_token_url():
    kite = KiteConnect(api_key=KITE_API_KEY)
    return {"login_url": kite.login_url()}


@app.get("/kite/callback")
def kite_callback(request_token: str = None):
    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        sess = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        save_access_token(sess["access_token"])
        return RedirectResponse("https://stockbot-dashboard.onrender.com?token_success=1")
    except Exception as e:
        print("Callback error:", e)
        return RedirectResponse("https://stockbot-dashboard.onrender.com?token_error=1")


# ==============================
# RUN STRATEGY
# ==============================
@app.post("/run_strategy")
def run_strategy(req: StrategyRequest, bg: BackgroundTasks):

    if req.strategy not in STRATEGIES:
        raise HTTPException(400, f"Unknown strategy {req.strategy}")

    if not is_token_valid():
        raise HTTPException(400, "No valid Kite access token")

    access = get_saved_token()

    def worker():
        try:
            fn = STRATEGIES[req.strategy]
            result = fn(
                API_KEY=KITE_API_KEY,
                ACCESS_TOKEN=access,
                START_DATE=req.start_date,
                END_DATE=req.end_date,
                NIFTY100_FILE=req.symbols_file
            )

            record = {
                "timestamp": datetime.datetime.utcnow(),
                "strategy": req.strategy,
                "summary": {},
                "equity_curve": result.get("equity", []),
                "trades": result.get("trades", [])
            }

            mongo_db["backtests"].insert_one(record)
            print("✅ Backtest saved.")
        except Exception as e:
            print("❌ Backtest error:", e)

    bg.add_task(worker)
    return {"status": "started"}


# ==============================
# FETCH SAVED BACKTESTS
# ==============================
@app.get("/backtests")
def get_backtests():
    try:
        data = list(mongo_db["backtests"].find({}, {"_id": 0}).sort("timestamp", -1))
        return {"status": "ok", "count": len(data), "data": data}
    except Exception as e:
        return JSONResponse({"detail": str(e)}, 500)
