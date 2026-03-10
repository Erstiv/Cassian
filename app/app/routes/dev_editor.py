"""
DEVELOPMENTAL EDITOR ROUTES
Big-picture structural editing: plot arcs, pacing, character development,
POV, theme, and chapter-by-chapter assessment.

This agent is ADVISORY ONLY — it does not modify chapter files.
The author reads the report and decides what to act on.

Routes:
  GET  /projects/{project_id}/dev-editor              — renders the dev editor page
  POST /projects/{project_id}/dev-editor/run          — run the dev editor agent via subprocess
  POST /projects/{project_id}/dev-editor/fix/preview  — AI generates a fix preview (HTMX)
  POST /projects/{project_id}/dev-editor/fix/apply    — apply previewed fix to working copy (HTMX)
"""

import asyncio
import base64
import json
import os
import re
import sys
import html as html_lib
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR       = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR      = CASSIAN_DIR / "projects"
DEV_EDITOR_AGENT  = CASSIAN_DIR / "agents" / "03a_dev_editor" / "dev_editor.py"


def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


# ── Auto-Fix helpers ──────────────────────────────────────────────────────────

def _load_gemini_config(project_id: int) -> tuple[str | None, str]:
    """Read API key + model. Returns (api_key, model_name)."""
    config_path = _get_project_dir(project_id) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    gemini  = config.get("gemini", {})
    api_key = gemini.get("api_key") or os.environ.get("GEMINI_API_KEY")
    model   = gemini.get("models", {}).get("fast", "gemini-2.5-flash")
    return api_key or None, model


def _call_gemini(
    api_key: str,
    model: str,
    prompt: str,
    temperature: float = 0.3,
    json_mode: bool = False,
    max_tokens: int = 16384,
) -> str:
    """Call Gemini synchronously. Raises RuntimeError on failure."""
    from google import genai
    from google.genai import types

    config_kwargs = dict(
        temperature=temperature,
        max_output_tokens=max_tokens,
    )
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"

    client   = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    return response.text.strip()


def _clean_json_string(s: str) -> str:
    """Best-effort cleanup of common Gemini JSON quirks."""
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s


