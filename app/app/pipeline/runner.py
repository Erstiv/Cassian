"""
╔══════════════════════════════════════════════════════════════════╗
║  PROJECT CASSIAN — PIPELINE RUNNER                               ║
║                                                                  ║
║  Executes Cassian agents as subprocesses, one at a time.         ║
║  Called as a FastAPI BackgroundTask so it doesn't block the UI.  ║
║                                                                  ║
║  Flow:                                                           ║
║    Agent 1 → 2 → 3 → [PAUSE if Agent 4 selected] → 5            ║
║    Pause at Agent 4 means: run the prompt-generation pass only,  ║
║    then wait for the web UI illustration approval before layout. ║
║                                                                  ║
║  Each AgentRun row is created before execution and updated       ║
║  after — this is what the /status polling endpoint reads.        ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app.database import SessionLocal
from app.models import Project, Chapter, PipelineRun, AgentRun, AgentStatus, RunStatus, ProjectStatus, WorldRule
from app.pipeline.snapshots import create_source_snapshot, take_snapshot


# ── Paths ─────────────────────────────────────────────────────────────────────
# runner.py is at:  Cassian/app/app/pipeline/runner.py
# Cassian root is:  Cassian/app/../..  =  Cassian/
APP_DIR      = Path(__file__).resolve().parent.parent.parent   # Cassian/app/
CASSIAN_DIR  = APP_DIR.parent                                  # Cassian/
PROJECTS_DIR = CASSIAN_DIR / "projects"                        # Cassian/projects/

AGENT_SCRIPTS = {
    1: "agents/01_ingestion/ingest.py",
    2: "agents/02_consistency/consistency.py",
    3: "agents/03_editing/edit.py",
    4: "agents/04_illustration/illustrate.py",
    5: "agents/05_layout/layout.py",
}

AGENT_NAMES = {
    1: "ingestion",
    2: "consistency",
    3: "editing",
    4: "illustration",
    5: "layout",
    6: "cover",
    7: "qc",
}


# ── Project directory helper ───────────────────────────────────────────────────

def _get_project_dir(project: Project) -> Path:
    """Return (and create if needed) the data directory for this project.

    Structure:  Cassian/projects/{project.id}/
                    input/chapters/     ← staged chapter files
                    output/             ← all agent outputs
                    config.json         ← project-specific config
    """
    project_dir = PROJECTS_DIR / str(project.id)
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fail_with_message(run: PipelineRun, message: str, db) -> None:
    """Mark a run as failed and record a synthetic AgentRun with the error."""
    run.status = RunStatus.FAILED
    failed_agent = AgentRun(
        pipeline_run_id = run.id,
        agent_num       = 0,
        agent_name      = "setup",
        status          = AgentStatus.FAILED,
        started_at      = datetime.now(),
        completed_at    = datetime.now(),
        summary         = {"output": message},
    )
    db.add(failed_agent)
    db.commit()


def _stage_chapters(manuscript_dir: str, project_dir: Path,
                    chapter_order: list | None = None) -> tuple[bool, str]:
    """Copy the project's uploaded chapters into project_dir/input/chapters/.

    Files are renamed with a numeric prefix (01_, 02_, …) based on the
    user's drag-and-drop order saved in the project's chapter_order field.
    This lets the ingestion agent determine order from the filename without
    the user having to rename their files.

    Any file on disk that isn't in chapter_order is appended at the end
    alphabetically — so newly uploaded files are never silently dropped.

    Also clears downstream output dirs (ingested, consistency, editing)
    so stale JSONs from a previous run don't bleed into this one.

    Returns (success, message).
    """
    src = Path(manuscript_dir)
    if not src.exists():
        return False, f"Manuscript folder not found: {src}"

    dst = project_dir / "input" / "chapters"
    dst.mkdir(parents=True, exist_ok=True)

    # Clear the input folder
    for old in dst.iterdir():
        if old.is_file():
            old.unlink()

    # Clear downstream output folders so stale JSONs don't bleed through
    for output_subdir in ("ingested", "consistency", "editing"):
        out_path = project_dir / "output" / output_subdir
        if out_path.exists():
            for old in out_path.iterdir():
                if old.is_file():
                    old.unlink()

    # Build the ordered file list
    accepted   = {".docx", ".doc", ".txt", ".pdf", ".epub", ".md"}
    on_disk    = {f.name: f for f in src.iterdir()
                  if f.is_file() and f.suffix.lower() in accepted}

    order = chapter_order or []
    ordered   = [(name, on_disk[name]) for name in order if name in on_disk]
    unordered = sorted(
        [(name, f) for name, f in on_disk.items() if name not in order],
        key=lambda x: x[0].lower()
    )
    final = ordered + unordered

    if not final:
        return False, "No chapter files found in the project's manuscript folder."

    # Copy with 01_, 02_, … prefixes so the ingestion agent gets ordering for free
    for idx, (name, src_file) in enumerate(final, start=1):
        dest_name = f"{idx:02d}_{name}"
        shutil.copy2(src_file, dst / dest_name)

    return True, f"Staged {len(final)} chapter(s) → {dst}"


def _ensure_project_config(project_dir: Path, run: PipelineRun,
                            layout_mode: str = "novel",
                            project_name: str = "",
                            project_author: str = "") -> None:
    """Ensure project_dir/config.json exists and is up to date.

    If no config.json exists in the project dir, copies the template from
    Cassian/projects/template/config.json (if present) or creates a minimal one.
    Then patches it with run-specific settings.
    """
    config_path = project_dir / "config.json"

    # If no config exists for this project, try to seed from a template
    if not config_path.exists():
        template = PROJECTS_DIR / "template" / "config.json"
        if template.exists():
            shutil.copy2(template, config_path)
        else:
            # Create a minimal config
            minimal = {
                "book": {"title": project_name or "Untitled", "author": project_author or ""},
                "gemini": {"api_key": "", "models": {"text": "gemini-2.5-pro", "fast": "gemini-2.5-flash"}},
                "editing": {"creativity_level": 3, "preserve_author_voice": True, "flag_not_edit": []},
                "formatting": {
                    "default_format": "hardcover",
                    "available_formats": {
                        "hardcover": {
                            "trim_width_inches": 6.0, "trim_height_inches": 9.0,
                            "margin_top_inches": 1.0, "margin_bottom_inches": 1.0,
                            "margin_inside_inches": 1.25, "margin_outside_inches": 0.75,
                            "bleed_inches": 0.125, "lulu_product": "hardcover-casewrap"
                        }
                    },
                    "fonts": {"body": "Garamond", "body_size_pt": 11,
                              "chapter_heading": "Garamond", "chapter_heading_size_pt": 24,
                              "line_spacing": 1.4}
                },
                "illustration": {},
                "world_rules": {},
                "layout_mode": layout_mode,
                "paths": {
                    "input_chapters": "input/chapters",
                    "output_ingested": "output/ingested",
                    "output_consistency": "output/consistency",
                    "output_editing": "output/editing",
                    "output_illustrations": "output/illustrations",
                    "output_formatting": "output/formatting",
                    "output_final": "output/final"
                }
            }
            with open(config_path, "w") as f:
                json.dump(minimal, f, indent=2)

    # Now patch with run-specific settings
    try:
        with open(config_path) as f:
            config = json.load(f)

        # Creativity level from run config
        agent3_cfg = (run.agent_config or {}).get("3", {})
        creativity = agent3_cfg.get("creativity_level", 3)
        if "editing" not in config:
            config["editing"] = {}
        config["editing"]["creativity_level"] = creativity

        # Layout mode
        config["layout_mode"] = layout_mode

        # Book title and author — wipe world_rules if title changed
        if project_name or project_author:
            if "book" not in config:
                config["book"] = {}
            if project_name:
                existing_title = config["book"].get("title", "")
                config["book"]["title"] = project_name
                if existing_title and existing_title.lower() != project_name.lower():
                    config["world_rules"] = {}
            if project_author:
                config["book"]["author"] = project_author

        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

    except Exception:
        pass  # Non-fatal — agents will use whatever is on disk


def _run_subprocess(agent_num: int, project_dir: Path,
                    extra_args: list | None = None) -> tuple[bool, str]:
    """Run one agent script as a subprocess.

    Sets CASSIAN_PROJECT_DIR env var so each agent knows which project's
    input/output folders to use.

    Returns (success: bool, log_tail: str).
    The log_tail is the last 4000 characters of stdout + stderr.
    """
    script_rel = AGENT_SCRIPTS.get(agent_num)
    if not script_rel:
        return False, f"No script registered for agent {agent_num}."

    script_path = CASSIAN_DIR / script_rel
    if not script_path.exists():
        return False, f"Agent script not found: {script_path}"

    # Pass project dir to agents via environment variable
    env = os.environ.copy()
    env["CASSIAN_PROJECT_DIR"] = str(project_dir)

    cmd = [sys.executable, str(script_path)] + (extra_args or [])

    try:
        result = subprocess.run(
            cmd,
            cwd=str(CASSIAN_DIR),    # agents resolve paths relative to Cassian root
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,            # 1 hour hard limit per agent
        )
        output = result.stdout
        if result.stderr.strip():
            output += "\n--- STDERR ---\n" + result.stderr
        return result.returncode == 0, output[-4000:].strip()

    except subprocess.TimeoutExpired:
        return False, "Agent timed out after 1 hour."
    except Exception as exc:
        return False, f"Unexpected error launching agent: {exc}"


# ── Post-ingestion: sync Chapter records ──────────────────────────────────────

def _post_ingestion_db_update(project: Project, project_dir: Path, db) -> str:
    """
    Called after Agent 1 (ingestion) completes successfully.

    Reads every chapter_XX.json produced by ingest.py and:
      • Deletes existing Chapter rows for this project (clean slate on re-ingest)
      • Creates a new Chapter row for each ingested JSON
      • Updates project.chapter_count
      • Sets project.status → ACTIVE if it was DRAFT

    Returns a short summary string (appended to the AgentRun log).

    NOTE: This function is intentionally the ONLY place that touches the DB
    after ingestion.  ingest.py remains a pure file-in / file-out script.
    """
    ingested_dir = project_dir / "output" / "ingested"
    if not ingested_dir.exists():
        return "No ingested output directory found — DB sync skipped."

    chapter_files = sorted(
        f for f in ingested_dir.glob("*.json")
        if f.name != "ingestion_summary.json"
    )
    if not chapter_files:
        return "No chapter JSON files found — DB sync skipped."

    # ── Wipe existing Chapter rows (cascades to Illustrations + Edits) ────────
    existing = db.query(Chapter).filter(Chapter.project_id == project.id).all()
    for ch in existing:
        db.delete(ch)
    db.flush()

    # ── Create fresh Chapter rows ─────────────────────────────────────────────
    count = 0
    for json_path in chapter_files:
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        # original_path points to the staged file the runner put in input/chapters/
        staged_path = project_dir / "input" / "chapters" / data.get("source_file", "")

        chapter = Chapter(
            project_id     = project.id,
            chapter_key    = data.get("chapter_id", ""),
            chapter_number = data.get("chapter_number"),
            title          = data.get("title", ""),
            word_count     = data.get("word_count", 0),
            original_path  = str(staged_path),
            ingested_path  = str(json_path),
        )
        db.add(chapter)
        count += 1

    # ── Update project stats ──────────────────────────────────────────────────
    project.chapter_count = count
    if project.status == ProjectStatus.DRAFT:
        project.status = ProjectStatus.ACTIVE

    db.commit()
    return f"Synced {count} chapter(s) to database; project status → active."


# ── Pre-consistency: export World Rules to JSON ───────────────────────────────

def _pre_consistency_setup(project: Project, project_dir: Path, db) -> None:
    """
    Called before Agent 2 (Consistency Editor) runs.

    Queries all active WorldRule records for this project and writes them
    to output/consistency/world_rules_export.json so the agent can read
    them without needing a database connection.

    NOTE: This is intentionally the ONLY place that touches the DB
    for pre-consistency work.  consistency.py remains a pure
    file-in / file-out script.
    """
    consistency_dir = project_dir / "output" / "consistency"
    consistency_dir.mkdir(parents=True, exist_ok=True)

    rules = (
        db.query(WorldRule)
        .filter(
            WorldRule.project_id == project.id,
            WorldRule.is_active == True,   # noqa: E712  (SQLAlchemy requires ==)
        )
        .order_by(WorldRule.category, WorldRule.sort_order)
        .all()
    )

    export = [
        {
            "category":  r.category,
            "title":     r.title,
            "content":   r.content,
            "rule_data": r.rule_data or {},
        }
        for r in rules
    ]

    export_path = consistency_dir / "world_rules_export.json"
    with open(export_path, "w") as f:
        json.dump(export, f, indent=2)


# ── Pre-dev-editor: export World Rules + take snapshot ────────────────────────

def _pre_dev_editor_setup(project: Project, project_dir: Path, db) -> None:
    """
    Called before Agent 3a (Developmental Editor) runs.

    1. Takes a snapshot of the current project state so the author can
       revert if needed.
    2. Queries all active WorldRule records and writes them to
       output/dev_editing/world_rules_export.json so the agent can read
       them without needing a database connection.

    The agent will fall back to output/consistency/world_rules_export.json
    if this file is not present (e.g. when run standalone).

    NOTE: dev_editor.py remains a pure file-in / file-out script.
    All database work happens here in runner.py.
    """
    dev_editing_dir = project_dir / "output" / "dev_editing"
    dev_editing_dir.mkdir(parents=True, exist_ok=True)

    # Take a snapshot before the developmental edit runs
    take_snapshot(project.id, project_dir, "pre_dev_editor")

    # Export world rules for the agent to consume
    rules = (
        db.query(WorldRule)
        .filter(
            WorldRule.project_id == project.id,
            WorldRule.is_active == True,   # noqa: E712
        )
        .order_by(WorldRule.category, WorldRule.sort_order)
        .all()
    )

    export = [
        {
            "category":  r.category,
            "title":     r.title,
            "content":   r.content,
            "rule_data": r.rule_data or {},
        }
        for r in rules
    ]

    export_path = dev_editing_dir / "world_rules_export.json"
    with open(export_path, "w") as f:
        json.dump(export, f, indent=2)


# ── Pre-copy-editor: export World Rules + take snapshot ───────────────────────

def _pre_copy_editor_setup(project: Project, project_dir: Path, db) -> None:
    """
    Called before Agent 3b (Copy & Line Editor) runs.

    1. Takes a snapshot of the current project state so the author can
       revert if needed.
    2. Queries all active WorldRule records and writes them to
       output/editing/world_rules_export.json so the agent can read
       them without needing a database connection.

    The agent will fall back to output/dev_editing/world_rules_export.json
    or output/consistency/world_rules_export.json if this file is not present
    (e.g. when run standalone).

    NOTE: copy_line_editor.py remains a pure file-in / file-out script.
    All database work happens here in runner.py.
    """
    editing_dir = project_dir / "output" / "editing"
    editing_dir.mkdir(parents=True, exist_ok=True)

    # Take a snapshot before the copy/line edit runs
    take_snapshot(project.id, project_dir, "pre_copy_editor")

    # Export world rules for the agent to consume
    rules = (
        db.query(WorldRule)
        .filter(
            WorldRule.project_id == project.id,
            WorldRule.is_active == True,   # noqa: E712
        )
        .order_by(WorldRule.category, WorldRule.sort_order)
        .all()
    )

    export = [
        {
            "category":  r.category,
            "title":     r.title,
            "content":   r.content,
            "rule_data": r.rule_data or {},
        }
        for r in rules
    ]

    export_path = editing_dir / "world_rules_export.json"
    with open(export_path, "w") as f:
        json.dump(export, f, indent=2)


# ── Main entry point ──────────────────────────────────────────────────────────

def start_pipeline(run_id: int) -> None:
    """Execute the pipeline for the given run.

    Called as a FastAPI BackgroundTask — runs in a thread pool.
    Creates its own DB session (don't pass the request session here,
    it would be closed by the time this runs).

    Pause logic:
      • If Agent 4 (Illustration) is selected, the pipeline pauses after Agent 3
        and sets run.status = PAUSED.  The web UI will show an illustration
        approval screen.  Once approved, a separate /resume endpoint will
        continue from Agent 5.
      • Agents 6 and 7 are not yet implemented — they're silently skipped.
    """
    db = SessionLocal()
    try:
        run: PipelineRun | None = db.get(PipelineRun, run_id)
        if not run:
            return

        # ── Mark as started ───────────────────────────────────────────────────
        run.status     = RunStatus.RUNNING
        run.started_at = datetime.now()
        db.commit()

        project: Project | None = db.get(Project, run.project_id)
        project_dir = _get_project_dir(project) if project else PROJECTS_DIR / "default"

        agents_to_run = sorted(run.agents_selected or [1])

        # ── Stage uploaded chapters (only when Agent 1 is included) ───────────
        # If the user is running only Agent 5 (re-layout), leave existing
        # ingested and edited files intact.
        if 1 in agents_to_run and project and project.manuscript_dir:
            ok, msg = _stage_chapters(
                project.manuscript_dir,
                project_dir,
                chapter_order = project.chapter_order or [],
            )
            if not ok:
                _fail_with_message(run, msg, db)
                return

        # ── Ensure project config.json is up to date ──────────────────────────
        _ensure_project_config(
            project_dir, run,
            layout_mode    = (project.layout_mode or "novel") if project else "novel",
            project_name   = (project.name   or "") if project else "",
            project_author = (project.author or "") if project else "",
        )

        # ── Run each selected agent in order ──────────────────────────────────
        for agent_num in agents_to_run:

            # Agents 6 & 7 not yet built — skip silently
            if agent_num > 5:
                continue

            # ── Agent 3: run, then pause for proposal review ──────────────────
            # Agent 3 produces tier-1 fixes (auto-applied) and tier-2 proposals
            # (paragraph-level AI suggestions awaiting user review).
            # The pipeline pauses here so the user can accept/reject proposals
            # in the Cassian UI before illustration and layout run.
            if agent_num == 3:
                # First run Agent 3 normally
                agent_run = AgentRun(
                    pipeline_run_id = run.id,
                    agent_num       = 3,
                    agent_name      = "editing",
                    status          = AgentStatus.RUNNING,
                    started_at      = datetime.now(),
                )
                db.add(agent_run)
                run.current_agent = 3
                db.commit()
                db.refresh(agent_run)

                success, output = _run_subprocess(3, project_dir)
                agent_run.status       = AgentStatus.COMPLETE if success else AgentStatus.FAILED
                agent_run.completed_at = datetime.now()
                agent_run.summary      = {"output": output}
                db.commit()

                if not success:
                    run.status = RunStatus.FAILED
                    db.commit()
                    return

                # Now pause for proposal review (same pattern as Agent 4)
                run.status        = RunStatus.PAUSED
                run.current_agent = 3
                db.commit()
                return   # UI takes over; pipeline resumes via /resume endpoint

            # ── Agent 4: pause for illustration approval ──────────────────────
            if agent_num == 4:
                run.status        = RunStatus.PAUSED
                run.current_agent = 4
                db.commit()
                return   # UI takes over; pipeline resumes via /resume endpoint

            # ── Agent 2: snapshot + world rules export ────────────────────────
            if agent_num == 2 and project:
                take_snapshot(project.id, project_dir, "pre_consistency")
                _pre_consistency_setup(project, project_dir, db)

            # ── Create AgentRun tracking row ──────────────────────────────────
            agent_run = AgentRun(
                pipeline_run_id = run.id,
                agent_num       = agent_num,
                agent_name      = AGENT_NAMES.get(agent_num, f"agent_{agent_num}"),
                status          = AgentStatus.RUNNING,
                started_at      = datetime.now(),
            )
            db.add(agent_run)
            run.current_agent = agent_num
            db.commit()
            db.refresh(agent_run)

            # ── Execute ───────────────────────────────────────────────────────
            success, output = _run_subprocess(agent_num, project_dir)

            # ── Record result ─────────────────────────────────────────────────
            agent_run.status       = AgentStatus.COMPLETE if success else AgentStatus.FAILED
            agent_run.completed_at = datetime.now()
            agent_run.summary      = {"output": output}

            # ── Post-ingestion: sync DB + create source snapshot ──────────────
            if agent_num == 1 and success and project:
                sync_msg = _post_ingestion_db_update(project, project_dir, db)
                agent_run.summary = {"output": output, "db_sync": sync_msg}
                db.commit()
                create_source_snapshot(project.id, project_dir)
            else:
                db.commit()

            if not success:
                run.status = RunStatus.FAILED
                db.commit()
                return

        # ── All done ──────────────────────────────────────────────────────────
        run.status        = RunStatus.COMPLETE
        run.current_agent = None
        run.completed_at  = datetime.now()
        db.commit()

    except Exception as exc:
        try:
            run = db.get(PipelineRun, run_id)
            if run:
                run.status = RunStatus.FAILED
                db.commit()
        except Exception:
            pass
        raise

    finally:
        db.close()


# ── Resume entry point ────────────────────────────────────────────────────────

def resume_pipeline(run_id: int) -> None:
    """Resume a PAUSED pipeline from after the current paused agent.

    Called after the user approves:
      • Agent 3 proposals → resumes from Agent 4 (or 5 if 4 not selected)
      • Agent 4 illustrations → resumes from Agent 5

    Agents 6 & 7 are skipped (not yet implemented).
    """
    db = SessionLocal()
    try:
        run: PipelineRun | None = db.get(PipelineRun, run_id)
        if not run or run.status != RunStatus.PAUSED:
            return

        paused_at = run.current_agent or 0

        run.status = RunStatus.RUNNING
        db.commit()

        project: Project | None = db.get(Project, run.project_id)
        project_dir = _get_project_dir(project) if project else PROJECTS_DIR / "default"

        # Run only agents that come AFTER the paused one
        agents_to_run = sorted(a for a in (run.agents_selected or []) if a > paused_at)

        for agent_num in agents_to_run:

            # Agents 6 & 7 not yet built — skip silently
            if agent_num > 5:
                continue

            # ── Agent 4: pause for illustration approval ───────────────────
            if agent_num == 4:
                run.status        = RunStatus.PAUSED
                run.current_agent = 4
                db.commit()
                return   # UI takes over again

            # ── Create AgentRun tracking row ──────────────────────────────
            agent_run = AgentRun(
                pipeline_run_id = run.id,
                agent_num       = agent_num,
                agent_name      = AGENT_NAMES.get(agent_num, f"agent_{agent_num}"),
                status          = AgentStatus.RUNNING,
                started_at      = datetime.now(),
            )
            db.add(agent_run)
            run.current_agent = agent_num
            db.commit()
            db.refresh(agent_run)

            # ── Execute ───────────────────────────────────────────────────
            success, output = _run_subprocess(agent_num, project_dir)

            # ── Record result ─────────────────────────────────────────────
            agent_run.status       = AgentStatus.COMPLETE if success else AgentStatus.FAILED
            agent_run.completed_at = datetime.now()
            agent_run.summary      = {"output": output}
            db.commit()

            if not success:
                run.status = RunStatus.FAILED
                db.commit()
                return

        # ── All done ──────────────────────────────────────────────────────
        run.status        = RunStatus.COMPLETE
        run.current_agent = None
        run.completed_at  = datetime.now()
        db.commit()

    except Exception as exc:
        try:
            run = db.get(PipelineRun, run_id)
            if run:
                run.status = RunStatus.FAILED
                db.commit()
        except Exception:
            pass
        raise

    finally:
        db.close()
