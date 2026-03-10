"""
COVER ROUTES
Publisher profile setup, spine width calculator, back-cover blurb editor,
AI cover image generation, text overlay settings, and approval workflow.

Routes:
  GET  /projects/{project_id}/cover                    — main cover page
  POST /projects/{project_id}/cover/profile            — create/update OutputProfile
  POST /projects/{project_id}/cover/blurb              — save back-cover blurb
  POST /projects/{project_id}/cover/blurb/generate     — AI-generate blurb via Gemini
  GET  /projects/{project_id}/cover/dimensions         — HTMX: recalculate dimensions panel
  POST /projects/{project_id}/cover/generate-prompt    — AI generates cover art prompt
  POST /projects/{project_id}/cover/generate           — AI generates front cover image
  POST /projects/{project_id}/cover/approve            — approve + compose wraparound
  POST /projects/{project_id}/cover/reject             — reject with optional note
  POST /projects/{project_id}/cover/regenerate         — regenerate with rejection context
  POST /projects/{project_id}/cover/reset              — reset to prompt stage
  GET  /projects/{project_id}/cover/img/{filename}     — serve cover images
  POST /projects/{project_id}/cover/text-settings      — save cover text overlay settings
  POST /projects/{project_id}/cover/recompose          — recompose wraparound with current settings
"""

import io
import json
import os
import textwrap
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Project, OutputProfile, Cover, IllustrationStyle, PipelineRun,
    Publisher, BookFormat, CoverType, CoverStatus, RunStatus,
)


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"


# ── Publisher-specific spine formulas ──────────────────────────────────────────

SPINE_FORMULAS = {
    "lulu": {
        "white_per_page": 0.002252,
        "cream_per_page": 0.0025,
        "cover_boards":   0.05,
        "min_spine_width": 0.25,
    },
    "ingram_spark": {
        "white_per_page": 0.002252,
        "cream_per_page": 0.0025,
        "cover_boards":   0.06,
        "min_spine_width": 0.0625,
    },
    "kdp": {
        "white_per_page": 0.002252,
        "cream_per_page": 0.0025,
        "cover_boards":   0.0,
        "min_spine_width": 0.0,
    },
    # Fallback for draft2digital / generic
    "generic": {
        "white_per_page": 0.002252,
        "cream_per_page": 0.0025,
        "cover_boards":   0.0,
        "min_spine_width": 0.0,
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _calculate_spine_width(page_count: int, output_profile: OutputProfile) -> float:
    """
    Apply the spine formula from the OutputProfile (or publisher defaults).
    Returns spine width in inches, rounded to 4 decimal places.
    """
    formula = output_profile.spine_formula or {}

    # Fall back to publisher defaults if spine_formula is empty
    if not formula:
        pub_key = (output_profile.publisher.value
                   if output_profile.publisher else "lulu")
        formula = SPINE_FORMULAS.get(pub_key, SPINE_FORMULAS["lulu"])

    paper_type      = (output_profile.paper_type or "cream").lower()
    thickness_key   = "cream_per_page" if paper_type == "cream" else "white_per_page"
    thickness       = formula.get(thickness_key, 0.0025)
    cover_boards    = formula.get("cover_boards", 0.05)
    min_spine       = formula.get("min_spine_width", 0.25)

    raw_width = (page_count * thickness) + cover_boards
    return round(max(raw_width, min_spine), 4)


def _build_cover_dimensions(output_profile: OutputProfile, spine_width: float) -> dict:
    """
    Build the full cover dimension data for the wraparound diagram.
    Returns a dict with all widths and heights (with bleed).
    """
    trim_w = output_profile.trim_width_inches  or 6.0
    trim_h = output_profile.trim_height_inches or 9.0
    bleed  = output_profile.bleed_inches       or 0.125
    cover_type = (output_profile.cover_type.value
                  if output_profile.cover_type else "wraparound")

    # Panel widths (trim = content only, panel = trim + bleed on each exposed edge)
    # For wraparound: left bleed | back | spine | front | right bleed
    front_w      = round(trim_w + bleed, 4)          # trim + right bleed
    back_w       = round(trim_w + bleed, 4)           # trim + left bleed
    total_w      = round(back_w + spine_width + front_w, 4)
    total_h      = round(trim_h + (2 * bleed), 4)     # top + bottom bleed

    return {
        "cover_type":  cover_type,
        "trim_w":      trim_w,
        "trim_h":      trim_h,
        "bleed":       bleed,
        "front_w":     front_w,
        "back_w":      back_w,
        "spine_w":     spine_width,
        "total_w":     total_w,
        "total_h":     total_h,
    }


def _load_page_count(project_id: int) -> int | None:
    """
    Read the layout_report.json to get page count, or return None if not available.
    """
    report_path = _get_project_dir(project_id) / "output" / "formatting" / "layout_report.json"
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return report.get("page_count") or report.get("total_pages") or None
    except Exception:
        return None


def _load_config(project_id: int) -> dict:
    config_path = _get_project_dir(project_id) / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(project_id: int, config: dict) -> None:
    """Write config.json back to disk."""
    config_path = _get_project_dir(project_id) / "config.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def _default_cover_text_settings() -> dict:
    """
    Return default cover text overlay settings.
    Stored in config.json under "cover_text".
    """
    return {
        "front": {
            "show_title":    True,
            "show_subtitle": True,
            "show_author":   True,
            "title_position":    "top",       # top, center, bottom
            "subtitle_position": "below_title",
            "author_position":   "bottom",    # top, bottom
            "title_font_size":    48,         # points at 300 DPI
            "subtitle_font_size": 24,
            "author_font_size":   22,
            "title_color":    "#FFFFFF",
            "subtitle_color": "#E0D8CC",
            "author_color":   "#E0D8CC",
            "text_shadow":    True,           # dark shadow behind text for readability
            "band_overlay":   True,           # semi-transparent dark band behind text
            "band_opacity":   100,            # 0–255
        },
        "spine": {
            "show_title":  True,
            "show_author": True,
            "font_size":   14,                # points at 300 DPI
            "color":       "#DCD2C3",
        },
        "back": {
            "show_title":  True,
            "show_author": True,
            "title_font_size":  28,
            "author_font_size": 18,
            "title_color":  "#FFFFFF",
            "author_color": "#DCD2C3",
        },
    }


def _load_cover_text_settings(project_id: int) -> dict:
    """Load cover_text settings from config.json, with defaults."""
    config   = _load_config(project_id)
    defaults = _default_cover_text_settings()
    saved    = config.get("cover_text", {})
    # Merge saved on top of defaults (two-level deep)
    merged = {}
    for section in ("front", "spine", "back"):
        merged[section] = {**defaults.get(section, {}), **saved.get(section, {})}
    # Include subtitle from book config
    merged["subtitle"] = config.get("book", {}).get("subtitle", "")
    return merged


def _get_or_create_pipeline_run(project_id: int, output_profile_id: int, db: Session) -> PipelineRun:
    """
    Return the latest PipelineRun for this project, or create a minimal cover-only one.
    """
    run = (
        db.query(PipelineRun)
        .filter(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.created_at.desc())
        .first()
    )
    if run:
        return run

    run = PipelineRun(
        project_id        = project_id,
        output_profile_id = output_profile_id,
        name              = "Cover (web)",
        status            = RunStatus.PENDING,
        agents_selected   = [6],  # cover agent only
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _load_chapter_summaries(project_id: int) -> str:
    """
    Read chapter titles and first paragraphs from edited/ingested JSON files.
    Returns a formatted string for the AI blurb prompt.
    """
    project_dir  = _get_project_dir(project_id)
    editing_dir  = project_dir / "output" / "editing"
    ingested_dir = project_dir / "output" / "ingested"

    # Prefer edited files
    chapter_files = []
    if editing_dir.exists():
        chapter_files = sorted(editing_dir.glob("chapter_*_edited.json"))
    if not chapter_files and ingested_dir.exists():
        chapter_files = sorted(ingested_dir.glob("chapter_*.json"))

    summaries = []
    for cf in chapter_files[:20]:   # cap at 20 chapters for prompt length
        try:
            data  = json.loads(cf.read_text(encoding="utf-8"))
            title = data.get("title") or data.get("chapter_title") or cf.stem
            paras = data.get("paragraphs") or data.get("content") or []
            first_para = ""
            if isinstance(paras, list) and paras:
                # paragraphs may be dicts with "text" key, or plain strings
                first = paras[0]
                first_para = (first.get("text") or first.get("original", "")
                              if isinstance(first, dict) else str(first))
                first_para = first_para[:400].strip()
            summaries.append(f"— {title}: {first_para}")
        except Exception:
            continue

    return "\n".join(summaries) if summaries else "(No chapter content available)"


# ── GET — main cover page ──────────────────────────────────────────────────────

@router.get("/projects/{project_id}/cover", response_class=HTMLResponse)
async def cover_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Default OutputProfile (is_default=True, or first one)
    output_profile = (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id, OutputProfile.is_default == True)
        .first()
    ) or (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id)
        .first()
    )

    # Latest Cover record (if any)
    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )

    # Page count from layout report
    page_count  = _load_page_count(project_id)
    spine_width = None
    cover_dims  = None

    if output_profile and page_count:
        spine_width = _calculate_spine_width(page_count, output_profile)
        cover_dims  = _build_cover_dimensions(output_profile, spine_width)
    elif output_profile:
        # No page count yet — we can still show a placeholder dimension diagram
        spine_width = None
        cover_dims  = _build_cover_dimensions(output_profile, 0.0)

    # Cover text overlay settings
    text_settings = _load_cover_text_settings(project_id)

    return templates.TemplateResponse(
        "cover.html",
        {
            "request":        request,
            "project":        project,
            "active_page":    "cover",
            "output_profile": output_profile,
            "cover":          cover,
            "page_count":     page_count,
            "spine_width":    spine_width,
            "cover_dims":     cover_dims,
            "text_settings":  text_settings,
        },
    )


