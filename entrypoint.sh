#!/bin/sh


# Start Server based on environment
if [ "$ENV" = "production" ]; then
    echo "Starting Production Server (Gunicorn)..."
    # 2 workers is safer for Railway starter plans (512MB RAM)
    # Using strict uvicorn for simplicity and better log visibility in Railway
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
else
    echo "Starting Development Server (Uvicorn)..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi
