"""
EXPORT ROUTES
Publish readiness checklist and file download/package builder.

Routes:
  GET  /projects/{project_id}/export                          — main export page
  GET  /projects/{project_id}/export/download/{file_type}    — serve individual files
  POST /projects/{project_id}/export/package                 — create ZIP package
  GET  /projects/{project_id}/export/checklist               — HTMX readiness checklist
"""

import json
import os
import zipfile
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, OutputProfile, Cover, CoverStatus


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _fmt_size(size_bytes: int) -> str:
    """Return a human-readable file size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def _file_info(path: Path) -> dict | None:
    """Return size and mtime info for a file, or None if it doesn't exist."""
    if not path or not path.exists():
        return None
    stat = path.stat()
    return {
        "path":     path,
        "size":     _fmt_size(stat.st_size),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%b %-d, %Y"),
        "modified_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def _find_interior_pdf(project_id: int) -> Path | None:
    final_dir = _get_project_dir(project_id) / "output" / "final"
    if not final_dir.exists():
        return None
    pdfs = sorted(final_dir.glob("*.pdf"))
    return pdfs[-1] if pdfs else None


def _find_cover_files(project_id: int) -> dict:
    """Return paths for cover wraparound files (PNG and TIFF)."""
    cover_dir = _get_project_dir(project_id) / "output" / "cover"
    result = {"png": None, "tif": None, "thumbnail": None}
    if not cover_dir.exists():
        return result

    # PNG wraparound
    pngs = list(cover_dir.glob("wraparound*.png")) + list(cover_dir.glob("combined*.png"))
    if pngs:
        result["png"] = sorted(pngs)[-1]

    # TIFF wraparound
    tifs = list(cover_dir.glob("wraparound*.tif")) + list(cover_dir.glob("combined*.tif")) + \
           list(cover_dir.glob("wraparound*.tiff")) + list(cover_dir.glob("combined*.tiff"))
    if tifs:
        result["tif"] = sorted(tifs)[-1]

    # Thumbnail
    thumbs = list(cover_dir.glob("thumbnail*.jpg")) + list(cover_dir.glob("thumbnail*.png"))
    if thumbs:
        result["thumbnail"] = sorted(thumbs)[-1]

    return result


def _find_metadata_json(project_id: int) -> Path | None:
    path = _get_project_dir(project_id) / "output" / "metadata" / "book_metadata.json"
    return path if path.exists() else None


def _find_layout_report(project_id: int) -> Path | None:
    path = _get_project_dir(project_id) / "output" / "formatting" / "layout_report.json"
    return path if path.exists() else None


def _build_checklist(project_id: int, db: Session) -> list[dict]:
    """
    Return a list of checklist items, each with:
      label, done (bool), detail (optional string), link (optional URL)
    """
    project_dir = _get_project_dir(project_id)
    items = []

    # 1. Manuscript ingested
    ingested_dir = project_dir / "output" / "ingested"
    chapter_files = list(ingested_dir.glob("chapter_*.json")) if ingested_dir.exists() else []
    items.append({
        "label":  "Manuscript ingested",
        "done":   len(chapter_files) > 0,
        "detail": f"{len(chapter_files)} chapter{'s' if len(chapter_files) != 1 else ''}" if chapter_files else "No chapters found",
        "link":   f"/projects/{project_id}",
    })

    # 2. Editing complete
    editing_dir  = project_dir / "output" / "editing"
    edited_files = list(editing_dir.glob("chapter_*_edited.json")) if editing_dir.exists() else []
    items.append({
        "label":  "Editing complete",
        "done":   len(edited_files) > 0,
        "detail": f"{len(edited_files)} chapter{'s' if len(edited_files) != 1 else ''} edited" if edited_files else "Not run yet",
        "link":   f"/projects/{project_id}/copy-line-editor",
    })

    # 3. Proofread
    proof_report = project_dir / "output" / "proofreading" / "proofread_report.json"
    if proof_report.exists():
        try:
            report = json.loads(proof_report.read_text(encoding="utf-8"))
            total   = report.get("total_issues", 0)
            resolved = report.get("resolved_issues", 0)
            detail  = f"{total} issues, {resolved} resolved"
            done    = True
        except Exception:
            detail = "Report found"
            done   = True
    else:
        detail = "Not run yet"
        done   = False
    items.append({
        "label":  "Proofreading complete",
        "done":   done,
        "detail": detail,
        "link":   f"/projects/{project_id}/proofread",
    })

    # 4. Layout built
    layout_report = _find_layout_report(project_id)
    interior_pdf  = _find_interior_pdf(project_id)
    if interior_pdf:
        try:
            if layout_report:
                report    = json.loads(layout_report.read_text(encoding="utf-8"))
                pages     = report.get("page_count") or report.get("total_pages") or "?"
                detail    = f"{pages} pages"
            else:
                detail = "PDF found"
            done = True
        except Exception:
            detail = "PDF found"
            done   = True
    else:
        detail = "Not run yet"
        done   = False
    items.append({
        "label":  "Layout built",
        "done":   done,
        "detail": detail,
        "link":   f"/projects/{project_id}/layout",
    })

    # 5. Cover approved
    cover = (
        db.query(Cover)
        .filter(Cover.project_id == project_id)
        .order_by(Cover.created_at.desc())
        .first()
    )
    cover_done = cover and cover.status == CoverStatus.APPROVED
    items.append({
        "label":  "Cover approved",
        "done":   bool(cover_done),
        "detail": cover.status.value.capitalize() if cover else "Not started",
        "link":   f"/projects/{project_id}/cover",
    })

    # 6. Metadata saved
    metadata_json = _find_metadata_json(project_id)
    if metadata_json:
        try:
            meta   = json.loads(metadata_json.read_text(encoding="utf-8"))
            issues = []
            if not meta.get("isbn_13") and not meta.get("isbn_10"):
                issues.append("missing ISBN")
            if not meta.get("description", {}).get("short"):
                issues.append("missing short description")
            if not meta.get("keywords"):
                issues.append("missing keywords")
            if issues:
                detail = "Incomplete — " + ", ".join(issues)
                done   = False   # treat as warning
                warn   = True
            else:
                detail = "Complete"
                done   = True
                warn   = False
        except Exception:
            detail = "File found"
            done   = True
            warn   = False
    else:
        detail = "Not saved yet"
        done   = False
        warn   = False
    items.append({
        "label":  "Metadata saved",
        "done":   done,
        "detail": detail,
        "warn":   metadata_json is not None and not done,   # amber vs red
        "link":   f"/projects/{project_id}/metadata",
    })

    return items


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/export", response_class=HTMLResponse)
async def export_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    checklist    = _build_checklist(project_id, db)
    done_count   = sum(1 for c in checklist if c["done"])

    interior_pdf  = _find_interior_pdf(project_id)
    cover_files   = _find_cover_files(project_id)
    metadata_json = _find_metadata_json(project_id)

    # Get publisher from the project's default output profile
    output_profile = (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id, OutputProfile.is_default == True)
        .first()
    )
    if not output_profile:
        output_profile = (
            db.query(OutputProfile)
            .filter(OutputProfile.project_id == project_id)
            .first()
        )

    publisher_label = "your publisher"
    if output_profile:
        pub_map = {
            "lulu":          "Lulu Hardcover Casewrap",
            "ingram_spark":  "IngramSpark",
            "kdp":           "KDP Trade Paperback",
            "draft2digital": "Draft2Digital",
            "generic":       "Generic Print",
        }
        publisher_label = pub_map.get(output_profile.publisher.value, "your publisher")

    # Check for existing export package
    safe_name = project.name.replace(" ", "_").replace("/", "_")[:40]
    package_path = _get_project_dir(project_id) / "output" / "export" / f"{safe_name}_publish_package.zip"

    return templates.TemplateResponse("export.html", {
        "request":         request,
        "project":         project,
        "active_page":     "export",
        "checklist":       checklist,
        "done_count":      done_count,
        "total_count":     len(checklist),
        "interior_pdf":    _file_info(interior_pdf),
        "cover_png":       _file_info(cover_files["png"]),
        "cover_tif":       _file_info(cover_files["tif"]),
        "metadata_json":   _file_info(metadata_json),
        "package_info":    _file_info(package_path),
        "publisher_label": publisher_label,
        "package_error":   None,
        "package_created": False,
    })