# ── POST — create/update OutputProfile ────────────────────────────────────────

@router.post("/projects/{project_id}/cover/profile", response_class=HTMLResponse)
async def cover_profile(
    project_id:  int,
    request:     Request,
    db:          Session = Depends(get_db),
    name:        str = Form(""),
    publisher:   str = Form("lulu"),
    book_format: str = Form("hardcover_casewrap"),
    cover_type:  str = Form("wraparound"),
    trim_width:  float = Form(6.0),
    trim_height: float = Form(9.0),
    bleed:       float = Form(0.125),
    paper_type:  str = Form("cream"),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate enum values (fall back to defaults on unknown)
    try:
        pub_enum    = Publisher(publisher)
    except ValueError:
        pub_enum    = Publisher.LULU
    try:
        fmt_enum    = BookFormat(book_format)
    except ValueError:
        fmt_enum    = BookFormat.HARDCOVER_CASEWRAP
    try:
        ctype_enum  = CoverType(cover_type)
    except ValueError:
        ctype_enum  = CoverType.WRAPAROUND

    # Auto-name if blank
    if not name.strip():
        name = f"{pub_enum.value.replace('_', ' ').title()} {trim_width}×{trim_height}"

    formula = SPINE_FORMULAS.get(publisher, SPINE_FORMULAS["generic"])

    # Find existing default profile or create new
    existing = (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id, OutputProfile.is_default == True)
        .first()
    ) or (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id)
        .first()
    )

    if existing:
        existing.name               = name
        existing.publisher          = pub_enum
        existing.book_format        = fmt_enum
        existing.cover_type         = ctype_enum
        existing.trim_width_inches  = trim_width
        existing.trim_height_inches = trim_height
        existing.bleed_inches       = bleed
        existing.paper_type         = paper_type
        existing.spine_formula      = formula
        existing.is_default         = True
        output_profile = existing
    else:
        output_profile = OutputProfile(
            project_id          = project_id,
            name                = name,
            publisher           = pub_enum,
            book_format         = fmt_enum,
            cover_type          = ctype_enum,
            trim_width_inches   = trim_width,
            trim_height_inches  = trim_height,
            bleed_inches        = bleed,
            paper_type          = paper_type,
            spine_formula       = formula,
            is_default          = True,
        )
        db.add(output_profile)

    db.commit()
    db.refresh(output_profile)

    # Recalculate dimensions
    page_count  = _load_page_count(project_id)
    spine_width = None
    cover_dims  = None
    if page_count:
        spine_width = _calculate_spine_width(page_count, output_profile)
        cover_dims  = _build_cover_dimensions(output_profile, spine_width)
    else:
        cover_dims = _build_cover_dimensions(output_profile, 0.0)

    return templates.TemplateResponse(
        "fragments/cover_config_panel.html",
        {
            "request":        request,
            "project":        project,
            "output_profile": output_profile,
            "page_count":     page_count,
            "spine_width":    spine_width,
            "cover_dims":     cover_dims,
            "saved":          True,
        },
    )


