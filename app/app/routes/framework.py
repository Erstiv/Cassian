"""
FRAMEWORK ROUTES — app/app/routes/framework.py

Phase 1: Genesis — Chapter outline and structure builder.

Routes:
  GET  /projects/{project_id}/framework                        — main framework page
  POST /projects/{project_id}/framework/generate               — Gemini chapter outline
  POST /projects/{project_id}/framework/chapter/{index}        — edit one chapter card
  POST /projects/{project_id}/framework/chapter/{index}/delete — delete a chapter
  POST /projects/{project_id}/framework/chapter/add            — insert a new chapter
  POST /projects/{project_id}/framework/reorder                — reorder chapters
  POST /projects/{project_id}/framework/apply                  — commit to DB + config
"""

import json
import os
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from google import genai

from app.database import get_db
from app.models import Project, Chapter


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


def _load_framework(project_id: int) -> dict | None:
    path = _get_genesis_dir(project_id) / "framework.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_framework(project_id: int, data: dict) -> None:
    path = _get_genesis_dir(project_id) / "framework.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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


def _save_config(project_id: int, config: dict) -> None:
    config_path = PROJECTS_DIR / str(project_id) / "config.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_gemini_client(project_id: int):
    """Returns (client, model_name) tuple."""
    config  = _load_config(project_id)
    api_key = config.get("gemini", {}).get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("No Gemini API key found in config.json or environment.")
    client = genai.Client(api_key=api_key)
    model_name = config.get("gemini", {}).get("models", {}).get("text", "gemini-2.5-pro")
    return client, model_name


def _reindex_chapters(chapters: list[dict]) -> list[dict]:
    """Re-number chapters sequentially after add/delete/reorder."""
    for i, ch in enumerate(chapters):
        ch["number"] = i + 1
    return chapters


OUTLINE_PROMPT = """You are a book editor creating a chapter outline.

Book concept:
{description}
Genre: {genre}

Structure type: {structure_type}
Target chapter count: {chapter_count}
Author's notes: {notes}

Generate a detailed chapter-by-chapter outline.

For each chapter provide:
- Chapter number
- Working title
- Summary (2-3 sentences describing what happens)
- Purpose (what this chapter accomplishes for the overall narrative)
- Key characters involved
- Intensity (low / medium / high)

Also include:
- A note on pacing (which chapters are high/low intensity)
- Suggested act breaks (if using act structure) — list chapter numbers where acts change

RESPOND IN THIS EXACT JSON FORMAT (no markdown, no code fences, raw JSON only):
{{
  "structure_type": "{structure_type}",
  "act_breaks": [],
  "chapters": [
    {{
      "number": 1,
      "title": "Working Title",
      "summary": "What happens in this chapter...",
      "purpose": "What this accomplishes narratively...",
      "characters": ["Character A", "Character B"],
      "intensity": "medium"
    }}
  ],
  "pacing_notes": "Overall pacing guidance..."
}}"""


# ── GET — main framework page ──────────────────────────────────────────────────

@router.get("/projects/{project_id}/framework", response_class=HTMLResponse)
async def framework_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapters_db = (
        db.query(Chapter)
        .filter(Chapter.project_id == project_id)
        .order_by(Chapter.chapter_number)
        .all()
    )
    framework   = _load_framework(project_id)
    brainstorm  = _load_brainstorm(project_id)

    return templates.TemplateResponse(
        "framework.html",
        {
            "request":     request,
            "project":     project,
            "active_page": "framework",
            "framework":   framework,
            "brainstorm":  brainstorm,
            "chapters_db": chapters_db,
        }
    )


# ── POST — generate outline via Gemini ────────────────────────────────────────

