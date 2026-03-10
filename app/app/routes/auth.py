"""
╔══════════════════════════════════════════════════════════════════╗
║  CASSIAN — AUTH ROUTES                                           ║
║                                                                  ║
║  /login          GET  → login page                               ║
║  /login          POST → email/password login                     ║
║  /register       GET  → registration page                        ║
║  /register       POST → create account                           ║
║  /auth/google    GET  → redirect to Google consent               ║
║  /auth/google/callback  GET → handle Google OAuth callback       ║
║  /logout         GET  → clear session, redirect to login         ║
╚══════════════════════════════════════════════════════════════════╝
"""

from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from authlib.integrations.starlette_client import OAuth

from app.database import get_db
from app.models import User, UserRole
from app.auth import hash_password, verify_password, login_user, logout_user
from app.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ── Google OAuth Setup ────────────────────────────────────────────
oauth = OAuth()

# Only register Google if credentials are configured
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# ── Login ─────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Show the login form."""
    # If already logged in, go to dashboard
    if request.state.user:
        return RedirectResponse("/", status_code=303)

    error = request.query_params.get("error", "")
    google_enabled = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "google_enabled": google_enabled,
    })


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Handle email/password login."""
    user = db.query(User).filter(User.email == email.strip().lower()).first()

    if not user or not user.password_hash:
        return RedirectResponse("/login?error=Invalid+email+or+password", status_code=303)

    if not verify_password(password, user.password_hash):
        return RedirectResponse("/login?error=Invalid+email+or+password", status_code=303)

    if not user.is_active:
        return RedirectResponse("/login?error=Account+is+disabled", status_code=303)

    # Success — log them in
    user.last_login = datetime.now()
    db.commit()
    login_user(request, user)

    # Redirect to where they were trying to go, or dashboard
    next_url = request.session.pop("next_url", "/")
    return RedirectResponse(next_url, status_code=303)


# ── Registration ──────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    """Show the registration form."""
    if request.state.user:
        return RedirectResponse("/", status_code=303)

    error = request.query_params.get("error", "")
    return templates.TemplateResponse("register.html", {
        "request": request,
        "error": error,
    })


@router.post("/register")
def register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create a new account with email/password."""
    email = email.strip().lower()
    name = name.strip()

    # Validation
    if not name:
        return RedirectResponse("/register?error=Name+is+required", status_code=303)
    if not email or "@" not in email:
        return RedirectResponse("/register?error=Valid+email+is+required", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/register?error=Password+must+be+at+least+8+characters", status_code=303)
    if password != password_confirm:
        return RedirectResponse("/register?error=Passwords+do+not+match", status_code=303)

    # Check if email is taken
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return RedirectResponse("/register?error=Email+already+registered", status_code=303)

    # Create user
    user = User(
        email=email,
        name=name,
        password_hash=hash_password(password),
        role=UserRole.OWNER,
        is_active=True,
        last_login=datetime.now(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Auto-login
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


# ── Google OAuth ──────────────────────────────────────────────────

@router.get("/auth/google")
async def google_login(request: Request):
    """Redirect user to Google's consent screen."""
    if not GOOGLE_CLIENT_ID:
        return RedirectResponse("/login?error=Google+OAuth+not+configured", status_code=303)

    return await oauth.google.authorize_redirect(request, GOOGLE_REDIRECT_URI)


@router.get("/auth/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    """Handle the redirect back from Google after user consents."""
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse("/login?error=Google+authentication+failed", status_code=303)

    # Get user info from the ID token
    user_info = token.get("userinfo")
    if not user_info:
        return RedirectResponse("/login?error=Could+not+get+Google+profile", status_code=303)

    google_id = user_info.get("sub")
    email     = user_info.get("email", "").lower()
    name      = user_info.get("name", "")
    avatar    = user_info.get("picture", "")

    if not email:
        return RedirectResponse("/login?error=Google+account+has+no+email", status_code=303)

    # Look up by google_id first, then by email (to link accounts)
    user = db.query(User).filter(User.google_id == google_id).first()

    if not user:
        # Try matching by email (user registered with email first, now linking Google)
        user = db.query(User).filter(User.email == email).first()
        if user:
            # Link the Google account to existing user
            user.google_id = google_id
            if not user.avatar_url:
                user.avatar_url = avatar
        else:
            # Brand new user
            user = User(
                email=email,
                name=name,
                avatar_url=avatar,
                google_id=google_id,
                role=UserRole.OWNER,
                is_active=True,
            )
            db.add(user)

    if not user.is_active:
        return RedirectResponse("/login?error=Account+is+disabled", status_code=303)

    # Update last login
    user.last_login = datetime.now()
    if name and not user.name:
        user.name = name
    db.commit()
    db.refresh(user)

    login_user(request, user)

    next_url = request.session.pop("next_url", "/")
    return RedirectResponse(next_url, status_code=303)


# ── Logout ────────────────────────────────────────────────────────

@router.get("/logout")
def logout(request: Request):
    """Clear session and redirect to login."""
    logout_user(request)
    return RedirectResponse("/login", status_code=303)
