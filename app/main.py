import os
import json
import time
import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

from kiteconnect import KiteConnect
from pymongo import MongoClient
import pandas as pd

# -------------------------------------------------------------------
#                🔥 GLOBAL MONGO CLIENT (Enterprise grade)
# -------------------------------------------------------------------

MONGO_URI = os.getenv("MONGO_URI")   # your MongoDB Atlas URI
DB_NAME = "stockbot"                 # always use this DB

if not MONGO_URI:
    raise Exception("❌ MONGO_URI is missing in Render env vars.")

mongo_client = MongoClient(MONGO_URI)     # Create once
mongo_db = mongo_client[DB_NAME]          # Shared DB

print("✅ MongoDB connected:", mongo_db.name)

# -------------------------------------------------------------------
#                🔥 ENV: KITE API CONFIG
# -------------------------------------------------------------------

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")

if not KITE_API_KEY or not KITE_API_SECRET:
    print("⚠️ WARNING: Kite API keys not set!")

# -------------------------------------------------------------------
#               🔥 IMPORT STRATEGIES
# -------------------------------------------------------------------
from app.swing_strategy import run_backtest as run_swing_backtest

# Simple, fast test strategy
def run_momentum_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE):
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)
    try:
        inst = kite.ltp("NSE:RELIANCE")
        token = list(inst.values())[0]["instrument_token"]
        raw = kite.historical_data(token, START_DATE, END_DATE, "day")
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])

        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()

        trades = []
        equity = 1_000_000
        eq_curve = []

        pos = 0
        entry = 0

        for _, row in df.iterrows():
            price = row["close"]
            eq_curve.append({
                "date": str(row["date"].date()),
                "portfolio_equity": equity
            })

            if pos == 0 and row["ema20"] > row["ema50"]:
                pos = 1
                entry = price
                trades.append({
                    "Symbol": "RELIANCE",
                    "Date": str(row["date"].date()),
                    "Action": "BUY",
                    "Price": price,
                    "Qty": 1
                })
            elif pos == 1 and row["ema20"] < row["ema50"]:
                pnl = price - entry
                equity += pnl
                pos = 0
                trades.append({
                    "Symbol": "RELIANCE",
                    "Date": str(row["date"].date()),
                    "Action": "SELL",
                    "Price": price,
                    "Qty": 1,
                    "PnL": pnl
                })

        return {"trades": trades, "equity": eq_curve}

    except Exception as e:
        print("Momentum error:", e)
        return {"trades": [], "equity": []}

# -------------------------------------------------------------------
#               🔥 MULTI-STRATEGY REGISTRY
# -------------------------------------------------------------------
STRATEGIES = {
    "swing": run_swing_backtest,
    "momentum": run_momentum_backtest
}

# -------------------------------------------------------------------
#                     FASTAPI APP
# -------------------------------------------------------------------
app = FastAPI(title="StockBot Backend", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# -------------------------------------------------------------------
#                     🔥 TOKEN STORAGE (Mongo)
# -------------------------------------------------------------------

def save_access_token(access_token: str, expires_in: int = 3600):
    """
    Stores the Kite access token AND expiry timestamp.
    expires_in = 3600 seconds (1 hour) – standard for Kite.
    """
    expiry_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)

    mongo_db["config"].update_one(
        {"name": "kite_access_token"},
        {
            "$set": {
                "access_token": access_token,
                "expires_at": expiry_time,
                "updated_at": datetime.datetime.utcnow()
            }
        },
        upsert=True
    )
    print("✅ Saved Kite token (with expiry) to MongoDB.")


def get_latest_access_token():
    """Returns token ONLY if not expired."""
    cfg = mongo_db["config"].find_one({"name": "kite_access_token"})
    if not cfg:
        return None

    expires = cfg.get("expires_at")
    if expires and expires < datetime.datetime.utcnow():
        print("⚠️ Token expired. Deleting from DB.")
        mongo_db["config"].delete_one({"name": "kite_access_token"})
        return None

    return cfg.get("access_token")


# -------------------------------------------------------------------
#            🔥 AUTOMATIC EXPIRED TOKEN CLEANER (Every call)
# -------------------------------------------------------------------

def clean_expired_token():
    """
    Runs on EVERY /run_strategy, /config, /backtests request.
    No CRON required – auto-cleaner.
    """
    cfg = mongo_db["config"].find_one({"name": "kite_access_token"})
    if cfg and cfg.get("expires_at") < datetime.datetime.utcnow():
        mongo_db["config"].delete_one({"name": "kite_access_token"})
        print("🗑️ Auto-cleaned expired token.")


# -------------------------------------------------------------------
#                    🔥 REQUEST MODELS
# -------------------------------------------------------------------
class StrategyRequest(BaseModel):
    strategy: str = "swing"
    start_date: str
    end_date: str
    symbols_file: str = "nifty100.csv"


# -------------------------------------------------------------------
#                 🔥 KITE LOGIN / TOKEN CALLBACK
# -------------------------------------------------------------------

