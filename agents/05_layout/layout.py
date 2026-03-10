"""
╔══════════════════════════════════════════════════════════════════╗
║  AGENT 5 — LAYOUT                                               ║
║  One Thousand Perfect Sighs                                      ║
║                                                                  ║
║  What this does:                                                 ║
║    Assembles the complete, print-ready interior PDF for         ║
║    the Lulu 6"×9" hardcover edition.                           ║
║                                                                  ║
║    Page by page, the agent:                                     ║
║      1. Builds front matter (title, copyright, epigraph)       ║
║      2. Opens each chapter on a fresh right-hand page          ║
║      3. Places the chapter's illustration as a header image    ║
║      4. Flows the edited body text with full typesetting       ║
║      5. Adds running headers and page numbers throughout       ║
║      6. Outputs a single PDF at the Lulu 6"×9" trim size      ║
║                                                                  ║
║  Lulu hardcover specs (from config.json):                       ║
║    Trim:     6.0" × 9.0"                                       ║
║    Margins:  top 1.0" | bottom 1.0" | gutter 1.25" | out 0.75"║
║    Font:     Garamond 11pt body / 24pt chapter headings        ║
║    Spacing:  1.4× line height                                  ║
║                                                                  ║
║  Font setup:                                                     ║
║    Drop Garamond .ttf/.otf files in agents/05_layout/fonts/    ║
║    Recommended free option: EB Garamond (Google Fonts)         ║
║    If no font files found, falls back to Times-Roman           ║
║                                                                  ║
║  Input:   output/editing/chapter_XX_edited.json                ║
║           output/illustrations/images/chapter_XX.tif           ║
║  Output:  output/final/one_thousand_perfect_sighs_lulu.pdf     ║
║           output/formatting/layout_report.json                 ║
║           output/formatting/layout_report.md                   ║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/05_layout/layout.py                           ║
║    python agents/05_layout/layout.py --no-illustrations        ║
║    python agents/05_layout/layout.py --chapter 01  (one only) ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import sys
import re
import os
import argparse
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

# ── ReportLab ─────────────────────────────────────────────────────────────────
try:
    from reportlab.lib.units import inch
    from reportlab.lib.pagesizes import inch as _inch_unused
    from reportlab.platypus import (
        BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
        PageBreak, NextPageTemplate, Image, KeepTogether,
        HRFlowable, Flowable
    )
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER, TA_JUSTIFY
    from reportlab.lib.colors import black, Color
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as rl_canvas
except ImportError:
    print("ERROR: reportlab is not installed.")
    print("       Run:  pip install reportlab")
    sys.exit(1)

# ── Pillow (for image conversion) ─────────────────────────────────────────────
try:
    from PIL import Image as PILImage
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

# ── Colorama ──────────────────────────────────────────────────────────────────
try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    def green(s):  return Fore.GREEN  + str(s) + Style.RESET_ALL
    def yellow(s): return Fore.YELLOW + str(s) + Style.RESET_ALL
    def cyan(s):   return Fore.CYAN   + str(s) + Style.RESET_ALL
    def red(s):    return Fore.RED    + str(s) + Style.RESET_ALL
    def bold(s):   return Style.BRIGHT + str(s) + Style.RESET_ALL
except ImportError:
    def green(s):  return str(s)
    def yellow(s): return str(s)
    def cyan(s):   return str(s)
    def red(s):    return str(s)
    def bold(s):   return str(s)

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
#  ORDINAL CHAPTER NUMBERS
#  Always derive the display heading from the sequential position in
#  reading_order — never from the text embedded in the manuscript docx,
#  which may be wrong (intercalary chapters, renumbered chapters, etc.)
# ─────────────────────────────────────────────────────────────────────────────

_ORDINALS = [
    '', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight',
    'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen',
    'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen', 'Twenty',
    'Twenty-One', 'Twenty-Two', 'Twenty-Three', 'Twenty-Four', 'Twenty-Five',
    'Twenty-Six', 'Twenty-Seven', 'Twenty-Eight', 'Twenty-Nine', 'Thirty',
    'Thirty-One', 'Thirty-Two', 'Thirty-Three', 'Thirty-Four', 'Thirty-Five',
]

def ordinal_word(n):
    """Return 'One', 'Two', ... 'Thirty' for n = 1 .. 35."""
    if 1 <= n <= len(_ORDINALS) - 1:
        return _ORDINALS[n]
    return str(n)


# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = (
    Path(os.environ['CASSIAN_PROJECT_DIR'])
    if 'CASSIAN_PROJECT_DIR' in os.environ
    else Path(__file__).resolve().parent.parent.parent
)
CONFIG_PATH    = BASE_DIR / "config.json"
EDITING_DIR    = BASE_DIR / "output" / "editing"
ILLUS_DIR      = BASE_DIR / "output" / "illustrations" / "images"
FORMATTING_DIR     = BASE_DIR / "output" / "formatting"
FINAL_DIR          = BASE_DIR / "output" / "final"
FONTS_DIR          = Path(__file__).resolve().parent / "fonts"
CHAPTER_NAMES_PATH = BASE_DIR / "output" / "formatting" / "chapter_names.json"

# ── Page geometry (Lulu 6"×9" hardcover trim) ─────────────────────────────────
PAGE_W  = 6.0  * inch
PAGE_H  = 9.0  * inch
M_TOP   = 1.0  * inch   # top margin
M_BOT   = 1.0  * inch   # bottom margin
M_IN    = 1.25 * inch   # inside / gutter margin (left side of PDF)
M_OUT   = 0.75 * inch   # outside margin (right side of PDF)
TEXT_W  = PAGE_W - M_IN - M_OUT    # 4.0"
TEXT_H  = PAGE_H - M_TOP - M_BOT   # 7.0"
HEADER_Y = PAGE_H - M_TOP + 0.32 * inch   # running header position
FOOTER_Y = M_BOT * 0.55                    # page number position


# ─────────────────────────────────────────────────────────────────────────────
#  FONT SETUP
# ─────────────────────────────────────────────────────────────────────────────

def _find_font_file(name_patterns, search_dirs):
    """Search directories (recursively) for a font file matching any of the name patterns.
    Skips variable fonts (prefer static weights for reliable ReportLab embedding).
    """
    for d in search_dirs:
        d = Path(d)
        if not d.exists():
            continue
        for f in sorted(d.rglob('*')):   # recurse into subdirectories
            if f.suffix.lower() not in ('.ttf', '.otf'):
                continue
            stem = f.stem.lower().replace(' ', '').replace('-', '')
            # Skip variable fonts — prefer static weights
            if 'variablefont' in stem:
                continue
            for pat in name_patterns:
                if pat.lower().replace(' ', '').replace('-', '') in stem:
                    return f
    return None


def _try_register_family(alias_prefix, pattern_sets, search_dirs):
    """
    Try to register a font family (Regular/Italic/Bold/BoldItalic).
    Also calls registerFontFamily so ReportLab knows the style relationships.
    Returns a dict of registered aliases, or {} if Regular not found.
    """
    aliases    = [f'{alias_prefix}Reg', f'{alias_prefix}Ital',
                  f'{alias_prefix}Bold', f'{alias_prefix}BoldItal']
    registered = {}
    for alias, patterns in zip(aliases, pattern_sets):
        found = _find_font_file(patterns, search_dirs)
        if found:
            try:
                pdfmetrics.registerFont(TTFont(alias, str(found)))
                registered[alias] = found.name
            except Exception as e:
                print(yellow(f"  ⚠  Could not register {found.name}: {e}"))

    if f'{alias_prefix}Reg' not in registered:
        return {}

    # Tell ReportLab how the variants relate — required for italic/bold to work
    reg  = f'{alias_prefix}Reg'
    pdfmetrics.registerFontFamily(
        reg,
        normal     = reg,
        bold       = registered.get(f'{alias_prefix}Bold',     reg),
        italic     = registered.get(f'{alias_prefix}Ital',     reg),
        boldItalic = registered.get(f'{alias_prefix}BoldItal', reg),
    )
    return registered


def setup_fonts():
    """
    Register fonts for the PDF.

    Priority:
      1. TTF/OTF files in agents/05_layout/fonts/  ← drop Garamond here
      2. Garamond in system font directories (macOS)
      3. Liberation Serif (Linux system — embeddable Times equivalent)
      4. Built-in Times-Roman (last resort, not embedded — avoid for Lulu)

    Returns a dict with keys: regular, italic, bold, bold_italic, name, found
    """
    system_dirs = [
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
        "/System/Library/Fonts",
        "/usr/share/fonts/truetype/liberation",
        "/usr/share/fonts/truetype/liberation2",
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype",
        "/usr/share/fonts/opentype",
        "/usr/share/fonts",
    ]
    local_dir   = FONTS_DIR if FONTS_DIR.exists() else None
    search_dirs = ([local_dir] if local_dir else []) + system_dirs

    # ── Tier 1: Garamond (user-supplied or system macOS) ─────────────────────
    garamond = _try_register_family('Book', [
        ["ebgaramond-regular", "garamond-regular", "garamondpremrpro", "garamond"],
        ["ebgaramond-italic",  "garamond-italic",  "garamondit"],
        ["ebgaramond-bold",    "garamond-bold",    "garamondbold"],
        ["ebgaramond-bolditalic", "garamond-bolditalic"],
    ], search_dirs)

    if garamond:
        print(green(f"  ✓ Garamond loaded: {garamond['BookReg']}"))
        return {
            'regular':    'BookReg',
            'italic':     'BookItal'     if 'BookItal'     in garamond else 'BookReg',
            'bold':       'BookBold'     if 'BookBold'     in garamond else 'BookReg',
            'bold_italic':'BookBoldItal' if 'BookBoldItal' in garamond else 'BookReg',
            'found': True, 'name': 'EB Garamond',
        }

    # ── Tier 2: Liberation Serif (embeddable, Times-compatible) ──────────────
    libserif = _try_register_family('Lib', [
        ["liberationserif-regular",  "LiberationSerif-Regular"],
        ["liberationserif-italic",   "LiberationSerif-Italic"],
        ["liberationserif-bold",     "LiberationSerif-Bold"],
        ["liberationserif-bolditalic","LiberationSerif-BoldItalic"],
    ], search_dirs)

    if libserif:
        print(green("  ✓ Liberation Serif loaded (embedded — Lulu-compatible)"))
        print(yellow("     For Garamond: place EBGaramond .ttf files in"))
        print(yellow(f"    {FONTS_DIR}"))
        # Use alias keys directly (not the values, which are filenames)
        return {
            'regular':    'LibReg',
            'italic':     'LibItal'     if 'LibItal'     in libserif else 'LibReg',
            'bold':       'LibBold'     if 'LibBold'     in libserif else 'LibReg',
            'bold_italic':'LibBoldItal' if 'LibBoldItal' in libserif else 'LibReg',
            'found': True, 'name': 'Liberation Serif (Times-compatible)',
        }

    # ── Tier 3: DejaVu Serif (embeddable fallback) ────────────────────────────
    dejavu = _try_register_family('Dvu', [
        ["dejavuserif.ttf", "dejavuserif"],
        ["dejavuserif-italic", "dejavuserifitalic"],
        ["dejavuserif-bold",   "dejavuserifbold"],
        ["dejavuserif-bolditalic"],
    ], search_dirs)

    if dejavu:
        print(green("  ✓ DejaVu Serif loaded (embedded)"))
        print(yellow("     For Garamond: place EBGaramond .ttf files in"))
        print(yellow(f"    {FONTS_DIR}"))
        return {
            'regular':    'DvuReg',
            'italic':     'DvuItal'     if 'DvuItal'     in dejavu else 'DvuReg',
            'bold':       'DvuBold'     if 'DvuBold'     in dejavu else 'DvuReg',
            'bold_italic':'DvuBoldItal' if 'DvuBoldItal' in dejavu else 'DvuReg',
            'found': True, 'name': 'DejaVu Serif',
        }

    # ── Tier 4: Built-in Times (not embedded — Lulu will warn) ───────────────
    print(yellow("  ⚠  No embeddable serif font found — using Times-Roman"))
    print(yellow("     Lulu will warn about font embedding."))
    print(yellow(f"     Fix: place EBGaramond .ttf files in {FONTS_DIR}"))
    return {
        'regular':     'Times-Roman',
        'italic':      'Times-Italic',
        'bold':        'Times-Bold',
        'bold_italic': 'Times-BoldItalic',
        'found': False, 'name': 'Times-Roman (not embedded)',
    }


# ─────────────────────────────────────────────────────────────────────────────
#  CYRILLIC FALLBACK FONT
#  Liberation Serif has no Cyrillic glyphs.  When the body text includes
#  Cyrillic passages we wrap them in <font name="CyrReg">…</font> tags so
#  ReportLab substitutes DejaVu Serif (which has full Cyrillic coverage).
# ─────────────────────────────────────────────────────────────────────────────

_CYR_FONT_REG  = None   # set by setup_cyrillic_font()
_CYR_FONT_ITAL = None


def setup_cyrillic_font():
    """Register DejaVu Serif for Cyrillic fallback. Returns True if registered."""
    global _CYR_FONT_REG, _CYR_FONT_ITAL
    dejavu_dir = Path('/usr/share/fonts/truetype/dejavu')
    reg_path  = dejavu_dir / 'DejaVuSerif.ttf'
    ital_path = dejavu_dir / 'DejaVuSerif-Italic.ttf'
    if reg_path.exists():
        try:
            pdfmetrics.registerFont(TTFont('CyrReg',  str(reg_path)))
            _CYR_FONT_REG = 'CyrReg'
            if ital_path.exists():
                pdfmetrics.registerFont(TTFont('CyrItal', str(ital_path)))
                _CYR_FONT_ITAL = 'CyrItal'
            print(green('  ✓ Cyrillic fallback: DejaVu Serif registered'))
            return True
        except Exception as e:
            print(yellow(f'  ⚠  Cyrillic font registration failed: {e}'))
    else:
        print(yellow('  ⚠  DejaVu Serif not found — Cyrillic may not render'))
    return False


_CYR_RE = re.compile(r'([\u0400-\u04FF\u0500-\u052F]+)')


def markup_for_cyrillic(text, cyrillic_font=None):
    """
    Wrap runs of Cyrillic characters in <font name="...">...</font> tags
    so ReportLab uses the Cyrillic-capable font for those segments.

    If no Cyrillic font is registered, returns the text unchanged.
    All segments are XML-escaped before wrapping.
    """
    if not cyrillic_font or not _CYR_RE.search(text):
        return escape_xml(text)

    parts = _CYR_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if not part:
            continue
        if i % 2 == 1:   # Cyrillic segment (captured group)
            out.append(f'<font name="{cyrillic_font}">{escape_xml(part)}</font>')
        else:             # Latin / punctuation segment
            out.append(escape_xml(part))
    return ''.join(out)


# ─────────────────────────────────────────────────────────────────────────────
#  PARAGRAPH STYLES
# ─────────────────────────────────────────────────────────────────────────────

def build_styles(fonts, line_spacing=1.4, body_size=11, heading_size=24):
    """Create all ParagraphStyle objects for the book."""
    reg  = fonts['regular']
    ital = fonts['italic']
    bod_lead = body_size * line_spacing

    s = {}

    # ── Body ──────────────────────────────────────────────────────────────────
    s['body'] = ParagraphStyle(
        'body',
        fontName=reg, fontSize=body_size,
        leading=bod_lead,
        firstLineIndent=0.25 * inch,
        spaceBefore=0, spaceAfter=0,
        alignment=TA_JUSTIFY,
    )
    s['body_first'] = ParagraphStyle(
        'body_first', parent=s['body'],
        firstLineIndent=0,
    )
    s['body_italic'] = ParagraphStyle(
        'body_italic', parent=s['body'],
        fontName=ital,
        firstLineIndent=0,
    )

    # ── Scene header (location/date) ──────────────────────────────────────────
    s['scene_header'] = ParagraphStyle(
        'scene_header',
        fontName=ital, fontSize=body_size,
        leading=bod_lead,
        spaceBefore=0, spaceAfter=bod_lead * 0.5,
        alignment=TA_LEFT,
    )

    # ── Section break "* * *" ────────────────────────────────────────────────
    s['section_break'] = ParagraphStyle(
        'section_break',
        fontName=reg, fontSize=body_size,
        leading=bod_lead,
        spaceBefore=bod_lead * 1.0,
        spaceAfter=bod_lead * 0.5,
        alignment=TA_CENTER,
    )

    # ── Chapter heading ───────────────────────────────────────────────────────
    s['chapter_label'] = ParagraphStyle(
        'chapter_label',
        fontName=reg, fontSize=10,
        leading=14,
        spaceBefore=0.4 * inch,
        spaceAfter=4,
        alignment=TA_CENTER,
        textColor=Color(0.4, 0.4, 0.4),
    )
    s['chapter_title'] = ParagraphStyle(
        'chapter_title',
        fontName=reg, fontSize=heading_size,
        leading=heading_size * 1.2,
        spaceBefore=4,
        spaceAfter=0.3 * inch,
        alignment=TA_CENTER,
    )
    s['chapter_subtitle'] = ParagraphStyle(
        'chapter_subtitle',
        fontName=ital, fontSize=body_size,
        leading=bod_lead,
        spaceBefore=0,
        spaceAfter=0.25 * inch,
        alignment=TA_CENTER,
    )

    # ── Front matter ──────────────────────────────────────────────────────────
    s['half_title'] = ParagraphStyle(
        'half_title',
        fontName=reg, fontSize=22,
        leading=28,
        spaceBefore=2.8 * inch,
        spaceAfter=0,
        alignment=TA_CENTER,
    )
    s['title_main'] = ParagraphStyle(
        'title_main',
        fontName=reg, fontSize=28,
        leading=34,
        spaceBefore=2.2 * inch,
        spaceAfter=0.15 * inch,
        alignment=TA_CENTER,
    )
    s['title_sub'] = ParagraphStyle(
        'title_sub',
        fontName=ital, fontSize=14,
        leading=18,
        spaceBefore=0.1 * inch, spaceAfter=0,
        alignment=TA_CENTER,
    )
    s['title_author'] = ParagraphStyle(
        'title_author',
        fontName=reg, fontSize=16,
        leading=20,
        spaceBefore=2.2 * inch, spaceAfter=0,
        alignment=TA_CENTER,
    )
    s['epigraph'] = ParagraphStyle(
        'epigraph',
        fontName=ital, fontSize=body_size,
        leading=bod_lead * 1.1,
        spaceBefore=2.5 * inch, spaceAfter=4,
        leftIndent=0.75 * inch, rightIndent=0.75 * inch,
        alignment=TA_LEFT,
    )
    s['epigraph_attr'] = ParagraphStyle(
        'epigraph_attr',
        fontName=reg, fontSize=9,
        leading=13,
        spaceBefore=4, spaceAfter=0,
        leftIndent=0.75 * inch, rightIndent=0.75 * inch,
        alignment=TA_RIGHT,
    )
    s['copyright'] = ParagraphStyle(
        'copyright',
        fontName=reg, fontSize=8,
        leading=11,
        spaceBefore=0, spaceAfter=5,
        alignment=TA_LEFT,
    )
    s['copyright_center'] = ParagraphStyle(
        'copyright_center', parent=s['copyright'],
        alignment=TA_CENTER,
    )

    return s


# ─────────────────────────────────────────────────────────────────────────────
#  CUSTOM FLOWABLES
# ─────────────────────────────────────────────────────────────────────────────

class BlankPage(Flowable):
    """Force a completely blank page (no header/footer)."""
    _is_blank_page = True

    def __init__(self):
        Flowable.__init__(self)

    def wrap(self, avW, avH):
        return 0, 0

    def draw(self):
        pass


class ChapterBreak(Flowable):
    """
    Marks the start of a new chapter.
    Carries metadata so BookDocTemplate can build the correct running header.
    Also marks this as a 'chapter opening' page (no running header).
    """
    def __init__(self, chapter_title, chapter_num_text, is_first=False,
                 seq_num=None, chapter_name=''):
        Flowable.__init__(self)
        self.chapter_title    = chapter_title    # e.g. "Chapter Three"
        self.chapter_num_text = chapter_num_text
        self.is_first         = is_first
        self.seq_num          = seq_num          # int, e.g. 3
        self.chapter_name     = chapter_name     # e.g. "Thirty Thousand Feet"

    def wrap(self, avW, avH):
        return 0, 0

    def draw(self):
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  BOOK DOC TEMPLATE  (handles running headers / footers)
# ─────────────────────────────────────────────────────────────────────────────

class BookDocTemplate(BaseDocTemplate):
    """
    Subclass of BaseDocTemplate that:
      - Maintains the current chapter title for the running header
      - Tracks which pages are chapter-opening pages (no header)
      - Draws the appropriate running header and page number on each page
    """

    def __init__(self, filename, book_title, fonts, styles, **kwargs):
        BaseDocTemplate.__init__(self, filename, **kwargs)
        self.book_title             = book_title
        self.fonts                  = fonts
        self.styles                 = styles
        self.current_chapter        = ""        # "Chapter Three" style label
        self.current_seq_num        = None      # integer sequential number
        self.current_chapter_name   = ""        # evocative name e.g. "Home Tastes Like Regret"
        self._this_page_is_chapter_open = False # reset each page; set by ChapterBreak
        self.front_matter_pages     = 0

    def afterFlowable(self, flowable):
        """Called after each flowable is rendered. Update chapter tracker."""
        if isinstance(flowable, ChapterBreak):
            self.current_chapter            = flowable.chapter_title
            self.current_seq_num            = flowable.seq_num
            self.current_chapter_name       = flowable.chapter_name
            self._this_page_is_chapter_open = True   # suppress header on this page

    def handle_pageBegin(self):
        BaseDocTemplate.handle_pageBegin(self)
        self._this_page_is_chapter_open = False   # reset for every new page

    def handle_pageEnd(self):
        # Draw headers/footers BEFORE the base class commits the page with
        # showPage().  Drawing after showPage() puts content on the NEXT page,
        # which is what caused the "previous chapter's header on opener" bug.
        self._draw_header_footer()
        BaseDocTemplate.handle_pageEnd(self)

    def _draw_header_footer(self):
        c = self.canv
        page = self.page
        reg  = self.fonts['regular']
        ital = self.fonts['italic']

        # ── Page number ───────────────────────────────────────────────────────
        c.saveState()
        c.setFont(reg, 9)
        page_str = str(page)
        if page % 2 == 1:   # recto — number on the right
            c.drawRightString(M_IN + TEXT_W, FOOTER_Y, page_str)
        else:                # verso — number on the left
            c.drawString(M_IN, FOOTER_Y, page_str)

        # ── Running header (skip on chapter-opening pages) ────────────────────
        if not self._this_page_is_chapter_open:
            c.setFont(ital, 9)
            if page % 2 == 1:   # recto — "Ch N: Chapter Name" right-aligned
                if self.current_seq_num and self.current_chapter_name:
                    header_text = f'Ch {self.current_seq_num}: {self.current_chapter_name}'
                elif self.current_chapter:
                    header_text = self.current_chapter
                else:
                    header_text = self.book_title
                c.drawRightString(M_IN + TEXT_W, HEADER_Y, header_text)
            else:               # verso — book title, left-aligned
                c.drawString(M_IN, HEADER_Y, self.book_title)
            # thin rule under the header
            c.setLineWidth(0.5)
            c.setStrokeColor(Color(0.6, 0.6, 0.6))
            rule_y = HEADER_Y - 3
            c.line(M_IN, rule_y, M_IN + TEXT_W, rule_y)

        c.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
#  TEXT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def escape_xml(text):
    """Escape characters that would break ReportLab's XML parser."""
    return (
        text
        .replace('&',  '&amp;')
        .replace('<',  '&lt;')
        .replace('>',  '&gt;')
    )


