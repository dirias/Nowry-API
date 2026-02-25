#!/bin/bash

# Nowry Test User Seed Script Runner
# Makes it easy to seed test data

set -e  # Exit on error

echo ""
echo "============================================================"
echo "🚀 NOWRY TEST USER SEED SCRIPT RUNNER"
echo "============================================================"
echo ""

# Check if we're in the right directory
if [ ! -f "seed_test_user.py" ]; then
    echo "❌ Error: seed_test_user.py not found"
    echo "   Please run this script from the Nowry-API directory"
    echo ""
    exit 1
fi

# Check if MongoDB is running
echo "🔍 Checking MongoDB connection..."
if ! python3 -c "from app.config.database import db; import asyncio; asyncio.run(db.command('ping'))" 2>/dev/null; then
    echo "⚠️  Warning: Cannot connect to MongoDB"
    echo "   Make sure MongoDB is running (e.g., docker-compose up -d mongodb)"
    echo ""
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# Run the seed script
echo ""
echo "🌱 Running seed script..."
echo ""

python3 seed_test_user.py

# Check exit code
if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Seed completed successfully!"
    echo ""
    echo "📝 Next steps:"
    echo "   1. Start the API server: uvicorn app.main:app --reload"
    echo "   2. Start the frontend: cd ../nowry && npm start"
    echo "   3. Login with email: dev@nowry.com"
    echo "      (You'll need to create a Firebase Auth user manually)"
    echo ""
else
    echo ""
    echo "❌ Seed failed with error code $?"
    echo "   Check the error messages above"
    echo ""
    exit 1
fi
