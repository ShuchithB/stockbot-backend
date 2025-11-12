from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import time
import os
import json
from apscheduler.schedulers.background import BackgroundScheduler

# === Import your backtest logic modules ===
# (If your code is inside app/strategy.py or app/core.py)
# from app.strategy import run_backtest
# from app.storage import log_trade, list_trades

app = FastAPI(title="📈 StockBot Backend", version="1.0.0")

# === CORS for frontend (React) ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # (change later for security)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Health Check ===
@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}

# === Root Route ===
@app.get("/")
def root():
    return {
        "message": "✅ StockBot backend is live and ready!",
        "docs": "Visit /docs for API documentation.",
        "repo": "https://github.com/YOUR_GITHUB_USERNAME/stockbot-backend"
    }

# === Example model for POST requests ===
class RunRequest(BaseModel):
    api_key: str
    access_token: str
    start_date: str = "2024-01-01"
    end_date: str = "2025-01-01"

# === Placeholder backtest function ===
def mock_backtest(api_key, access_token, start_date, end_date):
    # Simulate running your algorithm
    time.sleep(2)
    return {
        "api_key_used": api_key[-4:],  # just show partial key for confirmation
        "start_date": start_date,
        "end_date": end_date,
        "summary": {
            "Total PnL": 134000,
            "Win Rate %": 57.2,
            "Trades": 82,
            "Expectancy": 420.7
        }
    }

# === Manual trigger endpoint ===
@app.post("/run_once")
def run_once(data: RunRequest, background_tasks: BackgroundTasks):
    try:
        # You can later replace mock_backtest() with your real backtest
        background_tasks.add_task(mock_backtest, data.api_key, data.access_token, data.start_date, data.end_date)
        return {"status": "started", "message": "Backtest launched in background."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# === Check trade logs (placeholder) ===
@app.get("/trades")
def get_trades():
    # Replace this with MongoDB call if connected
    mock_data = [
        {"symbol": "RELIANCE", "PnL": 1200, "action": "BUY", "date": "2024-06-12"},
        {"symbol": "TCS", "PnL": -300, "action": "SELL", "date": "2024-06-13"},
    ]
    return {"trades": mock_data, "count": len(mock_data)}

# === Background scheduler (optional for auto-run) ===
scheduler = BackgroundScheduler()

def scheduled_task():
    print("📊 Scheduled backtest triggered at", time.ctime())

scheduler.add_job(scheduled_task, "interval", hours=24)
scheduler.start()

@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)
    print("🛑 Scheduler stopped cleanly.")

