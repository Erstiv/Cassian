"""
╔══════════════════════════════════════════════════════════════════╗
║  PROJECT CASSIAN — APPLICATION CONFIG                            ║
║                                                                  ║
║  All settings come from environment variables (loaded via .env). ║
║  Never hardcode secrets in source code.                          ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────
APP_DIR     = Path(__file__).resolve().parent.parent   # Cassian/app/
CASSIAN_DIR = APP_DIR.parent                            # Cassian/

# ── Secrets & Keys ─────────────────────────────────────────────────
# SESSION_SECRET signs the browser cookie. Generate one with:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-in-production-please")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── Google OAuth ───────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://planterpruner.com/auth/google/callback"
)

# ── Environment ────────────────────────────────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")  # "development" or "production"
IS_PRODUCTION = ENVIRONMENT == "production"
