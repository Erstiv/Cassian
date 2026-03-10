"""
LAYOUT ROUTES
Gives the layout agent (agents/05_layout/layout.py) a web UI.
The agent itself is NOT modified — this module only launches it and
displays its output.

Routes:
  GET  /projects/{project_id}/layout            — status + config + report
  POST /projects/{project_id}/layout/run        — launch agent, block until done
  GET  /projects/{project_id}/layout/report     — HTMX fragment: report panel only
  GET  /projects/{project_id}/layout/download   — serve the generated PDF
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"
LAYOUT_AGENT = CASSIAN_DIR / "agents" / "05_layout" / "layout.py"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _get_layout_status(project_id: int) -> dict:
    """
    Examine disk to determine the current layout state.

    Returns a dict:
      state:   "not_run" | "complete" | "stale"
      report:  parsed layout_report.json dict, or None
      pdf_path: Path to PDF if it exists, or None
    """
    project_dir  = _get_project_dir(project_id)
    report_path  = project_dir / "output" / "formatting" / "layout_report.json"
    editing_dir  = project_dir / "output" / "editing"

    if not report_path.exists():
        return {"state": "not_run", "report": None, "pdf_path": None}

    # Load the report
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "not_run", "report": None, "pdf_path": None}

    # Find the PDF — prefer path in report, fallback to scanning output/final/
    pdf_path = None
    raw_pdf  = report.get("output_pdf", "")
    if raw_pdf:
        candidate = Path(raw_pdf)
        if not candidate.is_absolute() or not candidate.exists():
            # Try as relative to project dir
            candidate = project_dir / raw_pdf
        if candidate.exists():
            pdf_path = candidate

    if pdf_path is None:
        final_dir = project_dir / "output" / "final"
        if final_dir.exists():
            pdfs = sorted(final_dir.glob("*.pdf"))
            if pdfs:
                pdf_path = pdfs[-1]

    # Stale check: any edited chapter newer than the report?
    report_mtime = report_path.stat().st_mtime
    state = "complete"

    if editing_dir.exists():
        for ef in editing_dir.glob("chapter_*_edited.json"):
            try:
                if ef.stat().st_mtime > report_mtime:
                    state = "stale"
                    break
            except Exception:
                pass

    return {"state": state, "report": report, "pdf_path": pdf_path}


def _load_config(project_id: int) -> dict:
    """Load and return the project's config.json, or an empty dict if missing."""
    config_path = _get_project_dir(project_id) / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(project_id: int, config: dict) -> None:
    config_path = _get_project_dir(project_id) / "config.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def _count_available_chapters(project_id: int) -> int:
    """Count how many chapters have been edited (preferred) or ingested."""
    project_dir = _get_project_dir(project_id)
    editing_dir = project_dir / "output" / "editing"
    ingested_dir = project_dir / "output" / "ingested"

    if editing_dir.exists():
        count = len(list(editing_dir.glob("chapter_*_edited.json")))
        if count:
            return count

    if ingested_dir.exists():
        return len(list(ingested_dir.glob("chapter_*.json")))

    return 0


def _count_available_illustrations(project_id: int) -> int:
    """Count unique chapter illustrations (excluding thumbnails and duplicates).

    Only counts one file per chapter — prefers .tif > .png > .jpg.
    Excludes _thumb files.
    """
    images_dir = _get_project_dir(project_id) / "output" / "illustrations" / "images"
    if not images_dir.exists():
        return 0
    # Collect unique chapter keys that have at least one image file
    chapter_keys = set()
    import re
    for f in images_dir.iterdir():
        if f.is_file() and "_thumb" not in f.name:
            m = re.match(r"chapter_(\d+)\.", f.name)
            if m:
                chapter_keys.add(m.group(1))
    return len(chapter_keys)


# ── GET — main layout page ─────────────────────────────────────────────────────

