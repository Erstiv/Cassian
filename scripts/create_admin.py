#!/usr/bin/env python3
"""
Create the admin user and assign all orphaned projects to them.

Run once after first deployment:
    cd Cassian/app
    python3 ../scripts/create_admin.py

You'll be prompted for name, email, and password.
"""

import sys
from pathlib import Path

# Add the app directory to the path so we can import app modules
app_dir = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(app_dir))

from app.database import SessionLocal, init_db
from app.models import User, UserRole, Project
from app.auth import hash_password


def main():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║  Cassian — Create Admin User          ║")
    print("  ╚══════════════════════════════════════╝")
    print()

    # Ensure tables exist
    init_db()

    db = SessionLocal()

    # Check if an admin already exists
    existing_admin = db.query(User).filter(User.is_admin == True).first()
    if existing_admin:
        print(f"  Admin already exists: {existing_admin.email}")
        print(f"  If you need to reset, edit the database directly.")
        db.close()
        return

    # Gather info
    name = input("  Name: ").strip()
    email = input("  Email: ").strip().lower()
    password = input("  Password (min 8 chars): ").strip()

    if len(password) < 8:
        print("  ERROR: Password must be at least 8 characters.")
        db.close()
        return

    # Check for existing user with this email
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        print(f"  User {email} already exists — promoting to admin.")
        existing.is_admin = True
        existing.password_hash = hash_password(password)
        if name:
            existing.name = name
        admin_user = existing
    else:
        admin_user = User(
            email=email,
            name=name,
            password_hash=hash_password(password),
            role=UserRole.OWNER,
            is_admin=True,
            is_active=True,
        )
        db.add(admin_user)

    db.flush()

    # Assign all orphaned projects (user_id = NULL) to this admin
    orphaned = db.query(Project).filter(Project.user_id == None).all()
    for project in orphaned:
        project.user_id = admin_user.id

    db.commit()

    print()
    print(f"  ✓  Admin created: {admin_user.email} (id={admin_user.id})")
    if orphaned:
        print(f"  ✓  Assigned {len(orphaned)} orphaned project(s) to admin")
    else:
        print(f"  ·  No orphaned projects found")
    print()

    db.close()


if __name__ == "__main__":
    main()
