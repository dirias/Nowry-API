#!/bin/sh

# Run Black formatter
echo "Running Black formatter..."
black /app

# Start FastAPI
echo "Starting FastAPI..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
