"""
CHAPTER MANAGER ROUTES — app/app/routes/chapter_manager.py

Manage chapters across the entire pipeline: add, delete, rename, reorder,
and set chapter types (appendix, character sheet, prologue, etc.).

Changes propagate to: framework.json, config.json, drafts, ingested, workbench.

Routes:
  GET  /projects/{project_id}/chapters                      — main page
  POST /projects/{project_id}/chapters/add                  — add a chapter
  POST /projects/{project_id}/chapters/{key}/rename         — rename
  POST /projects/{project_id}/chapters/{key}/delete         — delete + cleanup
  POST /projects/{project_id}/chapters/{key}/type           — change chapter type
  POST /projects/{project_id}/chapters/reorder              — reorder (drag-drop)
  POST /projects/{project_id}/chapters/{key}/generate       — AI-generate content
"""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_user
from app.models import Project

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


# ── Chapter types ────────────────────────────────────────────────────────────

CHAPTER_TYPES = {
    "chapter":         {"label": "Chapter",         "heading": "Chapter {n}",  "numbered": True},
    "prologue":        {"label": "Prologue",        "heading": "Prologue",     "numbered": False},
    "epilogue":        {"label": "Epilogue",        "heading": "Epilogue",     "numbered": False},
    "appendix":        {"label": "Appendix",        "heading": "Appendix",     "numbered": False},
    "character_sheet": {"label": "Character Sheet",  "heading": "",             "numbered": False},
    "interlude":       {"label": "Interlude",       "heading": "Interlude",    "numbered": False},
    "afterword":       {"label": "Afterword",       "heading": "Afterword",    "numbered": False},
    "custom":          {"label": "Custom",          "heading": "",             "numbered": False},
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _project_dir(project_id: int) -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent / "projects" / str(project_id)


def _load_json(path: Path) -> dict | list | None:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_framework(project_id: int) -> dict:
    path = _project_dir(project_id) / "output" / "genesis" / "framework.json"
    return _load_json(path) or {"structure_type": "", "chapters": []}


def _save_framework(project_id: int, framework: dict):
    path = _project_dir(project_id) / "output" / "genesis" / "framework.json"
    _save_json(path, framework)


def _load_config(project_id: int) -> dict:
    path = _project_dir(project_id) / "config.json"
    return _load_json(path) or {}


def _save_config(project_id: int, config: dict):
    path = _project_dir(project_id) / "config.json"
    _save_json(path, config)


def _chapter_key(num: int) -> str:
    """Zero-padded chapter key: 1 → '01', 12 → '12'."""
    return f"{num:02d}"


def _pipeline_dirs(project_id: int) -> list[tuple[str, str]]:
    """Return (dir_path, file_pattern) for each pipeline stage."""
    pd = _project_dir(project_id) / "output"
    return [
        (pd / "drafts",       "chapter_{key}_draft.json"),
        (pd / "ingested",     "chapter_{key}.json"),
        (pd / "workbench",    "chapter_{key}_working.json"),
        (pd / "consistency",  "chapter_{key}_consistency.json"),
        (pd / "dev_editing",  "chapter_{key}_dev.json"),
    ]


def _get_pipeline_status(project_id: int, chapter_key: str) -> dict:
    """Check which pipeline stages have data for a chapter."""
    status = {}
    for dir_path, pattern in _pipeline_dirs(project_id):
        fname = pattern.format(key=chapter_key)
        stage_name = dir_path.name
        status[stage_name] = (dir_path / fname).exists()
    return status


def _build_chapter_list(project_id: int) -> list[dict]:
    """Build a unified chapter list from framework + pipeline status."""
    framework = _load_framework(project_id)
    chapters = framework.get("chapters", [])
    result = []

    for ch in chapters:
        num = ch.get("number", 0)
        key = _chapter_key(num)
        ch_type = ch.get("type", "chapter")

        # Count words from draft if available
        draft_path = _project_dir(project_id) / "output" / "drafts" / f"chapter_{key}_draft.json"
        draft = _load_json(draft_path)
        word_count = draft.get("word_count", 0) if draft else 0

        result.append({
            "number":     num,
            "key":        key,
            "title":      ch.get("title", f"Chapter {num}"),
            "summary":    ch.get("summary", ""),
            "type":       ch_type,
            "type_label": CHAPTER_TYPES.get(ch_type, {}).get("label", ch_type.title()),
            "word_count": word_count,
            "pipeline":   _get_pipeline_status(project_id, key),
        })

    return result


def _sync_config(project_id: int, framework: dict):
    """Update config.json to match the current framework chapter list."""
    config = _load_config(project_id)
    chapters = framework.get("chapters", [])
    numbers = [ch["number"] for ch in chapters]

    if "book" not in config:
        config["book"] = {}
    config["book"]["total_chapters"] = len(chapters)
    config["book"]["reading_order"] = numbers
    _save_config(project_id, config)


def _reindex_chapters(chapters: list[dict]) -> list[dict]:
    """Re-number chapters sequentially: 1, 2, 3, ..."""
    for i, ch in enumerate(chapters):
        ch["number"] = i + 1
    return chapters


def _get_gemini_client(project_id: int):
    """Get Gemini client from project config."""
    from google import genai
    config = _load_config(project_id)
    api_key = config.get("gemini", {}).get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("No Gemini API key found in config.json or environment.")
    client = genai.Client(api_key=api_key)
    model_name = config.get("gemini", {}).get("models", {}).get("text", "gemini-2.5-pro")
    return client, model_name


# ── GET — main page ──────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/chapters", response_class=HTMLResponse)
async def chapter_manager_page(
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

    chapters = _build_chapter_list(project_id)
    config = _load_config(project_id)

    return templates.TemplateResponse(
        "chapter_manager.html",
        {
            "request":       request,
            "project":       project,
            "chapters":      chapters,
            "chapter_types": CHAPTER_TYPES,
            "total_words":   sum(ch["word_count"] for ch in chapters),
            "active_page":   "chapter_manager",
        },
    )


# ── POST — add a chapter ────────────────────────────────────────────────────

@router.post("/projects/{project_id}/chapters/add", response_class=HTMLResponse)
async def chapter_add(
    project_id:   int,
    request:      Request,
    db:           Session = Depends(get_db),
    title:        str = Form(""),
    chapter_type: str = Form("chapter"),
    position:     str = Form("end"),       # "end" or a number to insert after
    summary:      str = Form(""),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    chapters = framework.get("chapters", [])

    # Build the new chapter entry
    new_ch = {
        "number":     0,   # will be set by reindex
        "title":      title.strip() or f"New {CHAPTER_TYPES.get(chapter_type, {}).get('label', 'Chapter')}",
        "summary":    summary.strip(),
        "purpose":    "",
        "characters": [],
        "intensity":  5,
        "type":       chapter_type,
    }

    # Insert at the right position
    if position == "end" or not position.isdigit():
        chapters.append(new_ch)
    else:
        insert_after = int(position)
        # Find the index of the chapter with that number
        idx = next((i for i, ch in enumerate(chapters) if ch["number"] == insert_after), len(chapters))
        chapters.insert(idx + 1, new_ch)

    # Reindex and save
    framework["chapters"] = _reindex_chapters(chapters)
    _save_framework(project_id, framework)
    _sync_config(project_id, framework)

    # Create a blank draft file so it shows up in the pipeline
    new_num = new_ch["number"]
    new_key = _chapter_key(new_num)
    draft_path = _project_dir(project_id) / "output" / "drafts" / f"chapter_{new_key}_draft.json"
    if not draft_path.exists():
        draft_data = {
            "chapter_number": new_num,
            "title":          new_ch["title"],
            "generated_text": "",
            "word_count":     0,
            "model_used":     "",
            "generated_at":   datetime.now().isoformat(),
            "status":         "blank",
            "revision_count": 0,
            "revision_notes": [],
        }
        _save_json(draft_path, draft_data)

    # Return updated list
    chapters_list = _build_chapter_list(project_id)
    return templates.TemplateResponse(
        "fragments/chapter_manager_list.html",
        {
            "request":       request,
            "project":       project,
            "chapters":      chapters_list,
            "chapter_types": CHAPTER_TYPES,
            "total_words":   sum(ch["word_count"] for ch in chapters_list),
        },
    )


# ── POST — rename a chapter ─────────────────────────────────────────────────

@router.post("/projects/{project_id}/chapters/{key}/rename", response_class=HTMLResponse)
async def chapter_rename(
    project_id: int,
    key:        str,
    request:    Request,
    db:         Session = Depends(get_db),
    title:      str = Form(""),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    num = int(key)

    for ch in framework.get("chapters", []):
        if ch["number"] == num:
            ch["title"] = title.strip() or ch["title"]
            break

    _save_framework(project_id, framework)

    # Also update the draft title if it exists
    draft_path = _project_dir(project_id) / "output" / "drafts" / f"chapter_{_chapter_key(num)}_draft.json"
    draft = _load_json(draft_path)
    if draft:
        draft["title"] = title.strip() or draft.get("title", "")
        _save_json(draft_path, draft)

    chapters_list = _build_chapter_list(project_id)
    return templates.TemplateResponse(
        "fragments/chapter_manager_list.html",
        {
            "request":       request,
            "project":       project,
            "chapters":      chapters_list,
            "chapter_types": CHAPTER_TYPES,
            "total_words":   sum(ch["word_count"] for ch in chapters_list),
        },
    )


# ── POST — change chapter type ──────────────────────────────────────────────

@router.post("/projects/{project_id}/chapters/{key}/type", response_class=HTMLResponse)
async def chapter_type_change(
    project_id:   int,
    key:          str,
    request:      Request,
    db:           Session = Depends(get_db),
    chapter_type: str = Form("chapter"),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    num = int(key)

    for ch in framework.get("chapters", []):
        if ch["number"] == num:
            ch["type"] = chapter_type
            break

    _save_framework(project_id, framework)

    chapters_list = _build_chapter_list(project_id)
    return templates.TemplateResponse(
        "fragments/chapter_manager_list.html",
        {
            "request":       request,
            "project":       project,
            "chapters":      chapters_list,
            "chapter_types": CHAPTER_TYPES,
            "total_words":   sum(ch["word_count"] for ch in chapters_list),
        },
    )


# ── POST — delete a chapter ─────────────────────────────────────────────────

@router.post("/projects/{project_id}/chapters/{key}/delete", response_class=HTMLResponse)
async def chapter_delete(
    project_id: int,
    key:        str,
    request:    Request,
    db:         Session = Depends(get_db),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    num = int(key)
    old_key = _chapter_key(num)

    # Remove from framework
    chapters = [ch for ch in framework.get("chapters", []) if ch["number"] != num]

    # Delete all pipeline files for this chapter
    for dir_path, pattern in _pipeline_dirs(project_id):
        fpath = dir_path / pattern.format(key=old_key)
        if fpath.exists():
            fpath.unlink()

    # Reindex remaining chapters and rename pipeline files
    old_chapters = framework.get("chapters", [])
    framework["chapters"] = _reindex_chapters(chapters)
    _save_framework(project_id, framework)
    _sync_config(project_id, framework)

    # Rename pipeline files to match new numbering
    _rename_pipeline_files(project_id, framework)

    chapters_list = _build_chapter_list(project_id)
    return templates.TemplateResponse(
        "fragments/chapter_manager_list.html",
        {
            "request":       request,
            "project":       project,
            "chapters":      chapters_list,
            "chapter_types": CHAPTER_TYPES,
            "total_words":   sum(ch["word_count"] for ch in chapters_list),
        },
    )


def _rename_pipeline_files(project_id: int, framework: dict):
    """After reindex, rename pipeline files so chapter keys match new numbers.

    Strategy: move all existing files to temp names first, then rename to final.
    This avoids collisions when e.g. chapter 3 becomes chapter 2.
    """
    chapters = framework.get("chapters", [])
    pd = _project_dir(project_id) / "output"

    for dir_path, pattern in _pipeline_dirs(project_id):
        if not dir_path.exists():
            continue

        # Pass 1: find all existing chapter files and map to temp names
        temp_moves = []
        for ch in chapters:
            new_key = _chapter_key(ch["number"])
            fname = pattern.format(key=new_key)
            # We need to find the file that *was* this chapter before reindex
            # Since reindex is sequential, after delete + reindex the title is our anchor

        # Simpler approach: since we just did reindex, scan all chapter_XX files
        # in the directory and map them by content/title matching
        # Actually, the cleanest approach: we already deleted the removed chapter's
        # files, and the remaining files still have their OLD numbering.
        # Collect all existing files, sort by number, and rename sequentially.

        existing_files = sorted(dir_path.glob("chapter_*"))
        if not existing_files:
            continue

        # Move to temp names
        temp_map = []
        for i, fpath in enumerate(existing_files):
            temp_name = fpath.parent / f"_temp_{i}_{fpath.name}"
            fpath.rename(temp_name)
            temp_map.append(temp_name)

        # Rename to new sequential numbers
        for i, temp_path in enumerate(temp_map):
            if i >= len(chapters):
                # More files than chapters — leftover, remove
                temp_path.unlink()
                continue
            new_key = _chapter_key(chapters[i]["number"])
            new_name = pattern.format(key=new_key)
            final_path = dir_path / new_name
            temp_path.rename(final_path)

            # Update chapter_number/chapter_key inside JSON files
            try:
                data = json.loads(final_path.read_text(encoding="utf-8"))
                if "chapter_number" in data:
                    data["chapter_number"] = chapters[i]["number"]
                if "chapter_key" in data:
                    data["chapter_key"] = new_key
                if "title" in data and "title" in chapters[i]:
                    data["title"] = chapters[i]["title"]
                final_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass  # non-JSON or read error — skip internal update


# ── POST — reorder chapters ─────────────────────────────────────────────────

@router.post("/projects/{project_id}/chapters/reorder", response_class=HTMLResponse)
async def chapter_reorder(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    order:      str = Form(""),   # comma-separated old chapter numbers in new order
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    if not order.strip():
        return RedirectResponse(url=f"/projects/{project_id}/chapters", status_code=303)

    framework = _load_framework(project_id)
    chapters = framework.get("chapters", [])

    # Parse new order
    try:
        new_order = [int(x.strip()) for x in order.split(",") if x.strip()]
    except ValueError:
        return HTMLResponse("<div class='text-red-400'>Invalid order format.</div>")

    # Build lookup by old number
    by_num = {ch["number"]: ch for ch in chapters}

    # Reorder
    reordered = []
    for num in new_order:
        if num in by_num:
            reordered.append(by_num[num])

    # Add any chapters not in the order list at the end
    seen = set(new_order)
    for ch in chapters:
        if ch["number"] not in seen:
            reordered.append(ch)

    framework["chapters"] = _reindex_chapters(reordered)
    _save_framework(project_id, framework)
    _sync_config(project_id, framework)

    # Rename pipeline files to match new order
    _rename_pipeline_files(project_id, framework)

    chapters_list = _build_chapter_list(project_id)
    return templates.TemplateResponse(
        "fragments/chapter_manager_list.html",
        {
            "request":       request,
            "project":       project,
            "chapters":      chapters_list,
            "chapter_types": CHAPTER_TYPES,
            "total_words":   sum(ch["word_count"] for ch in chapters_list),
        },
    )


# ── POST — AI-generate content for a chapter ────────────────────────────────

@router.post("/projects/{project_id}/chapters/{key}/generate", response_class=HTMLResponse)
async def chapter_generate(
    project_id: int,
    key:        str,
    request:    Request,
    db:         Session = Depends(get_db),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    num = int(key)
    chapter = None
    for ch in framework.get("chapters", []):
        if ch["number"] == num:
            chapter = ch
            break

    if not chapter:
        return HTMLResponse("<div class='text-red-400 p-3'>Chapter not found in framework.</div>")

    # Load brainstorm for context
    brainstorm_path = _project_dir(project_id) / "output" / "genesis" / "idea_brainstorm.json"
    brainstorm = _load_json(brainstorm_path) or {}

    # Build prompt
    book_context = ""
    if brainstorm:
        book_context = f"""
Book concept: {brainstorm.get('refined_concept', brainstorm.get('concept', ''))}
Themes: {brainstorm.get('themes', '')}
Audience: {brainstorm.get('target_audience', '')}
"""

    ch_type = chapter.get("type", "chapter")
    type_label = CHAPTER_TYPES.get(ch_type, {}).get("label", "Chapter")

    prompt = f"""You are writing a {type_label.lower()} for a book.

{book_context}

{type_label} title: {chapter.get('title', '')}
Summary/purpose: {chapter.get('summary', '')}
{f"Purpose: {chapter.get('purpose', '')}" if chapter.get('purpose') else ""}

Write the full prose for this {type_label.lower()}. Match the tone and style described above.
Write approximately 2000-3000 words of engaging, polished prose.
Do not include the chapter heading/title in your output — just the body text."""

    try:
        client, model_name = _get_gemini_client(project_id)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
        )
        generated_text = response.text
    except Exception as exc:
        return HTMLResponse(f"<div class='text-red-400 p-3'>Generation failed: {exc}</div>")

    # Save as draft
    ch_key = _chapter_key(num)
    draft_data = {
        "chapter_number": num,
        "title":          chapter.get("title", f"Chapter {num}"),
        "generated_text": generated_text,
        "word_count":     len(generated_text.split()),
        "model_used":     model_name,
        "generated_at":   datetime.now().isoformat(),
        "status":         "draft",
        "revision_count": 0,
        "revision_notes": [],
    }
    draft_path = _project_dir(project_id) / "output" / "drafts" / f"chapter_{ch_key}_draft.json"
    _save_json(draft_path, draft_data)

    # Return updated list
    chapters_list = _build_chapter_list(project_id)
    return templates.TemplateResponse(
        "fragments/chapter_manager_list.html",
        {
            "request":       request,
            "project":       project,
            "chapters":      chapters_list,
            "chapter_types": CHAPTER_TYPES,
            "total_words":   sum(ch["word_count"] for ch in chapters_list),
        },
    )
