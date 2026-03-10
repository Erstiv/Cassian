"""
FONT MANAGER ROUTES
Lets the user browse, preview, and select fonts for the book.
Reads local TTF/OTF files from agents/05_layout/fonts/, shows a curated
list of Google Fonts, and saves selections to config.json.

Routes:
  GET  /projects/{project_id}/fonts                 — main Font Manager page
  POST /projects/{project_id}/fonts/save            — save font settings to config
  GET  /projects/{project_id}/fonts/preview         — HTMX fragment: live preview
  POST /projects/{project_id}/fonts/upload          — upload a new TTF/OTF file
  GET  /projects/{project_id}/fonts/serve/{filename} — serve local font file for @font-face
"""

import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import FileResponse, Response

from app.database import get_db
from app.models import Project


router    = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Cassian root: app/app/routes/ → app/app/ → app/ → Cassian/
CASSIAN_DIR  = Path(__file__).resolve().parent.parent.parent.parent
PROJECTS_DIR = CASSIAN_DIR / "projects"
FONTS_DIR    = CASSIAN_DIR / "agents" / "05_layout" / "fonts"

# Curated list of book-appropriate Google Fonts (serif, tested for print quality)
GOOGLE_FONTS = [
    {"name": "EB Garamond",        "google_family": "EB+Garamond:ital,wght@0,400;0,500;0,700;1,400"},
    {"name": "Merriweather",       "google_family": "Merriweather:ital,wght@0,300;0,400;0,700;1,300;1,400"},
    {"name": "Libre Baskerville",  "google_family": "Libre+Baskerville:ital,wght@0,400;0,700;1,400"},
    {"name": "Crimson Text",       "google_family": "Crimson+Text:ital,wght@0,400;0,600;1,400"},
    {"name": "Lora",               "google_family": "Lora:ital,wght@0,400;0,500;0,700;1,400"},
    {"name": "Playfair Display",   "google_family": "Playfair+Display:ital,wght@0,400;0,700;1,400"},
    {"name": "Source Serif 4",     "google_family": "Source+Serif+4:ital,wght@0,300;0,400;0,700;1,400"},
    {"name": "Cormorant Garamond", "google_family": "Cormorant+Garamond:ital,wght@0,400;0,500;0,700;1,400"},
    {"name": "PT Serif",           "google_family": "PT+Serif:ital,wght@0,400;0,700;1,400"},
    {"name": "Spectral",           "google_family": "Spectral:ital,wght@0,300;0,400;0,700;1,400"},
    {"name": "Alegreya",           "google_family": "Alegreya:ital,wght@0,400;0,700;1,400"},
]

MAX_FONT_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_project_dir(project_id: int) -> Path:
    return PROJECTS_DIR / str(project_id)


def _load_config(project_id: int) -> dict:
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


def _parse_font_filename(filename: str) -> dict:
    """
    Extract a human-friendly family name and style from a TTF/OTF filename.

    Handles standard naming:
      "EBGaramond-Regular.ttf"          → {family: "EB Garamond",  style: "Regular"}
      "Merriweather-BoldItalic.ttf"     → {family: "Merriweather", style: "Bold Italic"}
      "LibreBaskerville-Regular.ttf"    → {family: "Libre Baskerville", style: "Regular"}

    Also handles Google Fonts variable naming:
      "EBGaramond-VariableFont_wght.ttf"        → style: "Regular"
      "EBGaramond-Italic-VariableFont_wght.ttf" → style: "Italic"
    """
    stem = Path(filename).stem  # strip extension

    # Strip variable-font suffixes before the standard split
    # e.g. "-VariableFont_wght", "-VariableFont_ital,wght"
    stem = re.sub(r"-?VariableFont[^-]*$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_[a-z,]+$", "", stem)  # e.g. trailing "_wght"

    # Split at the last hyphen to separate family from style
    if "-" in stem:
        parts   = stem.rsplit("-", 1)
        family  = parts[0]
        style   = parts[1] if parts[1] else "Regular"
    else:
        family  = stem
        style   = "Regular"

    # Insert spaces to split CamelCase family names:
    #   "EBGaramond"        → "EB Garamond"   (acronym + word)
    #   "LibreBaskerville"  → "Libre Baskerville"
    #   "SourceSerif4"      → "Source Serif 4"
    family = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", family)   # "EBGar" → "EB Gar"
    family = re.sub(r"([a-z])([A-Z])",        r"\1 \2", family)   # "reM"   → "re M"
    family = re.sub(r"([a-zA-Z])(\d)",         r"\1 \2", family)  # "Serif4"→ "Serif 4"
    # Insert spaces in style too (e.g. BoldItalic → Bold Italic)
    style  = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", style) if style else "Regular"

    return {"family": family.strip(), "style": style.strip() or "Regular", "filename": filename}


