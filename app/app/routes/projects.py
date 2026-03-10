"""
PROJECT ROUTES
Handles: dashboard, create project, project detail, manuscript upload.
All page routes return HTML (via Jinja2 templates).
HTMX fragment routes return partial HTML for in-page updates.
"""

import json
import shutil
import zipfile
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import Response as FastAPIResponse, JSONResponse
from typing import List
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Project, ProjectStatus, OutputProfile,
    Publisher, BookFormat, CoverType
)

router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Where uploaded manuscripts are stored on disk
UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

# Cassian root (for checking project output directories)
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"


# ─────────────────────────────────────────────────────────────────
#  DASHBOARD  —  GET /
# ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Main dashboard — lists all projects."""
    projects_list = (
        db.query(Project)
        .order_by(Project.updated_at.desc())
        .all()
    )
    return templates.TemplateResponse("dashboard.html", {
        "request":  request,
        "projects": projects_list,
    })


# ─────────────────────────────────────────────────────────────────
#  NEW PROJECT FORM  —  GET /projects/new
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/new", response_class=HTMLResponse)
def new_project_form(request: Request):
    """Render the new project creation form."""
    return templates.TemplateResponse("project_new.html", {
        "request":    request,
        "publishers": [p.value for p in Publisher],
        "formats":    [f.value for f in BookFormat],
    })


# ─────────────────────────────────────────────────────────────────
#  CREATE PROJECT  —  POST /projects
# ─────────────────────────────────────────────────────────────────

@router.post("/projects")
def create_project(
    request:     Request,
    name:        str   = Form(...),
    author:      str   = Form(...),
    description: str   = Form(""),
    publisher:   str   = Form("lulu"),
    book_format: str   = Form("hardcover_casewrap"),
    layout_mode: str   = Form("novel"),
    db:          Session = Depends(get_db),
):
    """Create a new project and its default output profile, then redirect."""

    # Create the project
    project = Project(
        name        = name.strip(),
        author      = author.strip(),
        description = description.strip(),
        status      = ProjectStatus.DRAFT,
        layout_mode = layout_mode,
    )
    db.add(project)
    db.flush()  # get the project ID without committing yet

    # Create a default output profile using the Lulu spine formula
    profile = OutputProfile(
        project_id  = project.id,
        name        = _profile_name(publisher, book_format),
        publisher   = publisher,
        book_format = book_format,
        cover_type  = _default_cover_type(book_format),
        is_default  = True,
        **_format_specs(book_format),
        spine_formula = _spine_formula(publisher),
    )
    db.add(profile)
    db.commit()
    db.refresh(project)

    # Create config.json immediately so Gemini API key is always available
    _create_project_config(project)

    return RedirectResponse(f"/projects/{project.id}", status_code=303)


# ─────────────────────────────────────────────────────────────────
#  PROJECT DETAIL  —  GET /projects/{id}
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: int, request: Request, db: Session = Depends(get_db)):
    """Project detail page — shows status, runs, upload area."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    profiles = project.output_profiles
    runs     = sorted(project.runs, key=lambda r: r.created_at, reverse=True)
    chapters = _list_chapters(project)

    # Check if ingested chapter files already exist (e.g. from Draft Writer)
    ingested_dir = PROJECTS_DIR / str(project_id) / "output" / "ingested"
    ingested_count = 0
    if ingested_dir.exists():
        ingested_count = len(list(ingested_dir.glob("chapter_*.json")))

    return templates.TemplateResponse("project_detail.html", {
        "request":         request,
        "project":         project,
        "active_page":     "project_detail",
        "profiles":        profiles,
        "runs":            runs,
        "chapters":        chapters,
        "ingested_count":  ingested_count,
    })


# ─────────────────────────────────────────────────────────────────
#  UPLOAD MANUSCRIPT  —  POST /projects/{id}/upload
#  Accepts a .zip of .docx files OR individual .docx files.
#  Extracts them to uploads/{project_id}/chapters/
#  Updates project status and chapter_count.
#  Returns an HTMX fragment (partial HTML) — no full page reload.
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/upload", response_class=HTMLResponse)
async def upload_manuscript(
    project_id: int,
    request:    Request,
    files:      List[UploadFile] = File(...),
    db:         Session          = Depends(get_db),
):
    """
    Handle manuscript upload.
    Accepts multiple files at once (.zip, .docx, .doc, .txt).
    On success, sends HX-Refresh so HTMX reloads the full page —
    this updates the chapter count, Info panel, and New Run button.
    """
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    dest_dir = UPLOADS_DIR / str(project_id) / "chapters"
    dest_dir.mkdir(parents=True, exist_ok=True)

    accepted      = (".docx", ".doc", ".txt")
    added         = 0
    skipped       = []

    try:
        for file in files:
            filename = file.filename or "upload"
            suffix   = Path(filename).suffix.lower()

            if suffix == ".zip":
                zip_path = UPLOADS_DIR / str(project_id) / "manuscript.zip"
                with open(zip_path, "wb") as f:
                    shutil.copyfileobj(file.file, f)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for name in zf.namelist():
                        if any(name.lower().endswith(ext) for ext in accepted) \
                                and not name.startswith("__") \
                                and not name.startswith("._"):
                            zf.extract(name, dest_dir)
                            added += 1
                zip_path.unlink()

            elif suffix in accepted:
                out_path = dest_dir / filename
                with open(out_path, "wb") as f:
                    shutil.copyfileobj(file.file, f)
                added += 1

            else:
                skipped.append(filename)

        # Total chapter count is everything now in the folder
        chapter_count = len([
            f for f in dest_dir.iterdir()
            if f.suffix.lower() in accepted
        ])

        # Update project
        project.manuscript_dir = str(dest_dir)
        project.chapter_count  = chapter_count
        project.status         = ProjectStatus.ACTIVE
        project.updated_at     = datetime.now()
        db.commit()

        # HX-Refresh tells HTMX to reload the full page so everything updates
        msg = f"Uploaded successfully — {chapter_count} chapter{'s' if chapter_count != 1 else ''} in project."
        if skipped:
            msg += f" Skipped {len(skipped)} unsupported file(s): {', '.join(skipped)}."

        response = templates.TemplateResponse("fragments/upload_result.html", {
            "request": request,
            "success": True,
            "message": msg,
        })
        response.headers["HX-Refresh"] = "true"
        return response

    except Exception as e:
        return templates.TemplateResponse("fragments/upload_result.html", {
            "request": request,
            "success": False,
            "message": f"Upload failed: {str(e)}",
        })


# ─────────────────────────────────────────────────────────────────
#  REORDER CHAPTERS  —  POST /projects/{id}/reorder
#  Called by SortableJS in the UI when the user drags chapters.
#  Body: JSON { "order": ["file1.docx", "file2.docx", ...] }
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/reorder")
async def reorder_chapters(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    """Save the user-defined chapter reading order."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    data = await request.json()
    project.chapter_order = data.get("order", [])
    project.updated_at    = datetime.now()
    db.commit()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────
