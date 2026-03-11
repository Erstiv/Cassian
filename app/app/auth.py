"""
╔══════════════════════════════════════════════════════════════════╗
║  PROJECT CASSIAN — AUTHENTICATION                                ║
║                                                                  ║
║  Handles:                                                        ║
║    • Password hashing (bcrypt)                                   ║
║    • Session cookie reading → current user lookup                ║
║    • Route-level auth dependencies (require_user, require_admin) ║
║    • Google OAuth client setup                                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

from fastapi import Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import bcrypt

from app.database import get_db
from app.models import User

# ── Password Hashing ──────────────────────────────────────────────
# bcrypt is slow by design — makes brute-force attacks impractical
# Using bcrypt directly (passlib has compatibility issues with newer bcrypt)


def hash_password(plain_password: str) -> str:
    """Hash a password for storage."""
    password_bytes = plain_password.encode("utf-8")[:72]  # bcrypt max is 72 bytes
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a password against its hash. Returns True if they match."""
    password_bytes = plain_password.encode("utf-8")[:72]
    hash_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(password_bytes, hash_bytes)


# ── Current User from Session ─────────────────────────────────────

def get_current_user(request: Request, db: Session = Depends(get_db)):
    """
    Read user_id from the session cookie and look up the User.
    Returns the User object, or None if not logged in.

    Use this when you want OPTIONAL auth (e.g. showing a login button
    vs. a user menu in the template).
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    return user


def require_user(request: Request, db: Session = Depends(get_db)):
    """
    Like get_current_user, but redirects to /login if not authenticated.
    Use this on any route that REQUIRES a logged-in user.
    """
    user = get_current_user(request, db)
    if user is None:
        # Save where they were trying to go, so we can redirect back after login
        request.session["next_url"] = str(request.url)
        return RedirectResponse("/login", status_code=303)
    return user


def require_admin(request: Request, db: Session = Depends(get_db)):
    """
    Requires a logged-in user who is also an admin.
    Non-admins get a 403 (but we redirect to dashboard instead of showing an error).
    """
    user = require_user(request, db)
    # require_user may return a RedirectResponse if not logged in
    if isinstance(user, RedirectResponse):
        return user
    if not user.is_admin:
        return RedirectResponse("/", status_code=303)
    return user


# ── Session Helpers ───────────────────────────────────────────────

def login_user(request: Request, user: User):
    """Set the session cookie after successful authentication."""
    request.session["user_id"] = user.id


def logout_user(request: Request):
    """Clear the session cookie."""
    request.session.clear()
