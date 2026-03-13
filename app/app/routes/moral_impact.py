"""
MORAL IMPACT EDITOR ROUTES — app/app/routes/moral_impact.py

Gives the moral impact editor agent (agents/08_moral_impact/moral_impact.py) a web UI.
The agent is ADVISORY ONLY — it flags concerns but does not modify chapter files.
Authors review and acknowledge concerns in this UI.

Routes:
  GET  /projects/{project_id}/moral-impact                                   — main page
  POST /projects/{project_id}/moral-impact/run                               — run agent
  POST /projects/{project_id}/moral-impact/acknowledge/{chapter_key}/{idx}   — acknowledge one concern
  GET  /projects/{project_id}/moral-impact/chapter/{chapter_key}             — HTMX fragment
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_user
from app.models import Project


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR       = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR      = CASSIAN_DIR / "projects"
MORAL_IMPACT_AGENT = CASSIAN_DIR / "agents" / "08_moral_impact" / "moral_impact.py"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _get_moral_impact_status(project_id: int) -> str:
    """
    Returns: "not_run" | "complete" | "stale"

    Stale = report exists but at least one chapter source file is newer.
    """
    project_dir = _get_project_dir(project_id)
    report_path = project_dir / "output" / "moral_impact" / "moral_impact_report.json"

    if not report_path.exists():
        return "not_run"

    report_mtime = report_path.stat().st_mtime

    for subdir in ("output/workbench", "output/editing", "output/ingested"):
        check_dir = project_dir / subdir
        if not check_dir.exists():
            continue
        for f in check_dir.glob("chapter_*.json"):
            try:
                if f.stat().st_mtime > report_mtime:
                    return "stale"
            except Exception:
                pass

    return "complete"


def _load_report(project_id: int) -> dict | None:
    """Load moral_impact_report.json, or None if missing/unreadable."""
    report_path = _get_project_dir(project_id) / "output" / "moral_impact" / "moral_impact_report.json"
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_all_chapter_concerns(project_id: int) -> list[dict]:
    """Load all chapter_*_concerns.json files, sorted by chapter key."""
    mi_dir = _get_project_dir(project_id) / "output" / "moral_impact"
    if not mi_dir.exists():
        return []

    def sort_key(p: Path):
        stem = p.stem
        k    = stem.replace("chapter_", "").replace("_concerns", "")
        try:
            return (0, int(k))
        except ValueError:
            return (1, k)

    results = []
    for path in sorted(mi_dir.glob("chapter_*_concerns.json"), key=sort_key):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return results


def _load_chapter_concerns(project_id: int, chapter_key: str) -> dict | None:
    """Load concerns for a single chapter."""
    path = (
        _get_project_dir(project_id)
        / "output" / "moral_impact"
        / f"chapter_{chapter_key}_concerns.json"
    )
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_chapter_concerns(project_id: int, chapter_key: str, data: dict) -> None:
    path = (
        _get_project_dir(project_id)
        / "output" / "moral_impact"
        / f"chapter_{chapter_key}_concerns.json"
    )
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _discover_chapter_keys(project_id: int) -> list[str]:
    """
    Return sorted chapter keys from the most available source dirs.
    """
    project_dir = _get_project_dir(project_id)
    keys: set[str] = set()

    for subdir, pattern, strip_suffix in [
        ("output/workbench", "chapter_*_working.json", "_working"),
        ("output/editing",   "chapter_*_edited.json",  "_edited"),
        ("output/ingested",  "chapter_*.json",          ""),
    ]:
        d = project_dir / subdir
        if not d.exists():
            continue
        for f in d.glob(pattern):
            stem = f.stem
            k    = stem.replace("chapter_", "", 1)
            if strip_suffix:
                sfx = strip_suffix.lstrip("_")
                if k.endswith("_" + sfx):
                    k = k[: -(len(sfx) + 1)]
                elif k.endswith(sfx):
                    k = k[: -len(sfx)]
            keys.add(k)

    def sort_key(k: str):
        try:
            return (0, int(k))
        except ValueError:
            return (1, k)

    return sorted(keys, key=sort_key)


# ── GET — main moral impact page ──────────────────────────────────────────

@router.get("/projects/{project_id}/moral-impact", response_class=HTMLResponse)
async def moral_impact_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    error:      str = None,
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    status           = _get_moral_impact_status(project_id)
    report           = _load_report(project_id) if status != "not_run" else None
    chapter_concerns = _load_all_chapter_concerns(project_id) if status != "not_run" else []
    chapter_keys     = _discover_chapter_keys(project_id)

    return templates.TemplateResponse(
        "moral_impact.html",
        {
            "request":          request,
            "project":          project,
            "active_page":      "moral_impact",
            "status":           status,
            "report":           report,
            "chapter_concerns": chapter_concerns,
            "chapter_keys":     chapter_keys,
            "error":            error,
        }
    )


# ── Background task tracking ──────────────────────────────────────────────────
_running_tasks: dict[int, dict] = {}


def _progress_file(project_id: int) -> Path:
    return _get_project_dir(project_id) / "output" / "moral_impact" / ".progress.json"


def _write_progress(project_id: int, data: dict):
    pf = _progress_file(project_id)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data), encoding="utf-8")


async def _monitor_moral_impact(project_id: int, proc, total_chapters: int):
    """Read stdout lines from the agent and update a progress file."""
    done = 0
    current_chapter = ""
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("──") and "Chapter" in line:
                current_chapter = line.replace("──", "").replace("Chapter", "").strip()
            elif line.startswith("Analyzing"):
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Analyzing chapter {current_chapter}…",
                })
            elif "concern" in line.lower() or "strength" in line.lower():
                done += 1
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Finished chapter {current_chapter}",
                })

        await proc.wait()

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            err_text = stderr_bytes.decode("utf-8", errors="replace")[-500:]
            _write_progress(project_id, {
                "state": "error",
                "done": done,
                "total": total_chapters,
                "current": "",
                "message": f"Agent exited with code {proc.returncode}: {err_text[:200]}",
            })
        elif done == 0:
            _write_progress(project_id, {
                "state": "complete",
                "done": total_chapters,
                "total": total_chapters,
                "current": "",
                "message": "Moral impact analysis complete.",
            })
        else:
            _write_progress(project_id, {
                "state": "complete",
                "done": total_chapters,
                "total": total_chapters,
                "current": "",
                "message": f"Moral impact analysis complete — {done} chapters analyzed.",
            })
    except Exception as exc:
        _write_progress(project_id, {
            "state": "error",
            "done": done,
            "total": total_chapters,
            "current": "",
            "message": f"Monitor error: {exc}",
        })
    finally:
        _running_tasks.pop(project_id, None)


# ── GET/POST — run the agent ─────────────────────────────────

@router.get("/projects/{project_id}/moral-impact/run")
@router.post("/projects/{project_id}/moral-impact/run")
async def moral_impact_run(
    project_id:     int,
    request:        Request,
    db:             Session = Depends(get_db),
    single_chapter: str = None,
    chapter:        str = None,
):
    single_chapter = single_chapter or chapter
    if request.method == "POST":
        form_data = await request.form()
        single_chapter = form_data.get("single_chapter", single_chapter) or form_data.get("chapter", single_chapter)

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    # If already running, redirect to progress page
    if project_id in _running_tasks:
        return RedirectResponse(
            f"/projects/{project_id}/moral-impact/progress", status_code=303,
        )

    project_dir = _get_project_dir(project_id)

    has_chapters = any(
        (project_dir / subdir).exists()
        and list((project_dir / subdir).glob("chapter_*.json"))
        for subdir in ("output/workbench", "output/editing", "output/ingested")
    )
    if not has_chapters:
        return RedirectResponse(
            f"/projects/{project_id}/moral-impact?error=No+chapters+found.+Run+the+Intake+agent+first.",
            status_code=303,
        )

    if not MORAL_IMPACT_AGENT.exists():
        return RedirectResponse(
            f"/projects/{project_id}/moral-impact?error=Moral+impact+agent+not+found+at+{MORAL_IMPACT_AGENT}",
            status_code=303,
        )

    # Ensure config.json exists
    config_path = project_dir / "config.json"
    if not config_path.exists():
        import json as _json
        minimal_config = {
            "book": {
                "title": project.name or "Untitled",
                "author": project.author or "Unknown Author",
            },
            "gemini": {
                "api_key": "",
                "models": {"text": "gemini-2.5-flash"},
            },
        }
        config_path.write_text(_json.dumps(minimal_config, indent=2), encoding="utf-8")

    # Count chapters for progress tracking
    chapter_keys = _discover_chapter_keys(project_id)
    if single_chapter and single_chapter.strip():
        total_chapters = 1
    else:
        total_chapters = len(chapter_keys) or 1

    cmd = [sys.executable, "-u", str(MORAL_IMPACT_AGENT)]
    if single_chapter and single_chapter.strip():
        cmd.extend(["--chapter", single_chapter.strip()])

    env = {**os.environ, "CASSIAN_PROJECT_DIR": str(project_dir), "PYTHONUNBUFFERED": "1"}

    _write_progress(project_id, {
        "state": "running",
        "done": 0,
        "total": total_chapters,
        "current": "",
        "message": "Starting moral impact analysis…",
    })

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(CASSIAN_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        _progress_file(project_id).unlink(missing_ok=True)
        return RedirectResponse(
            f"/projects/{project_id}/moral-impact?error=Failed+to+launch+agent:+{exc}",
            status_code=303,
        )

    _running_tasks[project_id] = {"proc": proc, "total": total_chapters}
    asyncio.create_task(_monitor_moral_impact(project_id, proc, total_chapters))

    return RedirectResponse(
        f"/projects/{project_id}/moral-impact/progress", status_code=303,
    )


# ── GET — progress page ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/moral-impact/progress", response_class=HTMLResponse)
async def moral_impact_progress_page(
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

    pf = _progress_file(project_id)
    if pf.exists():
        progress = json.loads(pf.read_text(encoding="utf-8"))
    else:
        return RedirectResponse(f"/projects/{project_id}/moral-impact", status_code=303)

    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return RedirectResponse(
                f"/projects/{project_id}/moral-impact?error={err}", status_code=303,
            )
        return RedirectResponse(f"/projects/{project_id}/moral-impact", status_code=303)

    return templates.TemplateResponse(
        "agent_progress.html",
        {
            "request":          request,
            "project":          project,
            "agent_name":       "moral_impact",
            "agent_description": "Moral Impact Analysis",
            "progress":         progress,
            "poll_url":         f"/projects/{project_id}/moral-impact/progress/poll",
            "back_url":         f"/projects/{project_id}/moral-impact",
        },
    )


# ── GET — HTMX polling for progress ─────────────────────────────────────

@router.get("/projects/{project_id}/moral-impact/progress/poll", response_class=HTMLResponse)
async def moral_impact_progress_poll(
    project_id: int,
    request:    Request,
):
    pf = _progress_file(project_id)
    if not pf.exists():
        return HTMLResponse(
            content='<div hx-get="REDIRECT" hx-trigger="load"></div>',
            headers={"HX-Redirect": f"/projects/{project_id}/moral-impact"},
        )

    progress = json.loads(pf.read_text(encoding="utf-8"))

    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": f"/projects/{project_id}/moral-impact?error={err}"},
            )
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": f"/projects/{project_id}/moral-impact"},
        )

    done  = progress.get("done", 0)
    total = progress.get("total", 1)
    pct   = round(done / total * 100) if total else 0
    msg   = progress.get("message", "Processing…")

    return HTMLResponse(f"""
    <div id="progress-content"
         hx-get="/projects/{project_id}/moral-impact/progress/poll"
         hx-trigger="every 2s"
         hx-swap="outerHTML">
      <div class="flex items-center gap-3 mb-2">
        <span class="text-sm text-slate-400">{msg}</span>
        <span class="text-xs text-slate-600 ml-auto">{done} / {total}</span>
      </div>
      <div class="w-full h-2 bg-slate-800 rounded-full overflow-hidden">
        <div class="h-full bg-emerald-400 rounded-full transition-all duration-500"
             style="width: {pct}%"></div>
      </div>
    </div>
    """)


# ── POST — respond to a concern (accept / dismiss / add note / reset) ─────────

@router.post(
    "/projects/{project_id}/moral-impact/respond/{chapter_key}/{concern_index}",
    response_class=HTMLResponse,
)
async def moral_impact_respond(
    project_id:    int,
    chapter_key:   str,
    concern_index: int,
    request:       Request,
    db:            Session = Depends(get_db),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    data = _load_chapter_concerns(project_id, chapter_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Chapter concerns file not found")

    concerns = data.get("concerns", [])
    if concern_index < 0 or concern_index >= len(concerns):
        raise HTTPException(status_code=400, detail="Concern index out of range")

    form = await request.form()
    decision    = form.get("decision", "")
    author_note = form.get("author_note", "").strip()

    concern = concerns[concern_index]

    if decision == "accept":
        concern["decision"]    = "accept"
        concern["acknowledged"] = True
        if author_note:
            concern["author_note"] = author_note
    elif decision == "dismiss":
        concern["decision"]    = "dismiss"
        concern["acknowledged"] = True
        if author_note:
            concern["author_note"] = author_note
    elif decision == "reset":
        concern.pop("decision", None)
        concern.pop("author_note", None)
        concern["acknowledged"] = False

    if decision != "cancel":
        _save_chapter_concerns(project_id, chapter_key, data)

    return HTMLResponse(content=_render_concern_html(
        project_id, chapter_key, concern_index, concern
    ))


def _render_concern_html(
    project_id: int, chapter_key: str, idx: int, concern: dict
) -> str:
    """Render a single concern div for HTMX swap."""
    sev = concern.get("severity", "note").lower()
    cat = concern.get("category", "")
    decision    = concern.get("decision", "")
    author_note = concern.get("author_note", "")
    is_ack      = concern.get("acknowledged", False)

    sev_colors = {
        "flag":    "text-red-400 bg-red-900/20 border-red-700/30",
        "consider":"text-amber-400 bg-amber-900/20 border-amber-700/30",
        "note":    "text-blue-400 bg-blue-900/20 border-blue-700/30",
    }
    colors = sev_colors.get(sev, "text-slate-400 bg-slate-800 border-slate-700")
    sev_icons = {"flag": "🔴", "consider": "🟡", "note": "🔵"}
    icon = sev_icons.get(sev, "●")
    cat_display = cat.replace("_", " ").title()
    context     = concern.get("context", "")
    explanation = concern.get("explanation", "")
    suggestion  = concern.get("suggestion", "")
    para_idx    = concern.get("paragraph_index", "?")

    base_url = f"/projects/{project_id}/moral-impact/respond/{chapter_key}/{idx}"

    # Determine container classes
    if decision == "accept":
        container_cls = "bg-emerald-950/20 border-emerald-800/40"
        text_strike = ""
    elif decision == "dismiss":
        container_cls = f"opacity-40 {colors}"
        text_strike = "line-through"
    else:
        container_cls = colors
        text_strike = ""

    # Suggestion block
    suggestion_html = ""
    if suggestion and decision != "dismiss":
        suggestion_html = f"""
          <div class="text-xs mt-1.5 bg-slate-800/50 border border-slate-700/50 rounded-lg px-3 py-2">
            <span class="text-emerald-400 font-medium">Suggestion:</span>
            <span class="text-slate-300">{suggestion}</span>
          </div>"""

    # Author note display
    note_html = ""
    if author_note:
        note_html = f'<span class="text-xs text-slate-400 italic">— {author_note}</span>'

    # Action buttons
    if decision == "accept":
        actions = f"""
      <div class="flex items-center gap-2 flex-wrap">
        <span class="inline-flex items-center gap-1 text-xs font-medium text-emerald-400 bg-emerald-900/40 border border-emerald-700/50 rounded-lg px-3 py-1.5">
          ✓ Will address
        </span>
        {note_html}
        <button hx-post="{base_url}" hx-vals='{{"decision":"reset"}}' hx-target="#concern-{chapter_key}-{idx}" hx-swap="outerHTML"
                class="text-xs text-slate-500 hover:text-slate-300 transition-colors cursor-pointer ml-auto">
          Undo
        </button>
      </div>"""
    elif decision == "dismiss":
        actions = f"""
      <div class="flex items-center gap-2 flex-wrap">
        <span class="inline-flex items-center gap-1 text-xs font-medium text-slate-500 bg-slate-800/60 border border-slate-700/50 rounded-lg px-3 py-1.5">
          Dismissed — intentional choice
        </span>
        {note_html}
        <button hx-post="{base_url}" hx-vals='{{"decision":"reset"}}' hx-target="#concern-{chapter_key}-{idx}" hx-swap="outerHTML"
                class="text-xs text-slate-500 hover:text-slate-300 transition-colors cursor-pointer ml-auto">
          Undo
        </button>
      </div>"""
    else:
        actions = f"""
      <div class="flex items-center gap-2 flex-wrap">
        <button hx-post="{base_url}" hx-vals='{{"decision":"accept"}}' hx-target="#concern-{chapter_key}-{idx}" hx-swap="outerHTML"
                class="text-xs font-medium bg-emerald-900/60 hover:bg-emerald-800 text-emerald-300
                       border border-emerald-700/50 rounded-lg px-3 py-1.5 transition-colors cursor-pointer">
          ✓ Will Address
        </button>
        <button onclick="openConcernEditor('{chapter_key}', {idx})"
                class="text-xs font-medium bg-blue-900/40 hover:bg-blue-900/70 text-blue-400
                       border border-blue-800/40 rounded-lg px-3 py-1.5 transition-colors cursor-pointer">
          ✎ Add Note
        </button>
        <button hx-post="{base_url}" hx-vals='{{"decision":"dismiss"}}' hx-target="#concern-{chapter_key}-{idx}" hx-swap="outerHTML"
                class="text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-400
                       border border-slate-700 rounded-lg px-3 py-1.5 transition-colors cursor-pointer">
          Dismiss — Intentional Choice
        </button>
      </div>"""

    return f"""
