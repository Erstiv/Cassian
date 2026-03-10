#!/bin/bash
# Project Cassian — Production Server
# Called by systemd (cassian.service) — do not run manually unless testing.

cd "$(dirname "$0")/../app"

# Load environment variables from .env
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
else
  echo "ERROR: No .env file found in $(pwd)"
  echo "Copy ../deploy/.env.example to ./app/.env and fill in your GEMINI_API_KEY"
  exit 1
fi

# Create venv on first run
if [ ! -d "venv" ]; then
  echo "  Setting up virtual environment..."
  python3 -m venv venv
fi

# Activate and install deps
source venv/bin/activate
pip install -r requirements.txt -q
pip install -r requirements-pipeline.txt -q

echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║  Project Cassian — Production                    ║"
echo "  ║  Listening on 127.0.0.1:8003                     ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""

uvicorn main:app --host 127.0.0.1 --port 8003 --workers 2