def is_chapter_heading(text):
    t = text.strip()
    tu = t.upper()
    return (
        tu.startswith('CHAPTER ')
        or tu in ('PROLOGUE', 'EPILOGUE', 'PREFACE', 'AFTERWORD')
        or re.match(r'^PART\s+[IVXLCDM\d]+', tu)
        or re.match(r'^Chapter\s+(One|Two|Three|Four|Five|Six|Seven|Eight|'
                    r'Nine|Ten|Eleven|Twelve|Thirteen|Fourteen|Fifteen|'
                    r'Sixteen|Seventeen|Eighteen|Nineteen|Twenty|'
                    r'Twenty-[A-Za-z]+|Thirty|Forty|Fifty)', t)
    )


def is_section_break(text):
    return text.strip() in ('* * *', '***', '* * * * *', '—', '– –', '- - -')


def is_end_marker(text):
    t = text.strip()
    return (
        re.match(r'^—\s*END\s+OF', t, re.I)
        or re.match(r'^END\s+OF\s+CHAPTER', t, re.I)
    )


def looks_like_scene_header(text):
    """Short lines that look like scene/location lines, not body prose."""
    t = text.strip()
    if len(t) > 90 or '\n' in t:
        return False
    if is_chapter_heading(t) or is_section_break(t) or is_end_marker(t):
        return False
    # "City, Month Year." or "Place. Year."
    if re.match(r'^[A-Z][^.!?]+\.\s+\w', t) and len(t) < 70:
        return True
    # "The Year of the Garden" or "The Groundskeeper — ..."
    if re.match(r'^The [A-Z]', t) and len(t) < 80:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  IMAGE HANDLING
