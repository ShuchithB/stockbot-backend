# Base image
FROM python:3.11-slim

WORKDIR /app

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# 1️⃣ Install git + build tools (required for KiteConnect + numpy/pandas)
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    gcc \
    g++ \
 && rm -rf /var/lib/apt/lists/*

# 2️⃣ Upgrade pip and install dependencies
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# 3️⃣ Copy project files
COPY . .

ENV PYTHONUNBUFFERED=1

# 4️⃣ Start FastAPI server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
