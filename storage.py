from pymongo import MongoClient
from .config import settings
from typing import Dict, Any

_client = None

def get_db():
    global _client
    if not _client:
        _client = MongoClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    return _client[settings.DB_NAME]

def log_trade(record: Dict[str, Any]):
    db = get_db()
    db.trades.insert_one(record)

def log_event(record: Dict[str, Any]):
    db = get_db()
    db.logs.insert_one(record)

def list_trades(limit: int = 100):
    db = get_db()
    return list(db.trades.find().sort("ts", -1).limit(limit))