# ─────────────────────────────────────────────────────────────────────────────

def prepare_illustration(tif_path, tmp_dir, max_width_inch=4.0):
    """
    Convert a TIFF illustration to a JPEG for ReportLab embedding.
    Returns (jpeg_path, width_pt, height_pt) or None if unavailable.
    """
    if not PILLOW_AVAILABLE:
        return None
    tif = Path(tif_path)
    if not tif.exists():
        return None
    try:
        img = PILImage.open(tif)
        # Convert CMYK → RGB for JPEG embedding
        if img.mode == 'CMYK':
            img = img.convert('RGB')
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # Calculate display size (constrain to text width, maintain aspect)
        w_px, h_px = img.size
        aspect = h_px / w_px
        display_w = min(max_width_inch * inch, TEXT_W)
        display_h = display_w * aspect

        # Save as high-quality JPEG to temp directory
        out_path = Path(tmp_dir) / (tif.stem + "_rgb.jpg")
        img.save(out_path, 'JPEG', quality=95, dpi=(300, 300))

        return str(out_path), display_w, display_h
    except Exception as e:
        print(yellow(f"    ⚠  Could not process illustration {tif.name}: {e}"))
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  CHAPTER CONTENT → FLOWABLES
# ─────────────────────────────────────────────────────────────────────────────

