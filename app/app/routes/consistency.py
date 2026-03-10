"""
CONSISTENCY EDITOR ROUTES — app/app/routes/consistency.py

Gives the consistency agent (agents/02_consistency/consistency.py) a web UI.
Includes AI auto-fix: each issue gets a "Fix" button that sends the issue +
chapter text to Gemini, previews the fix, and applies it to the working copy.

Routes:
  GET  /projects/{project_id}/consistency                — main page
  POST /projects/{project_id}/consistency/run            — run agent
  POST /projects/{project_id}/consistency/fix/preview    — AI generates a fix preview (HTMX)
  POST /projects/{project_id}/consistency/fix/apply      — apply previewed fix to working copy (HTMX)
"""

import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR        = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR       = CASSIAN_DIR / "projects"
CONSISTENCY_AGENT  = CASSIAN_DIR / "agents" / "02_consistency" / "consistency.py"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _get_consistency_status(project_id: int) -> str:
    """
    Returns: "not_run" | "complete" | "stale"

    Stale = report exists but at least one chapter source file is newer.
    """
    project_dir = _get_project_dir(project_id)
    report_path = project_dir / "output" / "consistency" / "consistency_report.json"

    if not report_path.exists():
        return "not_run"

    report_mtime = report_path.stat().st_mtime

    # Check editing and ingested directories for anything newer.
    # NOTE: workbench is excluded because auto-fix writes there — including it
    # causes the report to appear "stale" immediately after every fix.
    for subdir in ("output/editing", "output/ingested"):
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
    """Load consistency_report.json, or None if missing/unreadable."""
    report_path = _get_project_dir(project_id) / "output" / "consistency" / "consistency_report.json"
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _summarise_report(report: dict) -> dict:
    """
    Extract quick-stats from the full report for the summary bar.

    IMPORTANT: We compute counts from the actual issue arrays, NOT from
    Gemini's self-reported total_issues_found / severity_counts, because
    Gemini frequently gets its own math wrong.
    """
    category_counts = {}
    high = medium = low = 0
    total = 0

    for cat in ("character_issues", "world_issues", "timeline_issues",
                "tone_issues", "world_rule_violations", "structural_notes"):
        items = report.get(cat, [])
        if items:
            category_counts[cat] = len(items)
            total += len(items)
            for item in items:
                sev = item.get("severity", "low").lower()
                if sev == "high":
                    high += 1
                elif sev == "medium":
                    medium += 1
                else:
                    low += 1

    return {
        "total_issues":     total,
        "high":             high,
        "medium":           medium,
        "low":              low,
        "category_counts":  category_counts,
        "positive_count":   len(report.get("positive_observations", [])),
    }


