"""
RUN ROUTES
Handles: configure new run, create run, start run, live status polling,
         and run detail page.
"""

from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, PipelineRun, AgentRun, RunStatus, OutputProfile
from app.auth import require_user
import json

from app.pipeline.runner import start_pipeline, resume_pipeline, PROJECTS_DIR, _get_project_dir


def _find_output_pdf(project_dir: Path) -> Path | None:
    """Find the most recently produced PDF in project_dir/output/final/."""
    final_dir = project_dir / "output" / "final"
    if not final_dir.exists():
        return None
    pdfs = sorted(final_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def _read_output_file(path: Path) -> str | None:
    """Read a pipeline output file and return its text, or None if missing."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None

router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

AGENTS = [
    {"num": 1, "name": "Ingestion",    "desc": "Reads your chapter files and converts them into structured data the pipeline can work with.",        "required": False},
    {"num": 2, "name": "Consistency",  "desc": "Reads the full manuscript and flags contradictions — character names, timelines, world-building.",   "required": False},
    {"num": 3, "name": "Editing",      "desc": "Three-tier editing: auto-fixes, AI prose polish, and flagged structural issues.",                    "required": False},
    {"num": 4, "name": "Illustration", "desc": "Analyses each chapter, generates an AI image, and presents it for your approval.",                  "required": False},
    {"num": 5, "name": "Layout",       "desc": "Assembles the print-ready interior PDF using your output profile's trim size and margins.",          "required": False},
    {"num": 6, "name": "Cover",        "desc": "Generates the wraparound cover (front + spine + back) sized to the final page count.",              "required": False},
    {"num": 7, "name": "QC",           "desc": "Final quality check — verifies the PDF meets the publisher's upload specification.",                 "required": False},
]


def _agent_runs_map(run_id: int, db: Session) -> dict:
    """Return {agent_num: AgentRun} for a given pipeline run."""
    rows = db.query(AgentRun).filter(AgentRun.pipeline_run_id == run_id).all()
    return {ar.agent_num: ar for ar in rows}


# ─────────────────────────────────────────────────────────────────
#  NEW RUN FORM  —  GET /projects/{id}/runs/new
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/runs/new", response_class=HTMLResponse)
def new_run_form(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project  = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    profiles = project.output_profiles

    return templates.TemplateResponse("run_new.html", {
        "request":  request,
        "project":  project,
        "profiles": profiles,
        "agents":   AGENTS,
    })


# ─────────────────────────────────────────────────────────────────
#  CREATE RUN  —  POST /projects/{id}/runs
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/runs")
async def create_run(
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

    # ── Which agents are selected ──────────────────────────────────────────────
    agents_selected = []
    for agent in AGENTS:
        if form.get(f"agent_{agent['num']}") == "on":
            agents_selected.append(agent["num"])
    if not agents_selected:
        agents_selected = [1]  # fallback: at least ingest

    # ── Per-agent config ───────────────────────────────────────────────────────
    agent_config = {}

    creativity = form.get("creativity_level", "3")
    agent_config["3"] = {"creativity_level": int(creativity)}

    illus_provider  = form.get("illus_provider",  "imagen3")
    illus_placement = form.get("illus_placement", "chapter_header")
    illus_edge      = form.get("illus_edge",      "straight")
    illus_count     = form.get("illus_count",     "1")
    agent_config["4"] = {
        "provider":                  illus_provider,
        "placement_type":            illus_placement,
        "edge_style":                illus_edge,
        "illustrations_per_chapter": int(illus_count),
    }

    # ── Other settings ─────────────────────────────────────────────────────────
    # Slider outputs 0–100; store as 0.0–1.0
    sensitivity = float(form.get("edit_sensitivity", "50")) / 100.0
    profile_id  = form.get("output_profile_id")
    profile_id  = int(profile_id) if profile_id else None
    run_name    = form.get("run_name", "").strip() or f"Run {len(project.runs) + 1}"

    # ── Create the run record ──────────────────────────────────────────────────
    run = PipelineRun(
        project_id        = project_id,
        output_profile_id = profile_id,
        name              = run_name,
        status            = RunStatus.PENDING,
        agents_selected   = agents_selected,
        agent_config      = agent_config,
        edit_sensitivity  = sensitivity,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    return RedirectResponse(f"/projects/{project_id}/runs/{run.id}", status_code=303)


# ─────────────────────────────────────────────────────────────────
#  START RUN  —  POST /projects/{id}/runs/{run_id}/start
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/runs/{run_id}/start")
async def start_run(
    project_id:       int,
    run_id:           int,
    background_tasks: BackgroundTasks,
    request:          Request,
    db:               Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    run = db.get(PipelineRun, run_id)
    if not run or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="Run not found")

    project = db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")

    if run.status != RunStatus.PENDING:
        # Already started — just redirect back (idempotent)
        return RedirectResponse(f"/projects/{project_id}/runs/{run_id}", status_code=303)

    # Kick off the pipeline in the background.
    # start_pipeline creates its own DB session — don't pass the request's session.
    background_tasks.add_task(start_pipeline, run_id)

    return RedirectResponse(f"/projects/{project_id}/runs/{run_id}", status_code=303)


# ─────────────────────────────────────────────────────────────────
#  LIVE STATUS FRAGMENT  —  GET /projects/{id}/runs/{run_id}/status
#  Called every 3 seconds by HTMX while the pipeline is running.
#  Returns just the pipeline steps section (no full page).
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/runs/{run_id}/status", response_class=HTMLResponse)
def run_status_fragment(
    project_id: int,
    run_id:     int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    run     = db.get(PipelineRun, run_id)
    if not project or not run or run.project_id != project_id or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")

    project_dir = _get_project_dir(project)
    pdf_path    = _find_output_pdf(project_dir)
    pdf_exists  = pdf_path is not None
    pdf_size_mb = round(pdf_path.stat().st_size / (1024 * 1024), 1) if pdf_exists else None

    # Count pending proposals (for the paused-at-3 banner)
    frag_proposals_pending = 0
    frag_proposals_dir = _get_project_dir(project) / "output" / "editing_proposals"
    if frag_proposals_dir.exists():
        for pf in frag_proposals_dir.glob("chapter_*_proposals.json"):
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
                frag_proposals_pending += sum(
                    1 for p in data.get("paragraphs", []) if p.get("approved") is None
                )
            except Exception:
                pass

    response = templates.TemplateResponse("fragments/pipeline_steps.html", {
        "request":           request,
        "project":           project,
        "run":               run,
        "agents":            AGENTS,
        "agent_runs_map":    _agent_runs_map(run_id, db),
        "pdf_exists":        pdf_exists,
        "pdf_size_mb":       pdf_size_mb,
        "proposals_pending": frag_proposals_pending,
    })

    # When the run has finished (complete or failed), force HTMX to reload the
    # full page so the sidebar status badge, "Finished" time, and changelog
    # panels all update — not just the pipeline steps fragment.
    if run.status in (RunStatus.COMPLETE, RunStatus.FAILED):
        response.headers["HX-Refresh"] = "true"

    return response


# ─────────────────────────────────────────────────────────────────
#  PDF DOWNLOAD  —  GET /output/pdf
#  Serves the most-recently-produced layout PDF directly from the
#  book-pipeline/output/final/ folder.
# ─────────────────────────────────────────────────────────────────

@router.get("/output/pdf")
def download_pdf(project_id: int = 0, request: Request = None, db: Session = Depends(get_db)):
    user = None
    if request:
        user = require_user(request, db)
        if isinstance(user, RedirectResponse):
            return user

    if project_id:
        project = db.get(Project, project_id)
        if not project or (user and project.user_id != user.id):
            raise HTTPException(status_code=404, detail="Project not found")
        project_dir = _get_project_dir(project) if project else PROJECTS_DIR / "default"
    else:
        # Fallback: find most recent PDF across all user's projects
        all_pdfs = []
        if user:
            user_project_ids = {p.id for p in db.query(Project).filter(Project.user_id == user.id).all()}
            for p_id in user_project_ids:
                p = PROJECTS_DIR / str(p_id)
                final_dir = p / "output" / "final"
                if final_dir.exists():
                    all_pdfs.extend(final_dir.glob("*.pdf"))
        if not all_pdfs:
            raise HTTPException(status_code=404, detail="No PDF found — run the pipeline first.")
        pdf_path = max(all_pdfs, key=lambda p: p.stat().st_mtime)
        return FileResponse(path=str(pdf_path), media_type="application/pdf", filename=pdf_path.name)

    pdf_path = _find_output_pdf(project_dir)
    if not pdf_path:
        raise HTTPException(status_code=404, detail="No PDF found — run the pipeline first.")
    return FileResponse(
        path        = str(pdf_path),
        media_type  = "application/pdf",
        filename    = pdf_path.name,
    )


# ─────────────────────────────────────────────────────────────────
#  RUN DETAIL  —  GET /projects/{id}/runs/{run_id}
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(
    project_id: int,
    run_id:     int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    run     = db.get(PipelineRun, run_id)

    if not project or not run or run.project_id != project_id or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")

    # Load editor output files if they exist
    project_dir = _get_project_dir(project)
    edit_out = project_dir / "output" / "editing"
    changelog     = _read_output_file(edit_out / "changelog.md")
    flags_review  = _read_output_file(edit_out / "flags_for_review.md")

    # Timestamp of the editing output — so the UI can warn if it's from a different run
    changelog_path = edit_out / "changelog.md"
    changelog_mtime = (
        datetime.fromtimestamp(changelog_path.stat().st_mtime)
        if changelog_path.exists() else None
    )
    # Is this changelog from the current run? Compare mod-time to run start.
    changelog_is_current = (
        changelog_mtime is not None
        and run.started_at is not None
        and changelog_mtime >= run.started_at
    )

    # Check if a PDF was produced
    pdf_path    = _find_output_pdf(project_dir)
    pdf_exists  = pdf_path is not None
    pdf_size_mb = round(pdf_path.stat().st_size / (1024 * 1024), 1) if pdf_exists else None

    # Count pending proposals (for the paused-at-3 banner)
    proposals_pending = 0
    proposals_dir = project_dir / "output" / "editing_proposals"
    if proposals_dir.exists():
        for pf in proposals_dir.glob("chapter_*_proposals.json"):
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
                proposals_pending += sum(
                    1 for p in data.get("paragraphs", []) if p.get("approved") is None
                )
            except Exception:
                pass

    return templates.TemplateResponse("run_detail.html", {
        "request":        request,
        "project":        project,
        "run":            run,
        "agents":         AGENTS,
        "agent_runs_map": _agent_runs_map(run_id, db),
        "changelog":           changelog,
        "flags_review":        flags_review,
        "changelog_mtime":     changelog_mtime,
        "changelog_is_current": changelog_is_current,
        "pdf_exists":          pdf_exists,
        "pdf_size_mb":         pdf_size_mb,
        "proposals_pending":   proposals_pending,
    })


# ─────────────────────────────────────────────────────────────────
#  PROPOSALS REVIEW  —  GET /projects/{id}/runs/{run_id}/proposals
#  Shows all AI-proposed paragraph edits for user approval.
# ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/runs/{run_id}/proposals", response_class=HTMLResponse)
def proposals_page(
    project_id: int,
    run_id:     int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    run     = db.get(PipelineRun, run_id)
    if not project or not run or run.project_id != project_id or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")

    project_dir    = _get_project_dir(project)
    proposals_dir  = project_dir / "output" / "editing_proposals"

    chapters = []
    if proposals_dir.exists():
        for pf in sorted(proposals_dir.glob("chapter_*_proposals.json")):
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
                chapters.append({
                    "file":       pf.name,
                    "chapter_id": data.get("chapter_id", pf.stem),
                    "title":      data.get("title", ""),
                    "confidence": data.get("edit_confidence", ""),
                    "paragraphs": data.get("paragraphs", []),
                    "flagged":    data.get("flagged_items", []),
                })
            except Exception:
                pass

    # Aggregate counts
    total   = sum(len(c["paragraphs"]) for c in chapters)
    accepted  = sum(1 for c in chapters for p in c["paragraphs"] if p.get("approved") is True)
    rejected  = sum(1 for c in chapters for p in c["paragraphs"] if p.get("approved") is False)
    pending   = total - accepted - rejected

    return templates.TemplateResponse("proposals.html", {
        "request":   request,
        "project":   project,
        "run":       run,
        "chapters":  chapters,
        "total":     total,
        "accepted":  accepted,
        "rejected":  rejected,
        "pending":   pending,
    })


# ─────────────────────────────────────────────────────────────────
#  ACCEPT/REJECT ONE PARAGRAPH  —  POST /…/proposals/{chid}/{idx}
#  HTMX endpoint — returns just the updated action buttons for that
#  paragraph (swapped in-place by HTMX).
# ─────────────────────────────────────────────────────────────────

@router.post(
    "/projects/{project_id}/runs/{run_id}/proposals/{chapter_id}/{index}",
    response_class=HTMLResponse,
)
async def review_proposal(
    project_id: int,
    run_id:     int,
    chapter_id: str,
    index:      int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    run     = db.get(PipelineRun, run_id)
    if not project or not run or run.project_id != project_id or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    decision = form.get("decision")   # "accept" | "reject" | "reset" | "edit"
    edited_text = form.get("edited_text", "").strip()

    project_dir   = _get_project_dir(project)
    proposals_dir = project_dir / "output" / "editing_proposals"
    pf = proposals_dir / f"chapter_{chapter_id}_proposals.json"

    if not pf.exists():
        raise HTTPException(status_code=404, detail="Proposals file not found")

    data = json.loads(pf.read_text(encoding="utf-8"))
    paragraphs = data.get("paragraphs", [])

    # Handle edit decision: update the proposed text and mark as accepted
    if decision == "edit" and edited_text:
        for p in paragraphs:
            if p.get("index") == index:
                p["proposed"] = edited_text
                p["approved"] = True
                p["edited_by_user"] = True
                break
        approved_value = True
    else:
        # Update the approved field for the matching paragraph
        approved_value = {"accept": True, "reject": False, "reset": None}.get(decision)
        for p in paragraphs:
            if p.get("index") == index:
                p["approved"] = approved_value
                break

    pf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Return just the updated action buttons for this paragraph
    base_url = f"/projects/{project_id}/runs/{run_id}/proposals/{chapter_id}/{index}"

    edit_btn = (
        f'<button onclick="openEditor(\'{chapter_id}\', {index})" '
        f'class="text-xs text-blue-400 hover:text-blue-300 transition-colors cursor-pointer">'
        f'✎ Edit</button>'
    )

    if approved_value is True:
        html = f"""
<div id="proposal-actions-{chapter_id}-{index}" class="flex items-center gap-2 flex-wrap">
  <span class="inline-flex items-center gap-1 text-xs font-medium text-emerald-400 bg-emerald-900/40 border border-emerald-700/50 rounded-lg px-3 py-1.5">
    ✓ Accepted
  </span>
  <button hx-post="{base_url}" hx-vals='{{"decision":"reset"}}'
          hx-target="#proposal-actions-{chapter_id}-{index}" hx-swap="outerHTML"
          class="text-xs text-slate-500 hover:text-slate-300 transition-colors">
    Undo
  </button>
  {edit_btn}
</div>"""
    elif approved_value is False:
        html = f"""
<div id="proposal-actions-{chapter_id}-{index}" class="flex items-center gap-2 flex-wrap">
  <span class="inline-flex items-center gap-1 text-xs font-medium text-red-400 bg-red-900/40 border border-red-700/50 rounded-lg px-3 py-1.5">
    ✕ Rejected
  </span>
  <button hx-post="{base_url}" hx-vals='{{"decision":"reset"}}'
          hx-target="#proposal-actions-{chapter_id}-{index}" hx-swap="outerHTML"
          class="text-xs text-slate-500 hover:text-slate-300 transition-colors">
    Undo
  </button>
  {edit_btn}
</div>"""
    else:
        html = f"""
<div id="proposal-actions-{chapter_id}-{index}" class="flex items-center gap-2 flex-wrap">
  <button hx-post="{base_url}" hx-vals='{{"decision":"accept"}}'
          hx-target="#proposal-actions-{chapter_id}-{index}" hx-swap="outerHTML"
          class="text-xs font-medium bg-emerald-900/60 hover:bg-emerald-800 text-emerald-300
                 border border-emerald-700/50 rounded-lg px-3 py-1.5 transition-colors cursor-pointer">
    ✓ Accept
  </button>
  <button onclick="openEditor('{chapter_id}', {index})"
          class="text-xs font-medium bg-blue-900/40 hover:bg-blue-900/70 text-blue-400
                 border border-blue-800/40 rounded-lg px-3 py-1.5 transition-colors cursor-pointer">
    ✎ Edit &amp; Accept
  </button>
  <button hx-post="{base_url}" hx-vals='{{"decision":"reject"}}'
          hx-target="#proposal-actions-{chapter_id}-{index}" hx-swap="outerHTML"
          class="text-xs font-medium bg-red-900/40 hover:bg-red-900/70 text-red-400
                 border border-red-800/40 rounded-lg px-3 py-1.5 transition-colors cursor-pointer">
    ✕ Reject
  </button>
</div>"""

    return HTMLResponse(content=html)


# ─────────────────────────────────────────────────────────────────
#  BULK ACCEPT/REJECT  —  POST /…/proposals/{chid}/bulk
#  Accept or reject all proposals in one chapter at once.
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/runs/{run_id}/proposals/{chapter_id}/bulk")
async def bulk_review_chapter(
    project_id: int,
    run_id:     int,
    chapter_id: str,
    request:    Request,
    db:         Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    run     = db.get(PipelineRun, run_id)
    if not project or not run or run.project_id != project_id or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    decision = form.get("decision")
    approved_value = {"accept": True, "reject": False}.get(decision)

    project_dir   = _get_project_dir(project)
    proposals_dir = project_dir / "output" / "editing_proposals"
    pf = proposals_dir / f"chapter_{chapter_id}_proposals.json"

    if pf.exists():
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
            for p in data.get("paragraphs", []):
                p["approved"] = approved_value
            pf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    return RedirectResponse(
        f"/projects/{project_id}/runs/{run_id}/proposals",
        status_code=303,
    )


# ─────────────────────────────────────────────────────────────────
#  APPLY PROPOSALS  —  POST /…/proposals/apply
#  Merges accepted proposals into the editing output, then resumes
#  the pipeline from Agent 4 onwards.
# ─────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/runs/{run_id}/proposals/apply")
async def apply_proposals(
    project_id:       int,
    run_id:           int,
    background_tasks: BackgroundTasks,
    request:          Request,
    db:               Session = Depends(get_db),
):
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    project = db.get(Project, project_id)
    run     = db.get(PipelineRun, run_id)
    if not project or not run or run.project_id != project_id or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")

    project_dir   = _get_project_dir(project)
    proposals_dir = project_dir / "output" / "editing_proposals"
    editing_dir   = project_dir / "output" / "editing"

    if proposals_dir.exists():
        for pf in sorted(proposals_dir.glob("chapter_*_proposals.json")):
            try:
                proposals_data = json.loads(pf.read_text(encoding="utf-8"))
                chapter_id     = proposals_data.get("chapter_id")
                paragraphs     = proposals_data.get("paragraphs", [])

                # Load the corresponding base editing file
                base_file = editing_dir / f"chapter_{chapter_id}_edited.json"
                if not base_file.exists():
                    continue

                base_data = json.loads(base_file.read_text(encoding="utf-8"))

                # Build a lookup of accepted proposals by index
                accepted_map = {
                    p["index"]: p["proposed"]
                    for p in paragraphs
                    if p.get("approved") is True
                }

                if not accepted_map:
                    # Nothing accepted — mark as resolved and continue
                    base_data["proposals_pending"] = False
                    base_data["editing_complete"]  = True
                    base_file.write_text(json.dumps(base_data, indent=2, ensure_ascii=False), encoding="utf-8")
                    continue

                # Apply accepted proposals to the paragraph list
                base_paragraphs = base_data.get("paragraphs", [])
                for i, para in enumerate(base_paragraphs):
                    if i in accepted_map:
                        para["text"] = accepted_map[i]

                base_data["paragraphs"]       = base_paragraphs
                base_data["proposals_pending"] = False
                base_data["editing_complete"]  = True
                base_file.write_text(json.dumps(base_data, indent=2, ensure_ascii=False), encoding="utf-8")

            except Exception:
                pass  # Non-fatal — carry on with remaining chapters

    # Resume the pipeline (runs agents after Agent 3)
    background_tasks.add_task(resume_pipeline, run_id)

    return RedirectResponse(f"/projects/{project_id}/runs/{run_id}", status_code=303)
