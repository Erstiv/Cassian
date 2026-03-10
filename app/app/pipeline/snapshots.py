"""
SNAPSHOT SYSTEM
Automatic timestamped backups of the manuscript state.

Before any agent modifies the manuscript, call take_snapshot() to
save a full copy of the current/ directory. The Source snapshot
(created at intake) is immutable and never overwritten.

Users can see a timeline of all snapshots and restore to any point.
"""

import shutil
from datetime import datetime
from pathlib import Path

from app.database import SessionLocal
from app.models import Snapshot


def take_snapshot(project_id: int, project_dir: Path, agent_name: str,
                  label: str = "") -> Snapshot | None:
    """Take a full snapshot of the project's current manuscript state.

    Copies the entire current/ directory (chapter JSONs, world rules,
    illustrations, etc.) into a timestamped snapshot folder.

    Args:
        project_id:  Database ID of the project
        project_dir: Path to the project's working directory
        agent_name:  Which agent is about to run (e.g. "dev_editor")
        label:       Optional human-readable description

    Returns:
        The created Snapshot model, or None if nothing to snapshot.
    """
    # Determine what to snapshot
    # For now, snapshot the current/ dir if it exists; otherwise editing output
    current_dir = project_dir / "current"
    if not current_dir.exists():
        # Fall back to the output dirs that have content
        # (for backward compatibility with the existing pipeline structure)
        output_dir = project_dir / "output"
        if not output_dir.exists():
            return None
        current_dir = output_dir

    # Create snapshot directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_name = f"{timestamp}_{agent_name}"
    snapshots_dir = project_dir / "snapshots"
    snapshot_path = snapshots_dir / snapshot_name
    snapshot_path.mkdir(parents=True, exist_ok=True)

    # Copy the current state
    try:
        shutil.copytree(current_dir, snapshot_path, dirs_exist_ok=True)
    except Exception as exc:
        print(f"  ⚠ Snapshot failed: {exc}")
        return None

    # Calculate size
    total_size = sum(f.stat().st_size for f in snapshot_path.rglob("*") if f.is_file())

    # Save to database
    db = SessionLocal()
    try:
        snapshot = Snapshot(
            project_id   = project_id,
            agent_name   = agent_name,
            snapshot_dir = str(snapshot_path),
            label        = label or f"Before {agent_name.replace('_', ' ').title()}",
            size_bytes   = total_size,
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        print(f"  ✓ Snapshot saved: {snapshot_name} ({total_size / 1024:.0f} KB)")
        return snapshot
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()


def create_source_snapshot(project_id: int, project_dir: Path) -> Snapshot | None:
    """Create the immutable Source snapshot from the original intake files.

    This is called once, right after the Intake Agent finishes.
    The source/ directory is NEVER modified after this.
    """
    # Copy ingested output to source/
    ingested_dir = project_dir / "output" / "ingested"
    if not ingested_dir.exists():
        return None

    source_dir = project_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copytree(ingested_dir, source_dir, dirs_exist_ok=True)
    except Exception as exc:
        print(f"  ⚠ Source snapshot failed: {exc}")
        return None

    total_size = sum(f.stat().st_size for f in source_dir.rglob("*") if f.is_file())

    db = SessionLocal()
    try:
        snapshot = Snapshot(
            project_id   = project_id,
            agent_name   = "source",
            snapshot_dir = str(source_dir),
            label        = "Original manuscript (immutable)",
            size_bytes   = total_size,
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        print(f"  ✓ Source snapshot created ({total_size / 1024:.0f} KB)")
        return snapshot
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()


def restore_snapshot(project_id: int, snapshot_id: int,
                     project_dir: Path) -> bool:
    """Restore a project to a previous snapshot state.

    Copies the snapshot's contents back into the project's current
    working directory, replacing whatever is there now.

    Takes a snapshot of the CURRENT state before restoring, so the
    user can undo the restore if needed.

    Returns True on success, False on failure.
    """
    db = SessionLocal()
    try:
        snapshot = db.get(Snapshot, snapshot_id)
        if not snapshot or snapshot.project_id != project_id:
            return False

        snapshot_path = Path(snapshot.snapshot_dir)
        if not snapshot_path.exists():
            return False

        # Take a safety snapshot of the current state before we overwrite it
        take_snapshot(project_id, project_dir, "pre_restore",
                      label=f"Auto-backup before restoring to '{snapshot.label}'")

        # Determine restore target
        current_dir = project_dir / "current"
        if not current_dir.exists():
            current_dir = project_dir / "output"

        # Clear and restore
        if current_dir.exists():
            shutil.rmtree(current_dir)
        shutil.copytree(snapshot_path, current_dir)

        print(f"  ✓ Restored to snapshot: {snapshot.label}")
        return True

    except Exception as exc:
        print(f"  ⚠ Restore failed: {exc}")
        return False
    finally:
        db.close()
