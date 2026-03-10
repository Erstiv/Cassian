"""
WORLD RULES ROUTES
The project's living "bible" — characters, locations, timelines,
rules, terminology, and style decisions that every agent respects.

Users can add/edit/delete rules manually. The Consistency Editor
auto-populates rules. Genre templates pre-fill sensible defaults.
"""

from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, WorldRule
from app.auth import require_user

router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# ── Category definitions ─────────────────────────────────────────────
CATEGORIES = [
    {"key": "character",       "label": "Characters",       "icon": "👤", "desc": "Named people, creatures, or entities in your project"},
    {"key": "location",        "label": "Locations",        "icon": "📍", "desc": "Places, settings, and geography"},
    {"key": "timeline",        "label": "Timeline",         "icon": "📅", "desc": "Key dates, chronology, and sequence of events"},
    {"key": "rule",            "label": "Rules",            "icon": "⚖️",  "desc": "World-building laws, constraints, and established facts"},
    {"key": "terminology",     "label": "Terminology",      "icon": "📝", "desc": "Special terms, jargon, made-up words, and naming conventions"},
    {"key": "style_decision",  "label": "Style Decisions",  "icon": "🎨", "desc": "Voice, tone, POV, formatting choices you've locked in"},
    {"key": "genre_default",   "label": "Genre Defaults",   "icon": "📚", "desc": "Pre-filled rules based on your project's genre"},
]

# ── Genre templates ──────────────────────────────────────────────────
# Pre-populated rules when a new project selects a genre.
GENRE_TEMPLATES = {
    "fiction": [
        {"category": "style_decision", "title": "Point of View", "content": "Confirm your POV choice (first person, third limited, omniscient) and maintain it consistently."},
        {"category": "rule", "title": "Tense", "content": "Confirm your tense (past or present) and maintain it consistently throughout."},
    ],
    "children": [
        {"category": "genre_default", "title": "Age-Appropriate Language", "content": "All language must be appropriate for the target age group. No profanity, graphic violence, or adult themes."},
        {"category": "genre_default", "title": "Reading Level", "content": "Text complexity should match the target age group. Set your target: picture book (ages 4-8), middle grade (ages 8-12), or young adult (ages 12-18)."},
        {"category": "genre_default", "title": "No Graphic Violence", "content": "Conflict is fine, but depictions of violence should be age-appropriate and not gratuitous."},
    ],
    "cookbook": [
        {"category": "genre_default", "title": "Measurement Consistency", "content": "Use one measurement system consistently: metric, imperial, or both. Include conversions if using both."},
        {"category": "genre_default", "title": "Dietary Restrictions", "content": "Flag any common allergens in each recipe: gluten, dairy, nuts, shellfish, soy, eggs."},
        {"category": "genre_default", "title": "Ingredient Formatting", "content": "List ingredients in the order they are used. Include prep instructions (diced, minced, etc.) with the ingredient, not in the method."},
    ],
    "sci-fi": [
        {"category": "rule", "title": "Technology Rules", "content": "Define the boundaries of technology in your world. What exists? What doesn't? What are the limitations?"},
        {"category": "rule", "title": "FTL / Travel Rules", "content": "How does faster-than-light travel work (if it exists)? What are its constraints and costs?"},
        {"category": "style_decision", "title": "Hard vs Soft Sci-Fi", "content": "Are you prioritising scientific accuracy (hard) or using science as a backdrop for character/theme (soft)?"},
    ],
    "poetry": [
        {"category": "style_decision", "title": "Form", "content": "Are your poems free verse, structured (sonnet, haiku, villanelle), or mixed? Note any form constraints."},
        {"category": "style_decision", "title": "Collection Theme", "content": "What unifying theme or arc connects the poems in this collection?"},
    ],
    "fantasy": [
        {"category": "rule", "title": "Magic System", "content": "Define your magic system: who can use it, what it costs, what its limits are. Consistency here prevents plot holes."},
        {"category": "rule", "title": "Races / Peoples", "content": "List the distinct peoples, species, or races in your world and their key traits."},
    ],
}


