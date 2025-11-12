from pydantic import BaseSettings

class Settings(BaseSettings):
    MONGO_URI: str = "mongodb://localhost:27017"
    DB_NAME: str = "stockbot"
    POLL_INTERVAL_SECONDS: int = 60
    START_DATE: str = "2024-01-01"
    END_DATE: str = "2025-01-01"
    NIFTY100_FILE: str = "nifty100.csv"
    DRY_RUN: bool = True

    class Config:
        env_file = ".env"

settings = Settings()
