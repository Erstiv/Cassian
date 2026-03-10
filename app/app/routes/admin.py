"""
╔══════════════════════════════════════════════════════════════════╗
║  CASSIAN — ADMIN PANEL                                           ║
║                                                                  ║
║  Only accessible to users with is_admin = True.                  ║
║                                                                  ║
║  /admin              GET  → admin dashboard                      ║
║  /admin/users        GET  → user management table                ║
║  /admin/users/{id}/role     POST → change user role (HTMX)      ║
║  /admin/users/{id}/toggle   POST → enable/disable user (HTMX)   ║
╚══════════════════════════════════════════════════════════════════╝
"""

from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import User, UserRole, Project
from app.auth import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


# ── Admin Dashboard ──────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    """Admin overview — user count, project count, system stats."""
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    stats = {
        "total_users": db.query(func.count(User.id)).scalar(),
        "active_users": db.query(func.count(User.id)).filter(User.is_active == True).scalar(),
        "total_projects": db.query(func.count(Project.id)).scalar(),
        "admin_count": db.query(func.count(User.id)).filter(User.is_admin == True).scalar(),
    }

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "stats": stats,
        "active_page": "admin",
    })


# ── User Management ─────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
def admin_users(request: Request, db: Session = Depends(get_db)):
    """List all users with management controls."""
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    users = db.query(User).order_by(User.created_at.desc()).all()

    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "users": users,
        "roles": [r.value for r in UserRole],
        "active_page": "admin",
    })


@router.post("/users/{user_id}/role")
def change_user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    """Change a user's role (HTMX partial swap)."""
    admin = require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return HTMLResponse("<span class='text-red-400'>User not found</span>")

    # Don't let admin demote themselves
    if target_user.id == admin.id:
        return HTMLResponse("<span class='text-red-400'>Can't change your own role</span>")

    try:
        target_user.role = UserRole(role)
        db.commit()
        return HTMLResponse(
            f"<span class='text-green-400'>Role updated to {role}</span>"
        )
    except ValueError:
        return HTMLResponse("<span class='text-red-400'>Invalid role</span>")


@router.post("/users/{user_id}/toggle")
def toggle_user_active(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
):
    """Enable or disable a user account (HTMX partial swap)."""
    admin = require_admin(request, db)
    if isinstance(admin, RedirectResponse):
        return admin

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return HTMLResponse("<span class='text-red-400'>User not found</span>")

    # Don't let admin disable themselves
    if target_user.id == admin.id:
        return HTMLResponse("<span class='text-red-400'>Can't disable your own account</span>")

    target_user.is_active = not target_user.is_active
    db.commit()

    status = "enabled" if target_user.is_active else "disabled"
    color = "green" if target_user.is_active else "yellow"
    return HTMLResponse(
        f"<span class='text-{color}-400'>Account {status}</span>"
    )
