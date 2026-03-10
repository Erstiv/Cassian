"""
PROOFREADER ROUTES — app/app/routes/proofread.py

Gives the proofreader agent (agents/06_proofreader/proofreader.py) a web UI.
The agent is ADVISORY ONLY — it flags issues but does not modify chapter files.
Users fix issues in the Workbench.

Routes:
  GET  /projects/{project_id}/proofread                               — main page
  POST /projects/{project_id}/proofread/run                           — run agent
  POST /projects/{project_id}/proofread/dismiss/{chapter_key}/{idx}   — dismiss one issue
  GET  /projects/{project_id}/proofread/chapter/{chapter_key}         — HTMX fragment
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
CASSIAN_DIR     = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR    = CASSIAN_DIR / "projects"
PROOFREAD_AGENT = CASSIAN_DIR / "agents" / "06_proofreader" / "proofreader.py"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _get_proofread_status(project_id: int) -> str:
    """
    Returns: "not_run" | "complete" | "stale"

    Stale = report exists but at least one chapter source file is newer.
    """
    project_dir  = _get_project_dir(project_id)
    report_path  = project_dir / "output" / "proofreading" / "proofread_report.json"

    if not report_path.exists():
        return "not_run"

    report_mtime = report_path.stat().st_mtime

    # Check workbench, editing, and ingested directories for anything newer
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
    """Load proofread_report.json, or None if missing/unreadable."""
    report_path = _get_project_dir(project_id) / "output" / "proofreading" / "proofread_report.json"
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_all_chapter_issues(project_id: int) -> list[dict]:
    """Load all chapter_*_issues.json files, sorted by chapter key."""
    proof_dir = _get_project_dir(project_id) / "output" / "proofreading"
    if not proof_dir.exists():
        return []

    def sort_key(p: Path):
        stem = p.stem  # "chapter_01_issues"
        k    = stem.replace("chapter_", "").replace("_issues", "")
        try:
            return (0, int(k))
        except ValueError:
            return (1, k)

    results = []
    for path in sorted(proof_dir.glob("chapter_*_issues.json"), key=sort_key):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return results


def _load_chapter_issues(project_id: int, chapter_key: str) -> dict | None:
    """Load issues for a single chapter."""
    path = (
        _get_project_dir(project_id)
        / "output" / "proofreading"
        / f"chapter_{chapter_key}_issues.json"
    )
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_chapter_issues(project_id: int, chapter_key: str, data: dict) -> None:
    path = (
        _get_project_dir(project_id)
        / "output" / "proofreading"
        / f"chapter_{chapter_key}_issues.json"
    )
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _discover_chapter_keys(project_id: int) -> list[str]:
    """
    Return sorted chapter keys from the most available source dirs.
    Used to populate the single-chapter selector dropdown.
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


# ── GET — main proofread page ──────────────────────────────────────────────────

@router.get("/projects/{project_id}/proofread", response_class=HTMLResponse)
async def proofread_page(
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

    status          = _get_proofread_status(project_id)
    report          = _load_report(project_id) if status != "not_run" else None
    chapter_issues  = _load_all_chapter_issues(project_id) if status != "not_run" else []
    chapter_keys    = _discover_chapter_keys(project_id)

    return templates.TemplateResponse(
        "proofread.html",
        {
            "request":        request,
            "project":        project,
            "active_page":    "proofread",
            "status":         status,
            "report":         report,
            "chapter_issues": chapter_issues,
            "chapter_keys":   chapter_keys,
            "error":          error,
        }
    )


# ── Background task tracking ──────────────────────────────────────────────────
# Simple dict keyed by project_id → { proc, progress_file, total_chapters }
_running_tasks: dict[int, dict] = {}


def _progress_file(project_id: int) -> Path:
    """Path to the ephemeral progress JSON for a running proofreader."""
    return _get_project_dir(project_id) / "output" / "proofreading" / ".progress.json"


def _write_progress(project_id: int, data: dict):
    pf = _progress_file(project_id)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data), encoding="utf-8")


