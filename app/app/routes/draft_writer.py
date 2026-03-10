"""
DRAFT WRITER ROUTES — app/app/routes/draft_writer.py

Phase 1: Genesis — AI-powered chapter prose generation from Framework outline.

Routes:
  GET  /projects/{project_id}/draft-writer                         — main page
  POST /projects/{project_id}/draft-writer/generate/{chapter_num}  — generate one chapter
  POST /projects/{project_id}/draft-writer/revise/{chapter_num}    — revise with feedback
  POST /projects/{project_id}/draft-writer/approve/{chapter_num}   — approve → ingested JSON
  POST /projects/{project_id}/draft-writer/approve-all             — bulk approve all drafts
  POST /projects/{project_id}/draft-writer/generate-all            — bulk generate remaining
"""

import asyncio
import json
import os
import re
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from google import genai

from app.database import get_db
from app.models import Project


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_genesis_dir(project_id: int) -> Path:
    d = PROJECTS_DIR / str(project_id) / "output" / "genesis"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_drafts_dir(project_id: int) -> Path:
    d = PROJECTS_DIR / str(project_id) / "output" / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_ingested_dir(project_id: int) -> Path:
    d = PROJECTS_DIR / str(project_id) / "output" / "ingested"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_framework(project_id: int) -> dict | None:
    path = _get_genesis_dir(project_id) / "framework.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_brainstorm(project_id: int) -> dict | None:
    path = _get_genesis_dir(project_id) / "idea_brainstorm.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_config(project_id: int) -> dict:
    config_path = PROJECTS_DIR / str(project_id) / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _get_gemini_client(project_id: int):
    """Returns (client, model_name) tuple."""
    config  = _load_config(project_id)
    api_key = config.get("gemini", {}).get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("No Gemini API key found in config.json or environment.")
    client = genai.Client(api_key=api_key)
    model_name = config.get("gemini", {}).get("models", {}).get("text", "gemini-2.5-pro")
    return client, model_name


def _load_draft(project_id: int, chapter_num: int) -> dict | None:
    path = _get_drafts_dir(project_id) / f"chapter_{chapter_num:02d}_draft.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_draft(project_id: int, chapter_num: int, data: dict) -> None:
    path = _get_drafts_dir(project_id) / f"chapter_{chapter_num:02d}_draft.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _count_sentences(text: str) -> int:
    """Count sentences by splitting on sentence-ending punctuation."""
    parts = re.split(r'[.!?]+', text)
    return len([p for p in parts if p.strip()])


def _draft_to_ingested(draft: dict) -> dict:
    """Convert a draft JSON to the standard ingested chapter JSON format."""
    text = draft.get("generated_text", "")
    paragraphs_raw = [p.strip() for p in text.split("\n\n") if p.strip()]
    paragraphs = [{"style": "Normal", "text": p} for p in paragraphs_raw]
    full_text = "\n\n".join(paragraphs_raw)

    return {
        "chapter_number": draft["chapter_number"],
        "chapter_id": f"{draft['chapter_number']:02d}",
        "title": draft.get("title", f"Chapter {draft['chapter_number']}"),
        "source_file": "draft_writer_generated",

        "word_count": len(full_text.split()),
        "sentence_count": _count_sentences(full_text),
        "paragraph_count": len(paragraphs),

        "paragraphs": paragraphs,
        "full_text": full_text,

        "images": [],

        "pipeline_status": {
            "ingested": True,
            "consistency_checked": False,
            "consistency_issues": [],
            "editing_complete": False,
            "editing_creativity_level": None,
            "illustration_prompt_generated": False,
            "formatted": False,
            "qc_passed": False,
            "qc_issues": [],
        },

        "metadata": {
            "ingested_at": datetime.now().isoformat(),
            "pipeline_version": "1.1",
            "source_format": "draft_writer",
        },
    }