def _parse_gemini_json(raw: str):
    """Robust extraction of a JSON object/array from Gemini's response."""
    if not raw or not raw.strip():
        return None

    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    fence_match = re.search(r"```(?:json)?\s*\n?(.*)\n?\s*```", raw, re.DOTALL)
    if fence_match:
        extracted = fence_match.group(1).strip()
        try:
            return json.loads(extracted)
        except (json.JSONDecodeError, ValueError):
            try:
                return json.loads(_clean_json_string(extracted))
            except (json.JSONDecodeError, ValueError):
                pass

    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = raw.find(start_char)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    candidate = raw[start:i+1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        try:
                            return json.loads(_clean_json_string(candidate))
                        except (json.JSONDecodeError, ValueError):
                            pass
                    break

    return None


def _load_chapter_text(project_id: int, chapter_key: str) -> tuple[dict | None, str]:
    """
    Load a chapter from the best source (working > edited > ingested).
    Returns (chapter_data, source_label) or (None, "").
    """
    pd = _get_project_dir(project_id)

    # Working copy
    wp = pd / "output" / "workbench" / f"chapter_{chapter_key}_working.json"
    if wp.exists():
        try:
            return json.loads(wp.read_text(encoding="utf-8")), "working"
        except Exception:
            pass

    # Edited
    ep = pd / "output" / "editing" / f"chapter_{chapter_key}_edited.json"
    if ep.exists():
        try:
            return json.loads(ep.read_text(encoding="utf-8")), "edited"
        except Exception:
            pass

    # Ingested
    ip = pd / "output" / "ingested" / f"chapter_{chapter_key}.json"
    if ip.exists():
        try:
            return json.loads(ip.read_text(encoding="utf-8")), "ingested"
        except Exception:
            pass

    return None, ""


def _normalise_paragraphs(raw_paragraphs: list) -> list[dict]:
    """Convert paragraph list to [{index, text}] format, skipping empties."""
    result = []
    for idx, p in enumerate(raw_paragraphs):
        text = p.get("text", "").strip() if isinstance(p, dict) else str(p).strip()
        if text:
            result.append({"index": idx, "text": text})
    return result


def _ensure_working_copy(project_id: int, chapter_key: str) -> dict | None:
    """Load or create the workbench working copy for a chapter."""
    pd = _get_project_dir(project_id)
    wp = pd / "output" / "workbench" / f"chapter_{chapter_key}_working.json"

    if wp.exists():
        try:
            return json.loads(wp.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Create from source
    data, source = _load_chapter_text(project_id, chapter_key)
    if data is None:
        return None

    # Normalise to working-copy format
    working = {
        "chapter_key":   chapter_key,
        "title":         data.get("title", f"Chapter {chapter_key}"),
        "source":        source,
        "paragraphs":    _normalise_paragraphs(data.get("paragraphs", [])),
        "last_modified": datetime.utcnow().isoformat(),
    }
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_text(json.dumps(working, indent=2, ensure_ascii=False), encoding="utf-8")
    return working


def _error_html(message: str) -> str:
    return f"""
    <div class="p-4 bg-red-900/30 border border-red-700/50 rounded-xl text-sm text-red-300">
      <span class="text-red-400 font-semibold">Error:</span> {message}
    </div>
    """


def _load_dev_report(project_id: int) -> dict | None:
    """Load dev_report.json if it exists, else return None."""
    report_path = _get_project_dir(project_id) / "output" / "dev_editing" / "dev_report.json"
    if not report_path.exists():
        return None
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _load_chapter_assessments(project_id: int) -> list[dict]:
    """Load all chapter_XX_assessment.json files from output/dev_editing/."""
    dev_dir = _get_project_dir(project_id) / "output" / "dev_editing"
    if not dev_dir.exists():
        return []

    assessments = []
    for path in sorted(dev_dir.glob("chapter_*_assessment.json")):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                assessments.append(json.load(f))
        except Exception:
            continue

    # Also pick up epilogue
    epilogue_path = dev_dir / "epilogue_assessment.json"
    if epilogue_path.exists():
        try:
            with open(epilogue_path, 'r', encoding='utf-8') as f:
                assessments.append(json.load(f))
        except Exception:
            pass

    return assessments


def _summarise_report(report: dict) -> dict:
    """
    Extract quick-stats from the full report for the sidebar panel:
    readiness level, counts of flags, structural recs, etc.
    """
    oa          = report.get("overall_assessment", {})
    struct_recs = report.get("structural_recommendations", [])
    ch_assess   = report.get("chapter_assessments", [])
    plot_holes  = report.get("plot_analysis", {}).get("plot_holes", [])

    # Count high/medium/low structural recs
    rec_counts = {"high": 0, "medium": 0, "low": 0}
    for r in struct_recs:
        pri = r.get("priority", "low")
        rec_counts[pri] = rec_counts.get(pri, 0) + 1

    # Count chapter flags
    total_flags = sum(len(ca.get("flags", [])) for ca in ch_assess)

    # Count chapters by pacing issue
    pacing_issues = sum(
        1 for ca in ch_assess
        if ca.get("pacing") in ("too_slow", "too_fast", "uneven")
    )

    # Count plot holes by severity
    hole_counts = {"high": 0, "medium": 0, "low": 0}
    for h in plot_holes:
        sev = h.get("severity", "low")
        hole_counts[sev] = hole_counts.get(sev, 0) + 1

    return {
        "readiness_level":    oa.get("readiness_level", "unknown"),
        "strengths_count":    len(oa.get("strengths", [])),
        "weaknesses_count":   len(oa.get("weaknesses", [])),
        "struct_recs_high":   rec_counts["high"],
        "struct_recs_medium": rec_counts["medium"],
        "struct_recs_low":    rec_counts["low"],
        "struct_recs_total":  len(struct_recs),
        "total_flags":        total_flags,
        "pacing_issues":      pacing_issues,
        "chapters_assessed":  len(ch_assess),
        "plot_holes_high":    hole_counts["high"],
        "plot_holes_total":   len(plot_holes),
        "characters_analysed": len(report.get("character_analysis", [])),
        "generated_at":       report.get("generated_at", ""),
    }


# ── GET — render the dev editor page ──────────────────────────────────────────

@router.get("/projects/{project_id}/dev-editor", response_class=HTMLResponse)
async def dev_editor_page(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    error: str = None,
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    report      = _load_dev_report(project_id)
    assessments = _load_chapter_assessments(project_id) if report else []
    stats       = _summarise_report(report) if report else None

    return templates.TemplateResponse(
        "dev_editor.html",
        {
            "request":     request,
            "project":     project,
            "report":      report,
            "assessments": assessments,
            "stats":       stats,
            "has_report":  report is not None,
            "error":       error,
        }
    )


# ── Background task tracking ──────────────────────────────────────────────────
_running_tasks: dict[int, dict] = {}


def _progress_file(project_id: int) -> Path:
    """Path to the ephemeral progress JSON for a running dev editor."""
    return _get_project_dir(project_id) / "output" / "dev_editing" / ".progress.json"


def _write_progress(project_id: int, data: dict):
    pf = _progress_file(project_id)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data), encoding="utf-8")


async def _monitor_dev_editor(project_id: int, proc, total_chapters: int):
    """Read stdout lines from the dev editor and update a progress file."""
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
            elif "Analyzing" in line or "Assessing" in line or "Reading" in line:
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Analyzing chapter {current_chapter}…",
                })
            elif "Assessment" in line or "assessment" in line or "flags" in line.lower():
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
                "message": "Developmental editing complete.",
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