# ── POST — save blurb ──────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/cover/blurb", response_class=HTMLResponse)
async def cover_blurb_save(
    project_id:      int,
    request:         Request,
    db:              Session = Depends(get_db),
    back_cover_text: str = Form(""),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Must have an OutputProfile before we can save a Cover record
    output_profile = (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id, OutputProfile.is_default == True)
        .first()
    ) or (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id)
        .first()
    )

    if not output_profile:
        return HTMLResponse(
            '<div class="text-sm text-amber-400 p-3 bg-amber-900/20 rounded-lg">'
            '⚠ Save your publisher profile first — the blurb will be linked to it.'
            '</div>',
            status_code=200,
        )

    # Get or create a PipelineRun to satisfy the FK
    pipeline_run = _get_or_create_pipeline_run(project_id, output_profile.id, db)

    # Get or create Cover record
    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )

    if cover:
        cover.back_cover_text = back_cover_text
    else:
        cover = Cover(
            project_id        = project_id,
            pipeline_run_id   = pipeline_run.id,
            output_profile_id = output_profile.id,
            status            = CoverStatus.PENDING,
            back_cover_text   = back_cover_text,
        )
        db.add(cover)

    db.commit()
    db.refresh(cover)

    return templates.TemplateResponse(
        "fragments/cover_blurb_panel.html",
        {
            "request": request,
            "project": project,
            "cover":   cover,
            "saved":   True,
        },
    )


# ── POST — AI-generate blurb ───────────────────────────────────────────────────

@router.post("/projects/{project_id}/cover/blurb/generate", response_class=HTMLResponse)
async def cover_blurb_generate(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Load API config
    config = _load_config(project_id)
    gemini_cfg = config.get("gemini", {})
    api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    model   = gemini_cfg.get("models", {}).get("fast", "gemini-2.5-flash")

    if not api_key:
        return templates.TemplateResponse(
            "fragments/cover_blurb_panel.html",
            {
                "request": request,
                "project": project,
                "cover":   None,
                "error":   "No Gemini API key found. Add it to projects/{id}/config.json under gemini → api_key.",
                "saved":   False,
            },
        )

    # Build chapter summaries
    chapter_summaries = _load_chapter_summaries(project_id)

    prompt = f"""You are a publishing professional writing back-cover copy for a book.

Book title: {project.name}
Author: {project.author}
Genre: {getattr(project, 'genre', 'fiction')}

Chapter summaries (titles and opening lines):
{chapter_summaries}

Write a compelling back-cover blurb for this book. The blurb should:
- Be 150-200 words
- Hook the reader with an intriguing opening line
- Set up the central conflict or premise without spoilers
- End with a question or cliffhanger that makes the reader want to open the book
- Match the tone of the genre (literary, suspenseful, lyrical, etc.)
- NOT include the author's name or the book title
- NOT include review quotes or praise

Return ONLY the blurb text, no commentary or labels."""

    try:
        from google import genai
        client   = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        generated_text = response.text.strip()
    except ImportError:
        return templates.TemplateResponse(
            "fragments/cover_blurb_panel.html",
            {
                "request": request,
                "project": project,
                "cover":   None,
                "error":   "google-genai library not installed. Run: pip install google-genai",
                "saved":   False,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "fragments/cover_blurb_panel.html",
            {
                "request": request,
                "project": project,
                "cover":   None,
                "error":   f"Gemini error: {exc}",
                "saved":   False,
            },
        )

    # Fetch existing cover record (blurb not saved yet — user must click Save)
    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )

    return templates.TemplateResponse(
        "fragments/cover_blurb_panel.html",
        {
            "request":        request,
            "project":        project,
            "cover":          cover,
            "generated_text": generated_text,
            "saved":          False,
        },
    )


# ── GET — HTMX dimensions fragment ────────────────────────────────────────────

@router.get("/projects/{project_id}/cover/dimensions", response_class=HTMLResponse)
async def cover_dimensions_fragment(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    output_profile = (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id, OutputProfile.is_default == True)
        .first()
    ) or (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id)
        .first()
    )

    page_count  = _load_page_count(project_id)
    spine_width = None
    cover_dims  = None

    if output_profile:
        if page_count:
            spine_width = _calculate_spine_width(page_count, output_profile)
        cover_dims = _build_cover_dimensions(output_profile, spine_width or 0.0)

    return templates.TemplateResponse(
        "fragments/cover_dimensions.html",
        {
            "request":        request,
            "project":        project,
            "output_profile": output_profile,
            "page_count":     page_count,
            "spine_width":    spine_width,
            "cover_dims":     cover_dims,
        },
    )


# ── Session 9b: AI Generation + Approval ──────────────────────────────────────

# Path to EB Garamond TTF for spine/back-cover text
_FONT_DIR  = CASSIAN_DIR / "agents" / "05_layout" / "fonts" / "EB_Garamond"
_FONT_PATH = _FONT_DIR / "EBGaramond-VariableFont_wght.ttf"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_api_config(project_id: int) -> dict:
    """Return api_key, text_model, image_model from config.json."""
    config     = _load_config(project_id)
    gemini_cfg = config.get("gemini", {})
    models     = gemini_cfg.get("models", {})
    return {
        "api_key":     gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", ""),
        "text_model":  models.get("fast", "gemini-2.5-flash"),
        "image_model": models.get("image_generation", "gemini-2.5-flash-preview-04-17"),
    }


def _get_default_style_description(project_id: int, db: Session) -> str:
    """Return a text description of the project's default IllustrationStyle, if any."""
    style = (
        db.query(IllustrationStyle)
        .filter(
            IllustrationStyle.project_id == project_id,
            IllustrationStyle.is_default  == True,
        )
        .first()
    )
    if not style:
        return "No interior illustration style set"

    parts = [style.name]
    if style.style_input:
        parts.append(style.style_input[:300])
    if style.style_profile:
        sp = style.style_profile
        for key in ("medium", "palette", "lighting", "mood", "texture"):
            val = sp.get(key)
            if val:
                parts.append(f"{key}: {val}")
    return " | ".join(parts)