def _discover_chapter_keys(project_id: int) -> list[str]:
    """
    Return sorted chapter keys from available source dirs.
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


# ── GET — main consistency page ────────────────────────────────────────────────

@router.get("/projects/{project_id}/consistency", response_class=HTMLResponse)
async def consistency_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    error:      str = None,
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    status       = _get_consistency_status(project_id)
    report       = _load_report(project_id) if status != "not_run" else None
    stats        = _summarise_report(report) if report else None
    chapter_keys = _discover_chapter_keys(project_id)

    return templates.TemplateResponse(
        "consistency.html",
        {
            "request":      request,
            "project":      project,
            "active_page":  "consistency",
            "status":       status,
            "report":       report,
            "stats":        stats,
            "chapter_keys": chapter_keys,
            "error":        error,
        }
    )


# ── Background task tracking ──────────────────────────────────────────────────
_running_tasks: dict[int, dict] = {}


def _progress_file(project_id: int) -> Path:
    """Path to the ephemeral progress JSON for a running consistency checker."""
    return _get_project_dir(project_id) / "output" / "consistency" / ".progress.json"


def _write_progress(project_id: int, data: dict):
    pf = _progress_file(project_id)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data), encoding="utf-8")


async def _monitor_consistency(project_id: int, proc, total_chapters: int):
    """Read stdout lines from the consistency checker and update a progress file."""
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
            elif "Analyzing" in line or "Checking" in line:
                _write_progress(project_id, {
                    "state": "running",
                    "done": done,
                    "total": total_chapters,
                    "current": current_chapter,
                    "message": f"Analyzing chapter {current_chapter}…",
                })
            elif "issues" in line.lower() or "found" in line.lower():
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
                "message": "Consistency check complete.",
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


# ── POST — run the consistency agent ──────────────────────────────────────────

@router.get("/projects/{project_id}/consistency/run")
@router.post("/projects/{project_id}/consistency/run")
async def consistency_run(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # If already running, redirect to progress page
    if project_id in _running_tasks:
        return RedirectResponse(
            f"/projects/{project_id}/consistency/progress", status_code=303,
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
            f"/projects/{project_id}/consistency?error=No+chapters+found.+Run+Intake+or+Draft+Writer+first.",
            status_code=303,
        )

    if not CONSISTENCY_AGENT.exists():
        return RedirectResponse(
            f"/projects/{project_id}/consistency?error=Consistency+agent+not+found+at+{CONSISTENCY_AGENT}",
            status_code=303,
        )

    # Ensure config.json exists — Draft Writer projects don't create one.
    # The agent needs it for book title/author in the prompt.
    config_path = project_dir / "config.json"
    if not config_path.exists():
        minimal_config = {
            "book": {
                "title": project.name or "Untitled",
                "author": project.author or "Unknown Author",
            },
            "gemini": {
                "api_key": "",  # agent will fall back to GEMINI_API_KEY env var
                "models": {"text": "gemini-2.5-flash"},
            },
        }
        config_path.write_text(json.dumps(minimal_config, indent=2), encoding="utf-8")

    # Count chapters for progress tracking
    chapter_keys = _discover_chapter_keys(project_id)
    total_chapters = len(chapter_keys) or 1

    # Build command — run async so the event loop stays free for other requests
    cmd = [sys.executable, "-u", str(CONSISTENCY_AGENT)]
    env = {**os.environ, "CASSIAN_PROJECT_DIR": str(project_dir), "PYTHONUNBUFFERED": "1"}

    # Initialize progress file
    _write_progress(project_id, {
        "state": "running",
        "done": 0,
        "total": total_chapters,
        "current": "",
        "message": "Starting consistency check…",
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
            f"/projects/{project_id}/consistency?error=Failed+to+launch+agent:+{exc}",
            status_code=303,
        )

    # Store task reference and launch background monitor
    _running_tasks[project_id] = {"proc": proc, "total": total_chapters}
    asyncio.create_task(_monitor_consistency(project_id, proc, total_chapters))

    # Redirect immediately to the progress page
    return RedirectResponse(
        f"/projects/{project_id}/consistency/progress", status_code=303,
    )


# ── GET — progress page (shown while agent runs) ─────────────────────────────

@router.get("/projects/{project_id}/consistency/progress", response_class=HTMLResponse)
async def consistency_progress_page(
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
        return RedirectResponse(f"/projects/{project_id}/consistency", status_code=303)

    # If already complete or errored, redirect to main page
    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return RedirectResponse(
                f"/projects/{project_id}/consistency?error={err}", status_code=303,
            )
        return RedirectResponse(f"/projects/{project_id}/consistency", status_code=303)

    return templates.TemplateResponse(
        "agent_progress.html",
        {
            "request":          request,
            "project":          project,
            "agent_name":       "consistency",
            "agent_description": "Consistency Check",
            "progress":         progress,
            "poll_url":         f"/projects/{project_id}/consistency/progress/poll",
            "back_url":         f"/projects/{project_id}/consistency",
        },
    )


# ── GET — HTMX polling endpoint for progress bar updates ─────────────────────

@router.get("/projects/{project_id}/consistency/progress/poll", response_class=HTMLResponse)
async def consistency_progress_poll(
    project_id: int,
    request:    Request,
):
    pf = _progress_file(project_id)
    if not pf.exists():
        # Done — tell HTMX to redirect
        return HTMLResponse(
            content='<div hx-get="REDIRECT" hx-trigger="load"></div>',
            headers={"HX-Redirect": f"/projects/{project_id}/consistency"},
        )

    progress = json.loads(pf.read_text(encoding="utf-8"))

    if progress.get("state") in ("complete", "error"):
        pf.unlink(missing_ok=True)
        if progress["state"] == "error":
            err = progress.get("message", "Unknown error")[:300]
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": f"/projects/{project_id}/consistency?error={err}"},
            )
        return HTMLResponse(
            content="",
            headers={"HX-Redirect": f"/projects/{project_id}/consistency"},
        )

    done  = progress.get("done", 0)
    total = progress.get("total", 1)
    pct   = round(done / total * 100) if total else 0
    msg   = progress.get("message", "Processing…")

    return HTMLResponse(f"""
    <div id="progress-content"
         hx-get="/projects/{project_id}/consistency/progress/poll"
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
    # JSON mode forces raw JSON output — no markdown fences
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
    # Remove trailing commas before } or ] (with optional whitespace)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Strip control characters (except \n \r \t) that break json.loads
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s