def _build_generate_prompt(brainstorm: dict | None, framework: dict, chapter: dict) -> str:
    """Build the generation prompt for a single chapter."""
    chapters = framework.get("chapters", [])
    ch_num = chapter.get("number", 1)

    # Extract brainstorm context
    sections = {}
    if brainstorm:
        sections = brainstorm.get("sections", {})

    title = ""
    premise = ""
    setting = ""
    tone = ""
    themes = ""
    characters_desc = ""
    genre = "fiction"

    if sections:
        title = sections.get("TITLE_OPTIONS", "").split("\n")[0] if sections.get("TITLE_OPTIONS") else ""
        premise = sections.get("PREMISE", "")
        setting = sections.get("SETTING", "")
        tone = sections.get("TONE_AND_STYLE", "")
        themes = sections.get("THEMES", "")
        characters_desc = sections.get("MAIN_CHARACTERS", "")

    if brainstorm and brainstorm.get("inputs", {}).get("genre"):
        genre = brainstorm["inputs"]["genre"]

    # Continuity context
    prev_title = ""
    prev_summary = ""
    next_title = ""
    next_summary = ""

    for ch in chapters:
        if ch.get("number") == ch_num - 1:
            prev_title = ch.get("title", "")
            prev_summary = ch.get("summary", "")
        elif ch.get("number") == ch_num + 1:
            next_title = ch.get("title", "")
            next_summary = ch.get("summary", "")

    continuity = ""
    if prev_title:
        continuity += f'Previous chapter: "{prev_title}" — {prev_summary}\n'
    if next_title:
        continuity += f'Next chapter: "{next_title}" — {next_summary}\n'
    if not continuity:
        continuity = "(This is the first/only chapter with no adjacent chapters yet.)"

    # Characters for this chapter
    ch_characters = ", ".join(chapter.get("characters", [])) or "Not specified"

    prompt = f"""You are a professional fiction author. You are writing Chapter {ch_num} of a {genre} novel.

=== BOOK CONCEPT ===
Title: {title or '(working title)'}
Premise: {premise or '(not specified)'}
Setting: {setting or '(not specified)'}
Tone & Style: {tone or '(not specified)'}
Themes: {themes or '(not specified)'}

=== CHARACTERS ===
{characters_desc or '(no character descriptions available)'}

=== CHAPTER OUTLINE ===
Chapter {ch_num}: "{chapter.get('title', '')}"
Summary: {chapter.get('summary', '')}
Purpose: {chapter.get('purpose', '')}
Characters in this chapter: {ch_characters}
Intensity: {chapter.get('intensity', 'medium')}

=== CONTINUITY CONTEXT ===
{continuity}

=== INSTRUCTIONS ===
Write the full prose for this chapter.
- Target length: 2,500–4,000 words
- Match the tone described above
- Include dialogue where natural
- End with a hook or transition to the next chapter
- Do NOT include the chapter title/number as a heading — just write the prose
- Write in third person unless the brainstorm specifies otherwise"""

    return prompt


def _build_revise_prompt(brainstorm: dict | None, framework: dict, chapter: dict, existing_draft: str, feedback: str) -> str:
    """Build the revision prompt."""
    base_prompt = _build_generate_prompt(brainstorm, framework, chapter)

    base_prompt += f"""

=== REVISION REQUEST ===
The reader has provided this feedback on your previous draft:
"{feedback}"

Here is your previous draft:
{existing_draft}

Please rewrite the chapter incorporating this feedback while maintaining continuity."""

    return base_prompt


def _compute_stats(project_id: int, chapters: list[dict]) -> dict:
    """Compute current draft stats for the stats bar."""
    stats = {"total": len(chapters), "generated": 0, "approved": 0, "total_words": 0}
    for ch in chapters:
        num = ch.get("number", 0)
        draft = _load_draft(project_id, num)
        if draft:
            status = draft.get("status", "draft")
            wc = draft.get("word_count", 0)
            if status == "approved":
                stats["approved"] += 1
                stats["generated"] += 1
                stats["total_words"] += wc
            elif status == "draft":
                stats["generated"] += 1
                stats["total_words"] += wc
    return stats


def _render_stats_oob(request, project_id: int, framework: dict) -> str:
    """Render the stats bar as an OOB swap HTML fragment."""
    chapters = framework.get("chapters", [])
    stats = _compute_stats(project_id, chapters)
    return templates.TemplateResponse(
        "fragments/draft_stats_bar.html",
        {"request": request, "stats": stats, "oob": True},
    ).body.decode("utf-8")


def _get_chapter_statuses(project_id: int, chapters: list[dict]) -> list[dict]:
    """Enrich framework chapters with draft status info."""
    enriched = []
    for ch in chapters:
        num = ch.get("number", 0)
        draft = _load_draft(project_id, num)
        info = dict(ch)
        if draft:
            info["draft_status"] = draft.get("status", "draft")
            info["draft_word_count"] = draft.get("word_count", 0)
            info["draft_generated_at"] = draft.get("generated_at", "")
            info["draft_revision_count"] = draft.get("revision_count", 0)
        else:
            info["draft_status"] = "not_started"
            info["draft_word_count"] = 0
            info["draft_generated_at"] = ""
            info["draft_revision_count"] = 0
        enriched.append(info)
    return enriched