def chapter_to_flowables(chapter_data, styles, illustration_info=None,
                          is_first_chapter=False, sequential_num=None,
                          use_illustrations=True, chapter_name='',
                          layout_mode='novel'):
    """
    Convert a chapter's data dict into a list of ReportLab flowables.

    chapter_data   : the parsed JSON dict from chapter_XX_edited.json
    sequential_num : 1-based position in reading order (for display)
    chapter_name   : evocative title from chapter_names.json (shown as subtitle
                     and in the recto running header as "Ch N: Name")
    layout_mode    : "novel"   → "Chapter Three" heading + optional subtitle
                     "poetry"  → section title only (from ingested title field),
                                 natural page flow, title in running header
                     "essays"  → essay title only, similar to poetry
    """
    flowables = []
    chapter_id    = chapter_data.get('chapter_id', '')
    full_text     = chapter_data.get('full_text', '')

    # ── Page break before every section except the very first ────────────────
    # (The first chapter follows directly after front matter.)
    if not is_first_chapter:
        flowables.append(PageBreak())

    # ── Parse paragraphs from full_text ──────────────────────────────────────
    raw_paras = [p.strip() for p in full_text.split('\n\n') if p.strip()]

    # ── Find where the heading line sits (so we can skip it in body text) ─────
    heading_idx = 0
    for i, p in enumerate(raw_paras):
        if is_chapter_heading(p):
            heading_idx = i
            break

    # ── Display heading ───────────────────────────────────────────────────────
    # Novel: "Chapter Three" (derived from sequential position — reliable even
    #        for intercalary / renumbered chapters in the manuscript).
    # Poetry / Essays: section title from the ingested data (which the runner
    #        populated from the original filename after stripping the order prefix).
    if chapter_id == 'epilogue':
        display_heading = 'Epilogue'
    elif layout_mode in ('poetry', 'essays'):
        # Prefer the ingested title; fall back to chapter_id if empty
        raw_title = chapter_data.get('title', '').strip()
        display_heading = raw_title if raw_title else str(chapter_id)
    else:
        display_heading = f'Chapter {ordinal_word(sequential_num)}'

    # ── ChapterBreak flowable (tells doc template this is a section open) ─────
    chapter_break = ChapterBreak(
        chapter_title    = display_heading,
        chapter_num_text = display_heading,
        is_first         = is_first_chapter,
        seq_num          = sequential_num if chapter_id != 'epilogue' else None,
        # For poetry the "chapter_name" subtitle is redundant — the title IS the name.
        chapter_name     = chapter_name if layout_mode == 'novel' else '',
    )
    flowables.append(chapter_break)

    # ── Section heading block ─────────────────────────────────────────────────
    flowables.append(Spacer(1, 0.8 * inch))

    heading_safe = escape_xml(display_heading)
    flowables.append(Paragraph(heading_safe, styles['chapter_title']))

    # ── Chapter name subtitle (novel only — evocative title, italic, centred) ─
    if chapter_name and layout_mode == 'novel':
        flowables.append(Paragraph(escape_xml(chapter_name), styles['chapter_subtitle']))

    # ── Illustration ──────────────────────────────────────────────────────────
    if use_illustrations and illustration_info:
        img_path, img_w, img_h = illustration_info
        try:
            img_flowable = Image(img_path, width=img_w, height=img_h)
            img_flowable.hAlign = 'CENTER'
            flowables.append(Spacer(1, 0.15 * inch))
            flowables.append(img_flowable)
            flowables.append(Spacer(1, 0.3 * inch))
        except Exception as e:
            print(yellow(f"    ⚠  Could not embed illustration: {e}"))
    else:
        flowables.append(Spacer(1, 0.2 * inch))
        flowables.append(HRFlowable(width=TEXT_W * 0.25, thickness=0.5,
                                     lineCap='round', color=Color(0.5,0.5,0.5),
                                     spaceAfter=0.25 * inch, hAlign='CENTER'))

    # ── Body paragraphs ───────────────────────────────────────────────────────
    # Collect paragraphs after the heading line; skip front matter for ch 1
    body_paras = raw_paras[heading_idx + 1:]

    # Also collect any subtitle/scene line right after heading
    subtitle_lines = []
    body_start = 0
    for i, p in enumerate(body_paras):
        if looks_like_scene_header(p) or (
            not is_chapter_heading(p)
            and not is_section_break(p)
            and not is_end_marker(p)
            and len(p) < 80
            and i < 3
            and p[0].isupper()
            and not p[0] == '"'
            and not p[0] == '\u201c'
        ):
            subtitle_lines.append(p)
            body_start = i + 1
        else:
            break

    for sub in subtitle_lines:
        flowables.append(Paragraph(markup_for_cyrillic(sub, _CYR_FONT_REG),
                                   styles['scene_header']))

    # Remaining body text
    first_body = True
    for p in body_paras[body_start:]:
        if not p:
            continue
        if is_end_marker(p):
            break
        if is_section_break(p):
            flowables.append(Paragraph('✦', styles['section_break']))
            first_body = True   # next paragraph after break = no indent
            continue
        if is_chapter_heading(p):
            # Sub-chapter heading (shouldn't normally appear)
            flowables.append(Paragraph(escape_xml(p), styles['chapter_subtitle']))
            first_body = True
            continue

        # Regular body paragraph — use Cyrillic-aware markup
        p_safe = markup_for_cyrillic(p, _CYR_FONT_REG)
        style  = styles['body_first'] if first_body else styles['body']
        flowables.append(Paragraph(p_safe, style))
        first_body = False

    return flowables


