"""
COPY & LINE EDITOR ROUTES
Grammar, spelling, punctuation, style (copy editing) and word choice,
rhythm, redundancy, flow (line editing).

The agent produces:
  - Tier 1 base chapters: output/editing/chapter_XX_edited.json
  - Tier 2 proposals:     output/editing_proposals/chapter_XX_proposals.json

Proposal review happens on the EXISTING proposals page in runs.py.
This page shows status and links to that review page — it does not
duplicate the review UI.

Routes:
  GET   /projects/{project_id}/copy-line-editor               — renders status page
  GET/POST /projects/{project_id}/copy-line-editor/run        — run agent inline
  POST  /projects/{project_id}/copy-line-editor/creativity    — save creativity level
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, PipelineRun, RunStatus


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR       = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR      = CASSIAN_DIR / "projects"
COPY_LINE_AGENT   = CASSIAN_DIR / "agents" / "03b_copy_line_editor" / "copy_line_editor.py"


def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _get_editor_status(project_id: int) -> dict:
    """
    Examine disk to determine the current state of the copy/line editing pass.

    Returns a dict with:
      state:           "not_run" | "proposals_pending" | "complete"
      stats:           dict of counts (or None if not run)
      chapters:        list of per-chapter summary dicts
      generated_at:    ISO timestamp of most recent proposals file (or "")
    """
    project_dir   = _get_project_dir(project_id)
    proposals_dir = project_dir / "output" / "editing_proposals"
    editing_dir   = project_dir / "output" / "editing"

    # No proposals directory at all — agent has never run
    if not proposals_dir.exists():
        return {"state": "not_run", "stats": None, "chapters": [], "generated_at": ""}

    proposal_files = sorted(proposals_dir.glob("chapter_*_proposals.json"))
    if not proposal_files:
        return {"state": "not_run", "stats": None, "chapters": [], "generated_at": ""}

    # Load all proposal files
    chapters     = []
    generated_at = ""
    for pf in proposal_files:
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
            chapters.append({
                "chapter_id": data.get("chapter_id", ""),
                "title":      data.get("title", ""),
                "confidence": data.get("edit_confidence", ""),
                "count":      data.get("proposals_count", 0),
                "paragraphs": data.get("paragraphs", []),
                "flagged":    data.get("flagged_items", []),
            })
            ts = data.get("generated_at", "")
            if ts > generated_at:
                generated_at = ts
        except Exception:
            continue

    if not chapters:
        return {"state": "not_run", "stats": None, "chapters": [], "generated_at": ""}

    # Aggregate review progress
    total    = sum(len(c["paragraphs"]) for c in chapters)
    accepted = sum(1 for c in chapters for p in c["paragraphs"] if p.get("approved") is True)
    rejected = sum(1 for c in chapters for p in c["paragraphs"] if p.get("approved") is False)
    pending  = total - accepted - rejected

    # Check how many edited chapter files are marked editing_complete = True
    complete_count = 0
    if editing_dir.exists():
        for ef in editing_dir.glob("chapter_*_edited.json"):
            try:
                data = json.loads(ef.read_text(encoding="utf-8"))
                if data.get("pipeline_status", {}).get("editing_complete"):
                    complete_count += 1
            except Exception:
                pass

    # Determine state
    if pending == 0 and total > 0 and complete_count > 0:
        state = "complete"
    else:
        state = "proposals_pending"

    # Per-chapter summaries (without full paragraph payloads for the template)
    ch_summaries = [
        {
            "chapter_id": c["chapter_id"],
            "title":      c["title"],
            "confidence": c["confidence"],
            "total":      len(c["paragraphs"]),
            "accepted":   sum(1 for p in c["paragraphs"] if p.get("approved") is True),
            "rejected":   sum(1 for p in c["paragraphs"] if p.get("approved") is False),
            "pending":    sum(1 for p in c["paragraphs"] if p.get("approved") is None),
            "flagged":    len(c["flagged"]),
        }
        for c in chapters
    ]

    stats = {
        "chapters_count":   len(chapters),
        "total_proposals":  total,
        "accepted":         accepted,
        "rejected":         rejected,
        "pending":          pending,
        "complete_chapters": complete_count,
    }

    return {
        "state":        state,
        "stats":        stats,
        "chapters":     ch_summaries,
        "generated_at": generated_at,
    }


def _get_creativity_level(project_id: int) -> int:
    """Read current creativity level from config.json. Defaults to 3."""
    config_path = _get_project_dir(project_id) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        return int(config.get("editing", {}).get("creativity_level", 3))
    except Exception:
        return 3


def _set_creativity_level(project_id: int, level: int) -> None:
    """Write creativity level to config.json (creates editing section if missing)."""
    config_path = _get_project_dir(project_id) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    if "editing" not in config:
        config["editing"] = {}
    config["editing"]["creativity_level"] = max(1, min(5, level))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


# ── GET — render the copy & line editor status page ───────────────────────────

@router.get("/projects/{project_id}/copy-line-editor", response_class=HTMLResponse)
async def copy_line_editor_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    error:      str = None,
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    status = _get_editor_status(project_id)
    creativity_level = _get_creativity_level(project_id)

    # Find the most recent pipeline run for this project so we can link to its
    # proposals page.  The proposals review UI lives at:
    #   /projects/{id}/runs/{run_id}/proposals
    latest_run = (
        db.query(PipelineRun)
        .filter(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.id.desc())
        .first()
    )

    # Self-healing: if proposals exist on disk but no PipelineRun record,
    # create one so the Review Proposals button works.
    if not latest_run and status["state"] != "not_run":
        latest_run = PipelineRun(
            project_id=project_id,
            name="Copy & Line Edit",
            status=RunStatus.PAUSED,
            current_agent=3,
            agents_selected=[3],
        )
        db.add(latest_run)
        db.commit()
        db.refresh(latest_run)

    return templates.TemplateResponse(
        "copy_line_editor.html",
        {
            "request":          request,
            "project":          project,
            "status":           status,
            "latest_run":       latest_run,
            "creativity_level": creativity_level,
            "error":            error,
        }
    )


# ── POST — save creativity level ──────────────────────────────────────────────

@router.post("/projects/{project_id}/copy-line-editor/creativity", response_class=HTMLResponse)
async def copy_line_editor_set_creativity(
    project_id: int,
    level:      int = Form(...),
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    _set_creativity_level(project_id, level)
    clamped = max(1, min(5, level))

    labels = {
        1: "Errors only — no rephrasing",
        2: "Errors + minor clarity fixes",
        3: "Errors + flow + tighten prose",
        4: "Active rewriting for rhythm and impact",
        5: "Full prose polish — rewrite freely",
    }
    return HTMLResponse(f"""
    <span class="text-amber-400 font-semibold">{clamped}</span>
    <span class="text-slate-500 ml-1">— {labels.get(clamped, '')}</span>
    """)


# ── Helpers for auto-approve ──────────────────────────────────────────────────

def _auto_approve_proposals(project_id: int) -> int:
    """
    For creativity levels 1-2 (mechanical fixes), auto-approve all proposals
    and apply them to the edited chapter files. Returns total applied count.
    """
    project_dir   = _get_project_dir(project_id)
    proposals_dir = project_dir / "output" / "editing_proposals"
    editing_dir   = project_dir / "output" / "editing"
    applied_total = 0

    if not proposals_dir.exists():
        return 0

    for pf in sorted(proposals_dir.glob("chapter_*_proposals.json")):
        try:
            prop_data = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue

        paragraphs = prop_data.get("paragraphs", [])
        if not paragraphs:
            continue

        # Mark all proposals as approved
        for para in paragraphs:
            para["approved"] = True

        prop_data["paragraphs"] = paragraphs
        pf.write_text(json.dumps(prop_data, indent=2, ensure_ascii=False), encoding="utf-8")

        # Now apply the approved proposals to the edited chapter file
        ch_id = prop_data.get("chapter_id", "")
        if ch_id == "epilogue":
            edited_path = editing_dir / "epilogue_edited.json"
        else:
            padded = str(ch_id).zfill(2)
            edited_path = editing_dir / f"chapter_{padded}_edited.json"

        if not edited_path.exists():
            continue

        try:
            ch_data  = json.loads(edited_path.read_text(encoding="utf-8"))
            ch_paras = ch_data.get("paragraphs", [])
            applied  = 0

            for para in paragraphs:
                idx = para.get("index")
                proposed = para.get("proposed", "")
                if idx is not None and 0 <= idx < len(ch_paras) and proposed:
                    ch_paras[idx]["text"] = proposed
                    applied += 1

            ch_data["paragraphs"] = ch_paras

            # Rebuild full_text from paragraphs
            ch_data["full_text"] = "\n\n".join(
                p.get("text", "") for p in ch_paras if p.get("text", "").strip()
            )

            # Mark as editing complete
            if "pipeline_status" not in ch_data:
                ch_data["pipeline_status"] = {}
            ch_data["pipeline_status"]["editing_complete"] = True
            ch_data["pipeline_status"]["proposals_pending"] = False

            edited_path.write_text(
                json.dumps(ch_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            applied_total += applied

        except Exception:
            continue

    return applied_total


# ── Background task tracking ──────────────────────────────────────────────────
_running_tasks: dict[int, dict] = {}


def _progress_file(project_id: int) -> Path:
    """Path to the ephemeral progress JSON for a running copy-line editor."""
    return _get_project_dir(project_id) / "output" / "editing_proposals" / ".progress.json"


def _write_progress(project_id: int, data: dict):
    pf = _progress_file(project_id)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data), encoding="utf-8")


async def _monitor_copy_line_editor(project_id: int, proc, total_chapters: int):
    """Read stdout lines from the copy-line editor and update a progress file."""
    done = 0
    current_chapter = ""
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            # Look for chapter markers (both standard and variations)
            if "── Chapter" in line or "Chapter" in line and ("──" in line or line.endswith("──")):
                # Extract chapter number
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.lower() == "chapter" and i + 1 < len(parts):
                        current_chapter = parts[i + 1].strip("─")
                        break
            elif "Tier 1" in line or "Tier 2" in line:
                done += 1
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Finished chapter {current_chapter}",
                })
            elif "processed" in line.lower() or "complete" in line.lower():
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": line[:80],
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
        else:
            _write_progress(project_id, {
                "state": "complete",
                "done": total_chapters,
                "total": total_chapters,
                "current": "",
                "message": "Copy & line editing complete.",
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


# ── GET/POST — run the copy & line editor agent ─────────────────────────────

@router.get("/projects/{project_id}/copy-line-editor/run")
@router.post("/projects/{project_id}/copy-line-editor/run")
async def copy_line_editor_run(
    project_id:     int,
    request:        Request,
    db:             Session = Depends(get_db),
    single_chapter: str = None,
    chapter:        str = None,       # alias — templates send ?chapter=
    error:          str = None,
):
    # Accept both param names
    single_chapter = single_chapter or chapter

    if request.method == "POST":
        form_data = await request.form()
        single_chapter = (
            form_data.get("single_chapter", single_chapter)
            or form_data.get("chapter", single_chapter)
        )

    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # If already running, redirect to progress page
    if project_id in _running_tasks:
        return RedirectResponse(
            f"/projects/{project_id}/copy-line-editor/progress", status_code=303,
        )

    project_dir = _get_project_dir(project_id)

    # Check that ingestion has run
    has_chapters = any(
        (project_dir / subdir).exists()
        and list((project_dir / subdir).glob("chapter_*.json"))
        for subdir in ("output/workbench", "output/editing", "output/ingested")
    )
    if not has_chapters:
        return RedirectResponse(
            f"/projects/{project_id}/copy-line-editor?error=No+chapters+found.+Run+Intake+first.",
            status_code=303,
        )

    if not COPY_LINE_AGENT.exists():
        return RedirectResponse(
            f"/projects/{project_id}/copy-line-editor?error=Copy+line+editor+agent+not+found.",
            status_code=303,
        )

    # Ensure config.json exists
    config_path = project_dir / "config.json"
    if not config_path.exists():
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
        config_path.write_text(
            json.dumps(minimal_config, indent=2), encoding="utf-8"
        )

    # Count chapters for progress tracking
    from app.routes.proofread import _discover_chapter_keys
    chapter_keys = _discover_chapter_keys(project_id)
    if single_chapter and single_chapter.strip():
        total_chapters = 1
    else:
        total_chapters = len(chapter_keys) or 1

    # Build command
    cmd = [sys.executable, "-u", str(COPY_LINE_AGENT)]
    if single_chapter and single_chapter.strip():
        cmd.extend(["--chapter", single_chapter.strip()])

    env = {
        **os.environ,
        "CASSIAN_PROJECT_DIR": str(project_dir),
        "PYTHONUNBUFFERED": "1",
    }

    # Initialize progress file
    _write_progress(project_id, {
        "state": "running",
        "done": 0,
        "total": total_chapters,
        "current": "",
        "message": "Starting copy & line editor…",
    })

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(CASSIAN_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,   # capture for progress parsing
            stderr=asyncio.subprocess.PIPE,    # capture errors for web UI
        )
    except Exception as exc:
        _progress_file(project_id).unlink(missing_ok=True)
        return RedirectResponse(
            f"/projects/{project_id}/copy-line-editor?error=Failed+to+launch+agent:+{exc}",
            status_code=303,
        )

    # Store task reference and launch background monitor
    _running_tasks[project_id] = {"proc": proc, "total": total_chapters}
    asyncio.create_task(_monitor_copy_line_editor(project_id, proc, total_chapters))

    # Redirect immediately to the progress page
    return RedirectResponse(
        f"/projects/{project_id}/copy-line-editor/progress", status_code=303,
    )


# ── GET — progress page (shown while agent runs) ─────────────────────────────

@router.get("/projects/{project_id}/copy-line-editor/progress", response_class=HTMLResponse)
async def copy_line_editor_progress_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Read current progress
    pf = _progress_file(project_id)
    if pf.exists():
        progress = json.loads(pf.read_text(encoding="utf-8"))
    else:
        # No progress file and not running → agent already finished
        return RedirectResponse(f"/projects/{project_id}/copy-line-editor", status_code=303)

    # If already complete or errored, redirect to main page
    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return RedirectResponse(
                f"/projects/{project_id}/copy-line-editor?error={err}", status_code=303,
            )
        return RedirectResponse(f"/projects/{project_id}/copy-line-editor", status_code=303)

    return templates.TemplateResponse(
        "agent_progress.html",
        {
            "request":          request,
            "project":          project,
            "agent_name":       "copy_line_editor",
            "agent_description": "Copy & Line Editing",
            "progress":         progress,
            "poll_url":         f"/projects/{project_id}/copy-line-editor/progress/poll",
            "back_url":         f"/projects/{project_id}/copy-line-editor",
        },
    )


# ── GET — HTMX polling endpoint for progress bar updates ─────────────────────

@router.get("/projects/{project_id}/copy-line-editor/progress/poll", response_class=HTMLResponse)
async def copy_line_editor_progress_poll(
    project_id: int,
    request:    Request,
):
    pf = _progress_file(project_id)
    if not pf.exists():
        # Done — tell HTMX to redirect
        return HTMLResponse(
            content='<div hx-get="REDIRECT" hx-trigger="load"></div>',
            headers={"HX-Redirect": f"/projects/{project_id}/copy-line-editor"},
        )

    progress = json.loads(pf.read_text(encoding="utf-8"))

    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": f"/projects/{project_id}/copy-line-editor?error={err}"},
            )
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": f"/projects/{project_id}/copy-line-editor"},
        )

    done  = progress.get("done", 0)
    total = progress.get("total", 1)
    pct   = round(done / total * 100) if total else 0
    msg   = progress.get("message", "Processing…")

    return HTMLResponse(f"""
    <div id="progress-content"
         hx-get="/projects/{project_id}/copy-line-editor/progress/poll"
         hx-trigger="every 2s"
         hx-swap="outerHTML">
      <div class="flex items-center gap-3 mb-2">
        <span class="text-sm text-slate-400">{msg}</span>
        <span class="text-xs text-slate-600 ml-auto">{done} / {total}</span>
      </div>
      <div class="w-full h-2 bg-slate-800 rounded-full overflow-hidden">
        <div class="h-full bg-amber-400 rounded-full transition-all duration-500"
             style="width: {pct}%"></div>
      </div>
    </div>
    """)