# ── GET — main draft writer page ──────────────────────────────────────────────

@router.get("/projects/{project_id}/draft-writer", response_class=HTMLResponse)
async def draft_writer_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework  = _load_framework(project_id)
    brainstorm = _load_brainstorm(project_id)

    chapters = []
    stats = {"total": 0, "generated": 0, "approved": 0, "total_words": 0}

    if framework:
        chapters = _get_chapter_statuses(project_id, framework.get("chapters", []))
        stats["total"] = len(chapters)
        for ch in chapters:
            if ch["draft_status"] == "approved":
                stats["approved"] += 1
                stats["generated"] += 1
                stats["total_words"] += ch["draft_word_count"]
            elif ch["draft_status"] == "draft":
                stats["generated"] += 1
                stats["total_words"] += ch["draft_word_count"]

    return templates.TemplateResponse(
        "draft_writer.html",
        {
            "request":      request,
            "project":      project,
            "active_page":  "draft_writer",
            "framework":    framework,
            "brainstorm":   brainstorm,
            "chapters":     chapters,
            "stats":        stats,
        }
    )


# ── POST — generate one chapter ──────────────────────────────────────────────

@router.post("/projects/{project_id}/draft-writer/generate/{chapter_num}", response_class=HTMLResponse)
async def draft_writer_generate(
    project_id:  int,
    chapter_num: int,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        return HTMLResponse(content="""
<div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
  <strong>Error:</strong> No framework found. Generate a Framework first.
</div>""")

    # Find the chapter in the framework
    chapter = None
    for ch in framework.get("chapters", []):
        if ch.get("number") == chapter_num:
            chapter = ch
            break

    if not chapter:
        return HTMLResponse(content=f"""
<div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
  <strong>Error:</strong> Chapter {chapter_num} not found in framework.
</div>""")

    brainstorm = _load_brainstorm(project_id)

    try:
        client, model_name = _get_gemini_client(project_id)
        prompt = _build_generate_prompt(brainstorm, framework, chapter)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
        )
        generated_text = response.text

        draft_data = {
            "chapter_number": chapter_num,
            "title": chapter.get("title", f"Chapter {chapter_num}"),
            "generated_text": generated_text,
            "word_count": len(generated_text.split()),
            "model_used": model_name,
            "generated_at": datetime.now().isoformat(),
            "status": "draft",
            "revision_count": 0,
            "revision_notes": [],
        }
        _save_draft(project_id, chapter_num, draft_data)

    except Exception as exc:
        return HTMLResponse(content=f"""
<div id="chapter-card-{chapter_num}" class="bg-slate-900 border border-slate-800 rounded-xl p-6">
  <div class="flex items-center justify-between mb-3">
    <h3 class="text-white font-semibold">Ch. {chapter_num}: {chapter.get('title', '')}</h3>
    <span class="text-xs px-2 py-1 rounded-full bg-red-900/50 text-red-400">Error</span>
  </div>
  <div class="bg-red-900/30 border border-red-700/50 rounded-lg p-4 text-red-300 text-sm mb-3">
    <strong>Generation failed:</strong> {exc}
  </div>
  <button hx-post="/projects/{project_id}/draft-writer/generate/{chapter_num}"
          hx-target="#chapter-card-{chapter_num}"
          hx-swap="outerHTML"
          hx-indicator="#spinner-{chapter_num}"
          class="bg-amber-500 hover:bg-amber-400 text-slate-950 font-semibold px-4 py-2 rounded-lg text-sm transition-colors">
    ✨ Retry Generate
  </button>
  <div id="spinner-{chapter_num}" class="htmx-indicator inline-block ml-2">
    <svg class="animate-spin h-5 w-5 text-amber-400 inline" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
    </svg>
  </div>
</div>""")

    # Return updated chapter card with draft info + OOB stats update
    enriched = dict(chapter)
    enriched["draft_status"] = "draft"
    enriched["draft_word_count"] = draft_data["word_count"]
    enriched["draft_generated_at"] = draft_data["generated_at"]
    enriched["draft_revision_count"] = 0

    card_html = templates.TemplateResponse(
        "fragments/draft_chapter_card.html",
        {"request": request, "project": project, "ch": enriched},
    ).body.decode("utf-8")

    stats_html = _render_stats_oob(request, project_id, framework)

    return HTMLResponse(content=card_html + stats_html)


# ── POST — revise a chapter ──────────────────────────────────────────────────

