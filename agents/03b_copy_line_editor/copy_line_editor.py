"""
╔══════════════════════════════════════════════════════════════════╗
║  AGENT 3b — COPY & LINE EDITOR                                   ║
║  Cassian Publishing Pipeline                                     ║
║                                                                  ║
║  What this does:                                                 ║
║    Applies two tiers of editing to each chapter:                ║
║                                                                  ║
║    TIER 1 — AUTOMATIC FIXES (no AI needed):                     ║
║      • Universal: smart quotes, double spaces, ellipsis,        ║
║        trailing whitespace                                       ║
║      • Project-specific: configurable find/replace pairs        ║
║        from config.json["editing"]["auto_fixes"]                ║
║                                                                  ║
║    TIER 2 — AI-ASSISTED PROPOSALS (Gemini):                     ║
║      • Grammar, spelling, punctuation (copy editing)            ║
║      • Word choice, rhythm, redundancy, flow (line editing)     ║
║      • Proposals saved for author review — nothing auto-applied ║
║                                                                  ║
║  This agent does NOT address structural/developmental issues —   ║
║  those belong to the Developmental Editor (Agent 3a).           ║
║                                                                  ║
║  Input:   output/editing/chapter_XX_edited.json  (preferred)   ║
║           output/ingested/chapter_XX.json        (fallback)    ║
║           output/editing/world_rules_export.json  (optional)   ║
║           output/consistency/consistency_report.json (optional) ║
║           output/dev_editing/dev_report.json      (optional)   ║
║  Output:  output/editing/chapter_XX_edited.json   (tier-1 base)║
║           output/editing_proposals/chapter_XX_proposals.json   ║
║           output/editing/changelog.md                          ║
║           output/editing/flags_for_review.md                   ║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/03b_copy_line_editor/copy_line_editor.py      ║
║    python agents/03b_copy_line_editor/copy_line_editor.py \    ║
║           --chapter 01   (one chapter only)                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from colorama import init, Fore, Style
init(autoreset=True)


# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = (
    Path(os.environ['CASSIAN_PROJECT_DIR'])
    if 'CASSIAN_PROJECT_DIR' in os.environ
    else Path(__file__).resolve().parent.parent.parent
)
CONFIG_PATH     = BASE_DIR / "config.json"
INGESTED_DIR    = BASE_DIR / "output" / "ingested"
EDITING_DIR     = BASE_DIR / "output" / "editing"
CONSISTENCY_DIR = BASE_DIR / "output" / "consistency"
DEV_EDITING_DIR = BASE_DIR / "output" / "dev_editing"
PROPOSALS_DIR   = BASE_DIR / "output" / "editing_proposals"


# ── Console helpers ───────────────────────────────────────────────────────────
def ok(msg):   print(f"{Fore.GREEN}  ✓ {msg}{Style.RESET_ALL}")
def info(msg): print(f"{Fore.CYAN}  → {msg}{Style.RESET_ALL}")
def warn(msg): print(f"{Fore.YELLOW}  ⚠ {msg}{Style.RESET_ALL}")
def err(msg):  print(f"{Fore.RED}  ✗ {msg}{Style.RESET_ALL}")
def head(msg): print(f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}")


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config.json if it exists. Returns {} if missing/unreadable."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def load_world_rules() -> list[dict]:
    """
    Load world rules, checking three candidate locations in priority order:
      1. output/editing/world_rules_export.json      (written by _pre_copy_editor_setup)
      2. output/dev_editing/world_rules_export.json  (fallback from dev editor run)
      3. output/consistency/world_rules_export.json  (fallback from consistency run)
    Returns empty list if none found.
    """
    candidates = [
        EDITING_DIR     / "world_rules_export.json",
        DEV_EDITING_DIR / "world_rules_export.json",
        CONSISTENCY_DIR / "world_rules_export.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    rules = json.load(f)
                if rules:
                    return rules
            except Exception:
                continue
    return []


def load_consistency_issues_for_chapter(chapter_id: str) -> list[str]:
    """
    Load consistency issues relevant to a specific chapter from the consistency
    report. Returns a list of issue description strings, or empty list.
    """
    report_path = CONSISTENCY_DIR / "consistency_report.json"
    if not report_path.exists():
        return []
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            report = json.load(f)
        issues = []
        for issue in report.get("issues", []):
            affected = [str(c) for c in issue.get("chapters_affected", [])]
            if str(chapter_id) in affected:
                desc = issue.get("description", "")
                if desc:
                    issues.append(desc)
        return issues
    except Exception:
        return []


def load_dev_report_summary() -> str:
    """
    Load the overall summary from the developmental editor report, so Gemini
    knows the big-picture context. Returns empty string if not found.
    """
    report_path = DEV_EDITING_DIR / "dev_report.json"
    if not report_path.exists():
        return ""
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            report = json.load(f)
        summary = report.get("overall_assessment", {}).get("summary", "")
        return summary
    except Exception:
        return ""


def load_chapters(target_chapter: str = None) -> list[dict]:
    """
    Load chapters for editing.

    Prefers already-edited versions from output/editing/ (from a previous
    agent pass) over raw ingested versions from output/ingested/. This way
    if Agent 3 (old editor) or a previous 3b run already applied Tier 1 fixes,
    we don't duplicate them.

    Returns chapters sorted by chapter number (epilogue last).
    """
    if target_chapter:
        if target_chapter == "epilogue":
            pairs = [(EDITING_DIR / "epilogue_edited.json",
                      INGESTED_DIR / "epilogue.json")]
        else:
            padded = target_chapter.zfill(2)
            pairs = [(EDITING_DIR  / f"chapter_{padded}_edited.json",
                      INGESTED_DIR / f"chapter_{padded}.json")]
    else:
        ingested_paths = sorted(INGESTED_DIR.glob("chapter_*.json"))
        epilogue_path  = INGESTED_DIR / "epilogue.json"
        if epilogue_path.exists():
            ingested_paths = list(ingested_paths) + [epilogue_path]

        pairs = []
        for ip in ingested_paths:
            if ip.name == "epilogue.json":
                pairs.append((EDITING_DIR / "epilogue_edited.json", ip))
            else:
                # chapter_01.json → chapter_01_edited.json
                pairs.append((EDITING_DIR / f"{ip.stem}_edited.json", ip))

    chapters = []
    for edited_path, ingested_path in pairs:
        if edited_path.exists():
            with open(edited_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data["_source"] = "editing"
            chapters.append(data)
        elif ingested_path.exists():
            with open(ingested_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data["_source"] = "ingested"
            chapters.append(data)
        else:
            warn(f"No file found for: {ingested_path.name}")

    chapters.sort(key=lambda c: 9999 if c.get("chapter_id") == "epilogue"
                                else (c.get("chapter_number") or 0))
    return chapters


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 1 — UNIVERSAL + CONFIGURABLE AUTO-FIXES
#  These are mechanical — no AI, no judgment required.
# ══════════════════════════════════════════════════════════════════════════════

def _smart_double_quotes(text: str) -> tuple[str, bool]:
    """
    Convert straight double quotes to curly (smart) quotes.
    Opening quote after whitespace, open brackets, or em/en-dashes.
    Closing quote everywhere else.
    Returns (converted_text, was_changed).
    """
    OPEN_CONTEXT  = set(' \t\n\r([{—–-\u201c')
    result  = []
    changed = False
    for i, ch in enumerate(text):
        if ch == '"':
            prev = text[i - 1] if i > 0 else ' '
            if prev in OPEN_CONTEXT:
                result.append('\u201c')  # "
            else:
                result.append('\u201d')  # "
            changed = True
        else:
            result.append(ch)
    return ''.join(result), changed


def apply_universal_fixes(text: str) -> tuple[str, list[str]]:
    """
    Apply universal mechanical fixes that apply to every project.
    Returns (fixed_text, list_of_change_descriptions).
    """
    changes = []

    # Double spaces → single space
    fixed = re.sub(r'  +', ' ', text)
    if fixed != text:
        changes.append("Double spaces normalised to single space")
        text = fixed

    # Trailing whitespace on each line
    fixed = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
    if fixed != text:
        changes.append("Trailing whitespace removed")
        text = fixed

    # Ellipsis: ... → … (unicode ellipsis character, U+2026)
    fixed = text.replace('...', '\u2026')
    if fixed != text:
        changes.append("Ellipsis normalised to unicode ellipsis (…)")
        text = fixed

    # Smart double quotes
    fixed, changed = _smart_double_quotes(text)
    if changed:
        changes.append("Straight double quotes converted to curly smart quotes")
        text = fixed

    return text, changes


def apply_project_fixes(text: str, auto_fixes: list[dict]) -> tuple[str, list[str]]:
    """
    Apply project-specific auto-fixes defined in config.json:
        "editing": {
            "auto_fixes": [
                {"find": "OldName", "replace": "NewName", "description": "Name fix"}
            ]
        }
    Returns (fixed_text, list_of_change_descriptions).
    """
    changes = []
    for fix in auto_fixes:
        find        = fix.get("find", "")
        replace     = fix.get("replace", "")
        description = fix.get("description", f'"{find}" → "{replace}"')
        if find and find in text:
            text = text.replace(find, replace)
            changes.append(description)
    return text, changes


def apply_tier1_to_paragraphs(
    paragraphs: list[dict],
    auto_fixes: list[dict],
) -> tuple[list[dict], list[str]]:
    """Run all Tier 1 fixes across every paragraph in a chapter."""
    all_changes = []
    fixed = []
    for para in paragraphs:
        text = para.get("text", "")
        text, u_ch = apply_universal_fixes(text)
        text, p_ch = apply_project_fixes(text, auto_fixes)
        fixed.append({**para, "text": text})
        all_changes.extend(u_ch)
        all_changes.extend(p_ch)
    # Deduplicate while preserving order
    return fixed, list(dict.fromkeys(all_changes))


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 2 — AI-ASSISTED PROPOSALS (Gemini)
# ══════════════════════════════════════════════════════════════════════════════

CREATIVITY_GUIDES = {
    1: (
        "CREATIVITY LEVEL 1 — ERRORS ONLY:\n"
        "Fix grammar, spelling, punctuation, and clear style guide violations.\n"
        "Do NOT rephrase any grammatically correct sentence.\n"
        "Do NOT adjust word choice for style or rhythm."
    ),
    2: (
        "CREATIVITY LEVEL 2 — ERRORS + CLARITY:\n"
        "Fix all errors as above. Also rewrite genuinely unclear sentences\n"
        "where the meaning is ambiguous or obscured.\n"
        "Minor clarity improvements only — no stylistic changes."
    ),
    3: (
        "CREATIVITY LEVEL 3 — ERRORS + FLOW:\n"
        "Fix errors. Improve sentence clarity. Tighten prose: remove redundancy,\n"
        "trim wordy phrases, smooth transitions between paragraphs.\n"
        "Preserve the author's sentence structures where they work."
    ),
    4: (
        "CREATIVITY LEVEL 4 — ACTIVE LINE EDITING:\n"
        "Fix errors and actively improve prose rhythm, word choice, and impact.\n"
        "You may rewrite sentences to improve cadence and precision,\n"
        "but always preserve the author's voice and stylistic signature."
    ),
    5: (
        "CREATIVITY LEVEL 5 — FULL PROSE POLISH:\n"
        "Fix errors and rewrite freely at the sentence and paragraph level\n"
        "for maximum clarity, rhythm, and impact.\n"
        "Treat the prose as a first draft to be polished.\n"
        "Preserve meaning, voice, and all plot/character content."
    ),
}


def _build_world_rules_block(world_rules: list[dict]) -> str:
    if not world_rules:
        return (
            "WORLD RULES:\n"
            "No world rules defined. Apply standard copy/line editing judgment."
        )
    grouped: dict[str, list] = {}
    for rule in world_rules:
        cat = rule.get("category", "general")
        grouped.setdefault(cat, []).append(rule)
    lines = ["ESTABLISHED WORLD RULES (do not 'correct' these — they are intentional):"]
    for cat, rules in grouped.items():
        lines.append(f"  [{cat.upper()}]")
        for r in rules:
            lines.append(f"  • {r['title']}: {r['content']}")
    return "\n".join(lines)


def build_copy_edit_prompt(
    chapter: dict,
    config: dict,
    world_rules: list[dict],
    consistency_issues: list[str],
    dev_summary: str,
) -> str:
    """
    Build the Gemini prompt for copy and line editing.
    Generic — works for any book.
    Requests paragraph-level proposals in the exact format the review UI expects.
    """
    book_title  = config.get("book", {}).get("title", "Untitled")
    book_author = config.get("book", {}).get("author", "Unknown Author")
    book_genre  = config.get("book", {}).get("genre", "fiction")
    creativity  = config.get("editing", {}).get("creativity_level", 3)
    creativity_desc = CREATIVITY_GUIDES.get(creativity, CREATIVITY_GUIDES[3])

    ch_id      = chapter.get("chapter_id", "?")
    title      = chapter.get("title", "Untitled")
    paragraphs = chapter.get("paragraphs", [])

    # Number the paragraphs so Gemini can reference them by index
    para_lines = []
    for i, p in enumerate(paragraphs):
        text = p.get("text", "").strip()
        if text:
            para_lines.append(f"[{i}] {text}")
    paragraphs_text = "\n\n".join(para_lines)

    world_rules_block = _build_world_rules_block(world_rules)

    consistency_block = ""
    if consistency_issues:
        consistency_block = (
            "CONSISTENCY ISSUES FOR THIS CHAPTER "
            "(fix at the prose level where possible):\n"
            + "\n".join(f"  • {issue}" for issue in consistency_issues)
        )

    dev_block = ""
    if dev_summary:
        dev_block = (
            "DEVELOPMENTAL EDITOR CONTEXT (for background — do NOT address\n"
            "structural or plot issues here; focus only on prose-level editing):\n"
            f"  {dev_summary}"
        )

    context_parts = "\n\n".join(
        part for part in [world_rules_block, consistency_block, dev_block] if part
    )

    return f"""You are a professional copy editor and line editor working on Chapter {ch_id} ("{title}")
