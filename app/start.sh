#!/bin/bash
# Project Cassian — start the development server
# Run from anywhere:  bash ~/Documents/"Elliot Projects"/"CoWork Projects"/Cassian/app/start.sh
# Or just:  ./start.sh  (if you're already in the app folder)

cd "$(dirname "$0")"   # always run from the app folder, regardless of where you call it from

# ── Gemini API key (used by all AI-powered pages) ──
# Load from .env file if it exists, otherwise fall back to env
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "$GEMINI_API_KEY" ]; then
  echo "  ⚠  WARNING: No GEMINI_API_KEY set."
  echo "     Create a .env file with: GEMINI_API_KEY=your-key-here"
  echo "     AI features will not work without it."
  echo ""
fi

# Create the venv if it doesn't exist yet
if [ ! -d "venv" ]; then
  echo "  Setting up virtual environment for the first time..."
  python3 -m venv venv
fi

# Activate and run
source venv/bin/activate
pip install -r requirements.txt -q           # web server deps
pip install -r requirements-pipeline.txt -q  # agent deps (python-docx, google-genai, etc.)

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║  Project Cassian starting up...  ║"
echo "  ║  Open: http://localhost:8000     ║"
echo "  ╚══════════════════════════════════╝"
echo ""

uvicorn main:app --reload --host 0.0.0.0 --port 8000