@router.post("/projects/{project_id}/draft-writer/revise/{chapter_num}", response_class=HTMLResponse)
async def draft_writer_revise(
    project_id:  int,
    chapter_num: int,
    request:     Request,
    db:          Session = Depends(get_db),
    feedback:    str = Form(""),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        return HTMLResponse(content="""
<div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
  No framework found.
</div>""")

    existing_draft = _load_draft(project_id, chapter_num)
    if not existing_draft:
        return HTMLResponse(content=f"""
<div class="bg-amber-900/30 border border-amber-700/50 rounded-xl p-5 text-amber-300 text-sm">
  No draft found for chapter {chapter_num}. Generate it first.
</div>""")

    chapter = None
    for ch in framework.get("chapters", []):
        if ch.get("number") == chapter_num:
            chapter = ch
            break
    if not chapter:
        return HTMLResponse(content="<div class='text-red-400'>Chapter not found in framework.</div>")

    brainstorm = _load_brainstorm(project_id)

    try:
        client, model_name = _get_gemini_client(project_id)
        prompt = _build_revise_prompt(
            brainstorm, framework, chapter,
            existing_draft.get("generated_text", ""),
            feedback,
        )
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
        )
        revised_text = response.text

        revision_notes = existing_draft.get("revision_notes", [])
        revision_notes.append(feedback)

        draft_data = {
            "chapter_number": chapter_num,
            "title": chapter.get("title", f"Chapter {chapter_num}"),
            "generated_text": revised_text,
            "word_count": len(revised_text.split()),
            "model_used": model_name,
            "generated_at": datetime.now().isoformat(),
            "status": "draft",
            "revision_count": existing_draft.get("revision_count", 0) + 1,
            "revision_notes": revision_notes,
        }
        _save_draft(project_id, chapter_num, draft_data)

    except Exception as exc:
        return HTMLResponse(content=f"""
<div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
  <strong>Revision failed:</strong> {exc}
</div>""")

    return templates.TemplateResponse(
        "fragments/draft_chapter_preview.html",
        {
            "request": request,
            "project": project,
            "draft": draft_data,
            "ch": chapter,
        }
    )


# ── POST — approve a chapter ─────────────────────────────────────────────────