# ─────────────────────────────────────────────────────────────────
#  WORLD RULES PAGE  —  GET /projects/{id}/world-rules
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/world-rules", response_class=HTMLResponse)
def world_rules_page(
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

    rules = (
        db.query(WorldRule)
        .filter(WorldRule.project_id == project_id, WorldRule.is_active == True)
        .order_by(WorldRule.category, WorldRule.sort_order)
        .all()
    )

    # Group rules by category
    grouped = {}
    for cat in CATEGORIES:
        grouped[cat["key"]] = [r for r in rules if r.category == cat["key"]]

    return templates.TemplateResponse("world_rules.html", {
        "request":    request,
        "project":    project,
        "categories": CATEGORIES,
        "grouped":    grouped,
        "total":      len(rules),
        "active_page": "world_rules",
    })


# ─────────────────────────────────────────────────────────────────
#  ADD RULE  —  POST /projects/{id}/world-rules
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/world-rules", response_class=HTMLResponse)
async def add_rule(
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

    form = await request.form()
    category = form.get("category", "rule")
    title    = form.get("title", "").strip()
    content  = form.get("content", "").strip()

    if not title or not content:
        raise HTTPException(status_code=400, detail="Title and content are required")

    # Get next sort_order for this category
    max_order = (
        db.query(WorldRule.sort_order)
        .filter(WorldRule.project_id == project_id, WorldRule.category == category)
        .order_by(WorldRule.sort_order.desc())
        .first()
    )
    next_order = (max_order[0] + 1) if max_order else 0

    rule = WorldRule(
        project_id = project_id,
        category   = category,
        title      = title,
        content    = content,
        source     = "manual",
        sort_order = next_order,
    )
    db.add(rule)
    db.commit()

    # Return the new rule card as an HTMX fragment
    return _rule_card_html(rule, project_id)


# ─────────────────────────────────────────────────────────────────
#  UPDATE RULE  —  PUT /projects/{id}/world-rules/{rule_id}
# ─────────────────────────────────────────────────────────────────

@router.put("/projects/{project_id}/world-rules/{rule_id}", response_class=HTMLResponse)
async def update_rule(
    project_id: int,
    rule_id:    int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    rule = db.get(WorldRule, rule_id)
    if not rule or rule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    form = await request.form()
    if form.get("title"):
        rule.title = form["title"].strip()
    if form.get("content"):
        rule.content = form["content"].strip()
    if form.get("category"):
        rule.category = form["category"]

    rule.updated_at = datetime.now()
    db.commit()

    return _rule_card_html(rule, project_id)


# ─────────────────────────────────────────────────────────────────
#  DELETE RULE  —  DELETE /projects/{id}/world-rules/{rule_id}
# ─────────────────────────────────────────────────────────────────

@router.delete("/projects/{project_id}/world-rules/{rule_id}", response_class=HTMLResponse)
async def delete_rule(
    project_id: int,
    rule_id:    int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    rule = db.get(WorldRule, rule_id)
    if not rule or rule.project_id != project_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    # Soft delete
    rule.is_active = False
    db.commit()

    # Return empty string — HTMX will remove the element
    return HTMLResponse(content="")


# ─────────────────────────────────────────────────────────────────
#  POPULATE GENRE DEFAULTS  —  POST /projects/{id}/world-rules/genre-defaults
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/world-rules/genre-defaults")
async def populate_genre_defaults(
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

    genre = getattr(project, 'genre', 'fiction') or 'fiction'
    template_rules = GENRE_TEMPLATES.get(genre, GENRE_TEMPLATES.get("fiction", []))

    added = 0
    for i, tmpl in enumerate(template_rules):
        # Don't duplicate if a rule with the same title already exists
        exists = (
            db.query(WorldRule)
            .filter(
                WorldRule.project_id == project_id,
                WorldRule.title == tmpl["title"],
                WorldRule.is_active == True,
            )
            .first()
        )
        if exists:
            continue

        rule = WorldRule(
            project_id = project_id,
            category   = tmpl["category"],
            title      = tmpl["title"],
            content    = tmpl["content"],
            source     = "genre_default",
            sort_order = i,
        )
        db.add(rule)
        added += 1

    db.commit()
    return JSONResponse({"added": added, "genre": genre})


# ─────────────────────────────────────────────────────────────────
#  EXPORT RULES AS JSON  —  GET /projects/{id}/world-rules/export
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/world-rules/export")
def export_rules(
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

    rules = (
        db.query(WorldRule)
        .filter(WorldRule.project_id == project_id, WorldRule.is_active == True)
        .order_by(WorldRule.category, WorldRule.sort_order)
        .all()
    )

    export = {
        "project": project.name,
        "exported_at": datetime.now().isoformat(),
        "rules": [
            {
                "category": r.category,
                "title": r.title,
                "content": r.content,
                "source": r.source,
                "rule_data": r.rule_data or {},
            }
            for r in rules
        ]
    }
    return JSONResponse(export)


# ── HTML fragment helper ─────────────────────────────────────────────

def _rule_card_html(rule: WorldRule, project_id: int) -> HTMLResponse:
    """Return a single rule card as an HTMX-swappable HTML fragment."""
    source_badge = ""
    if rule.source == "genre_default":
        source_badge = '<span class="text-xs text-violet-400 bg-violet-900/30 px-2 py-0.5 rounded">Genre default</span>'
    elif rule.source == "consistency_editor":
        source_badge = '<span class="text-xs text-teal-400 bg-teal-900/30 px-2 py-0.5 rounded">Auto-detected</span>'
    elif rule.source == "intake":
        source_badge = '<span class="text-xs text-blue-400 bg-blue-900/30 px-2 py-0.5 rounded">From intake scan</span>'

    html = f"""
    <div id="rule-{rule.id}" class="p-4 bg-slate-800/60 border border-slate-700/50 rounded-lg group">
      <div class="flex items-start justify-between gap-3">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-1">
            <h3 class="text-sm font-medium text-slate-200 truncate">{rule.title}</h3>
            {source_badge}
          </div>
          <p class="text-xs text-slate-400 leading-relaxed">{rule.content}</p>
        </div>
        <div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
          <button hx-delete="/projects/{project_id}/world-rules/{rule.id}"
                  hx-target="#rule-{rule.id}" hx-swap="outerHTML"
                  hx-confirm="Delete this rule?"
                  class="text-xs text-red-400 hover:text-red-300 px-2 py-1 rounded
                         hover:bg-red-900/30 transition-colors">
            Delete
          </button>
        </div>
      </div>
    </div>
    """
    return HTMLResponse(content=html)