def _parse_prompt_response(text: str) -> dict:
    """
    Parse the structured Gemini prompt-generation response.
    Returns dict with keys: image_prompt, mood, color_palette, raw.
    """
    data = {"image_prompt": "", "mood": "", "color_palette": "", "raw": text}

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("IMAGE_PROMPT:"):
            data["image_prompt"] = stripped[len("IMAGE_PROMPT:"):].strip()
        elif stripped.startswith("MOOD:"):
            data["mood"] = stripped[len("MOOD:"):].strip()
        elif stripped.startswith("COLOR_PALETTE:"):
            data["color_palette"] = stripped[len("COLOR_PALETTE:"):].strip()

    # Multi-line IMAGE_PROMPT: collect lines between IMAGE_PROMPT: and MOOD:
    if not data["image_prompt"]:
        lines      = text.splitlines()
        collecting = False
        prompt_lines = []
        for line in lines:
            if line.strip().startswith("IMAGE_PROMPT:"):
                collecting = True
                tail = line.strip()[len("IMAGE_PROMPT:"):].strip()
                if tail:
                    prompt_lines.append(tail)
            elif collecting and line.strip().startswith(("MOOD:", "COLOR_PALETTE:")):
                collecting = False
            elif collecting:
                prompt_lines.append(line.strip())
        data["image_prompt"] = " ".join(prompt_lines).strip()

    # Same for MOOD
    if not data["mood"]:
        lines      = text.splitlines()
        collecting = False
        mood_lines = []
        for line in lines:
            if line.strip().startswith("MOOD:"):
                collecting = True
                tail = line.strip()[len("MOOD:"):].strip()
                if tail:
                    mood_lines.append(tail)
            elif collecting and line.strip().startswith("COLOR_PALETTE:"):
                collecting = False
            elif collecting:
                mood_lines.append(line.strip())
        data["mood"] = " ".join(mood_lines).strip()

    # COLOR_PALETTE: everything after "COLOR_PALETTE:" to end
    if not data["color_palette"]:
        lines      = text.splitlines()
        collecting = False
        pal_lines  = []
        for line in lines:
            if line.strip().startswith("COLOR_PALETTE:"):
                collecting = True
                tail = line.strip()[len("COLOR_PALETTE:"):].strip()
                if tail:
                    pal_lines.append(tail)
            elif collecting:
                pal_lines.append(line.strip())
        data["color_palette"] = " ".join(pal_lines).strip()

    return data


def _preview_panel(request, project, cover, *, error: str = ""):
    """Shortcut: render the cover preview panel fragment."""
    return templates.TemplateResponse(
        "fragments/cover_preview_panel.html",
        {
            "request": request,
            "project": project,
            "cover":   cover,
            "error":   error,
        },
    )


def _wrap_text_pillow(draw, text: str, font, x: int, y: int,
                       max_width: int, line_spacing: int = 8,
                       fill: tuple = (220, 210, 195)) -> int:
    """
    Draw word-wrapped text onto a Pillow ImageDraw canvas.
    Returns the y position after the last line.
    """
    words     = text.split()
    lines     = []
    current   = []

    for word in words:
        test_line = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))

    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), line, font=font)
        y   += (bbox[3] - bbox[1]) + line_spacing

    return y


