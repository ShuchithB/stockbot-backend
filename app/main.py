from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from .config import settings
from .adapter import execute_once_and_persist
from .storage import list_trades
import time

try:
    from kiteconnect import KiteConnect
    KITE_LIB_AVAILABLE = True
except Exception:
    KITE_LIB_AVAILABLE = False

app = FastAPI(title="StockOrderBot API")

# Allow React dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

scheduler = BackgroundScheduler()
JOB_ID = "algo_job"
STATE = {"running": False}

class KiteExchangeIn(BaseModel):
    api_key: str
    api_secret: str
    request_token: str

class RunIn(BaseModel):
    api_key: str = None
    access_token: str = None
    symbols: list = None
    start_date: str = None
    end_date: str = None

@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}

@app.post("/kite/exchange")
def kite_exchange(body: KiteExchangeIn):
    if not KITE_LIB_AVAILABLE:
        raise HTTPException(500, "kiteconnect not available")
    try:
        kite = KiteConnect(api_key=body.api_key)
        session = kite.generate_session(body.request_token, api_secret=body.api_secret)
        return {"access_token": session.get("access_token"), "user": session.get("user_id")}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/run_once")
def run_once(body: RunIn = Body(...)):
    kite_creds = None
    if body.api_key and body.access_token:
        kite_creds = {"api_key": body.api_key, "access_token": body.access_token}
    result = execute_once_and_persist(symbols=body.symbols, start_date=body.start_date, end_date=body.end_date, kite_creds=kite_creds)
    return {"summary": result.get("summary"), "final_equity_avg": result.get("final_equity_avg"), "trades_count": len(result.get("trades", []))}

@app.post("/start")
def start(interval_seconds: int = settings.POLL_INTERVAL_SECONDS):
    if STATE["running"]:
        return {"status": "already running"}
    scheduler.add_job(lambda: execute_once_and_persist(), "interval", seconds=interval_seconds, id=JOB_ID)
    scheduler.start()
    STATE["running"] = True
    return {"status": "started", "interval_seconds": interval_seconds}

@app.post("/stop")
def stop():
    if not STATE["running"]:
        return {"status": "not running"}
    scheduler.remove_job(JOB_ID)
    STATE["running"] = False
    return {"status": "stopped"}

@app.get("/status")
def status():
    return {"running": STATE["running"]}

@app.get("/trades")
def trades(limit: int = 100):
    data = list_trades(limit)
    for rec in data:
        rec["_id"] = str(rec["_id"])
    return {"count": len(data), "trades": data}