of a {book_genre} novel called "{book_title}" by {book_author}.

YOUR ROLE:
  Copy editing: Fix grammar, spelling, punctuation, sentence structure, style consistency,
    dialogue tag formatting, number/date/capitalisation consistency.
  Line editing: Improve word choice and precision, sentence rhythm and cadence, remove
    redundancy and filler, improve show-vs-tell at the sentence level, smooth transitions
    between paragraphs, flag overused words or phrases.

CRITICAL CONSTRAINTS — READ BEFORE EDITING:
  • Do NOT suggest structural changes, plot modifications, or character arc adjustments.
    Those are handled by the Developmental Editor. Focus only on prose quality.
  • Do NOT change dialogue content, character names, proper nouns, or plot events.
  • Do NOT alter intentional stylistic choices defined in the World Rules below.
  • Preserve the author's voice above all else. When in doubt, do LESS, not more.

{creativity_desc}

{context_parts}

OUTPUT FORMAT — RETURN ONLY VALID JSON:
Return ONLY the paragraphs you actually want to change. If a paragraph is fine, omit it.
For each changed paragraph return:
  - "index"       : the [N] number shown before the paragraph (integer)
  - "original"    : the exact original text, copied verbatim from below
  - "proposed"    : your improved version
  - "change_type" : one of: grammar | word_choice | rhythm | redundancy |
                    clarity | punctuation | style | continuity | other
  - "reason"      : one sentence explaining what changed and why

