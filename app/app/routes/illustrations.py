"""
ILLUSTRATIONS ROUTES
Illustration Architect — gallery page, Style Ledger CRUD, and AI generation pipeline.

Session 7a (structure):
  GET  /projects/{project_id}/illustrations
  POST /projects/{project_id}/illustrations/styles
  GET  /projects/{project_id}/illustrations/styles/{style_id}
  PUT  /projects/{project_id}/illustrations/styles/{style_id}
  DELETE /projects/{project_id}/illustrations/styles/{style_id}
  POST /projects/{project_id}/illustrations/styles/{style_id}/set-default

Session 7b (AI generation + approval):
  POST /projects/{project_id}/illustrations/styles/{style_id}/generate-profile
       — AI generates a structured style profile from style_input text

  GET  /projects/{project_id}/illustrations/chapter/{chapter_key}/detail
       — chapter detail panel (HTMX fragment: scene analysis + image panel)

  GET  /projects/{project_id}/illustrations/img/{path:path}
       — serve generated image files from the project's illustrations output dir

  POST /projects/{project_id}/illustrations/chapter/{chapter_key}/analyze
       — AI scene analysis: reads chapter, selects visual moment, writes prompt

  POST /projects/{project_id}/illustrations/chapter/{chapter_key}/generate
       — Gemini image generation from the scene analysis prompt

  POST /projects/{project_id}/illustrations/chapter/{chapter_key}/approve
       — mark illustration as approved, optionally produce CMYK TIFF

  POST /projects/{project_id}/illustrations/chapter/{chapter_key}/reject
       — reject with optional rejection_note

  POST /projects/{project_id}/illustrations/chapter/{chapter_key}/regenerate
       — regenerate image (optionally with rejection note folded into prompt)
"""

import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Project, Chapter, Illustration, IllustrationStyle, IllustrationStatus,
    IllustrationProvider, PipelineRun, RunStatus, WorldRule,
)

router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"