# ── POST — run the developmental editor agent ────────────────────────────────

@router.get("/projects/{project_id}/dev-editor/run")
@router.post("/projects/{project_id}/dev-editor/run")
async def dev_editor_run(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # If already running, redirect to progress page
    if project_id in _running_tasks:
        return RedirectResponse(
            f"/projects/{project_id}/dev-editor/progress", status_code=303,
        )

    project_dir = _get_project_dir(project_id)

    # Check that ingestion has run (chapters exist)
    has_chapters = any(
        (project_dir / subdir).exists()
        and list((project_dir / subdir).glob("chapter_*.json"))
        for subdir in ("output/workbench", "output/editing", "output/ingested")
    )
    if not has_chapters:
        return RedirectResponse(
            f"/projects/{project_id}/dev-editor?error=No+chapters+found.+Run+Intake+or+Draft+Writer+first.",
            status_code=303,
        )

    if not DEV_EDITOR_AGENT.exists():
        return RedirectResponse(
            f"/projects/{project_id}/dev-editor?error=Dev+editor+agent+not+found.",
            status_code=303,
        )

    # Ensure config.json exists — Draft Writer projects don't create one.
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
        config_path.write_text(json.dumps(minimal_config, indent=2), encoding="utf-8")

    # Count chapters for progress tracking
    from app.routes.proofread import _discover_chapter_keys
    chapter_keys = _discover_chapter_keys(project_id)
    total_chapters = len(chapter_keys) or 1

    # Build command — run async so the event loop stays free for other requests
    cmd = [sys.executable, "-u", str(DEV_EDITOR_AGENT)]
    env = {**os.environ, "CASSIAN_PROJECT_DIR": str(project_dir), "PYTHONUNBUFFERED": "1"}

    # Initialize progress file
    _write_progress(project_id, {
        "state": "running",
        "done": 0,
        "total": total_chapters,
        "current": "",
        "message": "Starting developmental editor…",
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
            f"/projects/{project_id}/dev-editor?error=Failed+to+launch+agent:+{exc}",
            status_code=303,
        )

    # Store task reference and launch background monitor
    _running_tasks[project_id] = {"proc": proc, "total": total_chapters}
    asyncio.create_task(_monitor_dev_editor(project_id, proc, total_chapters))

    # Redirect immediately to the progress page
    return RedirectResponse(
        f"/projects/{project_id}/dev-editor/progress", status_code=303,
    )


# ── GET — progress page (shown while agent runs) ─────────────────────────────

@router.get("/projects/{project_id}/dev-editor/progress", response_class=HTMLResponse)
async def dev_editor_progress_page(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
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
        return RedirectResponse(f"/projects/{project_id}/dev-editor", status_code=303)

    # If already complete or errored, redirect to main page
    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return RedirectResponse(
                f"/projects/{project_id}/dev-editor?error={err}", status_code=303,
            )
        return RedirectResponse(f"/projects/{project_id}/dev-editor", status_code=303)

    return templates.TemplateResponse(
        "agent_progress.html",
        {
            "request":          request,
            "project":          project,
            "agent_name":       "dev_editor",
            "agent_description": "Developmental Editing",
            "progress":         progress,
            "poll_url":         f"/projects/{project_id}/dev-editor/progress/poll",
            "back_url":         f"/projects/{project_id}/dev-editor",
        },
    )


# ── GET — HTMX polling endpoint for progress bar updates ─────────────────────

@router.get("/projects/{project_id}/dev-editor/progress/poll", response_class=HTMLResponse)
async def dev_editor_progress_poll(
    project_id: int,
    request: Request,
):
    pf = _progress_file(project_id)
    if not pf.exists():
        # Done — tell HTMX to redirect
        return HTMLResponse(
            content='<div hx-get="REDIRECT" hx-trigger="load"></div>',
            headers={"HX-Redirect": f"/projects/{project_id}/dev-editor"},
        )

    progress = json.loads(pf.read_text(encoding="utf-8"))

    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": f"/projects/{project_id}/dev-editor?error={err}"},
            )
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": f"/projects/{project_id}/dev-editor"},
        )

    done  = progress.get("done", 0)
    total = progress.get("total", 1)
    pct   = round(done / total * 100) if total else 0
    msg   = progress.get("message", "Processing…")

    return HTMLResponse(f"""
    <div id="progress-content"
         hx-get="/projects/{project_id}/dev-editor/progress/poll"
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


# ── POST — AI fix preview (HTMX partial) ────────────────────────────────────

@router.post("/projects/{project_id}/dev-editor/fix/preview", response_class=HTMLResponse)
async def dev_editor_fix_preview(
    project_id:    int,
    request:       Request,
    issue_type:    str = Form(...),
    issue_index:   int = Form(...),
    chapter_id:    str = Form(None),
    db:            Session = Depends(get_db),
):
    """
    Generate an AI-powered fix preview for a dev editor issue.
    Issue types: plot_hole, structural_rec, chapter_flag
    """
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    api_key, model = _load_gemini_config(project_id)
    if not api_key:
        return HTMLResponse(
            _error_html("No Gemini API key configured. Check start.sh or config.json."),
            status_code=200,
        )

    # Load the dev report
    report = _load_dev_report(project_id)
    if not report:
        return HTMLResponse(
            _error_html("No dev report found. Run the dev editor agent first."),
            status_code=200,
        )

    # Locate the issue based on type and index
    issue = None
    affected_chapters = []

    if issue_type == "plot_hole":
        plot_holes = report.get("plot_analysis", {}).get("plot_holes", [])
        if issue_index < 0 or issue_index >= len(plot_holes):
            return HTMLResponse(
                _error_html(f"Plot hole index {issue_index} out of range."),
                status_code=200,
            )
        issue = plot_holes[issue_index]
        affected_chapters = issue.get("chapters_affected", [])

    elif issue_type == "structural_rec":
        struct_recs = report.get("structural_recommendations", [])
        if issue_index < 0 or issue_index >= len(struct_recs):
            return HTMLResponse(
                _error_html(f"Structural recommendation index {issue_index} out of range."),
                status_code=200,
            )
        issue = struct_recs[issue_index]
        affected_chapters = issue.get("chapters_affected", [])

    elif issue_type == "pacing":
        # Pacing issue for a specific chapter
        if not chapter_id:
            return HTMLResponse(
                _error_html("chapter_id required for pacing type."),
                status_code=200,
            )
        chapter_pacing = report.get("pacing_analysis", {}).get("chapter_pacing", [])
        pacing_obj = None
        for cp in chapter_pacing:
            if str(cp.get("chapter_id")) == str(chapter_id):
                pacing_obj = cp
                break
        if not pacing_obj:
            return HTMLResponse(
                _error_html(f"Pacing data not found for chapter {chapter_id}."),
                status_code=200,
            )
        # Build a synthetic issue dict from pacing data
        assessment = pacing_obj.get("assessment", "unknown")
        notes = pacing_obj.get("notes", "")
        issue = {
            "description": f"Pacing issue ({assessment}): {notes}",
            "suggested_fix": f"Adjust the pacing of this chapter — it is currently assessed as '{assessment}'. {notes}",
        }
        affected_chapters = [chapter_id]

    elif issue_type == "character_issue":
        # Character issue — index is the character index, chapter_id carries the issue sub-index
        chars = report.get("character_analysis", [])
        if issue_index < 0 or issue_index >= len(chars):
            return HTMLResponse(
                _error_html(f"Character index {issue_index} out of range."),
                status_code=200,
            )
        char_obj = chars[issue_index]
        # chapter_id is used to carry the issue sub-index for characters
        issue_sub_index = int(chapter_id) if chapter_id else 0
        char_issues = char_obj.get("issues", [])
        if issue_sub_index < 0 or issue_sub_index >= len(char_issues):
            return HTMLResponse(
                _error_html(f"Issue index {issue_sub_index} out of range for character {char_obj.get('character_name', '?')}."),
                status_code=200,
            )
        issue = {
            "description": f"Character issue for {char_obj.get('character_name', '?')}: {char_issues[issue_sub_index]}",
            "suggested_fix": char_issues[issue_sub_index],
        }
        affected_chapters = char_obj.get("chapters_present", [])

    elif issue_type == "chapter_flag":
        if not chapter_id:
            return HTMLResponse(
                _error_html("chapter_id required for chapter_flag type."),
                status_code=200,
            )
        ch_assessments = report.get("chapter_assessments", [])
        chapter_obj = None
        for ca in ch_assessments:
            if ca.get("chapter_key") == chapter_id:
                chapter_obj = ca
                break
        if not chapter_obj:
            return HTMLResponse(
                _error_html(f"Chapter {chapter_id} not found in assessments."),
                status_code=200,
            )
        flags = chapter_obj.get("flags", [])
        if issue_index < 0 or issue_index >= len(flags):
            return HTMLResponse(
                _error_html(f"Flag index {issue_index} out of range for chapter {chapter_id}."),
                status_code=200,
            )
        issue = flags[issue_index]
        affected_chapters = [chapter_id]
    else:
        return HTMLResponse(
            _error_html(f"Unknown issue type: {issue_type}"),
            status_code=200,
        )

    if not issue:
        return HTMLResponse(
            _error_html("Could not locate the specified issue."),
            status_code=200,
        )

    # Convert affected_chapters to chapter keys (zero-padded strings if numeric)
    chapter_keys = []
    for ch in affected_chapters:
        if isinstance(ch, int):
            chapter_keys.append(f"{ch:02d}")
        else:
            chapter_keys.append(str(ch))

    if not chapter_keys:
        return HTMLResponse(
            _error_html("No chapters identified for this issue."),
            status_code=200,
        )

    # Load chapter texts
    chapters_text = {}
    for ck in chapter_keys:
        data, _ = _load_chapter_text(project_id, ck)
        if data:
            title = data.get("title", f"Chapter {ck}")
            full_text = data.get("full_text", "")
            if not full_text:
                # Build from paragraphs
                paras = data.get("paragraphs", [])
                full_text = "\n\n".join(
                    p.get("text", str(p)) if isinstance(p, dict) else str(p)
                    for p in paras
                )
            chapters_text[ck] = f"=== CHAPTER {ck}: {title} ===\n\n{full_text}"

    if not chapters_text:
        return HTMLResponse(
            _error_html("Could not load any affected chapters."),
            status_code=200,
        )

    issue_desc = issue.get("issue", issue.get("description", ""))
    suggested  = issue.get("suggested_fix", issue.get("suggestion", ""))

    # For large multi-chapter fixes (>3 chapters), process in batches of 3
    # to avoid blowing the token limit on the response.
    MAX_CHAPTERS_PER_CALL = 2
    chapter_key_list = list(chapters_text.keys())
    all_changes = []
    all_summaries = []

    batches = [chapter_key_list[i:i+MAX_CHAPTERS_PER_CALL]
               for i in range(0, len(chapter_key_list), MAX_CHAPTERS_PER_CALL)]

    for batch_keys in batches:
        batch_text = "\n\n".join(chapters_text[ck] for ck in batch_keys)

        prompt = f"""You are a fiction editor helping to fix a developmental editing issue.

ISSUE TYPE: {issue_type}
ISSUE: {issue_desc}
SUGGESTED FIX: {suggested}

Below are the affected chapter(s). Find the specific paragraphs that need changing
to resolve this issue. Make MINIMAL changes — only fix what's needed.
Preserve the author's voice and style.

{batch_text}

Return your response as JSON with this exact format:
{{
  "changes": [
    {{
      "chapter_key": "01",
      "paragraph_index": 3,
      "original_text": "the exact original paragraph text",
      "fixed_text": "the corrected paragraph text",
      "explanation": "brief note on what changed and why"
    }}
  ],
  "summary": "one-sentence summary of what was fixed"
}}

Return ONLY the JSON. Find the specific paragraphs by matching the issue description.
If the issue spans multiple chapters, include changes for each affected chapter.
Use paragraph_index as the 0-based position of the paragraph in the chapter.
If a chapter doesn't need changes for this issue, return an empty changes array for it."""

        try:
            raw = await asyncio.to_thread(
                _call_gemini, api_key, model, prompt,
                json_mode=True,
                max_tokens=32768,
            )
        except Exception as exc:
            return HTMLResponse(_error_html(f"Gemini API error (batch {batch_keys}): {exc}"), status_code=200)

        batch_data = _parse_gemini_json(raw)

        if batch_data is None:
            snippet = raw[:200].replace('<', '&lt;').replace('>', '&gt;')
            return HTMLResponse(
                _error_html(f"Gemini returned invalid JSON for chapters {batch_keys}. Try again. (Response started with: {snippet}…)"),
                status_code=200,
            )

        all_changes.extend(batch_data.get("changes", []))
        if batch_data.get("summary"):
            all_summaries.append(batch_data["summary"])

    fix_data = {
        "changes": all_changes,
        "summary": "; ".join(all_summaries) if all_summaries else "",
    }

    changes = fix_data.get("changes", [])
    summary = fix_data.get("summary", "")

    if not changes:
        return HTMLResponse(
            _error_html("Gemini couldn't identify specific paragraphs to fix. You may need to fix this manually."),
            status_code=200,
        )

    # Render the preview panel
    return HTMLResponse(_render_fix_preview(
        project_id, issue_type, issue_index, chapter_id, changes, summary
    ))


def _render_fix_preview(
    project_id: int, issue_type: str, issue_index: int, chapter_id: str | None,
    changes: list, summary: str
) -> str:
    """Render the before/after preview panel as raw HTML for dev-editor endpoints."""

    changes_html_parts = []
    for i, change in enumerate(changes):
        ch   = change.get("chapter_key", "?")
        pidx = change.get("paragraph_index", "?")
        orig = html_lib.escape(change.get("original_text", ""))
        fixed = html_lib.escape(change.get("fixed_text", ""))
        expl  = html_lib.escape(change.get("explanation", ""))

        changes_html_parts.append(f"""
        <div class="border border-slate-700/50 rounded-lg overflow-hidden mb-3">
          <div class="px-3 py-2 bg-slate-800/60 text-xs text-slate-400 flex justify-between">
            <span>Chapter {ch}, paragraph {pidx}</span>
            <span class="text-slate-500">{expl}</span>
          </div>
          <div class="grid grid-cols-2 divide-x divide-slate-700/50">
            <div class="p-3">
              <div class="text-xs text-red-400 font-semibold mb-1.5 uppercase tracking-wide">Before</div>
              <div class="text-sm text-slate-400 leading-relaxed">{orig}</div>
            </div>
            <div class="p-3">
              <div class="text-xs text-green-400 font-semibold mb-1.5 uppercase tracking-wide">After</div>
              <div class="text-sm text-slate-200 leading-relaxed">{fixed}</div>
            </div>
          </div>
        </div>
        """)

    changes_html = "\n".join(changes_html_parts)
    # Base64-encode the JSON to avoid HTML/URL escaping issues with complex text
    changes_b64 = base64.b64encode(json.dumps(changes).encode()).decode()
    summary_escaped = html_lib.escape(summary)

    # Build the panel ID based on issue type (must match template IDs)
    if issue_type == "chapter_flag":
        panel_id = f"fix-panel-flag-{chapter_id}-{issue_index}"
    elif issue_type == "pacing":
        panel_id = f"fix-panel-pacing-{chapter_id}"
    elif issue_type == "character_issue":
        panel_id = f"fix-panel-char-{issue_index}-{chapter_id}"
    else:
        panel_id = f"fix-panel-{issue_type}-{issue_index}"

    return f"""
    <div class="mt-3 bg-slate-950/60 border border-amber-700/30 rounded-xl p-4">
      <div class="flex items-center justify-between mb-3">
        <div class="text-sm font-semibold text-amber-400">Proposed Fix</div>
        <div class="text-xs text-slate-500">{summary_escaped}</div>
      </div>

      {changes_html}

      <div class="flex items-center gap-3 mt-4">
        <form hx-post="/projects/{project_id}/dev-editor/fix/apply"
              hx-target="#{panel_id}"
              hx-swap="innerHTML transition:true"
              hx-disabled-elt="find button">
          <input type="hidden" name="changes_b64" value="{changes_b64}">
          <input type="hidden" name="issue_type"  value="{issue_type}">
          <input type="hidden" name="issue_index" value="{issue_index}">
          <input type="hidden" name="chapter_id"  value="{chapter_id or ''}">
          <input type="hidden" name="section_key" value="">
          <button type="submit"
                  class="flex items-center gap-1.5 bg-green-600 hover:bg-green-500 text-white
                         font-semibold py-1.5 px-4 rounded-lg transition-colors text-sm
                         disabled:opacity-50 disabled:cursor-wait">
            <span class="apply-label">✓ Apply Fix</span>
            <span class="htmx-indicator text-xs ml-1">Applying…</span>
          </button>
        </form>

        <button onclick="document.getElementById('{panel_id}').innerHTML=''"
                class="text-sm text-slate-500 hover:text-slate-300 transition-colors px-3 py-1.5">
          Dismiss
        </button>
      </div>
    </div>
    """


# ── POST — apply fix to working copy (HTMX partial) ─────────────────────────

@router.post("/projects/{project_id}/dev-editor/fix/apply", response_class=HTMLResponse)
async def dev_editor_fix_apply(
    project_id:   int,
    request:      Request,
    changes_b64:  str = Form(None),
    changes_json: str = Form(None),   # legacy fallback
    issue_type:   str = Form(None),
    issue_index:  int = Form(None),
    chapter_id:   str = Form(None),
    section_key:  str = Form(None),   # for consistency with caller
    db:           Session = Depends(get_db),
):
    """Apply the previewed changes to the workbench working copies."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        if changes_b64:
            raw_json = base64.b64decode(changes_b64).decode()
            changes = json.loads(raw_json)
        elif changes_json:
            changes = json.loads(changes_json)
        else:
            return HTMLResponse(_error_html("No changes data received."), status_code=200)
    except (json.JSONDecodeError, Exception) as exc:
        return HTMLResponse(_error_html(f"Invalid changes data: {exc}"), status_code=200)

    applied_count = 0
    errors = []

    for change in changes:
        chapter_key = change.get("chapter_key", "")
        para_index  = change.get("paragraph_index")
        fixed_text  = change.get("fixed_text", "")
        orig_text   = change.get("original_text", "")

        if not chapter_key or para_index is None or not fixed_text:
            errors.append(f"Skipped incomplete change for ch {chapter_key}")
            continue

        # Load or create working copy
        working = _ensure_working_copy(project_id, chapter_key)
        if working is None:
            errors.append(f"Could not load chapter {chapter_key}")
            continue

        paragraphs = working.get("paragraphs", [])

        # Try to find the paragraph — multiple strategies from strict to fuzzy
        matched = False
        para_idx_int = int(para_index)
        orig_lower = orig_text.lower().strip()
        orig_words = set(orig_lower.split())

        # Strategy 1: exact index match with text verification
        if 0 <= para_idx_int < len(paragraphs):
            p_text = paragraphs[para_idx_int].get("text", "")
            p_lower = p_text.lower().strip()
            if (p_lower[:50] == orig_lower[:50]
                    or orig_lower[:30] in p_lower
                    or p_lower[:30] in orig_lower):
                paragraphs[para_idx_int]["text"] = fixed_text
                matched = True

        # Strategy 2: substring search — original text start appears in a paragraph
        if not matched and orig_lower:
            for frag_len in [60, 40, 25]:
                orig_frag = orig_lower[:frag_len]
                if not orig_frag:
                    continue
                for i, p in enumerate(paragraphs):
                    if orig_frag in p.get("text", "").lower():
                        paragraphs[i]["text"] = fixed_text
                        matched = True
                        break
                if matched:
                    break

        # Strategy 3: word overlap — find the paragraph sharing the most words
        if not matched and len(orig_words) > 5:
            best_i, best_score = -1, 0
            for i, p in enumerate(paragraphs):
                p_words = set(p.get("text", "").lower().split())
                overlap = len(orig_words & p_words)
                score = overlap / max(len(orig_words), 1)
                if score > best_score and score > 0.5:
                    best_score = score
                    best_i = i
            if best_i >= 0:
                paragraphs[best_i]["text"] = fixed_text
                matched = True

        # Strategy 4: trust the index if within range (last resort)
        if not matched and 0 <= para_idx_int < len(paragraphs):
            paragraphs[para_idx_int]["text"] = fixed_text
            matched = True

        if matched:
            working["paragraphs"] = paragraphs
            working["last_modified"] = datetime.utcnow().isoformat()

            # Save
            wp = _get_project_dir(project_id) / "output" / "workbench" / f"chapter_{chapter_key}_working.json"
            wp.parent.mkdir(parents=True, exist_ok=True)
            wp.write_text(json.dumps(working, indent=2, ensure_ascii=False), encoding="utf-8")
            applied_count += 1
        else:
            errors.append(f"Could not find matching paragraph in ch {chapter_key}")

    # Touch the report so applying fixes doesn't trigger false "stale" status
    if applied_count > 0:
        report_path = _get_project_dir(project_id) / "output" / "dev_editing" / "dev_report.json"
        if report_path.exists():
            report_path.touch()

    # Return success/failure panel
    if applied_count > 0:
        error_note = ""
        if errors:
            error_note = f'<div class="text-xs text-amber-400 mt-2">⚠ {"; ".join(errors)}</div>'

        return HTMLResponse(f"""
        <div class="mt-3 p-4 bg-green-900/20 border border-green-700/40 rounded-xl text-sm">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-green-400 text-lg">✓</span>
            <span class="text-green-400 font-semibold">Fix Applied</span>
          </div>
          <div class="text-green-300/80 text-xs ml-7">
            Applied {applied_count} change{'s' if applied_count != 1 else ''} to working copy.
            Open <a href="/projects/{project_id}/workbench" class="underline hover:text-green-200">Workbench</a> to review.
          </div>
          {error_note}
        </div>
        """)
    else:
        return HTMLResponse(f"""
        <div class="mt-3 p-4 bg-red-900/20 border border-red-700/40 rounded-xl text-sm">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-red-400 text-lg">✗</span>
            <span class="text-red-400 font-semibold">Could not apply fixes</span>
          </div>
          <div class="text-red-300/70 text-xs ml-7">
            {'; '.join(errors) if errors else 'Unknown error — try running the fix again.'}
          </div>
        </div>
        """)
