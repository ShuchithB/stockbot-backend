# app/main.py
import os
import json
import time
import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

from pymongo import MongoClient
from bson.json_util import dumps
from kiteconnect import KiteConnect

# ---------------------------------------------------------
# Environment / Mongo setup
# ---------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")  # must match Zerodha app redirect URL

# Create global mongo client (safe to reuse)
if not MONGO_URI:
    print("⚠️ Warning: MONGO_URI not set. MongoDB operations will fail until you set it in env.")
    mongo_client = None
    mongo_db = None
else:
    mongo_client = MongoClient(MONGO_URI)
    mongo_db = mongo_client[DB_NAME]


def get_db():
    """Return a DB handle (uses global client if available)."""
    global mongo_client, mongo_db
    if mongo_db:
        return mongo_db
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI)
        mongo_db = mongo_client[DB_NAME]
        return mongo_db
    raise RuntimeError("MongoDB not configured (MONGO_URI missing).")

# ---------------------------------------------------------
# Import / register strategies
# ---------------------------------------------------------
# Ensure you have app/swing_strategy.py with run_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE)
try:
    from app.swing_strategy import run_backtest as run_swing_backtest
except Exception as e:
    print("⚠️ Could not import swing strategy:", e)
    run_swing_backtest = None

# lightweight momentum for quick testing
def run_momentum_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE):
    """Lightweight momentum backtest used for quick testing (single symbol)."""
    try:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(ACCESS_TOKEN)
        inst = kite.ltp("NSE:RELIANCE")
        token = list(inst.values())[0]["instrument_token"]
        raw = kite.historical_data(instrument_token=token,
                                   from_date=START_DATE, to_date=END_DATE, interval="day")
        if not raw:
            return {"trades": [], "equity": []}
        import pandas as pd
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])
        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()

        trades = []
        equity = 1_000_000.0
        position = 0
        entry_price = 0.0
        equity_curve = []
        for i in range(len(df)):
            row = df.iloc[i]
            price = float(row["close"])
            equity_curve.append({"date": str(row["date"].date()), "portfolio_equity": round(equity, 2)})
            if i < 50:
                continue
            if position == 0 and row["ema20"] > row["ema50"]:
                entry_price = price
                position = 1
                trades.append({"Symbol": "RELIANCE", "Date": str(row["date"].date()), "Action": "BUY", "Price": round(entry_price, 2), "Qty": 1})
            elif position > 0 and row["ema20"] < row["ema50"]:
                exit_price = price
                pnl = exit_price - entry_price
                equity += pnl
                trades.append({"Symbol": "RELIANCE", "Date": str(row["date"].date()), "Action": "SELL", "Price": round(exit_price, 2), "Qty": 1, "PnL": round(pnl, 2)})
                position = 0

        return {"trades": trades, "equity": equity_curve}
    except Exception as e:
        print("Momentum run error:", e)
        return {"trades": [], "equity": []}

STRATEGIES = {
    "swing": run_swing_backtest,
    "momentum": run_momentum_backtest,
}