# ─────────────────────────────────────────────────────────────────────────────
#  FRONT MATTER
# ─────────────────────────────────────────────────────────────────────────────

def build_front_matter(config, styles):
    """
    Returns a list of flowables for:
      p1 (recto)  — half-title
      p2 (verso)  — blank
      p3 (recto)  — full title page
      p4 (verso)  — copyright
      p5 (recto)  — epigraph
      p6 (verso)  — blank
    """
    book   = config['book']
    title  = book['title']
    author = book.get('author', '')
    year   = datetime.now().year
    flowables = []

    # ── Half-title page ───────────────────────────────────────────────────────
    flowables.append(Paragraph(escape_xml(title), styles['half_title']))
    flowables.append(PageBreak())

    # ── Blank verso ──────────────────────────────────────────────────────────
    flowables.append(Spacer(1, 1))   # placeholder
    flowables.append(PageBreak())

    # ── Full title page ───────────────────────────────────────────────────────
    # Subtitle: use config override, else derive from genre, else skip
    genre    = book.get('genre', '').strip().lower()
    subtitle = book.get('subtitle', '').strip()
    if not subtitle:
        _genre_subtitle = {
            'novel': 'A Novel',
            'fiction': 'A Novel',
            'literary fiction': 'A Novel',
            'science fiction': 'A Novel',
            'fantasy': 'A Novel',
            'mystery': 'A Novel',
            'thriller': 'A Novel',
            'romance': 'A Novel',
            'horror': 'A Novel',
            'poetry': 'Poems',
            'essays': 'Essays',
            "children's fiction": 'A Story',
            'childrens fiction': 'A Story',
            'short stories': 'Stories',
        }
        subtitle = _genre_subtitle.get(genre, '')
    flowables.append(Paragraph(escape_xml(title), styles['title_main']))
    if subtitle:
        flowables.append(Paragraph(escape_xml(subtitle), styles['title_sub']))
    flowables.append(Paragraph(escape_xml(author), styles['title_author']))
    flowables.append(PageBreak())

    # ── Copyright page ────────────────────────────────────────────────────────
    cr_lines = [
        f'Copyright © {year} {author}',
        '',
        'All rights reserved. No part of this publication may be reproduced, '
        'distributed, or transmitted in any form or by any means without the '
        'prior written permission of the publisher.',
        '',
        'This is a work of fiction. Names, characters, places, and incidents '
        'are either the product of the author\'s imagination or are used '
        'fictitiously.',
        '',
        'First published ' + str(year),
        '',
        f'Printed by Lulu Press, Inc.',
    ]
    flowables.append(Spacer(1, 2.8 * inch))
    for line in cr_lines:
        if line:
            flowables.append(Paragraph(escape_xml(line), styles['copyright']))
        else:
            flowables.append(Spacer(1, 5))
    flowables.append(PageBreak())

    # ── Epigraph page (read from config, skip if not set) ────────────────────
    epigraph_cfg = book.get('epigraph', {})
    epigraph_text = epigraph_cfg.get('text', '').strip()
    epigraph_attr = epigraph_cfg.get('attribution', '').strip()
    if epigraph_text:
        flowables.append(Paragraph(escape_xml(f'\u201c{epigraph_text}\u201d'), styles['epigraph']))
        if epigraph_attr:
            flowables.append(Paragraph(escape_xml(f'\u2014 {epigraph_attr}'), styles['epigraph_attr']))
        flowables.append(PageBreak())

    # ── Blank verso (so Chapter 1 opens on recto page 7) ─────────────────────
    flowables.append(Spacer(1, 1))
    flowables.append(PageBreak())

    return flowables   # 6 pages total