EDGE_STYLE_CHOICES = ["straight", "curved", "tattered", "vignette", "torn", "circular"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _get_illustration_status(project_id: int, db: Session) -> dict:
    """
    Build a per-chapter illustration status summary.

    Returns a dict keyed by chapter.id:
      {
        chapter_id: {
          "db_record": Illustration | None,
          "status": IllustrationStatus value string | "not_started" | "legacy",
          "thumbnail_path": str | None,
          "legacy_image": bool,
        }
      }
    """
    chapters   = db.query(Chapter).filter(Chapter.project_id == project_id).all()
    illust_map = {}

    # Index existing DB illustrations by chapter_id (take most recent per chapter)
    for ch in chapters:
        ill = (
            db.query(Illustration)
            .filter(Illustration.chapter_id == ch.id)
            .order_by(Illustration.id.desc())
            .first()
        )
        # Derive display status: if DB status is "pending" but prompt_data exists,
        # the scene analysis ran successfully → show as "analyzed"
        if ill:
            if ill.status.value == "pending" and ill.prompt_data:
                display_status = "analyzed"
            else:
                display_status = ill.status.value
        else:
            display_status = "not_started"

        illust_map[ch.id] = {
            "db_record":      ill,
            "status":         display_status,
            "thumbnail_path": ill.thumbnail_path if ill else None,
            "legacy_image":   False,
        }

    # Check disk for legacy TIFF files (from old CLI agent)
    images_dir = _get_project_dir(project_id) / "output" / "illustrations" / "images"
    if images_dir.exists():
        for ch in chapters:
            if illust_map[ch.id]["db_record"] is None:
                key = ch.chapter_key
                tif_files = list(images_dir.glob(f"chapter_{key}.tif*"))
                if tif_files:
                    illust_map[ch.id]["status"]       = "legacy"
                    illust_map[ch.id]["legacy_image"] = True
                    illust_map[ch.id]["legacy_path"]  = str(tif_files[0])

    return illust_map


def _get_or_create_pipeline_run(project_id: int, db: Session) -> PipelineRun:
    """Get the latest PipelineRun for a project, or create a minimal one."""
    run = (
        db.query(PipelineRun)
        .filter(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.id.desc())
        .first()
    )
    if not run:
        run = PipelineRun(
            project_id = project_id,
            name       = "Illustration Web UI Run",
            status     = RunStatus.RUNNING,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
    return run


def _load_chapter_text(project_id: int, chapter_key: str) -> str:
    """
    Load best available version of a chapter as a plain text string.
    Priority: working copy → edited → ingested.
    Returns "" if nothing found.
    """
    pd = _get_project_dir(project_id)

    def _paragraphs_to_text(paragraphs: list) -> str:
        parts = []
        for p in paragraphs:
            if isinstance(p, dict):
                text = p.get("text", "").strip()
            else:
                text = str(p).strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    # 1. Working copy
    wp = pd / "output" / "workbench" / f"chapter_{chapter_key}_working.json"
    if wp.exists():
        try:
            data = json.loads(wp.read_text(encoding="utf-8"))
            return _paragraphs_to_text(data.get("paragraphs", []))
        except Exception:
            pass

    # 2. Edited chapter
    edited = pd / "output" / "editing" / f"chapter_{chapter_key}_edited.json"
    if edited.exists():
        try:
            data = json.loads(edited.read_text(encoding="utf-8"))
            return _paragraphs_to_text(data.get("paragraphs", []))
        except Exception:
            pass

    # 3. Ingested chapter
    ingested = pd / "output" / "ingested" / f"chapter_{chapter_key}.json"
    if ingested.exists():
        try:
            data = json.loads(ingested.read_text(encoding="utf-8"))
            return _paragraphs_to_text(data.get("paragraphs", []))
        except Exception:
            pass

    return ""


def _load_gemini_config(project_id: int) -> dict:
    """
    Load Gemini API key and model names from project config.json.
    Returns dict with keys: api_key, text_model, fast_model, image_model.
    Raises ValueError if config is missing or malformed.
    """
    config_path = _get_project_dir(project_id) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}

    gemini = config.get("gemini", {})
    api_key = gemini.get("api_key", "").strip() or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("No Gemini API key found in config.json or environment.")

    models = gemini.get("models", {})
    return {
        "api_key":      api_key,
        "text_model":   models.get("text",             "gemini-2.5-pro"),
        "fast_model":   models.get("fast",             "gemini-2.5-flash"),
        "image_model":  models.get("image_generation", "gemini-2.5-flash-image"),
        "image_model_pro": models.get("image_generation_pro", "gemini-2.5-flash-image"),
    }


def _error_panel(message: str) -> HTMLResponse:
    """Standard error panel returned when AI calls or other operations fail."""
    html = f"""
<div class="bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300">
  <strong>Generation Error:</strong> {message}
</div>
"""
    return HTMLResponse(content=html)


def _parse_scene_analysis(text: str) -> dict:
    """
    Parse Gemini scene analysis response into a structured dict.
    Expected format:
      SCENE: ...
      IMAGE_PROMPT: ...
      NEGATIVE_PROMPT: ...
      MOOD: ...
      CHARACTERS: ...
    """
    result = {}
    # Extract each labelled section
    sections = [
        ("SCENE",           "scene"),
        ("IMAGE_PROMPT",    "image_prompt"),
        ("NEGATIVE_PROMPT", "negative_prompt"),
        ("MOOD",            "mood"),
        ("CHARACTERS",      "characters"),
    ]
    for label, key in sections:
        pattern = rf"(?:^|\n){label}:\s*(.+?)(?=\n(?:SCENE|IMAGE_PROMPT|NEGATIVE_PROMPT|MOOD|CHARACTERS):|$)"
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            result[key] = match.group(1).strip()

    # Parse mood into a tag list
    if "mood" in result:
        result["mood_tags"] = [
            m.strip() for m in result["mood"].split(",") if m.strip()
        ]
    else:
        result["mood_tags"] = []

    return result


def _render_card_html(request: Request, project: Project, chapter: Chapter,
                      ch_status: str, thumb: Optional[str], is_legacy: bool) -> str:
    """Render the chapter card fragment to an HTML string (for OOB swaps)."""
    card_template = templates.env.get_template("illustrations_chapter_card.html")
    return card_template.render(
        request    = request,
        project    = project,
        chapter    = chapter,
        ch_status  = ch_status,
        thumb      = thumb,
        is_legacy  = is_legacy,
    )


def _thumbnail_url(project_id: int, filename: str) -> str:
    """Build the web URL for serving a generated image or thumbnail."""
    return f"/projects/{project_id}/illustrations/img/{filename}"


def _build_style_preamble(default_style) -> str:
    """Build a consistent style preamble from the default IllustrationStyle.

    This is prepended to every image generation prompt so the image model
    receives the same art-direction brief regardless of scene content,
    producing a visually cohesive set of illustrations.
    """
    if not default_style:
        return ""

    parts = [f"ART STYLE — Every illustration in this book must look like it was painted by the same artist."]

    def _clean(val: str) -> str:
        """Strip trailing period to avoid double-period when we add one."""
        return val.rstrip(". ") if val else val

    profile = default_style.style_profile
    if profile:
        if profile.get("medium"):
            parts.append(f"Medium: {_clean(profile['medium'])}.")
        if profile.get("palette"):
            parts.append(f"Color palette: {_clean(profile['palette'])}.")
        if profile.get("lighting"):
            parts.append(f"Lighting: {_clean(profile['lighting'])}.")
        if profile.get("texture"):
            parts.append(f"Texture: {_clean(profile['texture'])}.")
        if profile.get("perspective"):
            parts.append(f"Perspective: {_clean(profile['perspective'])}.")
        if profile.get("mood"):
            parts.append(f"Mood: {_clean(profile['mood'])}.")
    elif default_style.style_input:
        parts.append(default_style.style_input)

    edge = (default_style.container_mask or "straight").strip()
    if edge and edge.lower() != "straight":
        parts.append(f"Frame: render inside a {edge} frame/border shape.")

    if profile and profile.get("negative"):
        parts.append(f"AVOID: {_clean(profile['negative'])}.")

    return " ".join(parts)


# ── GET — main illustrations page ────────────────────────────────────────────

@router.get("/projects/{project_id}/illustrations", response_class=HTMLResponse)
async def illustrations_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapters = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id)
        .order_by(Chapter.chapter_number.asc().nullslast(), Chapter.chapter_key.asc())
        .all()
    )

    styles = (
        db.query(IllustrationStyle)
        .filter(IllustrationStyle.project_id == project_id)
        .order_by(IllustrationStyle.created_at.asc())
        .all()
    )

    default_style = next((s for s in styles if s.is_default), None)
    illust_status = _get_illustration_status(project_id, db)

    total = len(chapters)
    # Count each display status
    status_counts = {}
    for v in illust_status.values():
        s = v["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    return templates.TemplateResponse(
        "illustrations.html",
        {
            "request":       request,
            "project":       project,
            "active_page":   "illustrations",
            "chapters":      chapters,
            "illust_status": illust_status,
            "styles":        styles,
            "default_style": default_style,
            "edge_choices":  EDGE_STYLE_CHOICES,
            "stats": {
                "total":         total,
                "approved":      status_counts.get("approved", 0),
                "generated":     status_counts.get("generated", 0),
                "analyzed":      status_counts.get("analyzed", 0),
                "not_started":   status_counts.get("not_started", 0),
                "rejected":      status_counts.get("rejected", 0),
                "generating":    status_counts.get("generating", 0),
                "legacy":        status_counts.get("legacy", 0),
            },
        },
    )