@router.post("/projects/{project_id}/draft-writer/approve/{chapter_num}", response_class=HTMLResponse)
async def draft_writer_approve(
    project_id:  int,
    chapter_num: int,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    draft = _load_draft(project_id, chapter_num)
    if not draft:
        return HTMLResponse(content=f"""
<div class="bg-amber-900/30 border border-amber-700/50 rounded-xl p-5 text-amber-300 text-sm">
  No draft found for chapter {chapter_num}.
</div>""")

    # Convert to ingested format
    ingested = _draft_to_ingested(draft)
    ingested_path = _get_ingested_dir(project_id) / f"chapter_{chapter_num:02d}.json"
    ingested_path.write_text(json.dumps(ingested, indent=2, ensure_ascii=False), encoding="utf-8")

    # Update draft status
    draft["status"] = "approved"
    _save_draft(project_id, chapter_num, draft)

    # Return updated chapter card
    framework = _load_framework(project_id)
    chapter = {}
    if framework:
        for ch in framework.get("chapters", []):
            if ch.get("number") == chapter_num:
                chapter = ch
                break

    enriched = dict(chapter)
    enriched["draft_status"] = "approved"
    enriched["draft_word_count"] = draft.get("word_count", 0)
    enriched["draft_generated_at"] = draft.get("generated_at", "")
    enriched["draft_revision_count"] = draft.get("revision_count", 0)

    card_html = templates.TemplateResponse(
        "fragments/draft_chapter_card.html",
        {"request": request, "project": project, "ch": enriched},
    ).body.decode("utf-8")

    stats_html = _render_stats_oob(request, project_id, framework)

    return HTMLResponse(content=card_html + stats_html)


# ── POST — bulk approve all drafts ───────────────────────────────────────────

@router.post("/projects/{project_id}/draft-writer/approve-all", response_class=HTMLResponse)
async def draft_writer_approve_all(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        return HTMLResponse(content="<div class='text-red-400'>No framework found.</div>")

    approved_count = 0
    for ch in framework.get("chapters", []):
        num = ch.get("number", 0)
        draft = _load_draft(project_id, num)
        if draft and draft.get("status") == "draft":
            ingested = _draft_to_ingested(draft)
            ingested_path = _get_ingested_dir(project_id) / f"chapter_{num:02d}.json"
            ingested_path.write_text(json.dumps(ingested, indent=2, ensure_ascii=False), encoding="utf-8")
            draft["status"] = "approved"
            _save_draft(project_id, num, draft)
            approved_count += 1

    # Redirect back to the page so stats refresh with a clean full-page load
    return RedirectResponse(
        url=f"/projects/{project_id}/draft-writer",
        status_code=303,
    )


# ── POST — bulk generate all remaining ───────────────────────────────────────

@router.post("/projects/{project_id}/draft-writer/generate-all", response_class=HTMLResponse)
async def draft_writer_generate_all(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        return HTMLResponse(content="<div class='text-red-400'>No framework found.</div>")

    brainstorm = _load_brainstorm(project_id)
    generated_count = 0
    errors = []

    try:
        client, model_name = _get_gemini_client(project_id)
    except Exception as exc:
        return HTMLResponse(content=f"""
<div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
  <strong>Error:</strong> {exc}
</div>""")

    for chapter in framework.get("chapters", []):
        num = chapter.get("number", 0)
        existing = _load_draft(project_id, num)
        if existing:
            continue  # Skip already generated

        try:
            prompt = _build_generate_prompt(brainstorm, framework, chapter)
            # Run blocking Gemini call off the event loop so the server stays
            # responsive during long batch generations.
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=prompt,
            )
            generated_text = response.text

            draft_data = {
                "chapter_number": num,
                "title": chapter.get("title", f"Chapter {num}"),
                "generated_text": generated_text,
                "word_count": len(generated_text.split()),
                "model_used": model_name,
                "generated_at": datetime.now().isoformat(),
                "status": "draft",
                "revision_count": 0,
                "revision_notes": [],
            }
            _save_draft(project_id, num, draft_data)
            generated_count += 1
        except Exception as exc:
            errors.append(f"Ch. {num}: {exc}")

    # Build result HTML
    result_parts = []

    if errors:
        error_html = "".join(f"<li>{e}</li>" for e in errors)
        result_parts.append(f"""
<div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm mb-4">
  <strong>Generated {generated_count} chapter(s), but some failed:</strong>
  <ul class="mt-2 list-disc list-inside">{error_html}</ul>
  <p class="mt-2 text-red-400">Try generating the failed chapters individually.</p>
</div>""")
    else:
        result_parts.append(f"""
<div class="bg-emerald-900/30 border border-emerald-700/50 rounded-xl p-5 text-emerald-300 text-sm mb-4">
  Successfully generated {generated_count} chapter(s).
</div>""")

    # OOB swap for stats bar
    result_parts.append(_render_stats_oob(request, project_id, framework))

    # OOB swaps for each chapter card that was generated
    for chapter in framework.get("chapters", []):
        num = chapter.get("number", 0)
        draft = _load_draft(project_id, num)
        if draft:
            enriched = dict(chapter)
            enriched["draft_status"] = draft.get("status", "draft")
            enriched["draft_word_count"] = draft.get("word_count", 0)
            enriched["draft_generated_at"] = draft.get("generated_at", "")
            enriched["draft_revision_count"] = draft.get("revision_count", 0)
            card_html = templates.TemplateResponse(
                "fragments/draft_chapter_card.html",
                {"request": request, "project": project, "ch": enriched},
            ).body.decode("utf-8")
            # Wrap for OOB swap
            card_html = card_html.replace(
                f'id="chapter-card-{num}"',
                f'id="chapter-card-{num}" hx-swap-oob="true"',
                1,
            )
            result_parts.append(card_html)

    return HTMLResponse(content="\n".join(result_parts))


# ── POST — view/get preview for a chapter ────────────────────────────────────

@router.post("/projects/{project_id}/draft-writer/preview/{chapter_num}", response_class=HTMLResponse)
async def draft_writer_preview(
    project_id:  int,
    chapter_num: int,
    request:     Request,
    db:          Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    draft = _load_draft(project_id, chapter_num)
    if not draft:
        return HTMLResponse(content=f"""
<div class="bg-amber-900/30 border border-amber-700/50 rounded-xl p-5 text-amber-300 text-sm">
  No draft found for chapter {chapter_num}.
</div>""")

    framework = _load_framework(project_id)
    chapter = {}
    if framework:
        for ch in framework.get("chapters", []):
            if ch.get("number") == chapter_num:
                chapter = ch
                break

    return templates.TemplateResponse(
        "fragments/draft_chapter_preview.html",
        {
            "request": request,
            "project": project,
            "draft": draft,
            "ch": chapter,
        }
    )