def _parse_gemini_json(raw: str):
    """Robust extraction of a JSON object/array from Gemini's response.

    Tries multiple strategies to handle markdown fences, preamble text,
    trailing commas, and other common Gemini formatting quirks.
    Returns parsed dict/list, or None on failure.
    """
    if not raw or not raw.strip():
        return None

    # Strategy 1: try raw string directly
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: extract from ```json ... ``` fences (greedy to handle
    # nested backticks in content — take the LAST ``` as the closer)
    fence_match = re.search(r"```(?:json)?\s*\n?(.*)\n?\s*```", raw, re.DOTALL)
    if fence_match:
        extracted = fence_match.group(1).strip()
        try:
            return json.loads(extracted)
        except (json.JSONDecodeError, ValueError):
            # Try with cleanup
            try:
                return json.loads(_clean_json_string(extracted))
            except (json.JSONDecodeError, ValueError):
                pass

    # Strategy 3: find the first { ... } or [ ... ] block by bracket matching
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


def _get_chapter_keys_for_issue(issue: dict) -> list[str]:
    """Extract chapter keys from an issue dict, normalised to zero-padded strings."""
    nums = []
    if "chapters_affected" in issue:
        nums = issue["chapters_affected"]
    elif "chapter_first" in issue:
        nums = [issue["chapter_first"]]
        if issue.get("chapter_conflict"):
            nums.append(issue["chapter_conflict"])
    elif "chapter" in issue:
        nums = [issue["chapter"]]

    keys = []
    for n in nums:
        if n is None:
            continue
        try:
            int_n = int(n)
            if int_n == 0:
                continue  # chapter 0 means "whole book" — skip it
            keys.append(f"{int_n:02d}")
        except (ValueError, TypeError):
            keys.append(str(n))
    return keys


# ── POST — AI fix preview (HTMX partial) ────────────────────────────────────

@router.post("/projects/{project_id}/consistency/fix/preview", response_class=HTMLResponse)
async def consistency_fix_preview(
    project_id:    int,
    request:       Request,
    section_key:   str = Form(...),
    issue_index:   int = Form(...),
    db:            Session = Depends(get_db),
):
    """
    Looks up the issue from the saved report by section_key + index,
    loads the affected chapters, sends them to Gemini with the fix request,
    returns a before/after preview panel.
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

    # Look up the issue from the saved report
    report = _load_report(project_id)
    if not report:
        return HTMLResponse(_error_html("No consistency report found. Run the check first."), status_code=200)

    issues_list = report.get(section_key, [])
    if issue_index < 0 or issue_index >= len(issues_list):
        return HTMLResponse(_error_html(f"Issue index {issue_index} out of range for {section_key}."), status_code=200)

    issue = issues_list[issue_index]

    # Determine affected chapters
    chapter_keys = _get_chapter_keys_for_issue(issue)
    if not chapter_keys:
        return HTMLResponse(_error_html("No chapter numbers found for this issue."), status_code=200)

    # Load chapter texts
    chapters_text = {}
    for ck in chapter_keys:
        data, _ = _load_chapter_text(project_id, ck)
        if data:
            title = data.get("title", f"Chapter {ck}")
            full_text = data.get("full_text", "")
            if not full_text:
                paras = data.get("paragraphs", [])
                full_text = "\n\n".join(
                    p.get("text", str(p)) if isinstance(p, dict) else str(p)
                    for p in paras
                )
            chapters_text[ck] = f"=== CHAPTER {ck}: {title} ===\n\n{full_text}"

    if not chapters_text:
        return HTMLResponse(_error_html("Could not load any affected chapters."), status_code=200)

    issue_desc = issue.get("issue", "")
    suggested  = issue.get("suggested_fix", "")

    # Process in batches of 2 chapters to avoid token limit on large fixes
    MAX_CHAPTERS_PER_CALL = 2
    chapter_key_list = list(chapters_text.keys())
    all_changes = []
    all_summaries = []

    batches = [chapter_key_list[i:i+MAX_CHAPTERS_PER_CALL]
               for i in range(0, len(chapter_key_list), MAX_CHAPTERS_PER_CALL)]

    for batch_keys in batches:
        batch_text = "\n\n".join(chapters_text[ck] for ck in batch_keys)

        prompt = f"""You are a fiction editor fixing a consistency issue in a novel.

ISSUE: {issue_desc}
SUGGESTED FIX: {suggested}

Below are the affected chapter(s). Find the specific paragraphs that need changing
to resolve this consistency issue. Make MINIMAL changes — only fix what's needed
for consistency. Preserve the author's voice and style.

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
If a chapter doesn't need changes for this issue, return an empty changes array."""

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
            _error_html("Gemini couldn't identify specific paragraphs to fix. You may need to fix this manually in the Workbench."),
            status_code=200,
        )

    # Render the preview panel
    return HTMLResponse(_render_fix_preview(
        project_id, section_key, issue_index, changes, summary
    ))