@router.get("/projects/{project_id}/layout", response_class=HTMLResponse)
async def layout_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    error:      str = None,
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    layout_status      = _get_layout_status(project_id)
    config             = _load_config(project_id)
    chapter_count      = _count_available_chapters(project_id)
    illustration_count = _count_available_illustrations(project_id)

    return templates.TemplateResponse(
        "layout.html",
        {
            "request":            request,
            "project":            project,
            "active_page":        "layout",
            "status":             layout_status["state"],
            "report":             layout_status["report"],
            "pdf_path":           str(layout_status["pdf_path"]) if layout_status["pdf_path"] else None,
            "config":             config,
            "chapter_count":      chapter_count,
            "illustration_count": illustration_count,
            "error":              error,
        }
    )


# ── Background task tracking ──────────────────────────────────────────────────
_running_tasks: dict[int, dict] = {}


def _progress_file(project_id: int) -> Path:
    """Path to the ephemeral progress JSON for a running layout agent."""
    return _get_project_dir(project_id) / "output" / "formatting" / ".progress.json"


def _write_progress(project_id: int, data: dict):
    pf = _progress_file(project_id)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data), encoding="utf-8")


async def _monitor_layout(project_id: int, proc, total_chapters: int):
    """Read stdout lines from the layout agent and update a progress file."""
    done = 0
    current_chapter = ""
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            # Generic parsing — look for chapter markers
            if "Chapter" in line and ("──" in line or line.count("─") > 2):
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.lower() == "chapter" and i + 1 < len(parts):
                        current_chapter = parts[i + 1].strip("─")
                        break
            elif "Processing" in line or "Laying out" in line or "Formatting" in line:
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Processing chapter {current_chapter}…",
                })
            elif "✓" in line or "Done" in line.lower() or "complete" in line.lower():
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
        else:
            _write_progress(project_id, {
                "state": "complete",
                "done": total_chapters,
                "total": total_chapters,
                "current": "",
                "message": "Layout generation complete.",
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


# ── GET/POST — run the layout agent ────────────────────────────────────────────

@router.get("/projects/{project_id}/layout/run")
@router.post("/projects/{project_id}/layout/run")
async def layout_run(
    project_id:         int,
    request:            Request,
    db:                 Session = Depends(get_db),
    format:             str  = "hardcover",       # noqa: A002 — matches template param name
    single_chapter:     str  = None,
    skip_illustrations: str = "0",
):
    # For POST, try to get from Form; for GET, they're already from query params
    if request.method == "POST":
        form_data = await request.form()
        format = form_data.get("format", format)
        single_chapter = form_data.get("single_chapter", single_chapter)
        skip_illustrations = form_data.get("skip_illustrations", skip_illustrations)

    # Rename to avoid shadowing Python's built-in format()
    output_format = format

    # Convert skip_illustrations to bool
    skip_illustrations = skip_illustrations in ("1", "true", True)
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # If already running, redirect to progress page
    if project_id in _running_tasks:
        return RedirectResponse(
            f"/projects/{project_id}/layout/progress", status_code=303,
        )

    project_dir = _get_project_dir(project_id)

    # ── Sync config.json with DB values + selected format ───────────────────
    config = _load_config(project_id)
    if "formatting" not in config:
        config["formatting"] = {}
    config["formatting"]["default_format"] = output_format

    # Keep book title and author in sync with database
    if "book" not in config:
        config["book"] = {}
    config["book"]["title"]  = project.name
    config["book"]["author"] = project.author
    try:
        _save_config(project_id, config)
    except Exception as exc:
        return RedirectResponse(
            f"/projects/{project_id}/layout?error=Could+not+save+config:+{exc}",
            status_code=303,
        )

    # ── Build the command ─────────────────────────────────────────────────────
    if not LAYOUT_AGENT.exists():
        return RedirectResponse(
            f"/projects/{project_id}/layout?error=Layout+agent+not+found+at+{LAYOUT_AGENT}",
            status_code=303,
        )

    # Count chapters for progress tracking
    chapter_count = _count_available_chapters(project_id)
    total_chapters = chapter_count or 1

    cmd = [sys.executable, "-u", str(LAYOUT_AGENT)]
    if single_chapter and single_chapter.strip():
        cmd.extend(["--chapter", single_chapter.strip()])
        total_chapters = 1
    if skip_illustrations:
        cmd.append("--no-illustrations")

    env = {**os.environ, "CASSIAN_PROJECT_DIR": str(project_dir), "PYTHONUNBUFFERED": "1"}

    # Initialize progress file
    _write_progress(project_id, {
        "state": "running",
        "done": 0,
        "total": total_chapters,
        "current": "",
        "message": "Starting layout generation…",
    })

    # ── Run async so the event loop stays free for other requests ──────────────
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
            f"/projects/{project_id}/layout?error=Failed+to+launch+agent:+{exc}",
            status_code=303,
        )

    # Store task reference and launch background monitor
    _running_tasks[project_id] = {"proc": proc, "total": total_chapters}
    asyncio.create_task(_monitor_layout(project_id, proc, total_chapters))

    # Redirect immediately to the progress page
    return RedirectResponse(
        f"/projects/{project_id}/layout/progress", status_code=303,
    )


# ── GET — progress page (shown while agent runs) ─────────────────────────────

@router.get("/projects/{project_id}/layout/progress", response_class=HTMLResponse)
async def layout_progress_page(
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
        return RedirectResponse(f"/projects/{project_id}/layout", status_code=303)

    # If already complete or errored, redirect to main page
    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return RedirectResponse(
                f"/projects/{project_id}/layout?error={err}", status_code=303,
            )
        return RedirectResponse(f"/projects/{project_id}/layout", status_code=303)

    return templates.TemplateResponse(
        "agent_progress.html",
        {
            "request":          request,
            "project":          project,
            "agent_name":       "layout",
            "agent_description": "Layout Generation",
            "progress":         progress,
            "poll_url":         f"/projects/{project_id}/layout/progress/poll",
            "back_url":         f"/projects/{project_id}/layout",
        },
    )


# ── GET — HTMX polling endpoint for progress bar updates ─────────────────────

@router.get("/projects/{project_id}/layout/progress/poll", response_class=HTMLResponse)
async def layout_progress_poll(
    project_id: int,
    request:    Request,
):
    pf = _progress_file(project_id)
    if not pf.exists():
        # Done — tell HTMX to redirect
        return HTMLResponse(
            content='<div hx-get="REDIRECT" hx-trigger="load"></div>',
            headers={"HX-Redirect": f"/projects/{project_id}/layout"},
        )

    progress = json.loads(pf.read_text(encoding="utf-8"))

    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": f"/projects/{project_id}/layout?error={err}"},
            )
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": f"/projects/{project_id}/layout"},
        )

    done  = progress.get("done", 0)
    total = progress.get("total", 1)
    pct   = round(done / total * 100) if total else 0
    msg   = progress.get("message", "Processing…")

    return HTMLResponse(f"""
    <div id="progress-content"
         hx-get="/projects/{project_id}/layout/progress/poll"
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


# ── GET — HTMX report fragment ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/layout/report", response_class=HTMLResponse)
async def layout_report_fragment(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    layout_status = _get_layout_status(project_id)

    return templates.TemplateResponse(
        "layout.html",
        {
            "request":  request,
            "project":  project,
            "status":   layout_status["state"],
            "report":   layout_status["report"],
            "pdf_path": str(layout_status["pdf_path"]) if layout_status["pdf_path"] else None,
            "config":   _load_config(project_id),
            "fragment": True,
        }
    )


# ── GET — download the generated PDF ──────────────────────────────────────────

@router.get("/projects/{project_id}/layout/download")
async def layout_download(
    project_id: int,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    layout_status = _get_layout_status(project_id)
    pdf_path      = layout_status.get("pdf_path")

    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF not found — run the layout agent first.")

    from starlette.responses import FileResponse
    pdf_path  = Path(pdf_path)
    safe_name = pdf_path.name or "book_layout.pdf"
    return FileResponse(
        path       = str(pdf_path),
        media_type = "application/pdf",
        filename   = safe_name,
    )
