import requests
from kiteconnect import KiteConnect
import pymongo
import os

# ============== CONFIGURATION ==============
API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
REDIRECT_URL = os.getenv("KITE_REDIRECT_URL")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "stockbot")

# ===========================================

def update_access_token():
    print("🔁 Starting Access Token Refresh Process...")

    # Step 1: Ask user to log in manually the first time only
    login_url = f"https://api.kite.trade/connect/login?api_key={API_KEY}"
    print(f"➡️ Visit this link once to get the request_token:\n{login_url}")

    request_token = input("Paste your request_token from URL: ").strip()

    # Step 2: Generate session
    kite = KiteConnect(api_key=API_KEY)
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]

    print("✅ Access token generated successfully:", access_token)

    # Step 3: Save token in MongoDB for backend use
    client = pymongo.MongoClient(MONGO_URI)
    db = client[DB_NAME]
    config = db["config"]

    config.update_one(
        {"name": "kite_access_token"},
        {"$set": {"access_token": access_token}},
        upsert=True
    )

    print("💾 Access token saved to MongoDB successfully.")

if __name__ == "__main__":
    update_access_token()