def _error_html(message: str) -> str:
    return f"""
    <div class="p-4 bg-red-900/30 border border-red-700/50 rounded-xl text-sm text-red-300">
      <span class="text-red-400 font-semibold">Error:</span> {message}
    </div>
    """


def _render_fix_preview(
    project_id: int, section_key: str, issue_index: int,
    changes: list, summary: str
) -> str:
    """Render the before/after preview panel as raw HTML."""
    import html as html_lib

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

    return f"""
    <div class="mt-3 bg-slate-950/60 border border-amber-700/30 rounded-xl p-4">
      <div class="flex items-center justify-between mb-3">
        <div class="text-sm font-semibold text-amber-400">Proposed Fix</div>
        <div class="text-xs text-slate-500">{summary_escaped}</div>
      </div>

      {changes_html}

      <div class="flex items-center gap-3 mt-4">
        <form hx-post="/projects/{project_id}/consistency/fix/apply"
              hx-target="#fix-panel-{section_key}-{issue_index}"
              hx-swap="innerHTML transition:true"
              hx-disabled-elt="find button">
          <input type="hidden" name="changes_b64" value="{changes_b64}">
          <input type="hidden" name="section_key"  value="{section_key}">
          <input type="hidden" name="issue_index"  value="{issue_index}">
          <button type="submit"
                  class="flex items-center gap-1.5 bg-green-600 hover:bg-green-500 text-white
                         font-semibold py-1.5 px-4 rounded-lg transition-colors text-sm
                         disabled:opacity-50 disabled:cursor-wait">
            <span class="apply-label">✓ Apply Fix</span>
            <span class="htmx-indicator text-xs ml-1">Applying…</span>
          </button>
        </form>

        <button onclick="document.getElementById('fix-panel-{section_key}-{issue_index}').innerHTML=''"
                class="text-sm text-slate-500 hover:text-slate-300 transition-colors px-3 py-1.5">
          Dismiss
        </button>
      </div>
    </div>
    """


