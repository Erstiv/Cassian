"""
╔══════════════════════════════════════════════════════════════════╗
║  CASSIAN — MAIN ENTRY POINT                                      ║
║                                                                  ║
║  How to run (from the Cassian/app/ folder):                     ║
║    uvicorn main:app --reload --host 0.0.0.0 --port 8000         ║
║                                                                  ║
║  Then open:  http://localhost:8000                               ║
║                                                                  ║
║  On Hetzner, Nginx will proxy port 8000 and serve it publicly.  ║
╚══════════════════════════════════════════════════════════════════╝
"""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from pathlib import Path

from app.database import init_db
from app.routes import projects, runs, world_rules, dev_editor, copy_line_editor, workbench, illustrations, layout, cover, fonts, proofread, idea, framework, draft_writer, diversity_reader, metadata, export, consistency, chapter_manager


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
    version     = "0.1.0",
    lifespan    = lifespan,
    docs_url    = "/api/docs",   # FastAPI's built-in API explorer at /api/docs
)


# ── Static files (CSS, JS, images) ────────────────────────────────────────────
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "app" / "static"),
    name="static",
)

# ── Routes ─────────────────────────────────────────────────────────────────────
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
