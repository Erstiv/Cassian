"""
WORKBENCH ROUTES
Interactive manuscript editor. NOT a pipeline agent — all logic lives here.
Working copies are stored as JSON files in output/workbench/.

Routes:
  GET  /projects/{project_id}/workbench
       — main two-panel page: chapter list + editor

  GET  /projects/{project_id}/workbench/chapter/{chapter_key}
       — load a chapter into the editor panel (HTMX partial)

  POST /projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}
       — edit, insert_before, insert_after, or delete a paragraph (HTMX partial)

  POST /projects/{project_id}/workbench/chapter/{chapter_key}/reset
       — discard working copy, revert to source (HTMX partial — reloads chapter panel)

  POST /projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}/ai
       — call Gemini Flash with an AI operation (expand/rephrase/rewrite), return suggestion panel

  POST /projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}/ai/accept
       — accept an AI suggestion: save to working copy, return paragraph block

  POST /projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}/ai/reject
       — reject an AI suggestion: return original paragraph block unchanged
"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_user
from app.models import Project


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _working_path(project_id: int, chapter_key: str) -> Path:
    return _project_dir(project_id) / "output" / "workbench" / f"chapter_{chapter_key}_working.json"


def _list_chapters(project_id: int) -> list[dict]:
    """
    Scan output/ingested/ for chapter_*.json files.
    Returns a list sorted by filename, each dict has:
      chapter_key, title, paragraph_count, has_working_copy
    """
    ingested_dir  = _project_dir(project_id) / "output" / "ingested"
    workbench_dir = _project_dir(project_id) / "output" / "workbench"

    if not ingested_dir.exists():
        return []

    chapters = []
    for f in sorted(ingested_dir.glob("chapter_*.json")):
        match = re.search(r"chapter_(.+?)\.json$", f.name)
        if not match:
            continue
        chapter_key = match.group(1)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

        has_working = (
            workbench_dir.exists()
            and (workbench_dir / f"chapter_{chapter_key}_working.json").exists()
        )
        chapters.append({
            "chapter_key":    chapter_key,
            "title":          data.get("title") or f"Chapter {chapter_key}",
            "paragraph_count": data.get("paragraph_count") or len(data.get("paragraphs", [])),
            "has_working_copy": has_working,
        })

    return chapters


def _normalise_paragraphs(raw_paragraphs: list) -> list[dict]:
    """
    Convert source paragraph list (list of {style, text} dicts OR plain strings)
    to working-copy format: [{"index": N, "text": "..."}]
    Skips empty paragraphs.
    """
    result = []
    idx = 0
    for p in raw_paragraphs:
        if isinstance(p, dict):
            text = p.get("text", "").strip()
        else:
            text = str(p).strip()
        if not text:
            continue
        result.append({"index": idx, "text": text})
        idx += 1
    return result


def _load_chapter_display(project_id: int, chapter_key: str) -> tuple[dict | None, str]:
    """
    Load the best available version of a chapter for display.
    Does NOT write to disk.

    Returns (data, source_label) where data is in working-copy format:
      {chapter_key, title, source, paragraphs: [{index, text}], last_modified}

    source_label: "working copy" | "edited" | "ingested" | ""
    Returns (None, "") if no source found.
    """
    pd = _project_dir(project_id)

    # 1. Working copy
    wp = _working_path(project_id, chapter_key)
    if wp.exists():
        try:
            return json.loads(wp.read_text(encoding="utf-8")), "working copy"
        except Exception:
            pass

    # 2. Edited chapter
    edited = pd / "output" / "editing" / f"chapter_{chapter_key}_edited.json"
    if edited.exists():
        try:
            raw = json.loads(edited.read_text(encoding="utf-8"))
            return {
                "chapter_key":   chapter_key,
                "title":         raw.get("title") or f"Chapter {chapter_key}",
                "source":        "edited",
                "paragraphs":    _normalise_paragraphs(raw.get("paragraphs", [])),
                "last_modified": datetime.utcnow().isoformat(),
            }, "edited"
        except Exception:
            pass

    # 3. Ingested chapter
    ingested = pd / "output" / "ingested" / f"chapter_{chapter_key}.json"
    if ingested.exists():
        try:
            raw = json.loads(ingested.read_text(encoding="utf-8"))
            return {
                "chapter_key":   chapter_key,
                "title":         raw.get("title") or f"Chapter {chapter_key}",
                "source":        "ingested",
                "paragraphs":    _normalise_paragraphs(raw.get("paragraphs", [])),
                "last_modified": datetime.utcnow().isoformat(),
            }, "ingested"
        except Exception:
            pass

    return None, ""


def _load_or_create_working(project_id: int, chapter_key: str) -> dict | None:
    """
    Load the working copy if it exists.
    If no working copy, create one from the best source (edited > ingested), save it, return it.
    Returns None if no source is found at all.
    """
    wp = _working_path(project_id, chapter_key)
    if wp.exists():
        try:
            return json.loads(wp.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Create from source
    data, source_label = _load_chapter_display(project_id, chapter_key)
    if data is None:
        return None

    # source_label won't be "working copy" here because we checked wp above and it was absent
    _save_working(project_id, chapter_key, data)
    return data


def _save_working(project_id: int, chapter_key: str, data: dict) -> None:
    """Write working copy JSON to disk, creating the directory if needed."""
    wp = _working_path(project_id, chapter_key)
    wp.parent.mkdir(parents=True, exist_ok=True)
    data["last_modified"] = datetime.utcnow().isoformat()
    wp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _reindex(paragraphs: list[dict]) -> list[dict]:
    """Ensure paragraphs are sequentially indexed from 0."""
    return [{"index": i, "text": p["text"]} for i, p in enumerate(paragraphs)]


# ── GET — main workbench page ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/workbench", response_class=HTMLResponse)
async def workbench_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    chapters = _list_chapters(project_id)

    return templates.TemplateResponse(
        "workbench.html",
        {
            "request":     request,
            "project":     project,
            "chapters":    chapters,
            "active_page": "workbench",
        },
    )


# ── GET — load a chapter (HTMX partial swap into #editor-panel) ───────────────

@router.get("/projects/{project_id}/workbench/chapter/{chapter_key}", response_class=HTMLResponse)
async def workbench_load_chapter(
    project_id:  int,
    chapter_key: str,
    request:     Request,
    db:          Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    data, source_label = _load_chapter_display(project_id, chapter_key)
    if data is None:
        return HTMLResponse(
            content='<div class="p-8 text-slate-500 text-sm">Chapter not found on disk.</div>'
        )

    has_working_copy = _working_path(project_id, chapter_key).exists()

    return templates.TemplateResponse(
        "workbench_chapter.html",
        {
            "request":         request,
            "project":         project,
            "chapter":         data,
            "chapter_key":     chapter_key,
            "source_label":    source_label,
            "has_working_copy": has_working_copy,
        },
    )


# ── POST — paragraph operations (HTMX partial) ────────────────────────────────

@router.post(
    "/projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}",
    response_class=HTMLResponse,
)
async def workbench_paragraph_op(
    project_id:  int,
    chapter_key: str,
    index:       int,
    request:     Request,
    action:      str = Form(...),
    text:        str = Form(""),
    db:          Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    working = _load_or_create_working(project_id, chapter_key)
    if working is None:
        raise HTTPException(status_code=404, detail="Chapter not found")

    paragraphs = working.get("paragraphs", [])

    # ── edit: replace a single paragraph's text ──────────────────────────────
    if action == "edit":
        new_text = text.strip()
        updated_para = None
        for p in paragraphs:
            if p["index"] == index:
                p["text"] = new_text
                updated_para = p
                break

        if updated_para is None:
            raise HTTPException(status_code=404, detail="Paragraph index not found")

        _save_working(project_id, chapter_key, working)

        # Return the single updated paragraph block (outerHTML swap)
        return templates.TemplateResponse(
            "workbench_para.html",
            {
                "request":     request,
                "project":     project,
                "chapter_key": chapter_key,
                "para":        updated_para,
            },
        )

    # ── insert_before / insert_after: add a new paragraph ────────────────────
    elif action in ("insert_before", "insert_after"):
        new_text = text.strip()
        if new_text:
            insert_pos = index if action == "insert_before" else index + 1
            new_paragraphs = []
            inserted = False
            for p in paragraphs:
                if p["index"] == insert_pos and not inserted:
                    new_paragraphs.append({"index": -1, "text": new_text})
                    inserted = True
                new_paragraphs.append({"index": -1, "text": p["text"]})
            if not inserted:
                # insert_pos is beyond the end
                new_paragraphs.append({"index": -1, "text": new_text})
            working["paragraphs"] = _reindex(new_paragraphs)
            _save_working(project_id, chapter_key, working)

        # Return the full paragraph list (innerHTML swap into #paragraphs-list)
        return templates.TemplateResponse(
            "workbench_paralist.html",
            {
                "request":     request,
                "project":     project,
                "chapter_key": chapter_key,
                "paragraphs":  working["paragraphs"],
            },
        )

    # ── delete: remove a paragraph ────────────────────────────────────────────
    elif action == "delete":
        new_paragraphs = [p for p in paragraphs if p["index"] != index]
        working["paragraphs"] = _reindex(new_paragraphs)
        _save_working(project_id, chapter_key, working)

        # Return the full paragraph list (innerHTML swap into #paragraphs-list)
        return templates.TemplateResponse(
            "workbench_paralist.html",
            {
                "request":     request,
                "project":     project,
                "chapter_key": chapter_key,
                "paragraphs":  working["paragraphs"],
            },
        )

    raise HTTPException(status_code=400, detail=f"Unknown action: {action!r}")


# ── POST — reset working copy (HTMX partial — reloads chapter panel) ──────────

@router.post("/projects/{project_id}/workbench/chapter/{chapter_key}/reset", response_class=HTMLResponse)
async def workbench_reset(
    project_id:  int,
    chapter_key: str,
    request:     Request,
    db:          Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    wp = _working_path(project_id, chapter_key)
    if wp.exists():
        wp.unlink()

    # Reload from source (no working copy now)
    data, source_label = _load_chapter_display(project_id, chapter_key)
    if data is None:
        return HTMLResponse(
            content='<div class="p-8 text-slate-500 text-sm">Chapter not found on disk.</div>'
        )

    return templates.TemplateResponse(
        "workbench_chapter.html",
        {
            "request":         request,
            "project":         project,
            "chapter":         data,
            "chapter_key":     chapter_key,
            "source_label":    source_label,
            "has_working_copy": False,
        },
    )


# ── AI Operations ─────────────────────────────────────────────────────────────
# These call Gemini 2.5 Flash directly from the route handler.
# No subprocess, no agent script — just a direct API call.

_VALID_OPERATIONS = frozenset(["expand", "rephrase", "rewrite", "generate_after"])


def _load_story_context(project_id: int) -> str:
    """Load brainstorm + framework context so AI knows the story."""
    context_parts = []

    brainstorm_path = _project_dir(project_id) / "output" / "genesis" / "idea_brainstorm.json"
    if brainstorm_path.exists():
        try:
            bs = json.loads(brainstorm_path.read_text(encoding="utf-8"))
            sections = bs.get("sections", {})
            if sections.get("PREMISE"):
                context_parts.append(f"Premise: {sections['PREMISE']}")
            if sections.get("TONE_AND_STYLE"):
                context_parts.append(f"Tone: {sections['TONE_AND_STYLE']}")
            if sections.get("SETTING"):
                context_parts.append(f"Setting: {sections['SETTING']}")
        except Exception:
            pass

    framework_path = _project_dir(project_id) / "output" / "genesis" / "framework.json"
    if framework_path.exists():
        try:
            fw = json.loads(framework_path.read_text(encoding="utf-8"))
            if fw.get("pacing_notes"):
                context_parts.append(f"Pacing: {fw['pacing_notes']}")
        except Exception:
            pass

    if context_parts:
        return "=== STORY CONTEXT ===\n" + "\n".join(context_parts) + "\n\n"
    return ""


def _get_surrounding_context(paragraphs: list[dict], index: int, window: int = 2) -> str:
    """Get the paragraphs before and after for continuity context."""
    parts = []
    for p in paragraphs:
        if abs(p["index"] - index) <= window and p["index"] != index:
            label = "before" if p["index"] < index else "after"
            parts.append(f"[Paragraph {label}]: {p['text'][:300]}")
    if parts:
        return "=== SURROUNDING PARAGRAPHS ===\n" + "\n".join(parts) + "\n\n"
    return ""


def _build_ai_prompt(
    operation: str,
    text: str,
    story_context: str,
    surrounding: str,
    direction: str = "",
    chapter_title: str = "",
) -> str:
    """Build the full AI prompt with story context for any operation."""

    chapter_line = f"This is from chapter: \"{chapter_title}\"\n\n" if chapter_title else ""

    if operation == "expand":
        return (
            f"You are a book editor's assistant working on a novel.\n\n"
            f"{story_context}{chapter_line}{surrounding}"
            f"Expand the following paragraph to be more detailed and vivid while maintaining "
            f"the same voice, tone, and point of view. Add sensory details, deeper character insight, "
            f"or world-building as appropriate. Make sure it fits naturally with the surrounding text.\n"
            f"Return ONLY the expanded paragraph text, no commentary.\n\n"
            f"Paragraph:\n{text}"
        )

    elif operation == "rephrase":
        return (
            f"You are a book editor's assistant working on a novel.\n\n"
            f"{story_context}{chapter_line}"
            f"Rephrase the following paragraph using different wording while keeping the exact same "
            f"meaning, length, and tone. The result should read naturally and maintain the author's voice.\n"
            f"Return ONLY the rephrased paragraph text, no commentary.\n\n"
            f"Paragraph:\n{text}"
        )

    elif operation == "rewrite":
        direction_line = ""
        if direction:
            direction_line = f"\nThe author wants this specific direction for the rewrite: \"{direction}\"\n"
        return (
            f"You are a book editor's assistant working on a novel.\n\n"
            f"{story_context}{chapter_line}{surrounding}"
            f"Rewrite the following paragraph with a fresh approach. You may change sentence structure, "
            f"pacing, and emphasis, but preserve the core narrative content and character voice. "
            f"The rewrite can be shorter or longer than the original.{direction_line}\n"
            f"Return ONLY the rewritten paragraph text, no commentary.\n\n"
            f"Paragraph:\n{text}"
        )

    elif operation == "generate_after":
        return (
            f"You are a book editor's assistant working on a novel.\n\n"
            f"{story_context}{chapter_line}{surrounding}"
            f"Write a NEW paragraph to be inserted after the following paragraph. "
            f"It should flow naturally from what comes before and transition smoothly to what follows.\n"
            f"\nThe author's direction for this new paragraph: \"{direction}\"\n\n"
            f"Match the existing style, voice, and tone exactly. "
            f"Return ONLY the new paragraph text, no commentary.\n\n"
            f"Insert after this paragraph:\n{text}"
        )

    return text  # fallback


def _load_gemini_config(project_id: int) -> tuple[str | None, str]:
    """
    Read the Gemini API key and fast model name from the project's config.json.
    Returns (api_key, model_name). api_key is None if config is missing or key absent.
    """
    config_path = _project_dir(project_id) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}

    gemini  = config.get("gemini", {})
    api_key = gemini.get("api_key") or os.environ.get("GEMINI_API_KEY")
    model   = gemini.get("models", {}).get("fast", "gemini-2.5-flash")
    return api_key or None, model


def _call_gemini(api_key: str, model: str, prompt: str) -> str:
    """
    Call Gemini synchronously and return the response text.
    Raises RuntimeError on failure.
    """
    from google import genai
    from google.genai import types

    client   = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.6,       # slightly creative for workbench AI ops
            max_output_tokens=4096,
        ),
    )
    return response.text.strip()


def _error_panel(index: int, message: str) -> HTMLResponse:
    """Return a styled error panel as an HTMX fragment."""
    html = (
        f'<div id="para-{index}" class="bg-red-900/30 border border-red-700/60 rounded-xl p-5 mb-3">'
        f'<div class="text-sm font-semibold text-red-400 mb-1">⚠ AI Error</div>'
        f'<div class="text-sm text-red-300">{message}</div>'
        f'<div class="text-xs text-slate-500 mt-3">Check your Gemini API key in the project\'s config.json.</div>'
        f'</div>'
    )
    return HTMLResponse(content=html)


# ── POST — call Gemini AI operation ──────────────────────────────────────────

@router.post(
    "/projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}/ai",
    response_class=HTMLResponse,
)
async def workbench_ai_op(
    project_id:  int,
    chapter_key: str,
    index:       int,
    request:     Request,
    operation:   str = Form(...),
    direction:   str = Form(""),
    db:          Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    if operation not in _VALID_OPERATIONS:
        raise HTTPException(status_code=400, detail=f"Unknown operation: {operation!r}")

    # Load (or create) the working copy so we have the current paragraph text
    working = _load_or_create_working(project_id, chapter_key)
    if working is None:
        return _error_panel(index, "Chapter not found on disk.")

    paragraphs = working.get("paragraphs", [])

    # Find the paragraph at the requested index
    para = next((p for p in paragraphs if p["index"] == index), None)
    if para is None:
        return _error_panel(index, f"Paragraph {index} not found in working copy.")

    original_text = para["text"]

    # Load Gemini config
    api_key, model = _load_gemini_config(project_id)
    if not api_key:
        return _error_panel(
            index,
            "No Gemini API key found. Add your key to projects/{id}/config.json "
            "under gemini → api_key, or set GEMINI_API_KEY environment variable."
        )

    # Load story context and surrounding paragraphs for richer AI responses
    story_context = _load_story_context(project_id)
    surrounding   = _get_surrounding_context(paragraphs, index)
    chapter_title = working.get("title", "")

    # Build and send the prompt
    prompt = _build_ai_prompt(
        operation=operation,
        text=original_text,
        story_context=story_context,
        surrounding=surrounding,
        direction=direction.strip(),
        chapter_title=chapter_title,
    )
    try:
        suggested_text = await asyncio.to_thread(_call_gemini, api_key, model, prompt)
    except Exception as exc:
        return _error_panel(index, f"Gemini call failed: {exc}")

    # Return the suggestion panel (user will Accept or Reject)
    return templates.TemplateResponse(
        "workbench_ai_suggestion.html",
        {
            "request":        request,
            "project":        project,
            "chapter_key":    chapter_key,
            "index":          index,
            "operation":      operation,
            "original_text":  original_text,
            "suggested_text": suggested_text,
        },
    )


# ── POST — accept an AI suggestion ───────────────────────────────────────────

@router.post(
    "/projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}/ai/accept",
    response_class=HTMLResponse,
)
async def workbench_ai_accept(
    project_id:  int,
    chapter_key: str,
    index:       int,
    request:     Request,
    text:        str = Form(...),
    db:          Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    working = _load_or_create_working(project_id, chapter_key)
    if working is None:
        raise HTTPException(status_code=404, detail="Chapter not found")

    # Replace the paragraph text with the accepted AI suggestion
    new_text = text.strip()
    updated_para = None
    for p in working.get("paragraphs", []):
        if p["index"] == index:
            p["text"] = new_text
            updated_para = p
            break

    if updated_para is None:
        raise HTTPException(status_code=404, detail="Paragraph not found")

    _save_working(project_id, chapter_key, working)

    # Return the standard paragraph block (same as after a manual edit)
    return templates.TemplateResponse(
        "workbench_para.html",
        {
            "request":     request,
            "project":     project,
            "chapter_key": chapter_key,
            "para":        updated_para,
        },
    )


# ── POST — reject an AI suggestion ───────────────────────────────────────────

@router.post(
    "/projects/{project_id}/workbench/chapter/{chapter_key}/paragraph/{index}/ai/reject",
    response_class=HTMLResponse,
)
async def workbench_ai_reject(
    project_id:  int,
    chapter_key: str,
    index:       int,
    request:     Request,
    db:          Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    # Load current working copy (no changes — user rejected the suggestion)
    working = _load_or_create_working(project_id, chapter_key)
    if working is None:
        raise HTTPException(status_code=404, detail="Chapter not found")

    para = next((p for p in working.get("paragraphs", []) if p["index"] == index), None)
    if para is None:
        raise HTTPException(status_code=404, detail="Paragraph not found")

    # Return the original paragraph block — no disk write, nothing changed
    return templates.TemplateResponse(
        "workbench_para.html",
        {
            "request":     request,
            "project":     project,
            "chapter_key": chapter_key,
            "para":        para,
        },
    )