# ── POST — apply fix to working copy (HTMX partial) ─────────────────────────

@router.post("/projects/{project_id}/consistency/fix/apply", response_class=HTMLResponse)
async def consistency_fix_apply(
    project_id:   int,
    request:      Request,
    changes_b64:  str = Form(None),
    changes_json: str = Form(None),   # legacy fallback
    section_key:  str = Form(...),
    issue_index:  int = Form(...),
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
        report_path = _get_project_dir(project_id) / "output" / "consistency" / "consistency_report.json"
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


# ── POST — batch fix all issues in one section ───────────────────────────────

@router.post("/projects/{project_id}/consistency/fix-section", response_class=HTMLResponse)
async def consistency_fix_section(
    project_id:  int,
    request:     Request,
    section_key: str = Form(...),
    db:          Session = Depends(get_db),
):
    """
    Batch auto-fix for a single section: iterate through all issues in the
    given section_key, generate fixes via Gemini, and apply them.
    """
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    api_key, model = _load_gemini_config(project_id)
    if not api_key:
        return HTMLResponse(
            _error_html("No Gemini API key configured. Check config.json."),
            status_code=200,
        )

    report = _load_report(project_id)
    if not report:
        return HTMLResponse(_error_html("No consistency report found. Run the check first."), status_code=200)

    issues_list = report.get(section_key, [])
    if not issues_list:
        return HTMLResponse(
            '<div class="py-3 text-xs text-slate-500">No issues in this section.</div>',
            status_code=200,
        )

    total_fixed = 0
    total_failed = 0
    fix_log = []

    for issue_idx, issue in enumerate(issues_list):
        issue_desc = issue.get("issue", "")
        suggested = issue.get("suggested_fix", "")

        chapter_keys = _get_chapter_keys_for_issue(issue)
        if not chapter_keys:
            fix_log.append(f"⚠ [{issue_idx}]: no chapters found")
            total_failed += 1
            continue

        chapters_text = {}
        for ck in chapter_keys:
            data, _ = _load_chapter_text(project_id, ck)
            if data:
                title = data.get("title", f"Chapter {ck}")
                full_text = data.get("full_text", "")
                if not full_text:
                    paras = data.get("paragraphs", [])
                    full_text = "\n\n".join(
                        p.get("text", str(p)) if isinstance(p, dict) else str(p)
                        for p in paras
                    )
                chapters_text[ck] = f"=== CHAPTER {ck}: {title} ===\n\n{full_text}"

        if not chapters_text:
            fix_log.append(f"⚠ [{issue_idx}]: could not load chapters")
            total_failed += 1
            continue

        MAX_CHAPTERS_PER_CALL = 2
        chapter_key_list = list(chapters_text.keys())
        all_changes = []

        batches = [chapter_key_list[i:i+MAX_CHAPTERS_PER_CALL]
                   for i in range(0, len(chapter_key_list), MAX_CHAPTERS_PER_CALL)]

        batch_ok = True
        for batch_keys in batches:
            batch_text = "\n\n".join(chapters_text[ck] for ck in batch_keys)

            prompt = f"""You are a fiction editor fixing a consistency issue in a novel.

ISSUE: {issue_desc}
SUGGESTED FIX: {suggested}

Below are the affected chapter(s). Find the specific paragraphs that need changing
to resolve this consistency issue. Make MINIMAL changes — only fix what's needed
for consistency. Preserve the author's voice and style.

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
Use paragraph_index as the 0-based position of the paragraph in the chapter.
If a chapter doesn't need changes for this issue, return an empty changes array."""

            try:
                raw = await asyncio.to_thread(
                    _call_gemini, api_key, model, prompt,
                    json_mode=True,
                    max_tokens=32768,
                )
            except Exception as exc:
                fix_log.append(f"✗ [{issue_idx}]: Gemini error — {exc}")
                total_failed += 1
                batch_ok = False
                break

            batch_data = _parse_gemini_json(raw)
            if batch_data is None:
                fix_log.append(f"✗ [{issue_idx}]: invalid JSON from Gemini")
                total_failed += 1
                batch_ok = False
                break

            all_changes.extend(batch_data.get("changes", []))

        if not batch_ok:
            continue

        if not all_changes:
            fix_log.append(f"⚠ [{issue_idx}]: Gemini returned no changes")
            total_failed += 1
            continue

        # Apply changes using the same 4-strategy matching
        applied = 0
        for change in all_changes:
            chapter_key = change.get("chapter_key", "")
            para_index = change.get("paragraph_index")
            fixed_text = change.get("fixed_text", "")
            orig_text = change.get("original_text", "")

            if not chapter_key or para_index is None or not fixed_text:
                continue

            working = _ensure_working_copy(project_id, chapter_key)
            if working is None:
                continue

            paragraphs = working.get("paragraphs", [])
            para_idx_int = int(para_index)
            orig_lower = orig_text.lower().strip()
            orig_words = set(orig_lower.split())
            matched = False

            if 0 <= para_idx_int < len(paragraphs):
                p_text = paragraphs[para_idx_int].get("text", "")
                p_lower = p_text.lower().strip()
                if (p_lower[:50] == orig_lower[:50]
                        or orig_lower[:30] in p_lower
                        or p_lower[:30] in orig_lower):
                    paragraphs[para_idx_int]["text"] = fixed_text
                    matched = True

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

            if not matched and 0 <= para_idx_int < len(paragraphs):
                paragraphs[para_idx_int]["text"] = fixed_text
                matched = True

            if matched:
                working["paragraphs"] = paragraphs
                working["last_modified"] = datetime.utcnow().isoformat()
                wp = _get_project_dir(project_id) / "output" / "workbench" / f"chapter_{chapter_key}_working.json"
                wp.parent.mkdir(parents=True, exist_ok=True)
                wp.write_text(json.dumps(working, indent=2, ensure_ascii=False), encoding="utf-8")
                applied += 1

        if applied > 0:
            total_fixed += 1
            fix_log.append(f"✓ [{issue_idx}]: applied {applied} change(s)")
        else:
            total_failed += 1
            fix_log.append(f"⚠ [{issue_idx}]: no paragraphs matched")

    # Touch report
    report_path = _get_project_dir(project_id) / "output" / "consistency" / "consistency_report.json"
    if report_path.exists():
        report_path.touch()

    log_html = "".join(f"<li>{entry}</li>" for entry in fix_log)

    if total_fixed > 0:
        return HTMLResponse(f"""
        <div class="py-3">
          <div class="bg-emerald-900/20 border border-emerald-700/40 rounded-lg p-4 text-sm">
            <div class="flex items-center gap-2 mb-2">
              <span class="text-emerald-400">✓</span>
              <span class="text-emerald-300 font-semibold">
                {total_fixed} issue(s) fixed, {total_failed} skipped
              </span>
            </div>
            <ul class="text-xs text-slate-400 space-y-0.5 ml-6 list-none">{log_html}</ul>
            <div class="mt-2 ml-6 text-xs text-slate-500">
              Open <a href="/projects/{project_id}/workbench" class="underline hover:text-slate-300">Workbench</a> to review.
            </div>
          </div>
        </div>""")
    else:
        return HTMLResponse(f"""
        <div class="py-3">
          <div class="bg-red-900/20 border border-red-700/40 rounded-lg p-4 text-sm">
            <div class="flex items-center gap-2 mb-2">
              <span class="text-red-400">✗</span>
              <span class="text-red-300 font-semibold">No issues could be auto-fixed</span>
            </div>
            <ul class="text-xs text-slate-400 space-y-0.5 ml-6 list-none">{log_html}</ul>
          </div>
        </div>""")


# ── POST — batch fix all issues ──────────────────────────────────────────────

@router.post("/projects/{project_id}/consistency/fix-all", response_class=HTMLResponse)
async def consistency_fix_all(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    """
    Batch auto-fix: iterate through all non-structural issues, generate fixes
    via Gemini, and apply them automatically. Returns a summary panel.
    """
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    api_key, model = _load_gemini_config(project_id)
    if not api_key:
        return HTMLResponse(
            _error_html("No Gemini API key configured. Check config.json."),
            status_code=200,
        )

    report = _load_report(project_id)
    if not report:
        return HTMLResponse(_error_html("No consistency report found. Run the check first."), status_code=200)

    # Sections to auto-fix (skip structural_notes — advisory only)
    fixable_sections = [
        "character_issues", "world_issues", "timeline_issues",
        "tone_issues", "world_rule_violations",
    ]

    total_fixed = 0
    total_failed = 0
    fix_log = []

    for section_key in fixable_sections:
        issues_list = report.get(section_key, [])
        if not issues_list:
            continue

        for issue_idx, issue in enumerate(issues_list):
            issue_desc = issue.get("issue", "")
            suggested = issue.get("suggested_fix", "")

            # Get affected chapters
            chapter_keys = _get_chapter_keys_for_issue(issue)
            if not chapter_keys:
                fix_log.append(f"⚠ {section_key}[{issue_idx}]: no chapters found")
                total_failed += 1
                continue

            # Load chapter texts
            chapters_text = {}
            for ck in chapter_keys:
                data, _ = _load_chapter_text(project_id, ck)
                if data:
                    title = data.get("title", f"Chapter {ck}")
                    full_text = data.get("full_text", "")
                    if not full_text:
                        paras = data.get("paragraphs", [])
                        full_text = "\n\n".join(
                            p.get("text", str(p)) if isinstance(p, dict) else str(p)
                            for p in paras
                        )
                    chapters_text[ck] = f"=== CHAPTER {ck}: {title} ===\n\n{full_text}"

            if not chapters_text:
                fix_log.append(f"⚠ {section_key}[{issue_idx}]: could not load chapters")
                total_failed += 1
                continue

            # Batch chapters (max 2 per call)
            MAX_CHAPTERS_PER_CALL = 2
            chapter_key_list = list(chapters_text.keys())
            all_changes = []

            batches = [chapter_key_list[i:i+MAX_CHAPTERS_PER_CALL]
                       for i in range(0, len(chapter_key_list), MAX_CHAPTERS_PER_CALL)]

            batch_ok = True
            for batch_keys in batches:
                batch_text = "\n\n".join(chapters_text[ck] for ck in batch_keys)

                prompt = f"""You are a fiction editor fixing a consistency issue in a novel.

ISSUE: {issue_desc}
SUGGESTED FIX: {suggested}

Below are the affected chapter(s). Find the specific paragraphs that need changing
to resolve this consistency issue. Make MINIMAL changes — only fix what's needed
for consistency. Preserve the author's voice and style.

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
If a chapter doesn't need changes for this issue, return an empty changes array."""

                try:
                    raw = await asyncio.to_thread(
                        _call_gemini, api_key, model, prompt,
                        json_mode=True,
                        max_tokens=32768,
                    )
                except Exception as exc:
                    fix_log.append(f"✗ {section_key}[{issue_idx}]: Gemini error — {exc}")
                    total_failed += 1
                    batch_ok = False
                    break

                batch_data = _parse_gemini_json(raw)
                if batch_data is None:
                    fix_log.append(f"✗ {section_key}[{issue_idx}]: invalid JSON from Gemini")
                    total_failed += 1
                    batch_ok = False
                    break

                all_changes.extend(batch_data.get("changes", []))

            if not batch_ok:
                continue

            if not all_changes:
                fix_log.append(f"⚠ {section_key}[{issue_idx}]: Gemini returned no changes")
                total_failed += 1
                continue

            # Apply changes
            applied = 0
            for change in all_changes:
                chapter_key = change.get("chapter_key", "")
                para_index = change.get("paragraph_index")
                fixed_text = change.get("fixed_text", "")
                orig_text = change.get("original_text", "")

                if not chapter_key or para_index is None or not fixed_text:
                    continue

                working = _ensure_working_copy(project_id, chapter_key)
                if working is None:
                    continue

                paragraphs = working.get("paragraphs", [])
                para_idx_int = int(para_index)
                orig_lower = orig_text.lower().strip()
                orig_words = set(orig_lower.split())
                matched = False

                # Strategy 1: index + text verification
                if 0 <= para_idx_int < len(paragraphs):
                    p_text = paragraphs[para_idx_int].get("text", "")
                    p_lower = p_text.lower().strip()
                    if (p_lower[:50] == orig_lower[:50]
                            or orig_lower[:30] in p_lower
                            or p_lower[:30] in orig_lower):
                        paragraphs[para_idx_int]["text"] = fixed_text
                        matched = True

                # Strategy 2: substring search
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

                # Strategy 3: word overlap
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

                # Strategy 4: trust index
                if not matched and 0 <= para_idx_int < len(paragraphs):
                    paragraphs[para_idx_int]["text"] = fixed_text
                    matched = True

                if matched:
                    working["paragraphs"] = paragraphs
                    working["last_modified"] = datetime.utcnow().isoformat()
                    wp = _get_project_dir(project_id) / "output" / "workbench" / f"chapter_{chapter_key}_working.json"
                    wp.parent.mkdir(parents=True, exist_ok=True)
                    wp.write_text(json.dumps(working, indent=2, ensure_ascii=False), encoding="utf-8")
                    applied += 1

            if applied > 0:
                total_fixed += 1
                fix_log.append(f"✓ {section_key}[{issue_idx}]: applied {applied} change(s)")
            else:
                total_failed += 1
                fix_log.append(f"⚠ {section_key}[{issue_idx}]: no paragraphs matched")

    # Touch report so stale check doesn't trigger
    report_path = _get_project_dir(project_id) / "output" / "consistency" / "consistency_report.json"
    if report_path.exists():
        report_path.touch()

    # Build result HTML
    log_html = "".join(f"<li>{entry}</li>" for entry in fix_log)

    if total_fixed > 0:
        return HTMLResponse(f"""
        <div class="bg-emerald-900/20 border border-emerald-700/40 rounded-xl p-5 text-sm">
          <div class="flex items-center gap-2 mb-2">
            <span class="text-emerald-400 text-lg">✓</span>
            <span class="text-emerald-300 font-semibold">
              Batch Fix Complete — {total_fixed} issue(s) fixed, {total_failed} failed
            </span>
          </div>
          <ul class="text-xs text-slate-400 space-y-1 ml-7 list-none">{log_html}</ul>
          <div class="mt-3 ml-7 text-xs text-slate-500">
            Open <a href="/projects/{project_id}/workbench" class="underline hover:text-slate-300">Workbench</a> to review changes.
          </div>
        </div>""")
    else:
        return HTMLResponse(f"""
        <div class="bg-red-900/20 border border-red-700/40 rounded-xl p-5 text-sm">
          <div class="flex items-center gap-2 mb-2">
            <span class="text-red-400 text-lg">✗</span>
            <span class="text-red-300 font-semibold">
              Batch Fix Failed — no issues could be auto-fixed
            </span>
          </div>
          <ul class="text-xs text-slate-400 space-y-1 ml-7 list-none">{log_html}</ul>
          <div class="mt-3 ml-7 text-xs text-slate-500">
            Try fixing issues individually or use the
            <a href="/projects/{project_id}/workbench" class="underline hover:text-slate-300">Workbench</a>.
          </div>
        </div>""")