@router.get("/projects/{project_id}/export/download/{file_type}")
async def export_download(
    project_id: int,
    file_type:  str,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if file_type == "interior_pdf":
        path = _find_interior_pdf(project_id)
        if not path:
            raise HTTPException(status_code=404, detail="Interior PDF not found")
        return FileResponse(
            str(path),
            media_type="application/pdf",
            filename=f"{project.name}_interior.pdf",
        )

    elif file_type == "cover_wraparound_png":
        cover_files = _find_cover_files(project_id)
        path = cover_files.get("png")
        if not path:
            raise HTTPException(status_code=404, detail="Cover PNG not found")
        return FileResponse(
            str(path),
            media_type="image/png",
            filename=f"{project.name}_cover_wraparound.png",
        )

    elif file_type == "cover_wraparound_tif":
        cover_files = _find_cover_files(project_id)
        path = cover_files.get("tif")
        if not path:
            raise HTTPException(status_code=404, detail="Cover TIFF not found")
        return FileResponse(
            str(path),
            media_type="image/tiff",
            filename=f"{project.name}_cover_wraparound.tif",
        )

    elif file_type == "metadata_json":
        path = _find_metadata_json(project_id)
        if not path:
            raise HTTPException(status_code=404, detail="Metadata JSON not found")
        return FileResponse(
            str(path),
            media_type="application/json",
            filename=f"{project.name}_metadata.json",
        )

    elif file_type == "full_package":
        safe_name    = project.name.replace(" ", "_").replace("/", "_")[:40]
        package_path = _get_project_dir(project_id) / "output" / "export" / f"{safe_name}_publish_package.zip"
        if not package_path.exists():
            raise HTTPException(status_code=404, detail="Package not built yet — use Create Package first")
        return FileResponse(
            str(package_path),
            media_type="application/zip",
            filename=f"{safe_name}_publish_package.zip",
        )

    else:
        raise HTTPException(status_code=400, detail=f"Unknown file_type: {file_type}")


@router.post("/projects/{project_id}/export/package", response_class=HTMLResponse)
async def export_create_package(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    safe_name    = project.name.replace(" ", "_").replace("/", "_")[:40]
    export_dir   = _get_project_dir(project_id) / "output" / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    package_path = export_dir / f"{safe_name}_publish_package.zip"

    interior_pdf  = _find_interior_pdf(project_id)
    cover_files   = _find_cover_files(project_id)
    metadata_json = _find_metadata_json(project_id)
    layout_report = _find_layout_report(project_id)

    files_to_pack: list[tuple[Path, str]] = []
    missing = []

    if interior_pdf:
        files_to_pack.append((interior_pdf, "interior.pdf"))
    else:
        missing.append("interior PDF")

    if cover_files["png"]:
        files_to_pack.append((cover_files["png"], "cover_wraparound.png"))
    if cover_files["tif"]:
        files_to_pack.append((cover_files["tif"], "cover_wraparound.tif"))
    if cover_files["thumbnail"]:
        files_to_pack.append((cover_files["thumbnail"], "cover_thumbnail.jpg"))

    if metadata_json:
        files_to_pack.append((metadata_json, "metadata.json"))
    else:
        missing.append("metadata")

    if layout_report:
        files_to_pack.append((layout_report, "layout_report.json"))

    if not files_to_pack:
        # Nothing to pack at all
        checklist    = _build_checklist(project_id, db)
        done_count   = sum(1 for c in checklist if c["done"])
        return templates.TemplateResponse("export.html", {
            "request":         request,
            "project":         project,
            "active_page":     "export",
            "checklist":       checklist,
            "done_count":      done_count,
            "total_count":     len(checklist),
            "interior_pdf":    _file_info(interior_pdf),
            "cover_png":       _file_info(cover_files["png"]),
            "cover_tif":       _file_info(cover_files["tif"]),
            "metadata_json":   _file_info(metadata_json),
            "package_info":    None,
            "publisher_label": "your publisher",
            "package_error":   "No files available to package. Run Layout and Cover first.",
            "package_created": False,
        })

    # Build the ZIP
    try:
        with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path, arcname in files_to_pack:
                zf.write(file_path, arcname=arcname)
    except Exception as exc:
        checklist  = _build_checklist(project_id, db)
        done_count = sum(1 for c in checklist if c["done"])
        return templates.TemplateResponse("export.html", {
            "request":         request,
            "project":         project,
            "active_page":     "export",
            "checklist":       checklist,
            "done_count":      done_count,
            "total_count":     len(checklist),
            "interior_pdf":    _file_info(interior_pdf),
            "cover_png":       _file_info(cover_files["png"]),
            "cover_tif":       _file_info(cover_files["tif"]),
            "metadata_json":   _file_info(metadata_json),
            "package_info":    None,
            "publisher_label": "your publisher",
            "package_error":   f"Failed to create ZIP: {exc}",
            "package_created": False,
        })

    checklist    = _build_checklist(project_id, db)
    done_count   = sum(1 for c in checklist if c["done"])

    output_profile = (
        db.query(OutputProfile)
        .filter(OutputProfile.project_id == project_id, OutputProfile.is_default == True)
        .first()
    )
    publisher_label = "your publisher"
    if output_profile:
        pub_map = {
            "lulu":          "Lulu Hardcover Casewrap",
            "ingram_spark":  "IngramSpark",
            "kdp":           "KDP Trade Paperback",
            "draft2digital": "Draft2Digital",
            "generic":       "Generic Print",
        }
        publisher_label = pub_map.get(output_profile.publisher.value, "your publisher")

    return templates.TemplateResponse("export.html", {
        "request":         request,
        "project":         project,
        "active_page":     "export",
        "checklist":       checklist,
        "done_count":      done_count,
        "total_count":     len(checklist),
        "interior_pdf":    _file_info(interior_pdf),
        "cover_png":       _file_info(cover_files["png"]),
        "cover_tif":       _file_info(cover_files["tif"]),
        "metadata_json":   _file_info(metadata_json),
        "package_info":    _file_info(package_path),
        "publisher_label": publisher_label,
        "package_error":   None,
        "package_created": True,
    })


@router.get("/projects/{project_id}/export/checklist", response_class=HTMLResponse)
async def export_checklist_fragment(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    """HTMX endpoint — returns just the checklist panel HTML."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    checklist  = _build_checklist(project_id, db)
    done_count = sum(1 for c in checklist if c["done"])

    return templates.TemplateResponse("fragments/export_checklist.html", {
        "request":     request,
        "project":     project,
        "checklist":   checklist,
        "done_count":  done_count,
        "total_count": len(checklist),
    })