def _hex_to_rgb(hex_color: str) -> tuple:
    """Convert '#RRGGBB' to (R, G, B) tuple."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (255, 255, 255)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _draw_text_centered(draw, text: str, font, y: int, x_center: int,
                         fill: tuple, shadow: bool = False,
                         max_width: int = 0):
    """
    Draw text centered horizontally at y, with optional drop shadow.
    If max_width > 0, word-wraps lines to fit within that width.
    Returns total height of all drawn lines.
    """
    if max_width > 0:
        # Word-wrap into multiple lines
        words = text.split()
        lines = []
        current = []
        for word in words:
            test_line = " ".join(current + [word])
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current.append(word)
            else:
                if current:
                    lines.append(" ".join(current))
                current = [word]
        if current:
            lines.append(" ".join(current))
    else:
        lines = [text]

    total_h = 0
    line_gap = int(font.size * 0.2) if hasattr(font, 'size') else 8
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x  = x_center - tw // 2
        line_y = y + total_h
        if shadow:
            draw.text((x + 3, line_y + 3), line, font=font, fill=(0, 0, 0))
            draw.text((x + 2, line_y + 2), line, font=font, fill=(0, 0, 0))
        draw.text((x, line_y), line, font=font, fill=fill)
        total_h += th + (line_gap if i < len(lines) - 1 else 0)

    return total_h


def _compose_wraparound(project_dir: Path, cover, output_profile) -> tuple:
    """
    Compose the full wraparound cover (back + spine + front) using Pillow.
    Overlays title, subtitle, and author text on front/back/spine per settings.
    Returns (png_path, tiff_path).
    """
    from PIL import Image, ImageDraw, ImageFont

    dpi       = getattr(output_profile, "dpi", None) or 300
    bleed     = output_profile.bleed_inches       or 0.125
    trim_w    = output_profile.trim_width_inches  or 6.0
    trim_h    = output_profile.trim_height_inches or 9.0
    spine_w   = cover.spine_width_inches          or 0.5

    # Total canvas dimensions in pixels
    total_w_in = bleed + trim_w + spine_w + trim_w + bleed
    total_h_in = bleed + trim_h + bleed
    px_w       = int(total_w_in * dpi)
    px_h       = int(total_h_in * dpi)

    bleed_px   = int(bleed * dpi)
    trim_w_px  = int(trim_w * dpi)
    trim_h_px  = int(trim_h * dpi)
    spine_px   = int(spine_w * dpi)

    # Pixel x-offsets
    back_start  = 0
    back_end    = bleed_px + trim_w_px
    spine_start = back_end
    spine_end   = spine_start + spine_px
    front_start = spine_end

    # Load cover text settings
    text_settings = _load_cover_text_settings(cover.project_id)
    fs = text_settings.get("front", {})
    ss = text_settings.get("spine", {})
    bs = text_settings.get("back",  {})

    # Project metadata
    project  = cover.project
    config   = _load_config(cover.project_id)
    title    = getattr(project, "name",   "") or ""
    subtitle = config.get("book", {}).get("subtitle", "") or ""
    author   = getattr(project, "author", "") or ""

    # Background: deep charcoal (matches front cover dark tones)
    canvas = Image.new("RGB", (px_w, px_h), color=(22, 22, 30))
    draw   = ImageDraw.Draw(canvas)

    # ── FRONT COVER IMAGE ─────────────────────────────────────────────────────
    front_path = project_dir / cover.front_image_path
    if front_path.exists():
        front_img          = Image.open(front_path).convert("RGB")
        front_panel_w      = px_w - front_start
        front_img_resized  = front_img.resize((front_panel_w, px_h), Image.LANCZOS)
        canvas.paste(front_img_resized, (front_start, 0))

    # ── BACK COVER ─────────────────────────────────────────────────────────────
    # Solid dark panel with subtle gradient feel: slightly lighter toward right
    for x in range(back_end):
        factor     = x / max(back_end, 1)
        r          = int(22 + factor * 15)
        g          = int(22 + factor * 12)
        b          = int(30 + factor * 18)
        draw.line([(x, 0), (x, px_h)], fill=(r, g, b))

    # ── SPINE BACKGROUND ──────────────────────────────────────────────────────
    # Slightly lighter strip
    for x in range(spine_start, spine_end):
        factor = (x - spine_start) / max(spine_px, 1)
        r      = int(35 + factor * 10)
        g      = int(35 + factor * 8)
        b      = int(48 + factor * 12)
        draw.line([(x, 0), (x, px_h)], fill=(r, g, b))

    # ── FONTS ──────────────────────────────────────────────────────────────────
    def _load_font(size: int, bold: bool = False):
        # Try bold variant first if requested
        if bold:
            bold_path = _FONT_DIR / "static" / "EBGaramond-Bold.ttf"
            if bold_path.exists():
                try:
                    return ImageFont.truetype(str(bold_path), size)
                except Exception:
                    pass
        if _FONT_PATH.exists():
            try:
                return ImageFont.truetype(str(_FONT_PATH), size)
            except Exception:
                pass
        return ImageFont.load_default()

    # ── FRONT COVER TEXT OVERLAYS ─────────────────────────────────────────────
    front_panel_w   = px_w - front_start
    front_center_x  = front_start + front_panel_w // 2
    use_shadow      = fs.get("text_shadow", True)

    # Calculate sizes: font size in config is in points, convert to pixels at DPI
    # pts × dpi / 72 = pixels
    title_px    = int(fs.get("title_font_size", 48) * dpi / 72)
    subtitle_px = int(fs.get("subtitle_font_size", 24) * dpi / 72)
    author_px   = int(fs.get("author_font_size", 22) * dpi / 72)

    title_font    = _load_font(title_px, bold=True)
    subtitle_font = _load_font(subtitle_px)
    author_font   = _load_font(author_px)

    title_color    = _hex_to_rgb(fs.get("title_color",    "#FFFFFF"))
    subtitle_color = _hex_to_rgb(fs.get("subtitle_color", "#E0D8CC"))
    author_color   = _hex_to_rgb(fs.get("author_color",   "#E0D8CC"))

    # -- Semi-transparent band overlay for readability --
    if fs.get("band_overlay", True) and (fs.get("show_title") or fs.get("show_subtitle")):
        from PIL import Image as PILImage
        band_opacity = fs.get("band_opacity", 100)
        title_pos    = fs.get("title_position", "top")

        if title_pos == "top":
            band_top = bleed_px
            band_h   = int(trim_h_px * 0.30)  # top 30% of trim area
        elif title_pos == "center":
            band_h   = int(trim_h_px * 0.25)
            band_top = bleed_px + (trim_h_px - band_h) // 2
        else:  # bottom
            band_h   = int(trim_h_px * 0.30)
            band_top = bleed_px + trim_h_px - band_h

        # Create RGBA overlay for the band on the front cover
        band_overlay = PILImage.new("RGBA", (front_panel_w, band_h), (0, 0, 0, band_opacity))
        # Convert canvas to RGBA temporarily for alpha compositing
        canvas_rgba = canvas.convert("RGBA")
        canvas_rgba.paste(band_overlay, (front_start, band_top), band_overlay)
        canvas = canvas_rgba.convert("RGB")
        draw   = ImageDraw.Draw(canvas)

    # -- Title text on front --
    if fs.get("show_title", True) and title:
        title_pos = fs.get("title_position", "top")
        margin_top = bleed_px + int(0.4 * dpi)  # 0.4" from trim edge

        if title_pos == "top":
            title_y = margin_top
        elif title_pos == "center":
            title_y = bleed_px + (trim_h_px - title_px) // 2
        else:  # bottom
            title_y = bleed_px + trim_h_px - int(0.6 * dpi) - title_px

        front_text_max_w = front_panel_w - int(0.8 * dpi)  # 0.4" margin each side
        th = _draw_text_centered(draw, title.upper(), title_font, title_y,
                                  front_center_x, title_color, shadow=use_shadow,
                                  max_width=front_text_max_w)

        # -- Subtitle below title --
        if fs.get("show_subtitle", True) and subtitle:
            sub_y = title_y + th + int(0.15 * dpi)
            _draw_text_centered(draw, subtitle, subtitle_font, sub_y,
                                front_center_x, subtitle_color, shadow=use_shadow,
                                max_width=front_text_max_w)

    # -- Author text on front --
    if fs.get("show_author", True) and author:
        author_pos = fs.get("author_position", "bottom")
        if author_pos == "bottom":
            author_y = bleed_px + trim_h_px - int(0.5 * dpi)

            # Add a bottom band if we didn't already add one covering this area
            if fs.get("band_overlay", True) and fs.get("title_position", "top") == "top":
                from PIL import Image as PILImage
                band_opacity = fs.get("band_opacity", 100)
                bottom_band_h = int(0.8 * dpi)
                bottom_band_top = bleed_px + trim_h_px - bottom_band_h
                band_overlay = PILImage.new("RGBA", (front_panel_w, bottom_band_h),
                                            (0, 0, 0, band_opacity))
                canvas_rgba = canvas.convert("RGBA")
                canvas_rgba.paste(band_overlay, (front_start, bottom_band_top), band_overlay)
                canvas = canvas_rgba.convert("RGB")
                draw   = ImageDraw.Draw(canvas)
        else:  # top
            author_y = bleed_px + int(0.25 * dpi)

        _draw_text_centered(draw, author, author_font, author_y,
                            front_center_x, author_color, shadow=use_shadow)

    # ── BACK COVER TEXT ────────────────────────────────────────────────────────
    margin_px    = int(0.55 * dpi)        # ~0.55" inner margin
    text_x       = bleed_px + margin_px
    text_area_w  = trim_w_px - (margin_px * 2)
    back_text_y  = bleed_px + int(0.5 * dpi)

    # Back cover title and author (above blurb)
    if bs.get("show_title", True) and title:
        back_title_size = int(bs.get("title_font_size", 28) * dpi / 72)
        back_title_font = _load_font(back_title_size, bold=True)
        back_title_color = _hex_to_rgb(bs.get("title_color", "#FFFFFF"))
        # Center the title in the back cover text area
        back_center_x = bleed_px + trim_w_px // 2
        back_text_max_w = text_area_w  # same width constraint as blurb
        th = _draw_text_centered(draw, title, back_title_font, back_text_y,
                                  back_center_x, back_title_color,
                                  max_width=back_text_max_w)
        back_text_y += th + int(0.1 * dpi)

    if bs.get("show_author", True) and author:
        back_author_size = int(bs.get("author_font_size", 18) * dpi / 72)
        back_author_font = _load_font(back_author_size)
        back_author_color = _hex_to_rgb(bs.get("author_color", "#DCD2C3"))
        back_center_x = bleed_px + trim_w_px // 2
        th = _draw_text_centered(draw, f"by {author}", back_author_font, back_text_y,
                                  back_center_x, back_author_color)
        back_text_y += th + int(0.3 * dpi)

    # Back cover blurb
    blurb = cover.back_cover_text or ""
    if blurb:
        blurb_font = _load_font(int(dpi * 0.14))  # ~42px at 300dpi ≈ 11pt
        _wrap_text_pillow(
            draw, blurb, blurb_font,
            x=text_x, y=back_text_y,
            max_width=text_area_w,
            line_spacing=int(dpi * 0.05),
            fill=(220, 210, 195),
        )

    # ── BARCODE PLACEHOLDER ────────────────────────────────────────────────────
    # ~2" × 1.2" white rect in lower-right of back cover
    barcode_w   = int(2.0 * dpi)
    barcode_h   = int(1.2 * dpi)
    barcode_x1  = back_end - bleed_px - margin_px - barcode_w
    barcode_y1  = px_h - bleed_px - int(0.4 * dpi) - barcode_h
    barcode_x2  = barcode_x1 + barcode_w
    barcode_y2  = barcode_y1 + barcode_h
    draw.rectangle([barcode_x1, barcode_y1, barcode_x2, barcode_y2], fill=(255, 255, 255))

    label_font = _load_font(int(dpi * 0.07))
    draw.text(
        (barcode_x1 + int(dpi * 0.15), barcode_y1 + int(dpi * 0.45)),
        "ISBN BARCODE",
        font=label_font,
        fill=(100, 100, 100),
    )

    # ── SPINE TEXT (bottom-to-top per US convention) ───────────────────────────
    if ss.get("show_title", True) or ss.get("show_author", True):
        spine_font_size = int(ss.get("font_size", 14) * dpi / 72)
        # Clamp to reasonable range for spine width
        spine_font_size = max(spine_font_size, int(dpi * 0.08))
        spine_font_size = min(spine_font_size, int(spine_px * 0.45))
        spine_font      = _load_font(spine_font_size)
        spine_color     = _hex_to_rgb(ss.get("color", "#DCD2C3"))

        spine_parts = []
        if ss.get("show_title", True) and title:
            spine_parts.append(title)
        if ss.get("show_author", True) and author:
            spine_parts.append(author)
        spine_text = "  ·  ".join(spine_parts)

        # Render onto a temp canvas, then rotate 90° CCW and paste onto spine
        try:
            txt_bbox  = draw.textbbox((0, 0), spine_text, font=spine_font)
            txt_w     = txt_bbox[2] - txt_bbox[0]
            txt_h     = txt_bbox[3] - txt_bbox[1]
            txt_surf  = Image.new("RGBA", (txt_w + 20, txt_h + 10), (0, 0, 0, 0))
            txt_draw  = ImageDraw.Draw(txt_surf)
            txt_draw.text((10, 5), spine_text, font=spine_font,
                          fill=(*spine_color, 255))
            rotated   = txt_surf.rotate(90, expand=True)

            # Center the rotated text in the spine strip
            paste_x = spine_start + (spine_px - rotated.width)  // 2
            paste_y = (px_h - rotated.height) // 2
            canvas_rgba = canvas.convert("RGBA")
            canvas_rgba.paste(rotated, (paste_x, paste_y), rotated)
            canvas = canvas_rgba.convert("RGB")
        except Exception:
            pass  # Skip spine text if font rendering fails

    # ── SAVE ───────────────────────────────────────────────────────────────────
    cover_dir  = project_dir / "output" / "cover"
    cover_dir.mkdir(parents=True, exist_ok=True)

    png_path  = cover_dir / "wraparound.png"
    tiff_path = cover_dir / "wraparound.tif"

    canvas.save(str(png_path), dpi=(dpi, dpi))
    canvas.save(str(tiff_path), dpi=(dpi, dpi))

    return png_path, tiff_path


# ── POST — generate cover art prompt ──────────────────────────────────────────

@router.post("/projects/{project_id}/cover/generate-prompt", response_class=HTMLResponse)
async def cover_generate_prompt(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cfg = _get_api_config(project_id)
    if not cfg["api_key"]:
        cover = (
            db.query(Cover)
            .filter(Cover.project_id == project_id)
            .order_by(Cover.created_at.desc())
            .first()
        )
        return _preview_panel(request, project, cover,
            error="No Gemini API key found. Add it to projects/{id}/config.json under gemini → api_key.")

    # Fetch cover record
    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    if not cover:
        return _preview_panel(request, project, None,
            error="Save your back-cover blurb first — the prompt generator reads it.")

    # Gather context
    style_description  = _get_default_style_description(project_id, db)
    chapter_summaries  = _load_chapter_summaries(project_id)
    genre              = getattr(project, "genre", "fiction") or "fiction"

    prompt = f"""You are a book cover designer creating an AI image generation prompt.

