"""
IDEA AGENT ROUTES — app/app/routes/idea.py

Phase 1: Genesis — AI-assisted book concept brainstorming.

Routes:
  GET  /projects/{project_id}/idea             — main brainstorm page
  POST /projects/{project_id}/idea/brainstorm  — call Gemini to generate a concept
  POST /projects/{project_id}/idea/refine      — refine existing brainstorm with feedback
  POST /projects/{project_id}/idea/apply       — save concept to Project record
"""

import json
import os
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from google import genai

from app.database import get_db
from app.auth import require_user
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


def _load_brainstorm(project_id: int) -> dict | None:
    path = _get_genesis_dir(project_id) / "idea_brainstorm.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_brainstorm(project_id: int, data: dict) -> None:
    path = _get_genesis_dir(project_id) / "idea_brainstorm.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_config(project_id: int) -> dict:
    config_path = PROJECTS_DIR / str(project_id) / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _get_gemini_client(project_id: int):
    """Initialise Gemini with the project's API key from config.json.
    Returns (client, model_name) tuple."""
    config = _load_config(project_id)
    api_key = config.get("gemini", {}).get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("No Gemini API key found in config.json or environment.")
    client = genai.Client(api_key=api_key)
    model_name = config.get("gemini", {}).get("models", {}).get("text", "gemini-2.5-pro")
    return client, model_name


BRAINSTORM_PROMPT = """You are a creative writing consultant helping develop a book concept.

The author has provided:
- Genre: {genre}
- Themes: {themes}
- Target audience: {audience}
- Tone: {tone}
- Inspirations: {inspirations}

Generate a comprehensive book concept including:

TITLE_OPTIONS:
[3 working title suggestions with brief rationale for each]

PREMISE:
[2-3 sentence elevator pitch]

CENTRAL_CONFLICT:
[The core tension or question driving the narrative]

MAIN_CHARACTERS:
[3-5 key characters with name, role, and one-line description]

SETTING:
[Time, place, and atmosphere]

THEMES:
[3-5 major themes the book explores]

TONE_AND_STYLE:
[Description of the writing voice and narrative approach]

COMPARABLE_TITLES:
[2-3 published books with similar appeal, with brief explanation]

CHAPTER_ESTIMATE:
[Estimated chapter count and approximate word count]

Respond in the exact format above. Be specific and creative — avoid generic suggestions."""


REFINE_PROMPT = """You are a creative writing consultant refining a book concept.

Original concept:
{original_text}

Author's feedback on what to change or keep:
{feedback}

Please generate an updated book concept that incorporates this feedback. Keep what's working,
change what the author wants changed, and maintain the same structured format:

TITLE_OPTIONS:
PREMISE:
CENTRAL_CONFLICT:
MAIN_CHARACTERS:
SETTING:
THEMES:
TONE_AND_STYLE:
COMPARABLE_TITLES:
CHAPTER_ESTIMATE:

Be specific and creative."""


def _parse_brainstorm_text(raw_text: str) -> dict:
    """
    Parse the structured Gemini response into a dict of sections.
    Each section becomes a key; its content is the value.
    """
    sections = {}
    current_key = None
    current_lines = []

    for line in raw_text.splitlines():
        stripped = line.strip()
        # Detect section headers: lines ending with ":" that are UPPERCASE_WITH_UNDERSCORES
        if stripped.endswith(":") and stripped[:-1].replace("_", "").replace(" ", "").isupper():
            if current_key:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = stripped[:-1]
            current_lines = []
        else:
            current_lines.append(line)

    if current_key and current_lines:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


# ── GET — main idea page ───────────────────────────────────────────────────────

