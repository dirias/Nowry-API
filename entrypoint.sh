#!/bin/sh

# Run Black formatter
echo "Running Black formatter..."
black /app

# Start Server based on environment
if [ "$ENV" = "production" ]; then
    echo "Starting Production Server (Gunicorn)..."
    # 2 workers is safer for Railway starter plans (512MB RAM)
    exec gunicorn app.main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000}
else
    echo "Starting Development Server (Uvicorn)..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi
