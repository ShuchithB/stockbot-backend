# app/main.py
import os
import uuid
import time
import json
import asyncio
import datetime
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

from kiteconnect import KiteConnect

# Async Mongo
import motor.motor_asyncio

# -----------------------
# Configuration / env
# -----------------------
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")  # must match the Zerodha app redirect
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://stockbot-dashboard.onrender.com")

TOKEN_EXPIRY_HOURS = float(os.getenv("TOKEN_EXPIRY_HOURS", 23.5))  # treat > this as expired

# Threadpool for running sync (strategy) code without blocking event loop
THREADPOOL = ThreadPoolExecutor(max_workers=int(os.getenv("THREADPOOL_SIZE", "4")))

# -----------------------
# Application + DB init
# -----------------------
app = FastAPI(title="StockBot Backend (Enterprise)", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lock down for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not MONGO_URI:
    print("⚠️ MONGO_URI is not set. Please set it in environment variables.")

# motor async client
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client[DB_NAME] if mongo_client else None

# In-memory job registry for running tasks. Single-instance only (works fine on Render single instance).
_jobs: Dict[str, Dict[str, Any]] = {}
_tasks: Dict[str, asyncio.Task] = {}

# -----------------------
# Strategy registry
# -----------------------
# Strategies must be synchronous functions (for now). They will be run in threadpool.
# Expected signature: run_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE) -> dict
# The dict should contain at least keys "trades" (list) and "equity" (list) or "equity_curve".
try:
    from app.swing_strategy import run_backtest as run_swing_backtest
except Exception as e:
    print("⚠️ Could not import swing_strategy:", e)
    run_swing_backtest = None

def run_momentum_backtest(API_KEY, ACCESS_TOKEN, START_DATE, END_DATE, NIFTY100_FILE):
    """Lightweight fallback momentum strategy (synchronous)."""
    try:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(ACCESS_TOKEN)
        inst = kite.ltp("NSE:RELIANCE")
        token = list(inst.values())[0]["instrument_token"]
        raw = kite.historical_data(instrument_token=token, from_date=START_DATE, to_date=END_DATE, interval="day")
        if not raw:
            return {"trades": [], "equity": []}
        import pandas as pd
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])
        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()
        trades, equity_curve = [], []
        eq = 1_000_000.0
        position = 0
        entry = 0.0
        for i, row in df.iterrows():
            equity_curve.append({"date": str(row["date"].date()), "portfolio_equity": round(eq,2)})
            if i < 50:
                continue
            price = float(row["close"])
            if position == 0 and row["ema20"] > row["ema50"]:
                position = 1
                entry = price
                trades.append({"Symbol":"RELIANCE","Date":str(row["date"].date()),"Action":"BUY","Price":round(entry,2),"Qty":1})
            elif position == 1 and row["ema20"] < row["ema50"]:
                pnl = price - entry
                eq += pnl
                trades.append({"Symbol":"RELIANCE","Date":str(row["date"].date()),"Action":"SELL","Price":round(price,2),"Qty":1,"PnL":round(pnl,2)})
                position = 0
        return {"trades": trades, "equity": equity_curve}
    except Exception as e:
        print("Momentum run error:", e)
        return {"trades": [], "equity": []}

STRATEGIES = {
    "swing": run_swing_backtest,
    "momentum": run_momentum_backtest
}

# -----------------------
# Helpers: DB and token
# -----------------------
def db():
    if not mongo_db:
        raise RuntimeError("MongoDB not configured (MONGO_URI missing).")
    return mongo_db

async def save_access_token_async(access_token: str):
    """Save access token (with timestamp) to config collection."""
    now = datetime.datetime.utcnow()
    cfg = {
        "name": "kite_access_token",
        "access_token": access_token,
        "created_at": now
    }
    await db()["config"].update_one({"name":"kite_access_token"}, {"$set": cfg}, upsert=True)
    print("✅ Saved Kite access token (async)")

async def get_access_token_doc():
    doc = await db()["config"].find_one({"name":"kite_access_token"})
    return doc

def is_token_doc_expired(doc: Optional[dict]) -> bool:
    if not doc:
        return True
    created = doc.get("created_at")
    if not created:
        return True
    # created might be stored as datetime (motor returns datetime)
    if isinstance(created, str):
        created_dt = datetime.datetime.fromisoformat(created)
    else:
        created_dt = created
    age_hours = (datetime.datetime.utcnow() - created_dt).total_seconds() / 3600.0
    return age_hours > TOKEN_EXPIRY_HOURS