# ---------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------
app = FastAPI(title="StockBot Backend (test)", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to your frontend URL for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# Helpers: Token storage and backtest persistence
# ---------------------------------------------------------
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


def get_latest_access_token() -> Optional[str]:
    try:
        db = get_db()
        cfg = db["config"].find_one({"name": "kite_access_token"})
        if cfg and "access_token" in cfg:
            return cfg["access_token"]
    except Exception as e:
        print("❌ Mongo read error:", e)
    return None


def save_backtest_result(record: dict):
    """
    Save the backtest result dictionary to 'backtests' collection.
    record expected keys: strategy, summary, equity (list), trades (list), symbols (list)
    """
    try:
        db = get_db()
        rec = {
            "timestamp": datetime.datetime.utcnow(),
            "strategy": record.get("strategy", "unknown"),
            "symbols": record.get("symbols", []),
            "summary": record.get("summary", {}),
            "equity_curve": record.get("equity", record.get("equity_curve", [])),
            "trades": record.get("trades", record.get("trade_log", []))
        }
        db["backtests"].insert_one(rec)
        print("✅ Backtest saved to Mongo.")
        # return a copy (without _id)
        out = rec.copy()
        out.pop("_id", None)
        return out
    except Exception as e:
        print("❌ Save backtest failed:", e)
        raise

# ---------------------------------------------------------
# Models
# ---------------------------------------------------------
class StrategyRequest(BaseModel):
    strategy: str = "swing"
    start_date: str = "2024-03-01"
    end_date: str = "2025-11-10"
    symbols_file: str = "nifty100.csv"

# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}


@app.get("/")
def root():
    return {"message": "StockBot Backend (test) - use /generate_token_url, /kite/callback, /run_strategy, /backtests"}


@app.get("/generate_token_url")
def generate_token_url():
    if not KITE_API_KEY:
        raise HTTPException(status_code=500, detail="Missing KITE_API_KEY env var")
    kite = KiteConnect(api_key=KITE_API_KEY)
    try:
        login_url = kite.login_url()
        return {"login_url": login_url}
    except Exception as e:
        print("generate_token_url error:", e)
        raise HTTPException(status_code=500, detail=f"Kite login_url error: {e}")


@app.get("/kite/callback")
def kite_callback(request_token: str = None, action: str = None, status: str = None):
    """
    Zerodha will redirect here with ?request_token=...&status=success
    We save the access token in DB. We do NOT automatically run backtest here (control from UI).
    """
    try:
        if not request_token:
            return JSONResponse(status_code=400, content={"detail": "Missing request_token in callback URL"})
        if not KITE_API_KEY or not KITE_API_SECRET:
            return JSONResponse(status_code=500, content={"detail": "Kite API key/secret not configured in env"})
        kite = KiteConnect(api_key=KITE_API_KEY)
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = data.get("access_token")
        if not access_token:
            return JSONResponse(status_code=500, content={"detail": "No access_token returned from Kite"})
        save_access_token(access_token)
        # redirect back to frontend with success flag
        frontend_url = os.getenv("FRONTEND_URL", "https://stockbot-dashboard.onrender.com")
        return RedirectResponse(url=f"{frontend_url}?token_success=true")
    except Exception as e:
        print("Callback error:", e)
        frontend_url = os.getenv("FRONTEND_URL", "https://stockbot-dashboard.onrender.com")
        return RedirectResponse(url=f"{frontend_url}?token_error=true")


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
            if strategy_fn is None:
                print("Strategy function not available:", req.strategy)
                return

            result = strategy_fn(API_KEY=KITE_API_KEY,
                                 ACCESS_TOKEN=access_token,
                                 START_DATE=req.start_date,
                                 END_DATE=req.end_date,
                                 NIFTY100_FILE=req.symbols_file)

            # normalize results
            trades = result.get("trades", []) if isinstance(result, dict) else []
            equity = result.get("equity", []) if isinstance(result, dict) else result.get("equity_curve", []) if isinstance(result, dict) else []

            # compute a simple summary if trades include PnL
            summary = {}
            try:
                import pandas as pd
                if trades:
                    tdf = pd.DataFrame(trades)
                    if "PnL" in tdf.columns:
                        total_pnl = float(tdf["PnL"].fillna(0).sum())
                        wins = int((tdf["PnL"] > 0).sum())
                        losses = int((tdf["PnL"] <= 0).sum())
                        win_rate = float(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
                        summary = {"Total PnL": round(total_pnl, 2), "Win Rate %": round(win_rate, 2), "Trades": int(len(tdf))}
            except Exception as e:
                print("Summary calc error:", e)

            record = {
                "strategy": req.strategy,
                "symbols": result.get("symbols", []) if isinstance(result, dict) else [],
                "summary": summary,
                "equity": equity,
                "trades": trades
            }
            saved = save_backtest_result(record)
            print(f"✅ Backtest finished and saved. strategy={req.strategy}")
        except Exception as e:
            print("Run strategy failed:", e)

    background_tasks.add_task(_runner)
    return {"status": "started", "message": f"{req.strategy} backtest started in background"}


@app.get("/backtests")
async def get_backtests(limit: int = 50):
    """
    Returns saved backtests (most recent first). JSON-safe.
    """
    try:
        db = get_db()
        col = db["backtests"]
        cursor = col.find({}, {"_id": 0}).sort([("timestamp", -1)]).limit(limit)
        results = list(cursor)
        # convert datetimes to iso strings for safety
        for r in results:
            if isinstance(r.get("timestamp"), datetime.datetime):
                r["timestamp"] = r["timestamp"].isoformat()
        return JSONResponse(content={"status": "ok", "count": len(results), "data": results}, status_code=200)
    except Exception as e:
        print("❌ ERROR in /backtests:", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/latest")
async def get_latest():
    """
    Returns the single most recent backtest (useful for UI to display latest quickly).
    """
    try:
        db = get_db()
        col = db["backtests"]
        doc = col.find_one({}, {"_id": 0}, sort=[("timestamp", -1)])
        if not doc:
            return JSONResponse(content={"status": "empty", "data": None}, status_code=200)
        if isinstance(doc.get("timestamp"), datetime.datetime):
            doc["timestamp"] = doc["timestamp"].isoformat()
        return JSONResponse(content={"status": "ok", "data": doc}, status_code=200)
    except Exception as e:
        print("❌ ERROR in /latest:", e)
        raise HTTPException(status_code=500, detail=str(e))
