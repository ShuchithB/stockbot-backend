from .user_algo import run_once
from .storage import log_trade, log_event
import time

def execute_once_and_persist(symbols=None, start_date=None, end_date=None, kite_creds=None):
    ts = time.time()
    try:
        result = run_once(symbols=symbols, start_date=start_date, end_date=end_date, kite_creds=kite_creds)
        trades = result.get("trades", [])
        for t in trades:
            t["ts"] = ts
            log_trade(t)
        log_event({"msg": "run_success", "count": len(trades), "ts": ts})
        return result
    except Exception as e:
        log_event({"msg": "run_error", "err": str(e), "ts": ts})
        raise