async def save_backtest_result_async(record: dict) -> dict:
    """
    Standardize and save backtest record.
    Returns saved record (without _id) and timestamp in ISO.
    """
    rec = {
        "timestamp": datetime.datetime.utcnow(),
        "strategy": record.get("strategy", "unknown"),
        "symbols": record.get("symbols", []),
        "summary": record.get("summary", {}),
        "equity_curve": record.get("equity", record.get("equity_curve", [])),
        "trades": record.get("trades", [])
    }
    res = await db()["backtests"].insert_one(rec)
    # convert timestamp to iso for return
    rec_out = rec.copy()
    rec_out["timestamp"] = rec_out["timestamp"].isoformat()
    return rec_out

# -----------------------
# Models
# -----------------------
class StrategyRequest(BaseModel):
    strategy: str = "swing"
    start_date: str = "2024-03-01"
    end_date: str = "2025-11-10"
    symbols_file: str = "nifty100.csv"

# -----------------------
# Endpoints
# -----------------------
@app.get("/health")
async def health():
    try:
        # simple DB ping
        if mongo_db:
            await mongo_db.command("ping")
            db_ok = True
        else:
            db_ok = False
    except Exception:
        db_ok = False
    return {"status": "ok", "time": time.time(), "db_connected": db_ok}

@app.get("/")
async def root():
    return {"message": "StockBot Enterprise Backend — /generate_token_url /kite/callback /run_strategy /job_status /backtests /latest"}

@app.get("/generate_token_url")
async def generate_token_url():
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
async def kite_callback(request_token: Optional[str] = None, status: Optional[str] = None):
    """
    Zerodha redirects with ?request_token=...&status=success
    We call kite.generate_session to get access_token and save it.
    Then redirect to frontend with token_success or token_error.
    """
    try:
        if not request_token:
            return JSONResponse(status_code=400, content={"detail":"Missing request_token in callback URL"})
        if not KITE_API_KEY or not KITE_API_SECRET:
            return JSONResponse(status_code=500, content={"detail":"Kite API key/secret not configured"})
        kite = KiteConnect(api_key=KITE_API_KEY)
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = data.get("access_token")
        if not access_token:
            print("Kite callback: no access_token returned", data)
            return RedirectResponse(url=f"{FRONTEND_URL}?token_error=true")
        # save token (async)
        await save_access_token_async(access_token)
        return RedirectResponse(url=f"{FRONTEND_URL}?token_success=true")
    except Exception as e:
        print("kite_callback error:", e)
        return RedirectResponse(url=f"{FRONTEND_URL}?token_error=true")

# -----------------------
# Job runner & management
# -----------------------
def _normalize_strategy_result(result: dict) -> dict:
    """Ensure result dict contains trades and equity lists."""
    if not isinstance(result, dict):
        return {"trades": [], "equity": []}
    trades = result.get("trades", []) or result.get("trade_log", []) or []
    equity = result.get("equity", []) or result.get("equity_curve", []) or []
    return {"trades": trades, "equity": equity, **{k:v for k,v in result.items() if k not in ("trades","equity","equity_curve","trade_log")} }