# ─────────────────────────────────────────────────────────────────────────────
#  LOAD CHAPTER DATA
# ─────────────────────────────────────────────────────────────────────────────

def load_chapter(chapter_key, editing_dir):
    """
    Load the edited JSON for a chapter.
    chapter_key: int (1–30) or 'epilogue'
    """
    if chapter_key == 'epilogue':
        fname = 'epilogue_edited.json'
    else:
        fname = f'chapter_{int(chapter_key):02d}_edited.json'

    path = Path(editing_dir) / fname
    if not path.exists():
        print(red(f"  ✗  Missing: {path.name}"))
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def find_illustration(chapter_key, illus_dir):
    """Return the illustration path for a chapter, or None."""
    if chapter_key == 'epilogue':
        illus_dir = Path(illus_dir)
        # Agent 4 saves epilogue as 'epilogue.tif' (no 'chapter_' prefix)
        for ext in ('.tif', '.tiff', '.png', '.jpg', '.jpeg'):
            for stem in ('epilogue', 'chapter_epilogue'):
                p = illus_dir / f'{stem}{ext}'
                if p.exists():
                    return p
        # Final fallback: glob for anything with 'epilogue' in the name
        candidates = sorted(illus_dir.glob('*epilogue*'))
        if candidates:
            return candidates[0]
        return None
    num = int(chapter_key)
    for ext in ('.tif', '.tiff', '.jpg', '.png'):
        p = Path(illus_dir) / f'chapter_{num:02d}{ext}'
        if p.exists():
            return p
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN BUILD
# ─────────────────────────────────────────────────────────────────────────────