Book title: {project.name}
Author: {getattr(project, 'author', '') or 'Unknown'}
Genre: {genre}

Back cover blurb:
{cover.back_cover_text or '(No blurb available)'}

Interior illustration style (for consistency):
{style_description}

Create a detailed image generation prompt for the FRONT COVER of this book.

RULES:
- The image should be striking and genre-appropriate
- Do NOT include any text, titles, or author names in the image — text will be overlaid later
- Describe a single powerful visual that captures the essence of the book
- Include specific color palette, lighting, composition, and mood
- The image should work at a tall portrait aspect ratio (approximately 2:3)
- Keep the top third and bottom quarter relatively uncluttered — text will go there

RESPOND IN THIS FORMAT:

IMAGE_PROMPT:
[100-150 words: complete image generation prompt]

MOOD:
[3-5 mood tags]

COLOR_PALETTE:
[specific colors that should dominate]"""

    try:
        from google import genai
        client   = genai.Client(api_key=cfg["api_key"])
        response = client.models.generate_content(model=cfg["text_model"], contents=prompt)
        raw_text = response.text.strip()
    except ImportError:
        return _preview_panel(request, project, cover,
            error="google-genai library not installed. Run: pip install google-genai")
    except Exception as exc:
        return _preview_panel(request, project, cover,
            error=f"Gemini error generating prompt: {exc}")

    parsed = _parse_prompt_response(raw_text)
    cover.front_prompt_data = parsed
    db.commit()
    db.refresh(cover)

    return _preview_panel(request, project, cover)


# ── POST — generate front cover image ─────────────────────────────────────────

@router.post("/projects/{project_id}/cover/generate", response_class=HTMLResponse)
async def cover_generate_image(
    project_id:    int,
    request:       Request,
    db:            Session = Depends(get_db),
    custom_prompt: str     = Form(""),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cfg = _get_api_config(project_id)
    if not cfg["api_key"]:
        cover = (
            db.query(Cover)
            .filter(Cover.project_id == project_id)
            .order_by(Cover.created_at.desc())
            .first()
        )
        return _preview_panel(request, project, cover,
            error="No Gemini API key found in config.json.")

    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    if not cover:
        return _preview_panel(request, project, None,
            error="No cover record found. Save a blurb first.")

    # Determine the image prompt to use
    if custom_prompt.strip():
        image_prompt = custom_prompt.strip()
    elif cover.front_prompt_data and cover.front_prompt_data.get("image_prompt"):
        image_prompt = cover.front_prompt_data["image_prompt"]
    else:
        return _preview_panel(request, project, cover,
            error="Generate a cover art prompt first, then click Generate Cover Art.")

    # Append style guardrails
    image_prompt = (
        image_prompt.rstrip(".")
        + ". Tall portrait format, approximately 2:3 aspect ratio. "
          "Do not include any text, letters, words, or typography in the image."
    )

    cover.status = CoverStatus.GENERATING
    db.commit()

    try:
        from google import genai
        from google.genai import types
        client   = genai.Client(api_key=cfg["api_key"])
        response = client.models.generate_content(
            model    = cfg["image_model"],
            contents = image_prompt,
            config   = types.GenerateContentConfig(
                response_modalities = ["image", "text"],
                temperature         = 0.8,
            ),
        )

        image_bytes = None
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                image_bytes = part.inline_data.data
                break

        if not image_bytes:
            cover.status = CoverStatus.PENDING
            db.commit()
            return _preview_panel(request, project, cover,
                error="Gemini returned no image. The prompt may have been filtered — try rephrasing.")

    except ImportError:
        cover.status = CoverStatus.PENDING
        db.commit()
        return _preview_panel(request, project, cover,
            error="google-genai library not installed. Run: pip install google-genai")
    except Exception as exc:
        cover.status = CoverStatus.PENDING
        db.commit()
        return _preview_panel(request, project, cover,
            error=f"Image generation failed: {exc}")

    # Save PNG and thumbnail
    project_dir = _get_project_dir(project_id)
    cover_dir   = project_dir / "output" / "cover"
    cover_dir.mkdir(parents=True, exist_ok=True)

    png_path   = cover_dir / "front_cover.png"
    thumb_path = cover_dir / "front_cover_thumb.jpg"

    png_path.write_bytes(image_bytes)

    # Generate thumbnail (400px wide)
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        thumb_h = int(400 * img.height / max(img.width, 1))
        img.resize((400, thumb_h), Image.LANCZOS).save(str(thumb_path), "JPEG", quality=85)
    except Exception:
        pass  # Thumbnail is nice-to-have; don't fail if Pillow isn't installed

    # Update Cover record (store relative paths)
    cover.front_image_path = "output/cover/front_cover.png"
    cover.thumbnail_path   = "output/cover/front_cover_thumb.jpg"
    cover.status           = CoverStatus.COMPLETE
    cover.attempts         = (cover.attempts or 0) + 1
    db.commit()
    db.refresh(cover)

    return _preview_panel(request, project, cover)


# ── POST — approve + compose wraparound ───────────────────────────────────────

@router.post("/projects/{project_id}/cover/approve", response_class=HTMLResponse)
async def cover_approve(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    if not cover or not cover.front_image_path:
        return _preview_panel(request, project, cover,
            error="No cover image to approve. Generate one first.")

    output_profile = db.get(OutputProfile, cover.output_profile_id)
    if not output_profile:
        return _preview_panel(request, project, cover,
            error="Output profile not found — cannot compute dimensions.")

    # If spine_width_inches not set, recalculate from page count
    if not cover.spine_width_inches:
        page_count = _load_page_count(project_id)
        if page_count and output_profile:
            cover.spine_width_inches = _calculate_spine_width(page_count, output_profile)

    # Compose wraparound
    project_dir = _get_project_dir(project_id)
    try:
        png_path, tiff_path = _compose_wraparound(project_dir, cover, output_profile)
        cover.combined_path = "output/cover/wraparound.png"
    except Exception as exc:
        return _preview_panel(request, project, cover,
            error=f"Wraparound composition failed: {exc}")

    cover.status      = CoverStatus.APPROVED
    cover.reviewed_at = datetime.now()
    db.commit()
    db.refresh(cover)

    return _preview_panel(request, project, cover)


# ── POST — reject cover ────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/cover/reject", response_class=HTMLResponse)
async def cover_reject(
    project_id:     int,
    request:        Request,
    db:             Session = Depends(get_db),
    rejection_note: str     = Form(""),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    if not cover:
        return _preview_panel(request, project, None, error="No cover record found.")

    cover.status         = CoverStatus.FAILED
    cover.rejection_note = rejection_note.strip()
    cover.reviewed_at    = datetime.now()
    db.commit()
    db.refresh(cover)

    return _preview_panel(request, project, cover)


# ── POST — regenerate cover ────────────────────────────────────────────────────

@router.post("/projects/{project_id}/cover/regenerate", response_class=HTMLResponse)
async def cover_regenerate(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cfg = _get_api_config(project_id)
    if not cfg["api_key"]:
        cover = (
            db.query(Cover)
            .filter(Cover.project_id == project_id)
            .order_by(Cover.created_at.desc())
            .first()
        )
        return _preview_panel(request, project, cover,
            error="No Gemini API key found in config.json.")

    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    if not cover:
        return _preview_panel(request, project, None, error="No cover record found.")

    # Build prompt, folding in rejection note
    base_prompt = ""
    if cover.front_prompt_data and cover.front_prompt_data.get("image_prompt"):
        base_prompt = cover.front_prompt_data["image_prompt"]

    if cover.rejection_note:
        image_prompt = (
            f"Previous attempt was rejected: {cover.rejection_note}. "
            f"Generate a different composition. Original concept: {base_prompt}"
        )
    else:
        image_prompt = base_prompt

    if not image_prompt.strip():
        return _preview_panel(request, project, cover,
            error="No prompt to regenerate from. Generate a cover prompt first.")

    image_prompt = (
        image_prompt.rstrip(".")
        + ". Tall portrait format, approximately 2:3 aspect ratio. "
          "Do not include any text, letters, words, or typography in the image."
    )

    cover.status = CoverStatus.GENERATING
    db.commit()

    try:
        from google import genai
        from google.genai import types
        client   = genai.Client(api_key=cfg["api_key"])
        response = client.models.generate_content(
            model    = cfg["image_model"],
            contents = image_prompt,
            config   = types.GenerateContentConfig(
                response_modalities = ["image", "text"],
                temperature         = 0.9,
            ),
        )

        image_bytes = None
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                image_bytes = part.inline_data.data
                break

        if not image_bytes:
            cover.status = CoverStatus.FAILED
            db.commit()
            return _preview_panel(request, project, cover,
                error="Gemini returned no image. Try editing the prompt and regenerating.")

    except ImportError:
        cover.status = CoverStatus.FAILED
        db.commit()
        return _preview_panel(request, project, cover,
            error="google-genai library not installed. Run: pip install google-genai")
    except Exception as exc:
        cover.status = CoverStatus.FAILED
        db.commit()
        return _preview_panel(request, project, cover,
            error=f"Image generation failed: {exc}")

    # Save regenerated image (overwrites previous)
    project_dir = _get_project_dir(project_id)
    cover_dir   = project_dir / "output" / "cover"
    cover_dir.mkdir(parents=True, exist_ok=True)

    png_path   = cover_dir / "front_cover.png"
    thumb_path = cover_dir / "front_cover_thumb.jpg"
    png_path.write_bytes(image_bytes)

    try:
        from PIL import Image
        img    = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        thumb_h = int(400 * img.height / max(img.width, 1))
        img.resize((400, thumb_h), Image.LANCZOS).save(str(thumb_path), "JPEG", quality=85)
    except Exception:
        pass

    cover.front_image_path = "output/cover/front_cover.png"
    cover.thumbnail_path   = "output/cover/front_cover_thumb.jpg"
    cover.status           = CoverStatus.COMPLETE
    cover.rejection_note   = ""
    cover.attempts         = (cover.attempts or 0) + 1
    db.commit()
    db.refresh(cover)

    return _preview_panel(request, project, cover)


# ── POST — reset to prompt stage ──────────────────────────────────────────────

@router.post("/projects/{project_id}/cover/reset", response_class=HTMLResponse)
async def cover_reset(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    if cover:
        cover.front_prompt_data = {}
        cover.front_image_path  = None
        cover.thumbnail_path    = None
        cover.combined_path     = None
        cover.status            = CoverStatus.PENDING
        cover.rejection_note    = ""
        cover.reviewed_at       = None
        db.commit()
        db.refresh(cover)

    return _preview_panel(request, project, cover)


# ── POST — save cover text overlay settings ───────────────────────────────────

@router.post("/projects/{project_id}/cover/text-settings", response_class=HTMLResponse)
async def cover_text_settings_save(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    """Save cover text overlay settings to config.json and return the updated panel."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    form = await request.form()
    config = _load_config(project_id)

    # Save subtitle to book section of config
    subtitle = form.get("subtitle", "").strip()
    if "book" not in config:
        config["book"] = {}
    config["book"]["subtitle"] = subtitle

    # Build settings from form data
    settings = {
        "front": {
            "show_title":       form.get("front_show_title") == "on",
            "show_subtitle":    form.get("front_show_subtitle") == "on",
            "show_author":      form.get("front_show_author") == "on",
            "title_position":   form.get("front_title_position", "top"),
            "author_position":  form.get("front_author_position", "bottom"),
            "title_font_size":  int(form.get("front_title_font_size", 48)),
            "subtitle_font_size": int(form.get("front_subtitle_font_size", 24)),
            "author_font_size": int(form.get("front_author_font_size", 22)),
            "title_color":      form.get("front_title_color", "#FFFFFF"),
            "subtitle_color":   form.get("front_subtitle_color", "#E0D8CC"),
            "author_color":     form.get("front_author_color", "#E0D8CC"),
            "text_shadow":      form.get("front_text_shadow") == "on",
            "band_overlay":     form.get("front_band_overlay") == "on",
            "band_opacity":     int(form.get("front_band_opacity", 100)),
        },
        "spine": {
            "show_title":  form.get("spine_show_title") == "on",
            "show_author": form.get("spine_show_author") == "on",
            "font_size":   int(form.get("spine_font_size", 14)),
            "color":       form.get("spine_color", "#DCD2C3"),
        },
        "back": {
            "show_title":       form.get("back_show_title") == "on",
            "show_author":      form.get("back_show_author") == "on",
            "title_font_size":  int(form.get("back_title_font_size", 28)),
            "author_font_size": int(form.get("back_author_font_size", 18)),
            "title_color":      form.get("back_title_color", "#FFFFFF"),
            "author_color":     form.get("back_author_color", "#DCD2C3"),
        },
    }

    config["cover_text"] = settings
    _save_config(project_id, config)

    # Include subtitle in the settings dict for the template
    settings["subtitle"] = subtitle

    # Return the text settings panel fragment with a success message
    return templates.TemplateResponse(
        "fragments/cover_text_settings.html",
        {
            "request":  request,
            "project":  project,
            "settings": settings,
            "saved":    True,
        },
    )