# ── POST — create style ───────────────────────────────────────────────────────

@router.post("/projects/{project_id}/illustrations/styles", response_class=HTMLResponse)
async def create_style(
    project_id:     int,
    request:        Request,
    name:           str  = Form(...),
    style_input:    str  = Form(""),
    container_mask: str  = Form("straight"),
    is_default:     bool = Form(False),
    db:             Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if is_default:
        db.query(IllustrationStyle).filter(
            IllustrationStyle.project_id == project_id
        ).update({"is_default": False})

    style = IllustrationStyle(
        project_id     = project_id,
        name           = name.strip(),
        style_input    = style_input.strip(),
        container_mask = container_mask,
        style_profile  = {},
        is_default     = is_default,
    )
    db.add(style)
    db.commit()
    db.refresh(style)

    return templates.TemplateResponse(
        "illustrations_style_card.html",
        {
            "request":      request,
            "project":      project,
            "style":        style,
            "edge_choices": EDGE_STYLE_CHOICES,
        },
    )


# ── GET — style detail fragment ───────────────────────────────────────────────

@router.get(
    "/projects/{project_id}/illustrations/styles/{style_id}",
    response_class=HTMLResponse,
)
async def get_style(
    project_id: int,
    style_id:   int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    style = db.get(IllustrationStyle, style_id)
    if not style or style.project_id != project_id:
        raise HTTPException(status_code=404, detail="Style not found")

    return templates.TemplateResponse(
        "illustrations_style_card.html",
        {
            "request":      request,
            "project":      project,
            "style":        style,
            "edge_choices": EDGE_STYLE_CHOICES,
        },
    )


# ── PUT — update style ────────────────────────────────────────────────────────

@router.put(
    "/projects/{project_id}/illustrations/styles/{style_id}",
    response_class=HTMLResponse,
)
async def update_style(
    project_id:     int,
    style_id:       int,
    request:        Request,
    name:           str  = Form(...),
    style_input:    str  = Form(""),
    container_mask: str  = Form("straight"),
    is_default:     bool = Form(False),
    db:             Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    style = db.get(IllustrationStyle, style_id)
    if not style or style.project_id != project_id:
        raise HTTPException(status_code=404, detail="Style not found")

    if is_default:
        db.query(IllustrationStyle).filter(
            IllustrationStyle.project_id == project_id
        ).update({"is_default": False})

    style.name           = name.strip()
    style.style_input    = style_input.strip()
    style.container_mask = container_mask
    style.is_default     = is_default
    db.commit()
    db.refresh(style)

    return templates.TemplateResponse(
        "illustrations_style_card.html",
        {
            "request":      request,
            "project":      project,
            "style":        style,
            "edge_choices": EDGE_STYLE_CHOICES,
        },
    )


# ── DELETE — delete style ─────────────────────────────────────────────────────

@router.delete(
    "/projects/{project_id}/illustrations/styles/{style_id}",
    response_class=HTMLResponse,
)
async def delete_style(
    project_id: int,
    style_id:   int,
    db:         Session = Depends(get_db),
):
    style = db.get(IllustrationStyle, style_id)
    if style and style.project_id == project_id:
        db.delete(style)
        db.commit()
    return HTMLResponse(content="")


# ── POST — set default style ──────────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/illustrations/styles/{style_id}/set-default",
    response_class=HTMLResponse,
)
async def set_default_style(
    project_id: int,
    style_id:   int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    db.query(IllustrationStyle).filter(
        IllustrationStyle.project_id == project_id
    ).update({"is_default": False})

    style = db.get(IllustrationStyle, style_id)
    if not style or style.project_id != project_id:
        raise HTTPException(status_code=404, detail="Style not found")

    style.is_default = True
    db.commit()

    styles = (
        db.query(IllustrationStyle)
        .filter(IllustrationStyle.project_id == project_id)
        .order_by(IllustrationStyle.created_at.asc())
        .all()
    )

    return templates.TemplateResponse(
        "illustrations_style_list.html",
        {
            "request":      request,
            "project":      project,
            "styles":       styles,
            "edge_choices": EDGE_STYLE_CHOICES,
        },
    )


# ═══════════════════════════════════════════════════════════════════
#  SESSION 7b — AI GENERATION + APPROVAL
# ═══════════════════════════════════════════════════════════════════

# ── POST — generate style profile ────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/illustrations/styles/{style_id}/generate-profile",
    response_class=HTMLResponse,
)
async def generate_style_profile(
    project_id: int,
    style_id:   int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    style = db.get(IllustrationStyle, style_id)
    if not style or style.project_id != project_id:
        raise HTTPException(status_code=404, detail="Style not found")

    if not style.style_input:
        return _error_panel("No style description to analyse. Add a description first.")

    # Load Gemini config
    try:
        cfg = _load_gemini_config(project_id)
    except ValueError as e:
        return _error_panel(str(e))

    prompt = f"""You are an art director creating a style guide for AI image generation.

The user described their desired illustration style as:
"{style.style_input}"

Generate a structured style profile with these exact fields:
- medium: the artistic medium (e.g., "digital oil painting", "ink wash", "watercolor")
- palette: the color palette description
- lighting: lighting quality and direction
- perspective: typical camera angle and composition
- texture: surface texture and brushwork description
- mood: emotional atmosphere
- negative: comma-separated list of things to AVOID in generation

Respond in valid JSON only, no markdown fences, no commentary."""

    try:
        from google import genai as _genai
        client = _genai.Client(api_key=cfg["api_key"])
        response = client.models.generate_content(
            model    = cfg["fast_model"],
            contents = prompt,
        )
        raw_text = response.text.strip()

        # Strip markdown code fences if Gemini added them anyway
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$",           "", raw_text)

        profile = json.loads(raw_text)

    except ImportError:
        return _error_panel("google-genai library not installed. Run: pip install google-genai")
    except json.JSONDecodeError as e:
        return _error_panel(f"AI returned invalid JSON: {e}")
    except Exception as e:
        return _error_panel(f"Gemini API error: {e}")

    # Persist the profile
    style.style_profile = profile
    db.commit()
    db.refresh(style)

    return templates.TemplateResponse(
        "illustrations_style_card.html",
        {
            "request":      request,
            "project":      project,
            "style":        style,
            "edge_choices": EDGE_STYLE_CHOICES,
        },
    )


# ── GET — serve illustration images ──────────────────────────────────────────

@router.get("/projects/{project_id}/illustrations/img/{path:path}")
async def serve_illustration_image(project_id: int, path: str):
    """Serve generated PNG/JPEG images from the project's illustrations output dir."""
    img_path = (
        _get_project_dir(project_id)
        / "output" / "illustrations" / "images"
        / path
    )
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(img_path))


# ── GET — chapter detail panel ────────────────────────────────────────────────

@router.get(
    "/projects/{project_id}/illustrations/chapter/{chapter_key}/detail",
    response_class=HTMLResponse,
)
async def chapter_detail(
    project_id:  int,
    chapter_key: str,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapter = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id, Chapter.chapter_key == chapter_key)
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    # Most recent illustration for this chapter
    illustration = (
        db.query(Illustration)
        .filter(Illustration.chapter_id == chapter.id)
        .order_by(Illustration.id.desc())
        .first()
    )

    default_style = (
        db.query(IllustrationStyle)
        .filter(
            IllustrationStyle.project_id == project_id,
            IllustrationStyle.is_default  == True,
        )
        .first()
    )

    return templates.TemplateResponse(
        "illustrations_chapter_detail.html",
        {
            "request":       request,
            "project":       project,
            "chapter":       chapter,
            "illustration":  illustration,
            "default_style": default_style,
        },
    )


# ── POST — scene analysis ─────────────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/illustrations/chapter/{chapter_key}/analyze",
    response_class=HTMLResponse,
)
async def analyze_chapter(
    project_id:  int,
    chapter_key: str,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapter = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id, Chapter.chapter_key == chapter_key)
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    # Load chapter text
    chapter_text = _load_chapter_text(project_id, chapter_key)
    if not chapter_text:
        return _error_panel("No chapter text found. Run the Intake agent first.")

    # Truncate if very long
    if len(chapter_text) > 6000:
        chapter_text = chapter_text[:6000] + "\n\n[... truncated ...]"

    # Load default style
    default_style = (
        db.query(IllustrationStyle)
        .filter(
            IllustrationStyle.project_id == project_id,
            IllustrationStyle.is_default  == True,
        )
        .first()
    )

    if default_style:
        if default_style.style_profile:
            profile = default_style.style_profile
            style_block = (
                f"Name: {default_style.name}\n"
                f"Medium: {profile.get('medium', '')}\n"
                f"Palette: {profile.get('palette', '')}\n"
                f"Lighting: {profile.get('lighting', '')}\n"
                f"Perspective: {profile.get('perspective', '')}\n"
                f"Texture: {profile.get('texture', '')}\n"
                f"Mood: {profile.get('mood', '')}\n"
                f"Avoid: {profile.get('negative', '')}"
            )
        else:
            style_block = default_style.style_input or "No style description available."
        # Inject edge/frame style into the style block
        edge = (default_style.container_mask or "straight").strip()
        if edge and edge != "straight":
            style_block += f"\nFrame / Edge Style: {edge} (the illustration MUST be rendered inside a {edge} frame or border shape)"
    else:
        style_block = "No style set — use a general artistic approach."

    # Load active character world rules
    char_rules = (
        db.query(WorldRule)
        .filter(
            WorldRule.project_id == project_id,
            WorldRule.category   == "character",
            WorldRule.is_active  == True,
        )
        .order_by(WorldRule.sort_order.asc())
        .all()
    )

    if char_rules:
        char_block = "\n\n".join(
            f"{r.title}:\n{r.content}" for r in char_rules
        )
    else:
        char_block = "No character descriptions available."

    prompt = f"""You are a visual development artist for a book illustration pipeline.

PROJECT STYLE:
{style_block}

CHARACTER REFERENCE (from World Rules):
{char_block}

CHAPTER TEXT:
{chapter_text}

YOUR TASK:
Read the chapter and identify the single strongest visual moment to illustrate.

RULES:
- Choose a moment from the actual text — not a symbolic or abstract concept
- Describe characters specifically (age, build, clothing, features) if they appear
- The prompt should describe the scene contents first, then style
- No split panels, diptychs, or side-by-side images
- Include specific color palette guidance that fits this chapter's mood

RESPOND IN THIS EXACT FORMAT (plain text, not JSON):

SCENE:
[2-3 sentences: the moment chosen and why it works visually]

IMAGE_PROMPT:
[80-120 words: complete image generation prompt with scene description + style instructions]

NEGATIVE_PROMPT:
[comma-separated terms to avoid]

MOOD:
[3-5 mood tags, comma-separated]

CHARACTERS:
[character names present, or NONE]"""

    try:
        cfg = _load_gemini_config(project_id)
    except ValueError as e:
        return _error_panel(str(e))

    # Model toggle: pro or flash (sent from the UI toggle)
    form = await request.form()
    model_tier = form.get("model_tier", "pro")
    text_model = cfg["text_model"] if model_tier == "pro" else cfg["fast_model"]

    # Scene nudge — optional user guidance to steer scene selection
    scene_nudge = (form.get("scene_nudge") or "").strip()
    if scene_nudge:
        prompt += f"\n\nAUTHOR GUIDANCE:\nThe author wants you to prefer a scene that: {scene_nudge}"

    try:
        from google import genai as _genai
        client   = _genai.Client(api_key=cfg["api_key"])
        response = client.models.generate_content(
            model    = text_model,
            contents = prompt,
        )
        raw_text = response.text.strip()
    except ImportError:
        return _error_panel("google-genai library not installed. Run: pip install google-genai")
    except Exception as e:
        return _error_panel(f"Gemini API error ({text_model}): {e}")

    # Parse the structured response
    prompt_data = _parse_scene_analysis(raw_text)
    prompt_data["raw_response"] = raw_text

    # Save to disk
    prompts_dir = _get_project_dir(project_id) / "output" / "illustrations" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompts_dir / f"chapter_{chapter_key}_prompt.json"
    prompt_file.write_text(json.dumps(prompt_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Create or update Illustration record
    pipeline_run = _get_or_create_pipeline_run(project_id, db)

    illustration = (
        db.query(Illustration)
        .filter(Illustration.chapter_id == chapter.id)
        .order_by(Illustration.id.desc())
        .first()
    )

    if illustration and illustration.status.value in ("pending", "rejected"):
        # Update existing pending/rejected record
        illustration.prompt_data = prompt_data
        illustration.status      = IllustrationStatus.PENDING
    else:
        # Create new record
        illustration = Illustration(
            chapter_id      = chapter.id,
            pipeline_run_id = pipeline_run.id,
            status          = IllustrationStatus.PENDING,
            provider        = IllustrationProvider.GEMINI_IMAGE,
            prompt_data     = prompt_data,
        )
        db.add(illustration)

    db.commit()
    db.refresh(illustration)

    return templates.TemplateResponse(
        "illustrations_scene_analysis.html",
        {
            "request":      request,
            "project":      project,
            "chapter":      chapter,
            "illustration": illustration,
            "prompt_data":  prompt_data,
            "model_used":   text_model,
        },
    )


# ── POST — generate image ─────────────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/illustrations/chapter/{chapter_key}/generate",
    response_class=HTMLResponse,
)
async def generate_image(
    project_id:  int,
    chapter_key: str,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapter = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id, Chapter.chapter_key == chapter_key)
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    illustration = (
        db.query(Illustration)
        .filter(Illustration.chapter_id == chapter.id)
        .order_by(Illustration.id.desc())
        .first()
    )

    if not illustration or not illustration.prompt_data:
        return _error_panel("No scene analysis found. Run Analyze first.")

    # Load config
    try:
        cfg = _load_gemini_config(project_id)
    except ValueError as e:
        return _error_panel(str(e))

    # Model toggle: pro or flash
    form = await request.form()
    model_tier = form.get("model_tier", "pro")
    image_model = cfg["image_model_pro"] if model_tier == "pro" else cfg["image_model"]

    # Mark as generating
    illustration.status = IllustrationStatus.GENERATING
    db.commit()

    # Build the full prompt with style preamble for visual consistency
    prompt_data   = illustration.prompt_data
    image_prompt  = prompt_data.get("image_prompt", "")
    negative      = prompt_data.get("negative_prompt", "")

    # Load default style for the preamble
    default_style = (
        db.query(IllustrationStyle)
        .filter(IllustrationStyle.project_id == project_id, IllustrationStyle.is_default == True)
        .first()
    )
    style_preamble = _build_style_preamble(default_style)

    # Assemble: style preamble → scene prompt → negative
    prompt_parts = []
    if style_preamble:
        prompt_parts.append(style_preamble)
    prompt_parts.append(f"SCENE: {image_prompt}")
    if negative:
        prompt_parts.append(f"Avoid: {negative}")
    full_prompt = " ".join(prompt_parts)

    # Gemini image generation
    try:
        from google import genai as _genai
        from google.genai import types as _types

        client   = _genai.Client(api_key=cfg["api_key"])
        response = client.models.generate_content(
            model    = image_model,
            contents = full_prompt,
            config   = _types.GenerateContentConfig(
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
            illustration.status = IllustrationStatus.PENDING
            db.commit()
            return _error_panel("Gemini returned no image. The prompt may have been filtered — try rephrasing.")

    except ImportError:
        illustration.status = IllustrationStatus.PENDING
        db.commit()
        return _error_panel("google-genai library not installed. Run: pip install google-genai")
    except Exception as e:
        illustration.status = IllustrationStatus.PENDING
        db.commit()
        return _error_panel(f"Image generation failed: {e}")

    # Save PNG and thumbnail
    images_dir = _get_project_dir(project_id) / "output" / "illustrations" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    png_filename   = f"chapter_{chapter_key}.png"
    thumb_filename = f"chapter_{chapter_key}_thumb.jpg"
    png_path       = images_dir / png_filename
    thumb_path     = images_dir / thumb_filename

    png_path.write_bytes(image_bytes)

    # Generate thumbnail with Pillow
    try:
        from PIL import Image as _Image
        img = _Image.open(io.BytesIO(image_bytes))
        img.thumbnail((300, 300))
        img.save(str(thumb_path), "JPEG", quality=85)
        thumb_url = _thumbnail_url(project_id, thumb_filename)
    except ImportError:
        # Pillow not available — use full image, scaled down by CSS
        thumb_url = _thumbnail_url(project_id, png_filename)
    except Exception:
        thumb_url = _thumbnail_url(project_id, png_filename)

    # Update illustration record
    illustration.raw_image_path = _thumbnail_url(project_id, png_filename)
    illustration.thumbnail_path = thumb_url
    illustration.status          = IllustrationStatus.GENERATED
    illustration.attempts        = (illustration.attempts or 0) + 1
    db.commit()
    db.refresh(illustration)

    return templates.TemplateResponse(
        "illustrations_image_result.html",
        {
            "request":      request,
            "project":      project,
            "chapter":      chapter,
            "illustration": illustration,
            "model_used":   image_model,
        },
    )


# ── POST — approve ────────────────────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/illustrations/chapter/{chapter_key}/approve",
    response_class=HTMLResponse,
)
async def approve_illustration(
    project_id:  int,
    chapter_key: str,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapter = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id, Chapter.chapter_key == chapter_key)
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    illustration = (
        db.query(Illustration)
        .filter(Illustration.chapter_id == chapter.id)
        .order_by(Illustration.id.desc())
        .first()
    )
    if not illustration:
        return _error_panel("No illustration found to approve.")

    illustration.status      = IllustrationStatus.APPROVED
    illustration.reviewed_at = datetime.now()

    # Optional: convert to CMYK TIFF for print
    if illustration.raw_image_path:
        try:
            from PIL import Image as _Image
            raw_url = illustration.raw_image_path
            png_filename = raw_url.split("/")[-1]
            images_dir = _get_project_dir(project_id) / "output" / "illustrations" / "images"
            png_path  = images_dir / png_filename
            if png_path.exists():
                tif_filename = f"chapter_{chapter_key}.tif"
                tif_path     = images_dir / tif_filename
                img = _Image.open(str(png_path)).convert("CMYK")
                img.save(str(tif_path), "TIFF", dpi=(300, 300))
                illustration.final_path = _thumbnail_url(project_id, tif_filename)
        except Exception:
            pass  # CMYK conversion is optional — don't block approval

    db.commit()
    db.refresh(illustration)

    # Build the updated gallery card (OOB swap)
    card_html = _render_card_html(
        request   = request,
        project   = project,
        chapter   = chapter,
        ch_status = "approved",
        thumb     = illustration.thumbnail_path,
        is_legacy = False,
    )
    # Add hx-swap-oob so HTMX replaces the card in the gallery
    oob_card = card_html.replace(
        f'id="chapter-card-{chapter_key}"',
        f'id="chapter-card-{chapter_key}" hx-swap-oob="outerHTML"',
        1,
    )

    primary_html = f"""
<div class="flex flex-col items-center justify-center gap-4 py-12 text-center">
  <div class="w-14 h-14 rounded-full bg-green-400/15 flex items-center justify-center">
    <svg class="w-7 h-7 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
    </svg>
  </div>
  <div>
    <div class="text-lg font-semibold text-green-400">Illustration Approved</div>
    <div class="text-sm text-slate-500 mt-1">Gallery updated · {illustration.attempts} attempt{"s" if illustration.attempts != 1 else ""}</div>
  </div>
  <img src="{illustration.thumbnail_path or ''}"
       alt="Approved illustration"
       class="rounded-xl border border-green-400/30 max-w-xs mt-2 shadow-lg" />
</div>
"""
    combined = primary_html + "\n" + oob_card
    return HTMLResponse(content=combined)


# ── POST — reject ─────────────────────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/illustrations/chapter/{chapter_key}/reject",
    response_class=HTMLResponse,
)
async def reject_illustration(
    project_id:     int,
    chapter_key:    str,
    request:        Request,
    rejection_note: str = Form(""),
    db:             Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapter = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id, Chapter.chapter_key == chapter_key)
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    illustration = (
        db.query(Illustration)
        .filter(Illustration.chapter_id == chapter.id)
        .order_by(Illustration.id.desc())
        .first()
    )
    if not illustration:
        return _error_panel("No illustration found to reject.")

    illustration.status         = IllustrationStatus.REJECTED
    illustration.rejection_note = rejection_note.strip()
    illustration.reviewed_at    = datetime.now()
    db.commit()
    db.refresh(illustration)

    # OOB card update
    card_html = _render_card_html(
        request   = request,
        project   = project,
        chapter   = chapter,
        ch_status = "rejected",
        thumb     = illustration.thumbnail_path,
        is_legacy = False,
    )
    oob_card = card_html.replace(
        f'id="chapter-card-{chapter_key}"',
        f'id="chapter-card-{chapter_key}" hx-swap-oob="outerHTML"',
        1,
    )

    note_display = f'<p class="text-xs text-slate-500 italic mt-1">{rejection_note}</p>' if rejection_note else ""

    primary_html = f"""
<div class="flex flex-col items-center justify-center gap-4 py-8 text-center">
  <div class="w-12 h-12 rounded-full bg-red-400/15 flex items-center justify-center">
    <svg class="w-6 h-6 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
    </svg>
  </div>
  <div>
    <div class="text-base font-semibold text-red-400">Illustration Rejected</div>
    {note_display}
  </div>
  <button
    hx-post="/projects/{project_id}/illustrations/chapter/{chapter_key}/regenerate"
    hx-target="#image-result-panel"
    hx-swap="innerHTML"
    hx-indicator="#regen-spinner"
    class="flex items-center gap-2 text-sm px-5 py-2 rounded-xl
           bg-amber-400 text-slate-950 font-semibold hover:bg-amber-300 transition-colors cursor-pointer">
    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
    </svg>
    Regenerate
    <svg id="regen-spinner" class="htmx-indicator w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
    </svg>
  </button>
</div>
"""
    combined = primary_html + "\n" + oob_card
    return HTMLResponse(content=combined)


# ── POST — regenerate ─────────────────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/illustrations/chapter/{chapter_key}/regenerate",
    response_class=HTMLResponse,
)
async def regenerate_image(
    project_id:  int,
    chapter_key: str,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapter = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id, Chapter.chapter_key == chapter_key)
        .first()
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    illustration = (
        db.query(Illustration)
        .filter(Illustration.chapter_id == chapter.id)
        .order_by(Illustration.id.desc())
        .first()
    )
    if not illustration or not illustration.prompt_data:
        return _error_panel("No scene analysis found. Run Analyze first.")

    # Load config
    try:
        cfg = _load_gemini_config(project_id)
    except ValueError as e:
        return _error_panel(str(e))

    # Model toggle: pro or flash
    form = await request.form()
    model_tier = form.get("model_tier", "pro")
    image_model = cfg["image_model_pro"] if model_tier == "pro" else cfg["image_model"]

    # Mark as regenerating
    illustration.status = IllustrationStatus.REGENERATING
    db.commit()

    # Build the generation prompt, incorporating rejection note if any
    prompt_data  = illustration.prompt_data
    image_prompt = prompt_data.get("image_prompt", "")
    negative     = prompt_data.get("negative_prompt", "")

    # Build style preamble for visual consistency
    default_style = (
        db.query(IllustrationStyle)
        .filter(IllustrationStyle.project_id == project_id, IllustrationStyle.is_default == True)
        .first()
    )
    style_preamble = _build_style_preamble(default_style)

    # Assemble: style preamble → scene prompt → rejection note → negative
    prompt_parts = []
    if style_preamble:
        prompt_parts.append(style_preamble)
    prompt_parts.append(f"SCENE: {image_prompt}")

    rejection_note = (illustration.rejection_note or "").strip()
    if rejection_note:
        prompt_parts.append(f"Previous attempt was rejected: {rejection_note}. Generate a different composition.")

    if negative:
        prompt_parts.append(f"Avoid: {negative}")

    full_prompt = " ".join(prompt_parts)

    # Gemini image generation
    try:
        from google import genai as _genai
        from google.genai import types as _types

        client   = _genai.Client(api_key=cfg["api_key"])
        response = client.models.generate_content(
            model    = image_model,
            contents = full_prompt,
            config   = _types.GenerateContentConfig(
                response_modalities = ["image", "text"],
                temperature         = 0.9,  # Slightly higher for variety
            ),
        )

        image_bytes = None
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                image_bytes = part.inline_data.data
                break

        if not image_bytes:
            illustration.status = IllustrationStatus.REJECTED
            db.commit()
            return _error_panel("Gemini returned no image. Try editing the prompt and regenerating.")

    except ImportError:
        illustration.status = IllustrationStatus.REJECTED
        db.commit()
        return _error_panel("google-genai library not installed. Run: pip install google-genai")
    except Exception as e:
        illustration.status = IllustrationStatus.REJECTED
        db.commit()
        return _error_panel(f"Image generation failed: {e}")

    # Save new PNG (overwrites previous for same chapter key)
    images_dir = _get_project_dir(project_id) / "output" / "illustrations" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    png_filename   = f"chapter_{chapter_key}.png"
    thumb_filename = f"chapter_{chapter_key}_thumb.jpg"
    png_path       = images_dir / png_filename
    thumb_path     = images_dir / thumb_filename

    png_path.write_bytes(image_bytes)

    try:
        from PIL import Image as _Image
        img = _Image.open(io.BytesIO(image_bytes))
        img.thumbnail((300, 300))
        img.save(str(thumb_path), "JPEG", quality=85)
        thumb_url = _thumbnail_url(project_id, thumb_filename)
    except ImportError:
        thumb_url = _thumbnail_url(project_id, png_filename)
    except Exception:
        thumb_url = _thumbnail_url(project_id, png_filename)

    # Update record
    illustration.raw_image_path = _thumbnail_url(project_id, png_filename)
    illustration.thumbnail_path = thumb_url
    illustration.status          = IllustrationStatus.GENERATED
    illustration.attempts        = (illustration.attempts or 0) + 1
    illustration.rejection_note  = ""   # Clear old rejection note
    illustration.reviewed_at     = None
    db.commit()
    db.refresh(illustration)

    # OOB gallery card update
    card_html = _render_card_html(
        request   = request,
        project   = project,
        chapter   = chapter,
        ch_status = "generated",
        thumb     = illustration.thumbnail_path,
        is_legacy = False,
    )
    oob_card = card_html.replace(
        f'id="chapter-card-{chapter_key}"',
        f'id="chapter-card-{chapter_key}" hx-swap-oob="outerHTML"',
        1,
    )

    image_result_template = templates.env.get_template("illustrations_image_result.html")
    primary_html = image_result_template.render(
        request      = request,
        project      = project,
        chapter      = chapter,
        illustration = illustration,
        model_used   = image_model,
    )

    combined = primary_html + "\n" + oob_card
    return HTMLResponse(content=combined)