def build_pdf(config, args, report):
    """Assemble the complete book PDF."""

    FORMATTING_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)

    fonts  = setup_fonts()
    setup_cyrillic_font()   # Register DejaVu Serif for Cyrillic passages
    styles = build_styles(
        fonts,
        line_spacing = config['formatting']['fonts']['line_spacing'],
        body_size    = config['formatting']['fonts']['body_size_pt'],
        heading_size = config['formatting']['fonts']['chapter_heading_size_pt'],
    )

    # ── Chapter names (from generate_chapter_names.py) ────────────────────────
    chapter_names = {}
    if CHAPTER_NAMES_PATH.exists():
        try:
            chapter_names = json.loads(CHAPTER_NAMES_PATH.read_text(encoding='utf-8'))
            print(green(f'  ✓  Chapter names loaded ({len(chapter_names)} entries)'))
        except Exception as e:
            print(yellow(f'  ⚠  Could not load chapter names: {e}'))

    book_title  = config['book']['title']
    layout_mode = config.get('layout_mode', 'novel')   # novel | poetry | essays
    # Name the PDF after the book title (slugified), falling back to a fixed name
    safe_title = re.sub(r'[^\w\s-]', '', book_title).strip().replace(' ', '_').lower()
    output_pdf  = FINAL_DIR / f'{safe_title}_lulu.pdf'

    # Always delete the old PDF before rebuilding — prevents ReportLab from
    # reusing stale cached state and guarantees the file is fresh after each run.
    if output_pdf.exists():
        output_pdf.unlink()
        print(yellow(f"  ↺  Removed old PDF: {output_pdf.name}"))

    print(cyan(f"\n  Building: {output_pdf.name}"))
    print(cyan(f"  Trim:     6.0\" × 9.0\" | Margins: {M_IN/inch:.2f}\" gutter, "
               f"{M_OUT/inch:.2f}\" outside"))
    print(cyan(f"  Font:     {fonts['name']}  {config['formatting']['fonts']['body_size_pt']}pt"))

    # ── Page frame ────────────────────────────────────────────────────────────
    frame = Frame(
        x1     = M_IN,
        y1     = M_BOT,
        width  = TEXT_W,
        height = TEXT_H,
        leftPadding=0, rightPadding=0,
        topPadding=0,  bottomPadding=0,
        id='body_frame',
    )
    page_template = PageTemplate(id='Normal', frames=[frame])

    doc = BookDocTemplate(
        str(output_pdf),
        book_title = book_title,
        fonts      = fonts,
        styles     = styles,
        pagesize   = (PAGE_W, PAGE_H),
        leftMargin  = M_IN,
        rightMargin = M_OUT,
        topMargin   = M_TOP,
        bottomMargin= M_BOT,
    )
    doc.addPageTemplates([page_template])

    # ── Collect all flowables ─────────────────────────────────────────────────
    all_flowables = []

    # Front matter
    print(f"\n  {bold('Front matter')} ...")
    all_flowables += build_front_matter(config, styles)
    doc.front_matter_pages = 6

    # Create a temp directory for converted illustration images
    tmp_dir = tempfile.mkdtemp(prefix='layout_illus_')

    reading_order = config['book'].get('reading_order')
    if not reading_order:
        # Fallback: derive reading order from edited chapter files on disk
        import re as _re
        keys = []
        for f in sorted(EDITING_DIR.glob('chapter_*_edited.json')):
            m = _re.match(r'chapter_(\d+)_edited\.json', f.name)
            if m:
                keys.append(int(m.group(1)))
        reading_order = sorted(keys) if keys else []
        if reading_order:
            print(yellow(f'  ⚠  No reading_order in config — derived from {len(reading_order)} files on disk'))

    # Filter to only chapters requested (--chapter flag)
    if args.chapter:
        wanted = str(args.chapter).lstrip('0') or '1'
        # map to key in reading_order
        chapter_filter = []
        for key in reading_order:
            if str(key) == wanted or key == wanted:
                chapter_filter = [key]
                break
    else:
        chapter_filter = reading_order

    # If building only one chapter, still include front matter
    chapters_done     = 0
    chapters_skipped  = 0
    illustrations_used = 0

    for seq_num, chapter_key in enumerate(chapter_filter, start=1):
        label = f'Chapter {chapter_key}' if chapter_key != 'epilogue' else 'Epilogue'
        print(f"  {green('►')}  {label}  ", end='', flush=True)

        # Load chapter data
        chapter_data = load_chapter(chapter_key, EDITING_DIR)
        if chapter_data is None:
            print(red('MISSING — skipped'))
            chapters_skipped += 1
            report['skipped'].append(str(chapter_key))
            continue

        word_count = chapter_data.get('word_count', 0)
        print(f"({word_count:,} words)", end=' ')

        # Load illustration
        illustration_info = None
        if not args.no_illustrations:
            tif_path = find_illustration(chapter_key, ILLUS_DIR)
            if tif_path:
                result = prepare_illustration(tif_path, tmp_dir)
                if result:
                    illustration_info = result
                    illustrations_used += 1
                    print(green('🖼'), end=' ')
                else:
                    print(yellow('(illus. error)'), end=' ')
            else:
                print(yellow('(no illus.)'), end=' ')

        # Look up evocative chapter name
        key_str     = 'epilogue' if chapter_key == 'epilogue' else str(int(chapter_key))
        chap_name   = chapter_names.get(key_str, '')

        # Convert to flowables
        is_first = (seq_num == 1 and not args.chapter)
        chapter_flowables = chapter_to_flowables(
            chapter_data      = chapter_data,
            styles            = styles,
            illustration_info = illustration_info,
            is_first_chapter  = is_first,
            layout_mode       = layout_mode,
            sequential_num    = seq_num,
            use_illustrations = not args.no_illustrations,
            chapter_name      = chap_name,
        )

        # chapter_to_flowables() already inserts a PageBreak for non-first
        # chapters, so we just extend here (no extra PageBreak needed).
        all_flowables.extend(chapter_flowables)

        chapters_done += 1
        report['chapters_included'].append({
            'key':        str(chapter_key),
            'label':      label,
            'word_count': word_count,
            'illustration': illustration_info is not None,
        })
        print(green('✓'))

    # ── Build PDF ─────────────────────────────────────────────────────────────
    print(f"\n  {bold('Rendering PDF')}  ({chapters_done} chapters) ...", flush=True)
    try:
        doc.build(all_flowables)
    except Exception as e:
        print(red(f"\n  ✗  PDF build failed: {e}"))
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Clean up temp illustrations
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Verify output ─────────────────────────────────────────────────────────
    if output_pdf.exists():
        size_mb = output_pdf.stat().st_size / (1024 * 1024)
        page_count = doc.page
        print(green(f"  ✓  PDF written: {output_pdf.name}"))
        print(green(f"     Pages: {page_count} | Size: {size_mb:.1f} MB"))
        report['output_pdf']     = str(output_pdf)
        report['page_count']     = page_count
        report['file_size_mb']   = round(size_mb, 2)
        report['illustrations_embedded'] = illustrations_used
        report['chapters_done']  = chapters_done
        report['chapters_skipped'] = chapters_skipped
        return True
    else:
        print(red("  ✗  Output PDF not found after build"))
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  REPORT
# ─────────────────────────────────────────────────────────────────────────────

