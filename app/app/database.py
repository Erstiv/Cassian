"""
DATABASE CONNECTION
Sets up the SQLite database and gives the rest of the app a way to
get a database session (a connection for reading/writing).

SQLite stores everything in a single file: cassian.db
No database server needed — just a file on disk.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from pathlib import Path
from .models import Base

# ── Database file location ─────────────────────────────────────────────────────
# Stored at the project root, one level above the app/ folder
DB_PATH = Path(__file__).resolve().parent.parent / "cassian.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

# ── Engine ─────────────────────────────────────────────────────────────────────
# connect_args check_same_thread=False is required for SQLite when used
# with FastAPI (which handles requests across multiple threads)
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,   # Set to True to see all SQL queries in the terminal (useful for debugging)
)

# ── Session factory ────────────────────────────────────────────────────────────
# A "session" is one unit of work with the database — open it, do stuff, close it
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create all tables in the database if they don't exist yet.
    Safe to call on every startup — won't overwrite existing data.
    Also runs lightweight column migrations for new fields added after initial creation."""
    Base.metadata.create_all(bind=engine)

    # ── Lightweight migrations ────────────────────────────────────────────────
    # SQLite supports ALTER TABLE ADD COLUMN. We just try each new column and
    # swallow the error if it already exists — no migration framework needed.
    new_columns = [
        "ALTER TABLE projects ADD COLUMN layout_mode VARCHAR(32) DEFAULT 'novel'",
        "ALTER TABLE projects ADD COLUMN chapter_order JSON DEFAULT '[]'",
        # Foundation v2: user ownership and genre
        "ALTER TABLE projects ADD COLUMN user_id INTEGER REFERENCES users(id)",
        "ALTER TABLE projects ADD COLUMN genre VARCHAR(64) DEFAULT 'fiction'",
    ]
    with engine.connect() as conn:
        for stmt in new_columns:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # Column already exists — that's fine

    print(f"  ✓  Database ready: {DB_PATH}")


def get_db():
    """
    FastAPI dependency — provides a database session per request.
    Automatically closes the session when the request is done.

    Usage in a route:
        from app.database import get_db
        from sqlalchemy.orm import Session
        from fastapi import Depends

        @app.get("/projects")
        def list_projects(db: Session = Depends(get_db)):
            return db.query(Project).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