@app.get("/generate_token_url")
def generate_token_url():
    if not KITE_API_KEY:
        raise HTTPException(500, "KITE_API_KEY missing")

    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        login_url = kite.login_url()
        return {"login_url": login_url}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/kite/callback")
def kite_callback(request_token: str = None, status: str = None):
    """
    Kite sends: ?request_token=xxxx&status=success
    """
    try:
        if not request_token:
            return JSONResponse({"detail": "Missing request_token"}, 400)

        kite = KiteConnect(api_key=KITE_API_KEY)
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)

        access_token = data.get("access_token")
        if not access_token:
            return JSONResponse({"detail": "Token generation failed"}, 500)

        # Save token WITH expiry
        save_access_token(access_token, expires_in=3600)

        return RedirectResponse("https://stockbot-dashboard.onrender.com?token_success=true")

    except Exception as e:
        print("Callback error:", e)
        return RedirectResponse("https://stockbot-dashboard.onrender.com?token_error=true")


# -------------------------------------------------------------------
#                 🔥 STRATEGY BACKTEST RUNNER
# -------------------------------------------------------------------

@app.post("/run_strategy")
def run_strategy(req: StrategyRequest, background_tasks: BackgroundTasks):
    clean_expired_token()  # auto purge expired tokens

    if req.strategy not in STRATEGIES:
        raise HTTPException(400, "Unknown strategy")

    access_token = get_latest_access_token()
    if not access_token:
        raise HTTPException(400, "No Kite access token found. Login again.")

    def _task():
        try:
            print(f"▶️ Running {req.strategy} from {req.start_date} to {req.end_date}")

            fn = STRATEGIES[req.strategy]
            result = fn(
                API_KEY=KITE_API_KEY,
                ACCESS_TOKEN=access_token,
                START_DATE=req.start_date,
                END_DATE=req.end_date,
                NIFTY100_FILE=req.symbols_file
            )

            trades = result.get("trades", [])
            equity = result.get("equity", [])

            # basic summary
            summary = {}
            if trades:
                df = pd.DataFrame(trades)
                if "PnL" in df.columns:
                    pnl = df["PnL"].sum()
                    wins = (df["PnL"] > 0).sum()
                    losses = (df["PnL"] <= 0).sum()
                    win_rate = round((wins / max(wins + losses, 1)) * 100, 2)
                    summary = {
                        "Total PnL": float(pnl),
                        "Win Rate %": win_rate,
                        "Trades": len(df)
                    }

            mongo_db["backtests"].insert_one({
                "timestamp": datetime.datetime.utcnow(),
                "strategy": req.strategy,
                "symbols": [],
                "summary": summary,
                "equity_curve": equity,
                "trades": trades
            })

            print(f"✅ Backtest saved for {req.strategy}")

        except Exception as e:
            print("Backtest error:", e)

    background_tasks.add_task(_task)

    return {"status": "started", "msg": f"{req.strategy} started in background"}


# -------------------------------------------------------------------
#                     🔥 GET /config
# -------------------------------------------------------------------

@app.get("/config")
def get_config():
    clean_expired_token()
    token = get_latest_access_token()
    return {
        "kite_token_active": bool(token),
        "backend_time": str(datetime.datetime.utcnow())
    }


# -------------------------------------------------------------------
#                     🔥 GET /backtests
# -------------------------------------------------------------------

@app.get("/backtests")
def get_backtests():
    clean_expired_token()
    data = list(
        mongo_db["backtests"].find({}, {"_id": 0}).sort("timestamp", -1)
    )
    return {"status": "ok", "count": len(data), "data": data}
# -------------------------------------------------------------------
#                      🔥 HEALTH CHECK ROUTES
# -------------------------------------------------------------------

@app.get("/health")
def health():
    clean_expired_token()
    return {
        "status": "ok",
        "server_time": str(datetime.datetime.utcnow()),
        "kite_token_active": bool(get_latest_access_token())
    }


# -------------------------------------------------------------------
#                      🔥 ROOT ROUTE
# -------------------------------------------------------------------

@app.get("/")
def root():
    clean_expired_token()
    return {
        "message": "StockBot Backend running ✔",
        "endpoints": [
            "/generate_token_url",
            "/kite/callback",
            "/run_strategy",
            "/backtests",
            "/config",
            "/health"
        ],
        "kite_token_active": bool(get_latest_access_token())
    }


# -------------------------------------------------------------------
#                🔥 ERROR HANDLER (OPTIONAL BUT RECOMMENDED)
# -------------------------------------------------------------------

from fastapi.responses import PlainTextResponse
from fastapi.requests import Request

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print("🔥 Global Error:", exc)
    return PlainTextResponse(str(exc), status_code=500)


# -------------------------------------------------------------------
#                      🔥 SERVER READY
# -------------------------------------------------------------------

print("🚀 StockBot Backend Loaded Successfully")
print("📌 Mongo Status:", "Connected" if mongo_db else "Not connected")
print("📌 Env -> API_KEY:", bool(KITE_API_KEY), " | SECRET:", bool(KITE_API_SECRET))
print("📌 Token Exists:", bool(get_latest_access_token()))