@router.post("/projects/{project_id}/framework/generate", response_class=HTMLResponse)
async def framework_generate(
    project_id:     int,
    request:        Request,
    db:             Session = Depends(get_db),
    chapter_count:  int  = Form(20),
    structure_type: str  = Form("three_act"),
    notes:          str  = Form(""),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Build context from project + any existing brainstorm
    brainstorm  = _load_brainstorm(project_id)
    description = project.description or ""
    if brainstorm:
        sections = brainstorm.get("sections", {})
        premise  = sections.get("PREMISE", "")
        if premise:
            description = f"{description}\n\nPremise: {premise}".strip()

    try:
        client, model_name = _get_gemini_client(project_id)
        prompt = OUTLINE_PROMPT.format(
            description=description or "(no description yet)",
            genre=project.genre or "fiction",
            structure_type=structure_type,
            chapter_count=chapter_count,
            notes=notes or "(none)",
        )
        response = client.models.generate_content(model=model_name, contents=prompt)
        raw_text = response.text.strip()

        # Strip markdown code fences if Gemini added them
        if raw_text.startswith("```"):
            lines    = raw_text.splitlines()
            raw_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = json.loads(raw_text)
        data["generated_at"] = datetime.now().isoformat()
        _save_framework(project_id, data)

    except json.JSONDecodeError as exc:
        return HTMLResponse(content=f"""
<div id="framework-outline" class="mt-6">
  <div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
    <strong>JSON parse error:</strong> {exc}<br>
    <details class="mt-2"><summary class="cursor-pointer text-slate-400">Raw response</summary>
    <pre class="mt-2 text-xs text-slate-500 whitespace-pre-wrap">{raw_text[:2000]}</pre></details>
  </div>
</div>
""")
    except Exception as exc:
        return HTMLResponse(content=f"""
<div id="framework-outline" class="mt-6">
  <div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
    <strong>Error generating outline:</strong> {exc}
  </div>
</div>
""")

    return templates.TemplateResponse(
        "framework_outline.html",
        {
            "request":   request,
            "project":   project,
            "framework": data,
        }
    )


# ── POST — edit a single chapter card ─────────────────────────────────────────

@router.post("/projects/{project_id}/framework/chapter/{index}", response_class=HTMLResponse)
async def framework_edit_chapter(
    project_id: int,
    index:      int,
    request:    Request,
    db:         Session = Depends(get_db),
    title:      str = Form(""),
    summary:    str = Form(""),
    notes:      str = Form(""),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        raise HTTPException(status_code=404, detail="No framework found. Generate one first.")

    chapters = framework.get("chapters", [])
    if index < 0 or index >= len(chapters):
        raise HTTPException(status_code=400, detail="Chapter index out of range")

    chapters[index]["title"]   = title.strip() or chapters[index].get("title", "")
    chapters[index]["summary"] = summary.strip() or chapters[index].get("summary", "")
    if notes.strip():
        chapters[index]["notes"] = notes.strip()

    framework["chapters"] = chapters
    _save_framework(project_id, framework)

    ch = chapters[index]
    return templates.TemplateResponse(
        "framework_chapter_card.html",
        {
            "request": request,
            "project": project,
            "ch":      ch,
            "index":   index,
            "act_breaks": framework.get("act_breaks", []),
        }
    )


# ── POST — delete a chapter ────────────────────────────────────────────────────

@router.post("/projects/{project_id}/framework/chapter/{index}/delete", response_class=HTMLResponse)
async def framework_delete_chapter(
    project_id: int,
    index:      int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        raise HTTPException(status_code=404, detail="No framework found.")

    chapters = framework.get("chapters", [])
    if index < 0 or index >= len(chapters):
        raise HTTPException(status_code=400, detail="Chapter index out of range")

    chapters.pop(index)
    framework["chapters"] = _reindex_chapters(chapters)
    _save_framework(project_id, framework)

    return templates.TemplateResponse(
        "framework_outline.html",
        {
            "request":   request,
            "project":   project,
            "framework": framework,
        }
    )


# ── POST — add a chapter ───────────────────────────────────────────────────────

@router.post("/projects/{project_id}/framework/chapter/add", response_class=HTMLResponse)
async def framework_add_chapter(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    title:      str = Form("New Chapter"),
    summary:    str = Form(""),
    position:   int = Form(-1),   # -1 = append at end
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id) or {
        "structure_type": "custom",
        "act_breaks": [],
        "chapters": [],
        "pacing_notes": "",
        "generated_at": datetime.now().isoformat(),
    }

    new_chapter = {
        "number":     len(framework["chapters"]) + 1,
        "title":      title.strip() or "New Chapter",
        "summary":    summary.strip(),
        "purpose":    "",
        "characters": [],
        "intensity":  "medium",
    }

    chapters = framework.get("chapters", [])
    if position < 0 or position >= len(chapters):
        chapters.append(new_chapter)
    else:
        chapters.insert(position, new_chapter)

    framework["chapters"] = _reindex_chapters(chapters)
    _save_framework(project_id, framework)

    return templates.TemplateResponse(
        "framework_outline.html",
        {
            "request":   request,
            "project":   project,
            "framework": framework,
        }
    )


# ── POST — reorder chapters ────────────────────────────────────────────────────

@router.post("/projects/{project_id}/framework/reorder", response_class=HTMLResponse)
async def framework_reorder(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    order:      str = Form(""),   # JSON array of current indices in new order
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        raise HTTPException(status_code=404, detail="No framework found.")

    try:
        new_order = json.loads(order)  # list of ints
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid order JSON")

    chapters = framework.get("chapters", [])
    reordered = [chapters[i] for i in new_order if 0 <= i < len(chapters)]
    framework["chapters"] = _reindex_chapters(reordered)
    _save_framework(project_id, framework)

    return templates.TemplateResponse(
        "framework_outline.html",
        {
            "request":   request,
            "project":   project,
            "framework": framework,
        }
    )


# ── POST — move chapter up or down ────────────────────────────────────────────

@router.post("/projects/{project_id}/framework/chapter/{index}/move", response_class=HTMLResponse)
async def framework_move_chapter(
    project_id: int,
    index:      int,
    request:    Request,
    db:         Session = Depends(get_db),
    direction:  str = Form("up"),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        raise HTTPException(status_code=404, detail="No framework found.")

    chapters = framework.get("chapters", [])
    if direction == "up" and index > 0:
        chapters[index], chapters[index - 1] = chapters[index - 1], chapters[index]
    elif direction == "down" and index < len(chapters) - 1:
        chapters[index], chapters[index + 1] = chapters[index + 1], chapters[index]

    framework["chapters"] = _reindex_chapters(chapters)
    _save_framework(project_id, framework)

    return templates.TemplateResponse(
        "framework_outline.html",
        {
            "request":   request,
            "project":   project,
            "framework": framework,
        }
    )


# ── POST — apply framework to project DB + config ────────────────────────────

@router.post("/projects/{project_id}/framework/apply", response_class=HTMLResponse)
async def framework_apply(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    framework = _load_framework(project_id)
    if not framework:
        return HTMLResponse(content="""
<div class="bg-amber-900/30 border border-amber-700/50 rounded-xl p-5 text-amber-300 text-sm">
  No framework to apply. Generate a chapter outline first.
</div>
""")

    chapters_data = framework.get("chapters", [])

    # Check for existing chapters
    existing_chapters = db.query(Chapter).filter(Chapter.project_id == project_id).all()
    has_existing = len(existing_chapters) > 0

    # Update Project record
    project.chapter_count = len(chapters_data)
    project.chapter_order = [ch.get("title", f"Chapter {ch['number']}") for ch in chapters_data]
    project.updated_at    = datetime.now()

    # Create or update Chapter records
    existing_by_num = {ch.chapter_number: ch for ch in existing_chapters if ch.chapter_number}

    for ch_data in chapters_data:
        num   = ch_data.get("number", 0)
        title = ch_data.get("title", "")
        key   = str(num).zfill(2)

        if num in existing_by_num:
            # Update existing record — only title (don't wipe word_count etc.)
            existing_by_num[num].title = title
            existing_by_num[num].chapter_name = title
        else:
            new_ch = Chapter(
                project_id     = project_id,
                chapter_key    = key,
                chapter_number = num,
                title          = title,
                chapter_name   = title,
            )
            db.add(new_ch)

    db.commit()

    # Update config.json
    config = _load_config(project_id)
    if config:
        config.setdefault("book", {})
        config["book"]["total_chapters"] = len(chapters_data)
        config["book"]["reading_order"]  = [ch.get("number", i + 1) for i, ch in enumerate(chapters_data)]
        _save_config(project_id, config)

    warning = ""
    if has_existing:
        warning = """
<p class="text-amber-300 text-sm mt-2">
  ⚠ This project already had chapters. Existing titles have been updated; no content was deleted.
</p>"""

    return HTMLResponse(content=f"""
<div id="apply-result" class="mt-4 bg-emerald-900/30 border border-emerald-700/50 rounded-xl p-5">
  <div class="flex items-center gap-3 mb-2">
    <span class="text-emerald-400 text-xl">✓</span>
    <span class="text-emerald-300 font-medium">Framework applied to project</span>
  </div>
  <p class="text-slate-400 text-sm">
    {len(chapters_data)} chapters saved to database and config.json updated.
  </p>
  {warning}
  <div class="mt-4 flex gap-3">
    <a href="/projects/{project_id}"
       class="inline-flex items-center gap-2 px-4 py-2 bg-amber-500 hover:bg-amber-400
              text-slate-900 text-sm font-semibold rounded-lg transition-colors">
      📥 Go to Intake →
    </a>
    <a href="/projects/{project_id}/world-rules"
       class="inline-flex items-center gap-2 px-4 py-2 bg-slate-700 hover:bg-slate-600
              text-slate-100 text-sm font-medium rounded-lg transition-colors">
      📖 World Rules
    </a>
  </div>
</div>
""")
