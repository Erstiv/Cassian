"""
METADATA ROUTES
Book metadata editor: ISBN, title, categories, keywords, descriptions,
series, rights, and pricing. Stored as JSON on disk (no database model).

Routes:
  GET  /projects/{project_id}/metadata                 — main metadata page
  POST /projects/{project_id}/metadata/save            — save metadata JSON
  POST /projects/{project_id}/metadata/generate-keywords    — AI keyword suggestions
  POST /projects/{project_id}/metadata/generate-description — AI publisher description
"""

import json
import os
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, Cover, CoverStatus


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"


# ── Common BISAC fiction categories ───────────────────────────────────────────

BISAC_CATEGORIES = [
    ("FIC000000", "Fiction / General"),
    ("FIC001000", "Fiction / Action & Adventure"),
    ("FIC002000", "Fiction / African American & Black / General"),
    ("FIC004000", "Fiction / Biographical"),
    ("FIC009000", "Fiction / Fantasy / General"),
    ("FIC009100", "Fiction / Fantasy / Epic"),
    ("FIC010000", "Fiction / Ghost"),
    ("FIC014000", "Fiction / Historical / General"),
    ("FIC015000", "Fiction / Horror"),
    ("FIC019000", "Fiction / Literary"),
    ("FIC022000", "Fiction / Mystery & Detective / General"),
    ("FIC024000", "Fiction / Occult & Supernatural"),
    ("FIC025000", "Fiction / Psychological"),
    ("FIC027050", "Fiction / Romance / Historical / General"),
    ("FIC028000", "Fiction / Science Fiction / General"),
    ("FIC028010", "Fiction / Science Fiction / Adventure"),
    ("FIC028020", "Fiction / Science Fiction / Military"),
    ("FIC031000", "Fiction / Thrillers / General"),
    ("FIC037000", "Fiction / Political"),
    ("FIC039000", "Fiction / Short Stories (single author)"),
    ("FIC045000", "Fiction / Satire"),
    ("FIC049000", "Fiction / Dystopian"),
    ("POE000000", "Poetry / General"),
    ("BIO000000", "Biography & Autobiography / General"),
    ("HIS000000", "History / General"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _metadata_path(project_id: int) -> Path:
    return _get_project_dir(project_id) / "output" / "metadata" / "book_metadata.json"


def _load_metadata(project_id: int, project: Project) -> dict:
    """
    Load metadata from disk. If no file exists, return a defaults dict
    pre-filled from the project record.
    """
    path = _metadata_path(project_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Defaults — pre-fill from project DB record
    return {
        "title":        project.name,
        "subtitle":     "",
        "author":       project.author,
        "contributors": [],
        "isbn_13":      "",
        "isbn_10":      "",
        "publisher":    "Self-published",
        "publication_date": "",
        "language":     "English",
        "edition":      "First Edition",
        "description": {
            "short": "",
            "long":  "",
        },
        "categories": {
            "bisac": [],
            "thema": [],
        },
        "keywords":         [],
        "age_range":        "Adult",
        "content_warnings": [],
        "series": {
            "name":   "",
            "number": None,
        },
        "rights":    "All rights reserved",
        "territory": "Worldwide",
        "pricing": {
            "currency":   "USD",
            "print_list": None,
            "ebook_list": None,
        },
        "last_updated": "",
    }


def _save_metadata(project_id: int, data: dict) -> None:
    path = _metadata_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_config(project_id: int) -> dict:
    config_path = _get_project_dir(project_id) / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_chapter_titles(project_id: int) -> list[str]:
    """Return a list of chapter titles from ingested/edited JSON files."""
    project_dir  = _get_project_dir(project_id)
    titles = []
    for search_dir in [
        project_dir / "output" / "editing",
        project_dir / "output" / "ingested",
    ]:
        if search_dir.exists():
            for f in sorted(search_dir.glob("chapter_*.json")):
                try:
                    ch = json.loads(f.read_text(encoding="utf-8"))
                    t = ch.get("title") or ch.get("chapter_title") or ""
                    if t:
                        titles.append(t)
                except Exception:
                    pass
            if titles:
                break
    return titles


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/metadata", response_class=HTMLResponse)
async def metadata_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    metadata = _load_metadata(project_id, project)

    return templates.TemplateResponse("metadata.html", {
        "request":         request,
        "project":         project,
        "active_page":     "metadata",
        "metadata":        metadata,
        "bisac_categories": BISAC_CATEGORIES,
        "saved":           False,
        "error":           None,
    })


@router.post("/projects/{project_id}/metadata/save", response_class=HTMLResponse)
async def metadata_save(
    project_id:       int,
    request:          Request,
    db:               Session = Depends(get_db),
    title:            str = Form(""),
    subtitle:         str = Form(""),
    author:           str = Form(""),
    publisher:        str = Form("Self-published"),
    publication_date: str = Form(""),
    language:         str = Form("English"),
    edition:          str = Form("First Edition"),
    isbn_13:          str = Form(""),
    isbn_10:          str = Form(""),
    description_short: str = Form(""),
    description_long:  str = Form(""),
    bisac_categories: str = Form(""),   # JSON array string
    keywords:         str = Form(""),   # comma-separated
    age_range:        str = Form("Adult"),
    content_warnings: str = Form(""),   # comma-separated
    series_name:      str = Form(""),
    series_number:    str = Form(""),
    rights:           str = Form("All rights reserved"),
    territory:        str = Form("Worldwide"),
    currency:         str = Form("USD"),
    print_list:       str = Form(""),
    ebook_list:       str = Form(""),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Parse BISAC list (sent as JSON array from the form)
    bisac_list = []
    if bisac_categories.strip():
        try:
            bisac_list = json.loads(bisac_categories)
        except Exception:
            bisac_list = [b.strip() for b in bisac_categories.split(",") if b.strip()]

    # Parse keywords
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]

    # Parse content warnings
    cw_list = [c.strip() for c in content_warnings.split(",") if c.strip()]

    # Parse prices
    try:
        print_price = float(print_list) if print_list.strip() else None
    except ValueError:
        print_price = None
    try:
        ebook_price = float(ebook_list) if ebook_list.strip() else None
    except ValueError:
        ebook_price = None

    # Parse series number
    try:
        series_num = int(series_number) if series_number.strip() else None
    except ValueError:
        series_num = None

    data = {
        "title":        title,
        "subtitle":     subtitle,
        "author":       author,
        "contributors": [],
        "isbn_13":      isbn_13.strip(),
        "isbn_10":      isbn_10.strip(),
        "publisher":    publisher,
        "publication_date": publication_date,
        "language":     language,
        "edition":      edition,
        "description": {
            "short": description_short,
            "long":  description_long,
        },
        "categories": {
            "bisac": bisac_list,
            "thema": [],
        },
        "keywords":         kw_list,
        "age_range":        age_range,
        "content_warnings": cw_list,
        "series": {
            "name":   series_name,
            "number": series_num,
        },
        "rights":    rights,
        "territory": territory,
        "pricing": {
            "currency":   currency,
            "print_list": print_price,
            "ebook_list": ebook_price,
        },
    }

    _save_metadata(project_id, data)

    # ── Sync title/subtitle/author back to DB + config.json ─────────────
    # This ensures the cover text overlay, title page, and copyright page
    # all stay in sync when you change metadata here.
    changed = False
    if title.strip() and title.strip() != project.name:
        project.name = title.strip()
        changed = True
    if author.strip() and author.strip() != project.author:
        project.author = author.strip()
        changed = True
    if changed:
        db.commit()
        db.refresh(project)

    # Sync to config.json (used by layout agent for title/copyright pages,
    # and by cover composition for text overlays)
    config_path = _get_project_dir(project_id) / "config.json"
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if "book" not in config:
        config["book"] = {}
    config["book"]["title"]    = project.name
    config["book"]["subtitle"] = subtitle.strip()
    config["book"]["author"]   = project.author
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")

    # Reload for display
    metadata = _load_metadata(project_id, project)
    return templates.TemplateResponse("metadata.html", {
        "request":         request,
        "project":         project,
        "active_page":     "metadata",
        "metadata":        metadata,
        "bisac_categories": BISAC_CATEGORIES,
        "saved":           True,
        "error":           None,
    })


@router.post("/projects/{project_id}/metadata/generate-keywords", response_class=HTMLResponse)
async def metadata_generate_keywords(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    config     = _load_config(project_id)
    gemini_cfg = config.get("gemini", {})
    api_key    = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    model      = gemini_cfg.get("models", {}).get("fast", "gemini-2.5-flash")

    if not api_key:
        return HTMLResponse(
            '<p class="text-red-400 text-sm">No Gemini API key found. '
            'Add it to projects/{id}/config.json under gemini → api_key.</p>'
        )

    chapter_titles = _load_chapter_titles(project_id)
    titles_str = ", ".join(chapter_titles[:10]) if chapter_titles else "N/A"

    prompt = (
        f'Generate 15-20 relevant keywords for a {project.genre or "fiction"} book '
        f'titled "{project.name}" '
        f'about: {project.description or "no description available"}. '
        f'Chapter titles include: {titles_str}. '
        "Include genre keywords, theme keywords, setting keywords, and "
        "comparable-title keywords. Return ONLY a comma-separated list, no commentary."
    )

    try:
        from google import genai
        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        keywords_text = response.text.strip()
    except ImportError:
        return HTMLResponse(
            '<p class="text-red-400 text-sm">google-genai library not installed. '
            "Run: pip install google-genai</p>"
        )
    except Exception as exc:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm">Gemini error: {exc}</p>'
        )

    # Return an HTMX fragment — just the value, injected into the keywords input
    return HTMLResponse(
        f'<textarea id="keywords-suggested" name="keywords_suggested" rows="3" '
        f'class="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 '
        f'text-sm text-slate-100 focus:outline-none focus:border-amber-400">'
        f'{keywords_text}</textarea>'
        f'<p class="text-xs text-slate-500 mt-1">✨ Suggested — review and copy into the Keywords field above.</p>'
    )


@router.post("/projects/{project_id}/metadata/generate-description", response_class=HTMLResponse)
async def metadata_generate_description(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    config     = _load_config(project_id)
    gemini_cfg = config.get("gemini", {})
    api_key    = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    model      = gemini_cfg.get("models", {}).get("fast", "gemini-2.5-flash")

    if not api_key:
        return HTMLResponse(
            '<p class="text-red-400 text-sm">No Gemini API key found. '
            'Add it to projects/{id}/config.json under gemini → api_key.</p>'
        )

    # Try to get blurb from Cover record
    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    blurb = cover.back_cover_text if cover else ""
    if not blurb:
        blurb = project.description or "No blurb available."

    prompt = (
        "Write a publisher-ready book description for:\n"
        f"Title: {project.name}\n"
        f"Author: {project.author}\n"
        f"Genre: {project.genre or 'fiction'}\n"
        f"Blurb: {blurb}\n\n"
        "Generate TWO versions:\n"
        "SHORT: A 150-word catalog description (factual, for librarians and retailers)\n"
        "LONG: A 500-word marketing description (compelling, for readers)\n\n"
        'RESPOND IN JSON ONLY: {"short": "...", "long": "..."}'
    )

    try:
        from google import genai
        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        raw = response.text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        descriptions = json.loads(raw)
        short_desc = descriptions.get("short", "")
        long_desc  = descriptions.get("long", "")
    except ImportError:
        return HTMLResponse(
            '<p class="text-red-400 text-sm">google-genai library not installed. '
            "Run: pip install google-genai</p>"
        )
    except json.JSONDecodeError:
        # Gemini returned non-JSON — use full text as long description
        short_desc = ""
        long_desc  = raw
    except Exception as exc:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm">Gemini error: {exc}</p>'
        )

    # Return HTMX fragment with both descriptions pre-filled
    short_escaped = short_desc.replace("</", "<\\/")
    long_escaped  = long_desc.replace("</", "<\\/")
    return HTMLResponse(
        f'<div class="space-y-3">'
        f'<div>'
        f'<label class="block text-xs text-slate-400 mb-1">✨ Suggested Short (150 words)</label>'
        f'<textarea rows="4" '
        f'class="w-full bg-slate-800 border border-amber-500/40 rounded-lg px-3 py-2 '
        f'text-sm text-slate-100 focus:outline-none focus:border-amber-400">'
        f'{short_desc}</textarea>'
        f'</div>'
        f'<div>'
        f'<label class="block text-xs text-slate-400 mb-1">✨ Suggested Long (500 words)</label>'
        f'<textarea rows="8" '
        f'class="w-full bg-slate-800 border border-amber-500/40 rounded-lg px-3 py-2 '
        f'text-sm text-slate-100 focus:outline-none focus:border-amber-400">'
        f'{long_desc}</textarea>'
        f'</div>'
        f'<p class="text-xs text-slate-500">Review and copy into the Description fields above, then save.</p>'
        f'</div>'
    )
