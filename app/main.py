# app/main.py
import os
import json
import time
import datetime
from typing import Optional
import math

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from app.fast_swing_strategy import run_fast_swing_backtest


import pymongo
from pymongo import MongoClient
from kiteconnect import KiteConnect

from bson.decimal128 import Decimal128
from bson.objectid import ObjectId

# -------------------------------
# Load environment variables
# -------------------------------
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")

if not MONGO_URI:
    print("❌ ERROR: MONGO_URI not set!")

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client[DB_NAME]

# Strategy imports
from app.swing_strategy import run_backtest as run_swing_backtest

# Lightweight momentum strategy for quick tests
def run_momentum_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE):
    try:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(ACCESS_TOKEN)

        inst = kite.ltp("NSE:RELIANCE")
        token = list(inst.values())[0]["instrument_token"]

        raw = kite.historical_data(token, START_DATE, END_DATE, "day")

        import pandas as pd
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])
        df["ema20"] = df["close"].ewm(20).mean()
        df["ema50"] = df["close"].ewm(50).mean()

        trades = []
        equity = 1_000_000
        entry = None
        eq = []

        for i in range(len(df)):
            price = df["close"].iloc[i]
            eq.append({"date": str(df["date"].iloc[i].date()), "portfolio_equity": equity})

            if i < 50:
                continue

            if entry is None and df["ema20"].iloc[i] > df["ema50"].iloc[i]:
                entry = price
                trades.append({"Symbol":"RELIANCE","Date":str(df["date"].iloc[i]),"Action":"BUY","Price":price,"Qty":1})
            elif entry is not None and df["ema20"].iloc[i] < df["ema50"].iloc[i]:
                pnl = price - entry
                equity += pnl
                trades.append({"Symbol":"RELIANCE","Date":str(df["date"].iloc[i]),"Action":"SELL","Price":price,"Qty":1,"PnL":pnl})
                entry = None

        return {"trades": trades, "equity": eq}

    except Exception as e:
        print("Momentum error:", e)
        return {"trades": [], "equity": []}


# -----------------------------------------
# Sanitizer for JSON safety
# -----------------------------------------
def sanitize(obj):
    """Recursively fix invalid JSON values (NaN, inf, datetime, ObjectId, Decimal128)."""
    if obj is None:
        return None

    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj

    if isinstance(obj, Decimal128):
        return float(obj.to_decimal())

    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()

    if isinstance(obj, ObjectId):
        return str(obj)

    if isinstance(obj, list):
        return [sanitize(v) for v in obj]

    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}

    return obj


# -----------------------------------------
# FastAPI App with CORS
# -----------------------------------------
app = FastAPI(title="StockBot Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True
)


# -----------------------------------------
# MongoDB Helpers
# -----------------------------------------
def save_access_token(access_token: str, expiry_minutes: int = 9):
    try:
        expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=expiry_minutes)
        mongo_db["config"].update_one(
            {"name": "kite_access_token"},
            {"$set": {"access_token": access_token, "expiry": expiry.isoformat()}},
            upsert=True
        )
        print("✅ Saved access token with expiry:", expiry)
    except Exception as e:
        print("❌ Failed saving token:", e)


def get_saved_token():
    try:
        cfg = mongo_db["config"].find_one({"name": "kite_access_token"})
        if not cfg:
            return None
        return cfg.get("access_token")
    except:
        return None


def is_token_valid():
    try:
        cfg = mongo_db["config"].find_one({"name": "kite_access_token"})
        if not cfg:
            return False

        expiry = cfg.get("expiry")
        if not expiry:
            return False

        if isinstance(expiry, str):
            try:
                expiry = datetime.datetime.fromisoformat(expiry)
            except:
                return False

        return expiry > datetime.datetime.utcnow()

    except Exception as e:
        print("Token check error:", e)
        return False


# -----------------------------------------
# Strategy Registry
# -----------------------------------------
STRATEGIES = {
    "swing": run_swing_backtest,
    "momentum": run_momentum_backtest
    "fast_swing": run_fast_swing_backtest
}


# -----------------------------------------
# API Models
# -----------------------------------------
class StrategyRequest(BaseModel):
    strategy: str = "swing"
    start_date: str
    end_date: str
    symbols_file: str = "nifty100.csv"


# -----------------------------------------
# Basic Routes
# -----------------------------------------
@app.get("/health")
def health():
    return {"status":"ok","time":time.time(),"token_valid":is_token_valid()}


@app.get("/")
def root():
    return {"message":"StockBot Backend Running"}


# -----------------------------------------
# Kite OAuth Routes
# -----------------------------------------
@app.get("/generate_token_url")
def generate_token_url():
    kite = KiteConnect(api_key=KITE_API_KEY)
    return {"login_url": kite.login_url()}


@app.get("/kite/callback")
def kite_callback(request_token: str = None, status: str = None):
    try:
        if not request_token:
            return JSONResponse({"detail": "Missing request_token"}, 400)

        kite = KiteConnect(api_key=KITE_API_KEY)
        sess = kite.generate_session(request_token, api_secret=KITE_API_SECRET)

        save_access_token(sess["access_token"])

        return RedirectResponse("https://stockbot-dashboard.onrender.com?token_success=1")

    except Exception as e:
        print("Callback error:", e)
        return RedirectResponse("https://stockbot-dashboard.onrender.com?token_error=1")


# -----------------------------------------
# Run Strategy (Background)
# -----------------------------------------
@app.post("/run_strategy")
def run_strategy(req: StrategyRequest, bg: BackgroundTasks):
    if req.strategy not in STRATEGIES:
        raise HTTPException(400, "Unknown strategy")

    if not is_token_valid():
        raise HTTPException(400, "No valid Kite token. Login again.")

    access_token = get_saved_token()

    def runner():
        try:
            fn = STRATEGIES[req.strategy]
            result = fn(
                API_KEY=KITE_API_KEY,
                ACCESS_TOKEN=access_token,
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
            print("Backtest error:", e)

    bg.add_task(runner)
    return {"status": "started"}


# -----------------------------------------
# GET BACKTESTS — FULLY PATCHED SAFE VERSION
# -----------------------------------------
@app.get("/backtests")
def get_backtests():
    try:
        cursor = mongo_db["backtests"].find({}).sort("timestamp", -1)
        raw = list(cursor)
        safe = sanitize(raw)

        return JSONResponse(
            content={"status":"ok","count":len(safe),"data":safe},
            status_code=200
        )

    except Exception as e:
        print("❌ ERROR /backtests:", e)
        return JSONResponse({"detail": str(e)}, 500)