def _scan_local_fonts(project_dir: Path) -> list[dict]:
    """
    Scan FONTS_DIR (and one level of subdirectories) for TTF/OTF files.
    Returns a list of font family dicts, each with a list of variant filenames.

    [
      {
        "family": "EB Garamond",
        "variants": [
          {"style": "Regular", "filename": "EBGaramond-VariableFont_wght.ttf"},
          ...
        ]
      },
      ...
    ]
    """
    if not FONTS_DIR.exists():
        return []

    # Collect all font files (root level + one/two subdir levels)
    # Google Fonts downloads nest files in {family}/static/*.ttf
    font_files = []
    for ext in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
        font_files.extend(FONTS_DIR.glob(ext))
        font_files.extend(FONTS_DIR.glob(f"*/{ext}"))
        font_files.extend(FONTS_DIR.glob(f"*/static/{ext}"))

    # Group by family
    families: dict[str, list[dict]] = {}
    for fp in sorted(font_files):
        info = _parse_font_filename(fp.name)
        fam  = info["family"]
        if fam not in families:
            families[fam] = []
        families[fam].append({"style": info["style"], "filename": fp.name})

    result = []
    for fam, variants in sorted(families.items()):
        result.append({"family": fam, "variants": variants})
    return result


def _get_sample_text(project_id: int) -> dict:
    """
    Load the first 2–3 paragraphs from the first available chapter.
    Returns:
      {
        "chapter_title": "...",
        "chapter_number": 1,
        "paragraphs": ["...", "...", "..."]
      }
    Falls back to lorem placeholder text.
    """
    project_dir = _get_project_dir(project_id)

    # Try editing directories first, then ingested
    search_dirs = []
    for d in project_dir.glob("output/editing*"):
        if d.is_dir():
            search_dirs.append((d, "*_edited.json"))
    ingested = project_dir / "output" / "ingested"
    if ingested.exists():
        search_dirs.append((ingested, "chapter_*.json"))

    for search_dir, pattern in search_dirs:
        candidates = sorted(search_dir.glob(pattern))
        if not candidates:
            continue
        # Pick the first chapter file
        for fp in candidates:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                paras_raw = data.get("paragraphs", [])
                # Filter out very short/title paragraphs; collect body text
                body_paras = []
                for p in paras_raw:
                    text = p.get("text", "") if isinstance(p, dict) else str(p)
                    text = text.strip()
                    # Skip blank lines and very short strings (titles, headings)
                    if len(text) > 60:
                        body_paras.append(text)
                    if len(body_paras) >= 3:
                        break

                if body_paras:
                    return {
                        "chapter_title":  data.get("title", ""),
                        "chapter_number": data.get("chapter_number", 1),
                        "paragraphs":     body_paras,
                    }
            except Exception:
                continue

    # Fallback placeholder
    return {
        "chapter_title":  "Sample Chapter",
        "chapter_number": 1,
        "paragraphs": [
            "Marcus Wright stood at the edge of the frozen lake, his breath forming small clouds "
            "that vanished before they could become anything worth remembering. The facility hummed "
            "somewhere below, a bass note felt in the ribs more than heard.",
            "He checked his watch — an old habit, pointless now. Time had collapsed into something "
            "less measurable than seconds. There were only decisions left, and the weight of each "
            "one pressed against his sternum like a hand.",
        ],
    }


def _get_font_config(config: dict) -> dict:
    """Extract font settings from config, with safe defaults."""
    fonts = config.get("formatting", {}).get("fonts", {})
    return {
        "body":                 fonts.get("body", "EB Garamond"),
        "body_size_pt":         fonts.get("body_size_pt", 11),
        "chapter_heading":      fonts.get("chapter_heading", "EB Garamond"),
        "chapter_heading_size_pt": fonts.get("chapter_heading_size_pt", 24),
        "line_spacing":         fonts.get("line_spacing", 1.4),
    }


# ── GET — main font manager page ───────────────────────────────────────────────