For anything you noticed but chose not to change, add a note to "flagged_items".
If you have no suggestions at all, return an empty "paragraph_edits" array.

{{
  "chapter_id": "{ch_id}",
  "paragraph_edits": [
    {{
      "index": 0,
      "original": "exact original text copied verbatim",
      "proposed": "your improved version",
      "change_type": "grammar",
      "reason": "One sentence explaining the change."
    }}
  ],
  "flagged_items": ["Anything worth noting but not changed"],
  "edit_confidence": "high|medium|low"
}}

Return ONLY the JSON. No preamble. No explanation outside the JSON.

CHAPTER PARAGRAPHS (numbered for reference):
{paragraphs_text}
"""


def strip_fences(raw: str) -> str:
    """Remove markdown code fences if Gemini wrapped its JSON response."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


# ══════════════════════════════════════════════════════════════════════════════
#  REPORT WRITERS
# ══════════════════════════════════════════════════════════════════════════════

def write_changelog(all_changes: list[dict], book_title: str, path: Path):
    lines = [
        "# Copy & Line Editing Changelog",
        f"## {book_title}",
        f"*Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}*\n",
        "---\n",
        "This log records every change made by the Copy & Line Editor (Agent 3b).\n",
        "Tier 1 = automatic mechanical fixes (applied immediately).",
        "Tier 2 = AI-assisted proposals (pending author review in Cassian UI).\n",
    ]

    for entry in all_changes:
        ch_id = entry.get("chapter_id", "?")
        lines.append(f"## Chapter {ch_id}\n")

        t1 = entry.get("tier1_changes", [])
        if t1:
            lines.append("**Tier 1 — Automatic fixes:**")
            for c in t1:
                lines.append(f"- {c}")
            lines.append("")

        props = entry.get("proposals_count", 0)
        if props:
            lines.append(f"**Tier 2 — AI proposals pending review:** {props} paragraph(s)")
            lines.append("")
        else:
            lines.append("**Tier 2:** No AI proposals generated.")
            lines.append("")

        flags = entry.get("flagged_items", [])
        if flags:
            lines.append("**🚩 Flagged for manual attention:**")
            for f in flags:
                lines.append(f"- {f}")
            lines.append("")

        confidence = entry.get("confidence", "")
        if confidence and confidence not in ("tier-1-only", ""):
            lines.append(f"*AI edit confidence: {confidence}*\n")

        lines.append("---\n")

    path.write_text("\n".join(lines), encoding='utf-8')