def write_reports(report, config):
    """Write JSON and Markdown layout reports to output/formatting/."""
    FORMATTING_DIR.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = FORMATTING_DIR / 'layout_report.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)

    # Markdown
    md_path = FORMATTING_DIR / 'layout_report.md'
    book    = config['book']
    fmt     = config['formatting']['available_formats']['hardcover']

    lines = [
        f"# Layout Report — {book['title']}",
        f"",
        f"Generated: {report['generated_at']}",
        f"",
        f"## PDF Output",
        f"",
        f"- **File:** `{Path(report.get('output_pdf','(not built)')).name}`",
        f"- **Pages:** {report.get('page_count','—')}",
        f"- **File size:** {report.get('file_size_mb','—')} MB",
        f"- **Illustrations embedded:** {report.get('illustrations_embedded','—')}",
        f"",
        f"## Lulu Hardcover Specs Applied",
        f"",
        f"| Setting | Value |",
        f"|---------|-------|",
        f"| Trim size | {fmt['trim_width_inches']}\" × {fmt['trim_height_inches']}\" |",
        f"| Top margin | {fmt['margin_top_inches']}\" |",
        f"| Bottom margin | {fmt['margin_bottom_inches']}\" |",
        f"| Inside (gutter) | {fmt['margin_inside_inches']}\" |",
        f"| Outside | {fmt['margin_outside_inches']}\" |",
        f"| Bleed (not in interior PDF) | {fmt['bleed_inches']}\" |",
        f"| Font | {config['formatting']['fonts']['body']} "
        f"{config['formatting']['fonts']['body_size_pt']}pt |",
        f"| Line spacing | {config['formatting']['fonts']['line_spacing']}× |",
        f"| Lulu product | {fmt['lulu_product']} |",
        f"",
        f"## Chapters",
        f"",
    ]

    for ch in report.get('chapters_included', []):
        illus = '🖼' if ch['illustration'] else '—'
        lines.append(
            f"- **{ch['label']}** — {ch['word_count']:,} words  |  illustration: {illus}"
        )

    if report.get('skipped'):
        lines += ['', '## Skipped', '']
        for s in report['skipped']:
            lines.append(f"- Chapter {s}")

    lines += [
        '',
        '## Font Notes',
        '',
        f"Font used: **{report.get('font_used', 'Times-Roman')}**",
        '',
        'To use Garamond, place `.ttf` or `.otf` font files in:',
        '`agents/05_layout/fonts/`',
        '',
        'Recommended free fonts: EB Garamond (fonts.google.com)',
    ]

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(green(f"  ✓  Reports written to output/formatting/"))
    return json_path, md_path


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print()
    print(bold(cyan('╔══════════════════════════════════════════════════════════════════╗')))
    print(bold(cyan('║  AGENT 5 — LAYOUT                                               ║')))
    print(bold(cyan('║  One Thousand Perfect Sighs  →  Lulu 6"×9" hardcover PDF        ║')))
    print(bold(cyan('╚══════════════════════════════════════════════════════════════════╝')))
    print()

    # ── Args ──────────────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description='Agent 5 — Layout: build the print-ready Lulu hardcover PDF'
    )
    parser.add_argument(
        '--chapter', metavar='N',
        help='Layout a single chapter only (e.g. --chapter 01)'
    )
    parser.add_argument(
        '--no-illustrations', action='store_true',
        help='Skip embedding illustrations (faster, useful for proofing)'
    )
    args = parser.parse_args()

    # ── Config ────────────────────────────────────────────────────────────────
    if not CONFIG_PATH.exists():
        print(red(f'  ✗  config.json not found at {CONFIG_PATH}'))
        sys.exit(1)
    with open(CONFIG_PATH, encoding='utf-8') as f:
        config = json.load(f)
    print(green(f'  ✓  Config loaded'))

    if args.no_illustrations:
        print(yellow('  ℹ  --no-illustrations: illustrations will be skipped'))

    # ── Report skeleton ───────────────────────────────────────────────────────
    report = {
        'generated_at':        datetime.now().isoformat(),
        'book_title':          config['book']['title'],
        'format':              'hardcover',
        'lulu_product':        'hardcover-casewrap',
        'trim_size':           '6.0" × 9.0"',
        'font_used':           None,
        'chapters_included':   [],
        'chapters_done':       0,
        'chapters_skipped':    0,
        'skipped':             [],
        'illustrations_embedded': 0,
        'output_pdf':          None,
        'page_count':          None,
        'file_size_mb':        None,
    }

    # ── Font check message ────────────────────────────────────────────────────
    print(f'\n  {bold("Fonts")}')
    tmp_fonts = setup_fonts()
    setup_cyrillic_font()
    report['font_used'] = tmp_fonts['name']

    # ── Editing output check ──────────────────────────────────────────────────
    edited_files = list(EDITING_DIR.glob('*_edited.json'))
    if not edited_files:
        print(red(f'\n  ✗  No edited chapter files found in {EDITING_DIR}'))
        print(red('     Run Agent 3 (edit.py) first.'))
        sys.exit(1)
    print(green(f'\n  ✓  Found {len(edited_files)} edited chapter files'))

    # ── Build ─────────────────────────────────────────────────────────────────
    print(f'\n  {bold("Building PDF")} ...')
    success = build_pdf(config, args, report)

    # ── Reports ───────────────────────────────────────────────────────────────
    print(f'\n  {bold("Writing reports")} ...')
    write_reports(report, config)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if success:
        print(bold(green('  ═══════════════════════════════════════════════════')))
        print(bold(green(f'  ✓  COMPLETE')))
        print(bold(green(f'     {report["page_count"]} pages  |  {report["file_size_mb"]} MB')))
        print(bold(green(f'     {report["illustrations_embedded"]} illustrations embedded')))
        print(bold(green(f'     output/final/one_thousand_perfect_sighs_lulu.pdf')))
        print(bold(green('  ═══════════════════════════════════════════════════')))
    else:
        print(bold(red('  ✗  Build failed — see errors above')))
        sys.exit(1)

    print()


if __name__ == '__main__':
    main()