@router.get("/projects/{project_id}/fonts", response_class=HTMLResponse)
async def fonts_page(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir  = _get_project_dir(project_id)
    config       = _load_config(project_id)
    font_config  = _get_font_config(config)
    local_fonts  = _scan_local_fonts(project_dir)
    sample_text  = _get_sample_text(project_id)

    return templates.TemplateResponse(
        "fonts.html",
        {
            "request":      request,
            "project":      project,
            "active_page":  "fonts",
            "font_config":  font_config,
            "local_fonts":  local_fonts,
            "google_fonts": GOOGLE_FONTS,
            "sample_text":  sample_text,
        },
    )


# ── POST — save font settings ──────────────────────────────────────────────────

@router.post("/projects/{project_id}/fonts/save", response_class=HTMLResponse)
async def fonts_save(
    project_id:   int,
    request:      Request,
    db:           Session = Depends(get_db),
    body_font:    str  = Form(...),
    body_size:    float = Form(11),
    heading_font: str  = Form(...),
    heading_size: float = Form(24),
    line_spacing: float = Form(1.4),
    same_as_body: str  = Form(None),   # checkbox → "on" or absent
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if same_as_body == "on":
        heading_font = body_font

    config = _load_config(project_id)
    if "formatting" not in config:
        config["formatting"] = {}
    if "fonts" not in config["formatting"]:
        config["formatting"]["fonts"] = {}

    config["formatting"]["fonts"].update({
        "body":                    body_font,
        "body_size_pt":            body_size,
        "chapter_heading":         heading_font,
        "chapter_heading_size_pt": heading_size,
        "line_spacing":            line_spacing,
    })

    _save_config(project_id, config)

    # Return a small HTMX confirmation fragment
    return HTMLResponse("""
        <div class="flex items-center gap-2 text-green-400 text-sm htmx-added">
          <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M5 13l4 4L19 7"/>
          </svg>
          Font settings saved to config.json
        </div>
    """)


# ── GET — live preview fragment ────────────────────────────────────────────────

@router.get("/projects/{project_id}/fonts/preview", response_class=HTMLResponse)
async def fonts_preview(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    font_name:  str   = Query("EB Garamond"),
    size:       float = Query(11),
    text:       str   = Query(""),
):
    """
    Returns a small HTML fragment that renders the given text in the requested font.
    The caller supplies a Google Fonts family name; loading is handled client-side.
    This endpoint is kept minimal — the real live preview uses client-side JS.
    """
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if not text:
        sample = _get_sample_text(project_id)
        text   = " ".join(sample["paragraphs"][:2])

    escaped_font = font_name.replace("'", "\\'")
    fragment = f"""
    <p style="font-family: '{escaped_font}', Georgia, serif;
              font-size: {size}pt;
              line-height: 1.5;
              color: #1e293b;
              padding: 1rem;">
      {text[:500]}
    </p>
    """
    return HTMLResponse(fragment)


# ── POST — upload a new font file ──────────────────────────────────────────────

@router.post("/projects/{project_id}/fonts/upload", response_class=HTMLResponse)
async def fonts_upload(
    project_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    font_file:  UploadFile = File(...),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    filename = font_file.filename or ""
    ext      = Path(filename).suffix.lower()

    if ext not in (".ttf", ".otf"):
        return HTMLResponse(
            '<p class="text-red-400 text-sm">Only .ttf and .otf files are accepted.</p>',
            status_code=400,
        )

    # Read file and check size
    content = await font_file.read()
    if len(content) > MAX_FONT_UPLOAD_BYTES:
        return HTMLResponse(
            '<p class="text-red-400 text-sm">File exceeds 10 MB limit.</p>',
            status_code=400,
        )

    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = FONTS_DIR / filename
    dest.write_bytes(content)

    # Re-scan and return updated local font list fragment
    local_fonts = _scan_local_fonts(_get_project_dir(project_id))
    font_items  = ""
    for fam in local_fonts:
        variants_str = ", ".join(v["style"] for v in fam["variants"])
        font_items += f"""
        <label class="flex items-start gap-2.5 cursor-pointer group py-1">
          <input type="radio" name="body_font" value="{fam['family']}"
                 class="mt-0.5 accent-amber-400"
                 onchange="updatePreview('body')">
          <span class="text-sm text-slate-300 group-hover:text-slate-100">
            {fam['family']}
            <span class="text-xs text-slate-500 ml-1">({variants_str})</span>
          </span>
        </label>
        """

    return HTMLResponse(f"""
        <div class="htmx-added space-y-1">{font_items}</div>
        <p class="text-green-400 text-xs mt-2 htmx-added">
          ✓ {filename} uploaded successfully
        </p>
    """)


# ── GET — serve a local font file ──────────────────────────────────────────────

@router.get("/projects/{project_id}/fonts/serve/{filename}")
async def fonts_serve(
    project_id: int,
    filename:   str,
    db:         Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Sanitise filename — no path traversal
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    ext = Path(safe_name).suffix.lower()
    if ext not in (".ttf", ".otf"):
        raise HTTPException(status_code=400, detail="Not a font file")

    # Search root, one subdir, and Google Fonts static/ subdir
    candidates = (
        list(FONTS_DIR.glob(safe_name))
        + list(FONTS_DIR.glob(f"*/{safe_name}"))
        + list(FONTS_DIR.glob(f"*/static/{safe_name}"))
    )
    if not candidates:
        raise HTTPException(status_code=404, detail="Font file not found")

    font_path = candidates[0]
    media_type = "font/otf" if ext == ".otf" else "font/ttf"
    return FileResponse(
        path       = str(font_path),
        media_type = media_type,
        headers    = {"Cache-Control": "public, max-age=86400"},
    )
