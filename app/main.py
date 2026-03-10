"""
╔══════════════════════════════════════════════════════════════════╗
║  CASSIAN — MAIN ENTRY POINT                                      ║
║                                                                  ║
║  How to run (from the Cassian/app/ folder):                     ║
║    uvicorn main:app --reload --host 0.0.0.0 --port 8000         ║
║                                                                  ║
║  Then open:  http://localhost:8000                               ║
║                                                                  ║
║  On Hetzner, Nginx will proxy port 8003 and serve it publicly.  ║
╚══════════════════════════════════════════════════════════════════╝
"""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from pathlib import Path
from starlette.middleware.sessions import SessionMiddleware

from app.database import init_db, get_db
from app.config import SESSION_SECRET
from app.models import User
from app.routes import (
    auth, admin,
    projects, runs, world_rules, dev_editor, copy_line_editor,
    workbench, illustrations, layout, cover, fonts, proofread,
    idea, framework, draft_writer, diversity_reader, metadata,
    export, consistency, chapter_manager,
)


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once on startup before accepting requests."""
    print()
    print("  ╔══════════════════════════════════╗")
    print("  ║  CASSIAN  —  starting up         ║")
    print("  ╚══════════════════════════════════╝")
    init_db()
    yield
    # (anything here runs on shutdown)
    print("  Cassian shutting down.")


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "Cassian",
    description = "Book pipeline management: from manuscript to print-ready PDF.",
    version     = "0.2.0",
    lifespan    = lifespan,
    docs_url    = "/api/docs",   # FastAPI's built-in API explorer at /api/docs
)


# ── Session Middleware ────────────────────────────────────────────────────────
# Signs a cookie called "session" with SESSION_SECRET.
# The cookie holds { "user_id": 123 } — nothing sensitive.
# max_age = 30 days in seconds.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="cassian_session",
    max_age=60 * 60 * 24 * 30,      # 30 days
    same_site="lax",                  # CSRF protection
    https_only=False,                 # Set True once HTTPS is live on Filou
)


# ── Middleware: inject current_user into every request ─────────────────────────
@app.middleware("http")
async def inject_user_middleware(request: Request, call_next):
    """
    Reads user_id from the session cookie and attaches the User object
    to request.state.user. Templates can then do {{ request.state.user.name }}.
    If not logged in, request.state.user is None.
    """
    request.state.user = None
    user_id = request.session.get("user_id")
    if user_id:
        db = next(get_db())
        try:
            user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
            request.state.user = user
        finally:
            db.close()
    response = await call_next(request)
    return response


# ── Static files (CSS, JS, images) ────────────────────────────────────────────
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "app" / "static"),
    name="static",
)

# ── Routes ─────────────────────────────────────────────────────────────────────
# Auth routes first (login, register, oauth, logout)
app.include_router(auth.router)
app.include_router(admin.router)

# App routes
app.include_router(projects.router)
app.include_router(runs.router)
app.include_router(world_rules.router)
app.include_router(dev_editor.router)
app.include_router(copy_line_editor.router)
app.include_router(workbench.router)
app.include_router(illustrations.router)
app.include_router(layout.router)
app.include_router(cover.router)
app.include_router(fonts.router)
app.include_router(proofread.router)
app.include_router(diversity_reader.router)
app.include_router(idea.router)
app.include_router(framework.router)
app.include_router(draft_writer.router)
app.include_router(metadata.router)
app.include_router(export.router)
app.include_router(consistency.router)
app.include_router(chapter_manager.router)