async def _run_strategy_job(job_id: str, payload: dict):
    """Internal runner executed inside an asyncio Task."""
    _jobs[job_id]["status"] = "running"
    _jobs[job_id]["started_at"] = datetime.datetime.utcnow().isoformat()
    try:
        strategy = payload["strategy"]
        start_date = payload["start_date"]
        end_date = payload["end_date"]
        symbols_file = payload["symbols_file"]
        access_token_doc = await get_access_token_doc()
        if is_token_doc_expired(access_token_doc):
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = "Kite access token expired or missing. Re-login required."
            return

        access_token = access_token_doc["access_token"]
        strategy_fn = STRATEGIES.get(strategy)
        if strategy_fn is None:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = f"Strategy not available: {strategy}"
            return

        # Run strategy in threadpool (since it's synchronous)
        loop = asyncio.get_running_loop()
        print(f"[job {job_id}] launching strategy {strategy} in threadpool...")
        result = await loop.run_in_executor(
            THREADPOOL,
            lambda: strategy_fn(API_KEY=KITE_API_KEY, ACCESS_TOKEN=access_token, START_DATE=start_date, END_DATE=end_date, NIFTY100_FILE=symbols_file)
        )
        norm = _normalize_strategy_result(result)
        # compute simple summary if trades include PnL
        summary = {}
        try:
            import pandas as pd
            if norm["trades"]:
                tdf = pd.DataFrame(norm["trades"])
                if "PnL" in tdf.columns:
                    total_pnl = float(tdf["PnL"].fillna(0).sum())
                    wins = int((tdf["PnL"] > 0).sum())
                    losses = int((tdf["PnL"] <= 0).sum())
                    win_rate = float(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
                    summary = {"Total PnL": round(total_pnl,2), "Win Rate %": round(win_rate,2), "Trades": int(len(tdf))}
        except Exception as e:
            print(f"[job {job_id}] summary calc error:", e)

        record = {
            "strategy": strategy,
            "symbols": norm.get("symbols", []),
            "summary": summary,
            "equity": norm["equity"],
            "trades": norm["trades"]
        }

        saved = await save_backtest_result_async(record)
        _jobs[job_id]["status"] = "completed"
        _jobs[job_id]["finished_at"] = datetime.datetime.utcnow().isoformat()
        _jobs[job_id]["result"] = saved
        print(f"[job {job_id}] completed and saved.")
    except asyncio.CancelledError:
        print(f"[job {job_id}] cancelled.")
        _jobs[job_id]["status"] = "cancelled"
        _jobs[job_id]["cancelled_at"] = datetime.datetime.utcnow().isoformat()
    except Exception as e:
        print(f"[job {job_id}] failed:", e)
        _jobs[job_id]["status"] = "failed"
        _jobs[job_id]["error"] = str(e)

@app.post("/run_strategy")
async def run_strategy(req: StrategyRequest):
    """
    Start a backtest job. Returns job_id to poll status.
    """
    if req.strategy not in STRATEGIES:
        raise HTTPException(status_code=400, detail="Unknown strategy")

    # check token existence/expiry
    token_doc = await get_access_token_doc()
    if not token_doc:
        raise HTTPException(status_code=400, detail="No Kite access token found. Generate via /generate_token_url and login first.")
    if is_token_doc_expired(token_doc):
        raise HTTPException(status_code=400, detail="Kite access token expired. Please login again via /generate_token_url.")

    job_id = str(uuid.uuid4())
    payload = {
        "strategy": req.strategy,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "symbols_file": req.symbols_file
    }
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "payload": payload,
        "created_at": datetime.datetime.utcnow().isoformat()
    }

    # create asyncio task and store it (for cancellation)
    task = asyncio.create_task(_run_strategy_job(job_id, payload))
    _tasks[job_id] = task

    return {"status": "started", "job_id": job_id}

@app.get("/job_status/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.post("/cancel_job/{job_id}")
async def cancel_job(job_id: str):
    task = _tasks.get(job_id)
    if not task:
        raise HTTPException(status_code=404, detail="Job not running or not found")
    cancelled = task.cancel()
    _jobs[job_id]["status"] = "cancelling"
    return {"cancelled": cancelled}

# -----------------------
# Backtests / history endpoints
# -----------------------
@app.get("/backtests")
async def get_backtests(limit: int = 50):
    try:
        c = db()["backtests"].find({}, {"_id": 0}).sort([("timestamp", -1)]).limit(limit)
        results = []
        async for doc in c:
            # ensure timestamp iso string
            ts = doc.get("timestamp")
            if isinstance(ts, datetime.datetime):
                doc["timestamp"] = ts.isoformat()
            results.append(doc)
        return JSONResponse(content={"status": "ok", "count": len(results), "data": results}, status_code=200)
    except Exception as e:
        print("ERROR /backtests:", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/latest")
async def get_latest():
    try:
        doc = await db()["backtests"].find_one({}, {"_id": 0}, sort=[("timestamp", -1)])
        if not doc:
            return JSONResponse(content={"status":"empty","data":None}, status_code=200)
        ts = doc.get("timestamp")
        if isinstance(ts, datetime.datetime):
            doc["timestamp"] = ts.isoformat()
        return JSONResponse(content={"status":"ok","data":doc}, status_code=200)
    except Exception as e:
        print("ERROR /latest:", e)
        raise HTTPException(status_code=500, detail=str(e))

# -----------------------
# Debug endpoint: show if token exists (safe for dev only)
# -----------------------
@app.get("/debug/token")
async def debug_token():
    doc = await get_access_token_doc()
    exists = bool(doc and doc.get("access_token"))
    token_preview = None
    if exists:
        token_preview = doc["access_token"][-8:] if isinstance(doc["access_token"], str) else str(doc["access_token"])
    created_at = doc.get("created_at").isoformat() if doc and isinstance(doc.get("created_at"), datetime.datetime) else None
    return {"has_token": exists, "token_preview": token_preview, "created_at": created_at}

# -----------------------
# Shutdown handling: cancel running tasks (best-effort)
# -----------------------
@app.on_event("shutdown")
async def shutdown_event():
    print("Shutdown: cancelling running backtests...")
    for job_id, task in list(_tasks.items()):
        if not task.done():
            task.cancel()
            _jobs[job_id]["status"] = "cancelled"
    # allow some short time for tasks to cancel
    await asyncio.sleep(0.2)
    print("Shutdown complete.")