async def _monitor_proofread(project_id: int, proc, total_chapters: int):
    """Read stdout lines from the proofreader and update a progress file.

    The proofreader prints lines like:
        ── Chapter 01 ──
        Proofreading…
        Issues: 5  Rating: 7.5
        ✅ Done — 12 total issues across 10 chapters.
    We parse these to track which chapter is being processed.
    """
    done = 0
    current_chapter = ""
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("── Chapter"):
                # e.g. "── Chapter 03 ──"
                current_chapter = line.replace("──", "").replace("Chapter", "").strip()
            elif line.startswith("Proofreading"):
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Proofreading chapter {current_chapter}…",
                })
            elif line.startswith("Issues:"):
                done += 1
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Finished chapter {current_chapter} — {line}",
                })
            elif "Done" in line and "total issues" in line:
                _write_progress(project_id, {
                    "state": "complete",
                    "done": total_chapters,
                    "total": total_chapters,
                    "current": "",
                    "message": line.strip().lstrip("✅ "),
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
            # Process finished but we didn't parse any completions — still mark done
            _write_progress(project_id, {
                "state": "complete",
                "done": total_chapters,
                "total": total_chapters,
                "current": "",
                "message": "Proofreading complete.",
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


# ── GET/POST — run the proofreader agent ──────────────────────────────────────

@router.get("/projects/{project_id}/proofread/run")
@router.post("/projects/{project_id}/proofread/run")
async def proofread_run(
    project_id:     int,
    request:        Request,
    db:             Session = Depends(get_db),
    single_chapter: str = None,
    chapter:        str = None,        # alias — templates send ?chapter=
):
    # Accept both ?chapter= and ?single_chapter= (templates use the former)
    single_chapter = single_chapter or chapter
    # For POST, try to get from Form; for GET, it's already from query params
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
            f"/projects/{project_id}/proofread/progress", status_code=303,
        )

    project_dir = _get_project_dir(project_id)

    # Check that at least some chapter source files exist
    has_chapters = any(
        (project_dir / subdir).exists()
        and list((project_dir / subdir).glob("chapter_*.json"))
        for subdir in ("output/workbench", "output/editing", "output/ingested")
    )
    if not has_chapters:
        return RedirectResponse(
            f"/projects/{project_id}/proofread?error=No+chapters+found.+Run+the+Intake+agent+first.",
            status_code=303,
        )

    if not PROOFREAD_AGENT.exists():
        return RedirectResponse(
            f"/projects/{project_id}/proofread?error=Proofreader+agent+not+found+at+{PROOFREAD_AGENT}",
            status_code=303,
        )

    # Ensure config.json exists — Draft Writer projects don't create one.
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

    # Build command
    cmd = [sys.executable, "-u", str(PROOFREAD_AGENT)]
    if single_chapter and single_chapter.strip():
        cmd.extend(["--chapter", single_chapter.strip()])

    env = {**os.environ, "CASSIAN_PROJECT_DIR": str(project_dir), "PYTHONUNBUFFERED": "1"}

    # Initialize progress file
    _write_progress(project_id, {
        "state": "running",
        "done": 0,
        "total": total_chapters,
        "current": "",
        "message": "Starting proofreader…",
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
            f"/projects/{project_id}/proofread?error=Failed+to+launch+agent:+{exc}",
            status_code=303,
        )

    # Store task reference and launch background monitor
    _running_tasks[project_id] = {"proc": proc, "total": total_chapters}
    asyncio.create_task(_monitor_proofread(project_id, proc, total_chapters))

    # Redirect immediately to the progress page
    return RedirectResponse(
        f"/projects/{project_id}/proofread/progress", status_code=303,
    )


# ── GET — progress page (shown while agent runs) ─────────────────────────────

@router.get("/projects/{project_id}/proofread/progress", response_class=HTMLResponse)
async def proofread_progress_page(
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

    # Read current progress
    pf = _progress_file(project_id)
    if pf.exists():
        progress = json.loads(pf.read_text(encoding="utf-8"))
    else:
        # No progress file and not running → agent already finished
        return RedirectResponse(f"/projects/{project_id}/proofread", status_code=303)

    # If already complete or errored, redirect to main page
    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return RedirectResponse(
                f"/projects/{project_id}/proofread?error={err}", status_code=303,
            )
        return RedirectResponse(f"/projects/{project_id}/proofread", status_code=303)

    return templates.TemplateResponse(
        "proofread_progress.html",
        {
            "request":  request,
            "project":  project,
            "active_page": "proofread",
            "progress": progress,
        },
    )


# ── GET — HTMX polling endpoint for progress bar updates ─────────────────────

@router.get("/projects/{project_id}/proofread/progress/poll", response_class=HTMLResponse)
async def proofread_progress_poll(
    project_id: int,
    request:    Request,
):
    pf = _progress_file(project_id)
    if not pf.exists():
        # Done — tell HTMX to redirect
        return HTMLResponse(
            content='<div hx-get="REDIRECT" hx-trigger="load"></div>',
            headers={"HX-Redirect": f"/projects/{project_id}/proofread"},
        )

    progress = json.loads(pf.read_text(encoding="utf-8"))

    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": f"/projects/{project_id}/proofread?error={err}"},
            )
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": f"/projects/{project_id}/proofread"},
        )

    done  = progress.get("done", 0)
    total = progress.get("total", 1)
    pct   = round(done / total * 100) if total else 0
    msg   = progress.get("message", "Processing…")

    return HTMLResponse(f"""
    <div id="progress-content"
         hx-get="/projects/{project_id}/proofread/progress/poll"
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


# ── POST — dismiss a single issue ─────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/proofread/dismiss/{chapter_key}/{issue_index}",
    response_class=HTMLResponse,
)
async def proofread_dismiss(
    project_id:  int,
    chapter_key: str,
    issue_index: int,
    request:     Request,
    db:          Session = Depends(get_db),
):

    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    data = _load_chapter_issues(project_id, chapter_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Chapter issues file not found")

    issues = data.get("issues", [])
    if issue_index < 0 or issue_index >= len(issues):
        raise HTTPException(status_code=400, detail="Issue index out of range")

    issues[issue_index]["dismissed"] = True
    _save_chapter_issues(project_id, chapter_key, data)

    issue = issues[issue_index]

    # Return a small HTML fragment showing the dismissed issue (HTMX swap)
    cat      = issue.get("category", "")
    cat_colors = {
        "typo":         "text-red-400    bg-red-900/20    border-red-700/30",
        "repeated_word":"text-orange-400 bg-orange-900/20 border-orange-700/30",
        "homophone":    "text-purple-400 bg-purple-900/20 border-purple-700/30",
        "punctuation":  "text-amber-400  bg-amber-900/20  border-amber-700/30",
        "capitalization":"text-blue-400  bg-blue-900/20   border-blue-700/30",
        "formatting":   "text-slate-400  bg-slate-800/40  border-slate-700/30",
        "continuity":   "text-cyan-400   bg-cyan-900/20   border-cyan-700/30",
    }
    colors = cat_colors.get(cat, "text-slate-400 bg-slate-800 border-slate-700")

    html = f"""
<div class="flex items-start gap-3 p-3 rounded-lg border opacity-40 {colors} line-through">
  <span class="text-xs font-semibold uppercase tracking-wide flex-shrink-0 mt-0.5 opacity-60">
    {cat.replace('_', ' ')} ¶{issue.get('paragraph_index', '?')}
  </span>
  <div class="flex-1 min-w-0">
    <div class="text-xs text-slate-500 font-mono leading-relaxed break-words">
      {issue.get('context', '')}
    </div>
    <div class="text-xs text-slate-600 mt-1">
      → {issue.get('suggestion', '')}
    </div>
  </div>
  <span class="text-xs text-slate-600 flex-shrink-0">Dismissed</span>
</div>
"""
    return HTMLResponse(content=html)


# ── POST — auto-fix a single issue ────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/proofread/fix/{chapter_key}/{issue_index}",
    response_class=HTMLResponse,
)
async def proofread_fix(
    project_id:  int,
    chapter_key: str,
    issue_index: int,
    request:     Request,
    db:          Session = Depends(get_db),
):
    """Apply the proofreader's suggestion directly to the chapter text.

    Finds the bolded text (**original**) in the issue context, locates it in
    the paragraph, and replaces it with the suggestion.
    """
    import re


    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user


    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    # Load issue data
    data = _load_chapter_issues(project_id, chapter_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Chapter issues file not found")

    issues = data.get("issues", [])
    if issue_index < 0 or issue_index >= len(issues):
        raise HTTPException(status_code=400, detail="Issue index out of range")

    issue = issues[issue_index]
    suggestion = (issue.get("suggestion") or "").strip()
    if not suggestion:
        return HTMLResponse(content=_fix_result_html(
            issue, issue_index, chapter_key, success=False,
            message="No suggestion available for this issue.",
        ))

    # Extract the bolded text from context — that's what needs replacing
    context = issue.get("context", "")
    bold_matches = re.findall(r'\*\*(.*?)\*\*', context)
    if not bold_matches:
        return HTMLResponse(content=_fix_result_html(
            issue, issue_index, chapter_key, success=False,
            message="Could not identify the text to replace (no bolded text in context).",
        ))

    original_text = bold_matches[0]

    # Load the chapter working file
    project_dir = _get_project_dir(project_id)
    chapter_file = None
    for subdir in ("output/workbench", "output/editing", "output/ingested"):
        candidates = list((project_dir / subdir).glob(f"chapter_{chapter_key}*.json"))
        if candidates:
            chapter_file = candidates[0]
            break

    if not chapter_file:
        return HTMLResponse(content=_fix_result_html(
            issue, issue_index, chapter_key, success=False,
            message="Chapter file not found on disk.",
        ))

    try:
        chapter_data = json.loads(chapter_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return HTMLResponse(content=_fix_result_html(
            issue, issue_index, chapter_key, success=False,
            message=f"Could not read chapter file: {exc}",
        ))

    paragraphs = chapter_data.get("paragraphs", [])
    para_idx = issue.get("paragraph_index")

    # Try to find and replace in the specific paragraph first, then fall back to scanning all
    replaced = False
    if para_idx is not None and 0 <= para_idx < len(paragraphs):
        para = paragraphs[para_idx]
        if original_text in para.get("text", ""):
            para["text"] = para["text"].replace(original_text, suggestion, 1)
            replaced = True

    if not replaced:
        # Scan all paragraphs as fallback
        for para in paragraphs:
            if original_text in para.get("text", ""):
                para["text"] = para["text"].replace(original_text, suggestion, 1)
                replaced = True
                break

    if not replaced:
        return HTMLResponse(content=_fix_result_html(
            issue, issue_index, chapter_key, success=False,
            message=f"Could not find \"{original_text}\" in chapter text. It may have already been fixed.",
        ))

    # Save the updated chapter file
    chapter_data["last_modified"] = datetime.now(timezone.utc).isoformat()
    chapter_file.write_text(json.dumps(chapter_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Mark the issue as fixed in the proofreading data
    issues[issue_index]["dismissed"] = True
    issues[issue_index]["auto_fixed"] = True
    _save_chapter_issues(project_id, chapter_key, data)

    return HTMLResponse(content=_fix_result_html(
        issue, issue_index, chapter_key, success=True,
        message=f"Fixed: \"{original_text}\" → \"{suggestion}\"",
    ))


def _fix_result_html(issue: dict, idx: int, chapter_key: str,
                     success: bool, message: str) -> str:
    """Return the HTML fragment that replaces the issue row after a fix attempt."""
    cat = issue.get("category", "")
    cat_colors = {
        "typo":         "text-red-400    bg-red-900/20    border-red-700/30",
        "repeated_word":"text-orange-400 bg-orange-900/20 border-orange-700/30",
        "homophone":    "text-purple-400 bg-purple-900/20 border-purple-700/30",
        "punctuation":  "text-amber-400  bg-amber-900/20  border-amber-700/30",
        "capitalization":"text-blue-400  bg-blue-900/20   border-blue-700/30",
        "formatting":   "text-slate-400  bg-slate-800/40  border-slate-700/30",
        "continuity":   "text-cyan-400   bg-cyan-900/20   border-cyan-700/30",
    }
    colors = cat_colors.get(cat, "text-slate-400 bg-slate-800 border-slate-700")

    if success:
        return f"""
<div id="issue-{chapter_key}-{idx}"
     class="flex items-start gap-3 px-5 py-3.5 border rounded-none opacity-50 {colors}">
  <span class="text-xs font-semibold uppercase tracking-wide flex-shrink-0 mt-0.5 w-36">
    {cat.replace('_', ' ')} ¶{issue.get('paragraph_index', '?')}
  </span>
  <div class="flex-1 min-w-0">
    <div class="text-xs text-green-400 font-mono leading-relaxed">
      ✓ {message}
    </div>
  </div>
  <span class="flex-shrink-0 text-xs text-green-400">Fixed</span>
</div>"""
    else:
        return f"""
<div id="issue-{chapter_key}-{idx}"
     class="flex items-start gap-3 px-5 py-3.5 border rounded-none {colors}">
  <span class="text-xs font-semibold uppercase tracking-wide flex-shrink-0 mt-0.5 w-36">
    {cat.replace('_', ' ')} ¶{issue.get('paragraph_index', '?')}
  </span>
  <div class="flex-1 min-w-0">
    <div class="text-xs text-red-400/80 leading-relaxed">
      ✗ {message}
    </div>
  </div>
</div>"""


# ── GET — HTMX chapter detail fragment ────────────────────────────────────────

@router.get(
    "/projects/{project_id}/proofread/chapter/{chapter_key}",
    response_class=HTMLResponse,
)
async def proofread_chapter_fragment(
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

    chapter_data = _load_chapter_issues(project_id, chapter_key)
    if chapter_data is None:
        return HTMLResponse(
            content='<p class="text-slate-500 text-xs p-4">No issues data for this chapter.</p>'
        )

    return templates.TemplateResponse(
        "proofread_chapter_issues.html",
        {
            "request":      request,
            "project":      project,
            "chapter_data": chapter_data,
            "chapter_key":  chapter_key,
        }
    )