def write_flags_report(all_changes: list[dict], book_title: str, path: Path):
    lines = [
        "# Copy & Line Editor — Items for Your Attention",
        f"## {book_title}",
        f"*Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}*\n",
        "---\n",
        "These items were noticed during editing but not automatically changed.",
        "Review each one and decide whether to address it.\n",
        "---\n",
    ]

    any_flags = False
    for entry in all_changes:
        flags = entry.get("flagged_items", [])
        if flags:
            any_flags = True
            ch_id = entry.get("chapter_id", "?")
            lines.append(f"## Chapter {ch_id}")
            for f in flags:
                lines.append(f"- 🚩 {f}")
            lines.append("")

    if not any_flags:
        lines.append("*No items flagged during this editing pass.*\n")

    path.write_text("\n".join(lines), encoding='utf-8')


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(target_chapter: str = None):
    print()
    print("═" * 62)
    head("  AGENT 3b — COPY & LINE EDITOR")
    head("  Cassian Publishing Pipeline")
    print("═" * 62)
    print()

    EDITING_DIR.mkdir(parents=True, exist_ok=True)
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)

    config     = load_config()
    book_title = config.get("book", {}).get("title", "Untitled")
    # API key: try config.json first, fall back to env var
    api_key = (
        config.get("gemini", {}).get("api_key", "")
        or os.environ.get("GEMINI_API_KEY", "")
    )
    model_name = (
        config.get("gemini", {}).get("models", {}).get("text", "")
        or "gemini-2.5-pro"
    )
    fast_model = (
        config.get("gemini", {}).get("models", {}).get("fast", "")
        or "gemini-2.5-flash"
    )
    auto_fixes = config.get("editing", {}).get("auto_fixes", [])
    creativity = config.get("editing", {}).get("creativity_level", 3)

    if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
        err("No Gemini API key found!")
        err("Set GEMINI_API_KEY env var or add it to config.json.")
        sys.exit(1)

    # ── Load context ──────────────────────────────────────────────────────────
    info("Loading world rules...")
    world_rules = load_world_rules()
    if world_rules:
        ok(f"Loaded {len(world_rules)} world rules.\n")
    else:
        warn("No world rules found — continuing without them.\n")

    info("Loading dev editor context (if available)...")
    dev_summary = load_dev_report_summary()
    if dev_summary:
        ok("Dev editor summary loaded as context.\n")
    else:
        warn("No dev editor report found — that's fine.\n")

    if auto_fixes:
        info(f"Project-specific auto-fixes: {len(auto_fixes)} configured in config.json\n")
    else:
        info("No project auto-fixes in config.json — universal fixes only.\n")

    # ── Load chapters ─────────────────────────────────────────────────────────
    info("Loading chapters...")
    chapters = load_chapters(target_chapter)
    if not chapters:
        err("No chapters found. Run Agent 1 (Intake) first.")
        sys.exit(1)
    ok(f"Loaded {len(chapters)} chapter(s)\n")

    # ── Connect to Gemini ─────────────────────────────────────────────────────
    info(f"Connecting to Gemini ({model_name})...")
    client = genai.Client(api_key=api_key)
    ok(f"Connected. Creativity level: {creativity}/5\n")

    all_changes = []

    for chapter in chapters:
        ch_id  = chapter.get("chapter_id", "?")
        title  = chapter.get("title", "Untitled")
        source = chapter.pop("_source", "ingested")  # remove internal tracking key

        print(f"  {'─' * 58}")
        info(f"Chapter {ch_id}: \"{title}\"  (source: {source})")

        # ── TIER 1: Mechanical auto-fixes ─────────────────────────────────────
        paragraphs = chapter.get("paragraphs", [])
        fixed_paragraphs, t1_changes = apply_tier1_to_paragraphs(paragraphs, auto_fixes)

        # Apply same fixes to full_text field
        full_text = chapter.get("full_text", "")
        full_text, _ = apply_universal_fixes(full_text)
        full_text, _ = apply_project_fixes(full_text, auto_fixes)

        if t1_changes:
            ok(f"Tier 1: {len(t1_changes)} fix type(s) applied")
            for c in t1_changes:
                print(f"       • {c}")
        else:
            info("Tier 1: nothing to fix")

        # ── TIER 2: AI-assisted proposals ─────────────────────────────────────
        chapter_for_ai = {
            **chapter,
            "full_text":  full_text,
            "paragraphs": fixed_paragraphs,
        }
        consistency_issues = load_consistency_issues_for_chapter(ch_id)

        paragraph_edits = []
        flagged         = []
        confidence      = "tier-1-only"

        info(f"Tier 2: sending to Gemini...")
        try:
            prompt = build_copy_edit_prompt(
                chapter_for_ai, config, world_rules, consistency_issues, dev_summary
            )

            # Try primary model, fall back to Flash on failure
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=65536,
                    )
                )
            except Exception as primary_err:
                warn(f"Primary model ({model_name}) failed: {primary_err}")
                warn(f"Retrying with {fast_model}...")
                response = client.models.generate_content(
                    model=fast_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=65536,
                    )
                )

            raw = strip_fences(response.text)
            result          = json.loads(raw)
            paragraph_edits = result.get("paragraph_edits", [])
            flagged         = result.get("flagged_items", [])
            confidence      = result.get("edit_confidence", "medium")

            ok(f"Tier 2: {len(paragraph_edits)} paragraph proposal(s) (confidence: {confidence})")
            for ed in paragraph_edits:
                reason_preview = (ed.get("reason", "") or "")[:70]
                print(f"       • [{ed.get('index','?')}] {ed.get('change_type','edit')}: {reason_preview}")
            for f in flagged:
                print(f"{Fore.YELLOW}       🚩 {f}{Style.RESET_ALL}")

        except Exception as e:
            warn(f"Tier 2 AI edit failed: {e}")
            warn("Saving Tier 1 fixes only — no proposals generated.")

        # ── Determine output paths ────────────────────────────────────────────
        if ch_id == "epilogue":
            out_path      = EDITING_DIR   / "epilogue_edited.json"
            proposal_path = PROPOSALS_DIR / "epilogue_proposals.json"
        else:
            padded        = str(ch_id).zfill(2)
            out_path      = EDITING_DIR   / f"chapter_{padded}_edited.json"
            proposal_path = PROPOSALS_DIR / f"chapter_{padded}_proposals.json"

        # ── Save tier-1 base chapter ──────────────────────────────────────────
        # full_text and paragraphs here are Tier 1 only.
        # Tier 2 proposals are stored separately and applied after user review.
        edited_chapter = {
            **chapter,
            "full_text":  full_text,
            "paragraphs": fixed_paragraphs,
            "pipeline_status": {
                **chapter.get("pipeline_status", {}),
                "editing_complete":         False,   # set True after proposals reviewed
                "proposals_pending":        len(paragraph_edits) > 0,
                "editing_creativity_level": creativity,
            },
            "editing_metadata": {
                "edited_at":     datetime.now().isoformat(),
                "tier1_changes": t1_changes,
                "tier2_changes": [],            # filled after proposal approval
                "flagged_items": flagged,
                "confidence":    confidence,
            },
        }

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(edited_chapter, f, indent=2, ensure_ascii=False)
        ok(f"Base (tier-1) → {out_path.name}")

        # ── Save proposals file ───────────────────────────────────────────────
        # Format must exactly match what runs.py proposals review UI expects.
        # approved=null means pending; true=accepted; false=rejected.
        proposals_doc = {
            "chapter_id":      ch_id,
            "title":           title,
            "generated_at":    datetime.now().isoformat(),
            "edit_confidence": confidence,
            "flagged_items":   flagged,
            "proposals_count": len(paragraph_edits),
            "paragraphs": [
                {
                    "index":       ed.get("index"),
                    "original":    ed.get("original", ""),
                    "proposed":    ed.get("proposed", ""),
                    "change_type": ed.get("change_type", "other"),
                    "reason":      ed.get("reason", ""),
                    "approved":    None,    # null = awaiting review
                }
                for ed in paragraph_edits
            ],
        }

        with open(proposal_path, 'w', encoding='utf-8') as f:
            json.dump(proposals_doc, f, indent=2, ensure_ascii=False)
        ok(f"Proposals    → {proposal_path.name}\n")

        all_changes.append({
            "chapter_id":      ch_id,
            "tier1_changes":   t1_changes,
            "proposals_count": len(paragraph_edits),
            "flagged_items":   flagged,
            "confidence":      confidence,
        })

        # Small pause between chapters — kind to the API
        time.sleep(1)

    # ── Write summary reports ─────────────────────────────────────────────────
    changelog_path = EDITING_DIR / "changelog.md"
    flags_path     = EDITING_DIR / "flags_for_review.md"

    write_changelog(all_changes, book_title, changelog_path)
    write_flags_report(all_changes, book_title, flags_path)

    ok(f"Changelog     → {changelog_path.name}")
    ok(f"Flags report  → {flags_path.name}")

    # ── Final summary ─────────────────────────────────────────────────────────
    total_t1    = sum(len(e.get("tier1_changes", [])) for e in all_changes)
    total_props = sum(e.get("proposals_count", 0) for e in all_changes)
    total_flags = sum(len(e.get("flagged_items", [])) for e in all_changes)

    print()
    print("=" * 62)
    ok("COPY & LINE EDIT COMPLETE")
    print(f"     Chapters processed       : {len(all_changes)}")
    print(f"     Tier 1 (auto) fixes      : {total_t1}")
    print(f"     Tier 2 proposals pending : {total_props}")
    print(f"     Items flagged            : {total_flags}")
    print()
    print("  Next: open Cassian and review the proposals.")
    print("  output/editing/          — tier-1 base versions (ready)")
    print("  output/editing_proposals/ — AI proposals (awaiting review)")
    print("  output/editing/flags_for_review.md — manual attention items")
    print("=" * 62)
    print()


if __name__ == "__main__":
    target = None
    if "--chapter" in sys.argv:
        idx = sys.argv.index("--chapter")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]
    run(target_chapter=target)