@router.get("/projects/{project_id}/idea", response_class=HTMLResponse)
async def idea_page(
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

    brainstorm = _load_brainstorm(project_id)

    return templates.TemplateResponse(
        "idea.html",
        {
            "request":     request,
            "project":     project,
            "active_page": "idea",
            "brainstorm":  brainstorm,
        }
    )


# ── POST — generate a new brainstorm ──────────────────────────────────────────

@router.post("/projects/{project_id}/idea/brainstorm", response_class=HTMLResponse)
async def idea_brainstorm(
    project_id:   int,
    request:      Request,
    db:           Session = Depends(get_db),
    genre:        str = Form("fiction"),
    themes:       str = Form(""),
    audience:     str = Form("adult"),
    tone:         str = Form(""),
    inspirations: str = Form(""),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        client, model_name = _get_gemini_client(project_id)
        prompt = BRAINSTORM_PROMPT.format(
            genre=genre,
            themes=themes or "(not specified)",
            audience=audience,
            tone=tone or "(not specified)",
            inspirations=inspirations or "(not specified)",
        )
        response  = client.models.generate_content(model=model_name, contents=prompt)
        raw_text  = response.text

        sections  = _parse_brainstorm_text(raw_text)
        data = {
            "raw":         raw_text,
            "sections":    sections,
            "inputs": {
                "genre":        genre,
                "themes":       themes,
                "audience":     audience,
                "tone":         tone,
                "inspirations": inspirations,
            },
            "generated_at": datetime.now().isoformat(),
        }
        _save_brainstorm(project_id, data)

    except Exception as exc:
        return HTMLResponse(content=f"""
<div id="brainstorm-results" class="mt-6">
  <div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
    <strong>Error generating brainstorm:</strong> {exc}
  </div>
</div>
""")

    return templates.TemplateResponse(
        "idea_results.html",
        {
            "request":    request,
            "project":    project,
            "brainstorm": data,
        }
    )


# ── POST — refine existing brainstorm ─────────────────────────────────────────

@router.post("/projects/{project_id}/idea/refine", response_class=HTMLResponse)
async def idea_refine(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    feedback:   str = Form(""),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    existing = _load_brainstorm(project_id)
    if not existing:
        return HTMLResponse(content="""
<div class="bg-amber-900/30 border border-amber-700/50 rounded-xl p-5 text-amber-300 text-sm">
  No existing brainstorm found. Run the brainstorm first.
</div>
""")

    try:
        client, model_name = _get_gemini_client(project_id)
        prompt = REFINE_PROMPT.format(
            original_text=existing.get("raw", ""),
            feedback=feedback or "(no specific feedback provided)",
        )
        response = client.models.generate_content(model=model_name, contents=prompt)
        raw_text = response.text

        sections = _parse_brainstorm_text(raw_text)
        data = {
            "raw":          raw_text,
            "sections":     sections,
            "inputs":       existing.get("inputs", {}),
            "refined":      True,
            "feedback":     feedback,
            "generated_at": datetime.now().isoformat(),
        }
        _save_brainstorm(project_id, data)

    except Exception as exc:
        return HTMLResponse(content=f"""
<div class="bg-red-900/30 border border-red-700/50 rounded-xl p-5 text-red-300 text-sm">
  <strong>Error refining brainstorm:</strong> {exc}
</div>
""")

    return templates.TemplateResponse(
        "idea_results.html",
        {
            "request":    request,
            "project":    project,
            "brainstorm": data,
        }
    )


# ── POST — apply concept to project record ────────────────────────────────────

@router.post("/projects/{project_id}/idea/apply", response_class=HTMLResponse)
async def idea_apply(
    project_id:  int,
    request:     Request,
    db:          Session = Depends(get_db),
    title:       str = Form(""),
    description: str = Form(""),
    genre:       str = Form(""),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    if title.strip():
        project.name = title.strip()
    if description.strip():
        project.description = description.strip()
    if genre.strip():
        project.genre = genre.strip()

    project.updated_at = datetime.now()
    db.commit()

    return HTMLResponse(content=f"""
<div id="apply-result" class="mt-4 bg-emerald-900/30 border border-emerald-700/50 rounded-xl p-5">
  <div class="flex items-center gap-3 mb-3">
    <span class="text-emerald-400 text-xl">✓</span>
    <span class="text-emerald-300 font-medium">Project updated successfully</span>
  </div>
  <p class="text-slate-400 text-sm mb-4">
    Title, description, and genre have been saved to your project.
  </p>
  <a href="/projects/{project_id}/framework"
     class="inline-flex items-center gap-2 px-4 py-2 bg-amber-500 hover:bg-amber-400
            text-slate-900 text-sm font-semibold rounded-lg transition-colors">
    🏗 Continue to Framework →
  </a>
</div>
""")