<div id="concern-{chapter_key}-{idx}"
     class="px-5 py-3.5 border rounded-none transition-opacity {container_cls}">
  <div class="flex items-start gap-3">
    <div class="flex-shrink-0 w-40">
      <div class="text-xs font-semibold uppercase tracking-wide">{icon} {sev.title()}</div>
      <div class="text-xs mt-0.5">{cat_display} <span class="text-slate-600 ml-1">¶{para_idx}</span></div>
    </div>
    <div class="flex-1 min-w-0">
      <div class="text-xs font-mono leading-relaxed break-words concern-context {text_strike}">{context}</div>
      <div class="text-xs text-slate-500 mt-1 {text_strike}">
        <span class="text-slate-600">→</span> {explanation}
      </div>
      {suggestion_html}
    </div>
  </div>
  <div id="concern-actions-{chapter_key}-{idx}" class="mt-2.5 ml-[10.5rem]">
    {actions}
  </div>
</div>"""


# ── GET — HTMX chapter detail fragment ────────────────────────────────────────

@router.get(
    "/projects/{project_id}/moral-impact/chapter/{chapter_key}",
    response_class=HTMLResponse,
)
async def moral_impact_chapter_fragment(
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

    chapter_data = _load_chapter_concerns(project_id, chapter_key)
    if chapter_data is None:
        return HTMLResponse(
            content='<p class="text-slate-500 text-xs p-4">No concerns data for this chapter.</p>'
        )

    return templates.TemplateResponse(
        "moral_impact_chapter_concerns.html",
        {
            "request":      request,
            "project":      project,
            "chapter_data": chapter_data,
            "chapter_key":  chapter_key,
        }
    )