# ── POST — recompose wraparound with current text settings ────────────────────

@router.post("/projects/{project_id}/cover/recompose", response_class=HTMLResponse)
async def cover_recompose(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    """Re-compose the wraparound using current text settings (no image regeneration)."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    if not cover or not cover.front_image_path:
        return _preview_panel(request, project, cover,
            error="No front cover image found. Generate one first before composing.")

    output_profile = db.get(OutputProfile, cover.output_profile_id)
    if not output_profile:
        return _preview_panel(request, project, cover,
            error="Output profile not found — cannot compute dimensions.")

    # Recalculate spine if needed
    if not cover.spine_width_inches:
        page_count = _load_page_count(project_id)
        if page_count and output_profile:
            cover.spine_width_inches = _calculate_spine_width(page_count, output_profile)

    project_dir = _get_project_dir(project_id)
    try:
        png_path, tiff_path = _compose_wraparound(project_dir, cover, output_profile)
        cover.combined_path = "output/cover/wraparound.png"
    except Exception as exc:
        return _preview_panel(request, project, cover,
            error=f"Recompose failed: {exc}")

    # Keep approved status if it was approved
    if cover.status != CoverStatus.APPROVED:
        cover.status = CoverStatus.APPROVED
    cover.reviewed_at = datetime.now()
    db.commit()
    db.refresh(cover)

    return _preview_panel(request, project, cover)


# ── GET — serve cover images ───────────────────────────────────────────────────

@router.get("/projects/{project_id}/cover/img/{filename}", response_class=FileResponse)
async def cover_image_serve(
    project_id: int,
    filename:   str,
    db:         Session = Depends(get_db),
):
    # Safety: no path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    image_path = _get_project_dir(project_id) / "output" / "cover" / filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail=f"Image not found: {filename}")

    # Determine media type
    suffix = image_path.suffix.lower()
    media_types = {
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tif":  "image/tiff",
        ".tiff": "image/tiff",
    }
    media_type = media_types.get(suffix, "application/octet-stream")

    return FileResponse(str(image_path), media_type=media_type)