#  SET LAYOUT MODE  —  POST /projects/{id}/layout-mode
#  Called by the layout-mode selector in the project detail sidebar.
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/layout-mode")
def set_layout_mode(
    project_id:  int,
    layout_mode: str     = Form(...),
    db:          Session = Depends(get_db),
):
    """Update the project's layout mode (novel / poetry / essays)."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.layout_mode = layout_mode
    project.updated_at  = datetime.now()
    db.commit()
    return JSONResponse({"ok": True, "layout_mode": layout_mode})


# ─────────────────────────────────────────────────────────────────
#  HELPERS  —  format specs and spine formulas
# ─────────────────────────────────────────────────────────────────

def _create_project_config(project: Project) -> None:
    """Create config.json for a new project so Gemini is ready immediately.

    Copies the API key (and full gemini block) from the most recent existing
    project that has one.  Falls back to a sensible default structure with
    an empty key (the env-var fallback in idea.py / illustrations.py will
    still work if GEMINI_API_KEY is set in the shell).
    """
    project_dir = PROJECTS_DIR / str(project.id)
    project_dir.mkdir(parents=True, exist_ok=True)
    config_path = project_dir / "config.json"

    if config_path.exists():
        return  # already has one — don't overwrite

    # Try to grab gemini config from an existing project
    gemini_block = {
        "api_key": "",
        "models": {
            "text": "gemini-2.5-pro",
            "fast": "gemini-2.5-flash",
            "image_generation": "gemini-2.5-flash-image",
            "image_generation_pro": "gemini-3.1-flash-image-preview",
            "deep_research": "deep-research-pro-12-2025",
        },
    }
    if PROJECTS_DIR.exists():
        for sibling in sorted(PROJECTS_DIR.iterdir(), reverse=True):
            sibling_cfg = sibling / "config.json"
            if sibling_cfg.exists() and sibling.name != str(project.id):
                try:
                    existing = json.loads(sibling_cfg.read_text(encoding="utf-8"))
                    key = existing.get("gemini", {}).get("api_key", "")
                    if key:
                        gemini_block = existing["gemini"]
                        break
                except Exception:
                    continue

    config = {
        "book": {
            "title": project.name,
            "author": project.author,
        },
        "gemini": gemini_block,
        "editing": {
            "creativity_level": 3,
        },
        "formatting": {
            "default_format": "hardcover",
            "available_formats": {
                "hardcover": {
                    "trim_width_inches": 6.0,
                    "trim_height_inches": 9.0,
                    "margin_top_inches": 1.0,
                    "margin_bottom_inches": 1.0,
                    "margin_inside_inches": 1.25,
                    "margin_outside_inches": 0.75,
                    "bleed_inches": 0.125,
                },
                "trade_paperback": {
                    "trim_width_inches": 6.0,
                    "trim_height_inches": 9.0,
                    "margin_top_inches": 0.875,
                    "margin_bottom_inches": 0.875,
                    "margin_inside_inches": 1.0,
                    "margin_outside_inches": 0.75,
                    "bleed_inches": 0.125,
                },
            },
            "fonts": {
                "body": "EB Garamond",
                "body_size_pt": 11.0,
                "chapter_heading": "EB Garamond",
                "chapter_heading_size_pt": 24.0,
                "line_spacing": 1.4,
            },
        },
        "layout_mode": project.layout_mode or "novel",
    }

    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _profile_name(publisher: str, book_format: str) -> str:
    pub_labels = {
        "lulu": "Lulu", "ingram_spark": "IngramSpark",
        "kdp": "KDP", "draft2digital": "Draft2Digital", "generic": "Custom",
    }
    fmt_labels = {
        "hardcover_casewrap":   "6×9 Hardcover",
        "hardcover_dustjacket": "6×9 Hardcover (Dust Jacket)",
        "trade_paperback":      "6×9 Trade Paperback",
        "mass_market_pb":       "4.25×6.87 Mass Market",
        "ebook_epub":           "eBook (EPUB)",
    }
    return f"{pub_labels.get(publisher, publisher)} {fmt_labels.get(book_format, book_format)}"


def _default_cover_type(book_format: str) -> str:
    if book_format == "hardcover_dustjacket":
        return CoverType.DUST_JACKET
    if book_format == "ebook_epub":
        return CoverType.FRONT_ONLY
    return CoverType.WRAPAROUND


def _format_specs(book_format: str) -> dict:
    """Return trim size and margin specs for a given format."""
    specs = {
        "hardcover_casewrap": dict(
            trim_width_inches=6.0, trim_height_inches=9.0,
            margin_top_inches=1.0, margin_bottom_inches=1.0,
            margin_inside_inches=1.25, margin_outside_inches=0.75,
            bleed_inches=0.125,
        ),
        "hardcover_dustjacket": dict(
            trim_width_inches=6.0, trim_height_inches=9.0,
            margin_top_inches=1.0, margin_bottom_inches=1.0,
            margin_inside_inches=1.25, margin_outside_inches=0.75,
            bleed_inches=0.125,
        ),
        "trade_paperback": dict(
            trim_width_inches=6.0, trim_height_inches=9.0,
            margin_top_inches=0.875, margin_bottom_inches=0.875,
            margin_inside_inches=1.0, margin_outside_inches=0.75,
            bleed_inches=0.125,
        ),
        "mass_market_pb": dict(
            trim_width_inches=4.25, trim_height_inches=6.87,
            margin_top_inches=0.75, margin_bottom_inches=0.75,
            margin_inside_inches=0.875, margin_outside_inches=0.625,
            bleed_inches=0.125,
        ),
        "ebook_epub": dict(
            trim_width_inches=0, trim_height_inches=0,
            margin_top_inches=0, margin_bottom_inches=0,
            margin_inside_inches=0, margin_outside_inches=0,
            bleed_inches=0,
        ),
    }
    return specs.get(book_format, specs["hardcover_casewrap"])


def _list_chapters(project: Project) -> list[dict]:
    """Return the uploaded chapter files in their saved reading order.

    Files in project.chapter_order come first, in that order.
    Any file on disk that isn't in the saved order is appended alphabetically
    (handles newly uploaded files that haven't been ordered yet).

    Returns a list of dicts: { filename, stem }
    """
    if not project.manuscript_dir:
        return []
    src = Path(project.manuscript_dir)
    if not src.exists():
        return []

    accepted = {".docx", ".doc", ".txt"}
    on_disk  = {f.name for f in src.iterdir()
                if f.is_file() and f.suffix.lower() in accepted}

    saved_order = project.chapter_order or []
    ordered     = [n for n in saved_order if n in on_disk]
    unordered   = sorted(n for n in on_disk if n not in saved_order)
    final       = ordered + unordered

    return [{"filename": n, "stem": Path(n).stem} for n in final]


def _spine_formula(publisher: str) -> dict:
    """
    Spine width = (page_count × paper_thickness) + cover_boards
    Each publisher uses slightly different paper thickness values.
    All measurements in inches.
    """
    formulas = {
        "lulu": {
            "white_per_page":  0.002252,
            "cream_per_page":  0.002500,
            "cover_boards":    0.050,
            "min_spine_width": 0.250,
            "note": "Lulu hardcover casewrap formula",
        },
        "ingram_spark": {
            "white_per_page":  0.002200,
            "cream_per_page":  0.002400,
            "cover_boards":    0.040,
            "min_spine_width": 0.125,
            "note": "IngramSpark formula",
        },
        "kdp": {
            "white_per_page":  0.002347,
            "cream_per_page":  0.002500,
            "cover_boards":    0.0,
            "min_spine_width": 0.0,
            "note": "KDP paperback formula (no hardcover)",
        },
    }
    return formulas.get(publisher, formulas["lulu"])
