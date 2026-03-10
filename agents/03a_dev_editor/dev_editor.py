"""
╔══════════════════════════════════════════════════════════════════╗
║  AGENT 3a — DEVELOPMENTAL EDITOR                                 ║
║  Cassian Publishing Pipeline                                     ║
║                                                                  ║
║  What this does:                                                 ║
║    Reads the full manuscript and produces a big-picture          ║
║    structural editorial report — plot arcs, pacing, character    ║
║    development, POV, theme, and chapter-by-chapter assessment.   ║
║                                                                  ║
║    This agent is ADVISORY ONLY. It does not change any chapter   ║
║    files. The author reads the report and decides what to act    ║
║    on. Changes are made by later agents (Copy & Line Editor,     ║
║    or directly in the Workbench).                                ║
║                                                                  ║
║  Input:   output/ingested/chapter_XX.json (all chapters)        ║
║           output/consistency/world_rules_export.json (optional) ║
║           output/dev_editing/world_rules_export.json  (optional)║
║           output/consistency/consistency_report.json  (optional) ║
║  Output:  output/dev_editing/dev_report.json                    ║
║           output/dev_editing/dev_report.md    (readable)        ║
║           output/dev_editing/chapter_XX_assessment.json         ║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/03a_dev_editor/dev_editor.py                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import os
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
CONFIG_PATH        = BASE_DIR / "config.json"
INGESTED_DIR       = BASE_DIR / "output" / "ingested"
CONSISTENCY_DIR    = BASE_DIR / "output" / "consistency"
OUTPUT_DIR         = BASE_DIR / "output" / "dev_editing"


# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):    print(f"{Fore.GREEN}  ✓ {msg}{Style.RESET_ALL}")
def info(msg):  print(f"{Fore.CYAN}  → {msg}{Style.RESET_ALL}")
def warn(msg):  print(f"{Fore.YELLOW}  ⚠ {msg}{Style.RESET_ALL}")
def err(msg):   print(f"{Fore.RED}  ✗ {msg}{Style.RESET_ALL}")
def head(msg):  print(f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}")


# ── Load config ───────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load config.json if it exists. Returns {} if missing/unreadable."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


# ── Load all ingested chapters, sorted by chapter number ─────────────────────
def load_chapters() -> list[dict]:
    json_files = sorted(INGESTED_DIR.glob("chapter_*.json"))
    epilogue_path = INGESTED_DIR / "epilogue.json"
    if epilogue_path.exists():
        json_files = list(json_files) + [epilogue_path]

    if not json_files:
        raise FileNotFoundError(
            f"No chapter JSON files found in {INGESTED_DIR}\n"
            "Please run Agent 1 (ingestion) first."
        )

    chapters = []
    for path in json_files:
        with open(path, 'r', encoding='utf-8') as f:
            chapters.append(json.load(f))

    def sort_key(c):
        if c.get("chapter_id") == "epilogue":
            return 9999
        return c.get("chapter_number") or 0

    chapters.sort(key=sort_key)
    return chapters


# ── Load world rules ──────────────────────────────────────────────────────────
def load_world_rules() -> list[dict]:
    """
    Looks for world rules in two locations:
      1. output/dev_editing/world_rules_export.json  (written by runner's pre-setup)
      2. output/consistency/world_rules_export.json  (fallback from consistency run)

    Returns an empty list if neither file exists.
    """
    for candidate in [
        OUTPUT_DIR / "world_rules_export.json",
        CONSISTENCY_DIR / "world_rules_export.json",
    ]:
        if candidate.exists():
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    rules = json.load(f)
                if rules:
                    return rules
            except Exception:
                continue
    return []


# ── Load consistency report summary (optional context) ────────────────────────
def load_consistency_summary() -> str:
    """
    If a consistency report exists, extract its summary paragraph so Gemini
    knows what has already been flagged — avoiding duplicate warnings.
    Returns an empty string if not found.
    """
    report_path = CONSISTENCY_DIR / "consistency_report.json"
    if not report_path.exists():
        return ""
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            report = json.load(f)
        summary = report.get("summary", "")
        total   = report.get("total_issues_found", 0)
        sc      = report.get("severity_counts", {})
        if summary:
            return (
                f"{summary}\n"
                f"(Total consistency issues already flagged: {total} — "
                f"High: {sc.get('high', 0)}, "
                f"Medium: {sc.get('medium', 0)}, "
                f"Low: {sc.get('low', 0)})"
            )
    except Exception:
        pass
    return ""


# ── Build the Gemini prompt ───────────────────────────────────────────────────
def build_prompt(chapters: list[dict], config: dict,
                 world_rules: list[dict], consistency_summary: str) -> str:
    """
    Assembles the full manuscript text plus developmental editing instructions
    into one large prompt for Gemini. Fully generic — works for any book.
    """
    book_title  = config.get("book", {}).get("title", "Untitled")
    book_author = config.get("book", {}).get("author", "Unknown Author")
    book_genre  = config.get("book", {}).get("genre", "fiction")

    # Build compact manuscript text: chapter heading + full text
    book_text_parts = []
    for ch in chapters:
        ch_id  = ch.get("chapter_id", ch.get("chapter_number", "?"))
        title  = ch.get("title", "Untitled")
        text   = ch.get("full_text", "")
        book_text_parts.append(
            f"═══ CHAPTER {ch_id}: {title} ═══\n\n{text}"
        )
    full_book = "\n\n\n".join(book_text_parts)

    # Build world rules block
    if world_rules:
        grouped: dict[str, list] = {}
        for rule in world_rules:
            cat = rule.get("category", "rule")
            grouped.setdefault(cat, []).append(rule)

        rules_lines = []
        for cat, rules in grouped.items():
            rules_lines.append(f"  [{cat.upper()}]")
            for r in rules:
                rules_lines.append(f"  • {r['title']}: {r['content']}")
                if r.get("rule_data"):
                    rules_lines.append(
                        f"    Extra data: {json.dumps(r['rule_data'])}"
                    )
        world_rules_section = (
            "═══════════════════════════════════════════════════════\n"
            "AUTHOR'S ESTABLISHED WORLD RULES — READ BEFORE ANYTHING ELSE\n"
            "═══════════════════════════════════════════════════════\n"
            "The following are confirmed, intentional decisions by the author.\n"
            "Do NOT flag these as problems. Only flag places where the manuscript\n"
            "CONTRADICTS or FAILS TO DELIVER on these intentions.\n\n"
            + "\n".join(rules_lines)
        )
    else:
        world_rules_section = (
            "═══════════════════════════════════════════════════════\n"
            "WORLD RULES\n"
            "═══════════════════════════════════════════════════════\n"
            "No world rules have been defined for this project."
        )

    # Build consistency context block
    if consistency_summary:
        consistency_section = (
            "═══════════════════════════════════════════════════════\n"
            "CONSISTENCY REPORT SUMMARY (already flagged — do not repeat)\n"
            "═══════════════════════════════════════════════════════\n"
            + consistency_summary
        )
    else:
        consistency_section = (
            "═══════════════════════════════════════════════════════\n"
            "CONSISTENCY REPORT\n"
            "═══════════════════════════════════════════════════════\n"
            "No consistency report has been run yet. Focus on developmental\n"
            "issues only — do not flag minor typos or factual inconsistencies."
        )

    # Build list of chapter IDs for the schema instructions
    chapter_ids = []
    for ch in chapters:
        ch_id = ch.get("chapter_id", "")
        if ch_id:
            chapter_ids.append(str(ch_id))

    prompt = f"""You are a professional developmental editor performing a full manuscript evaluation
of a {book_genre} novel called "{book_title}" by {book_author}.

A developmental editor works at the HIGHEST level — you do NOT fix typos, grammar, or
prose style. You evaluate the big-picture structure: plot, pacing, character arcs, point
of view, theme, and chapter-level purpose.

{world_rules_section}

{consistency_section}

═══════════════════════════════════════════════════════
YOUR DEVELOPMENTAL EDITING TASKS
═══════════════════════════════════════════════════════

Evaluate the manuscript across ALL of the following dimensions:

1. OVERALL ASSESSMENT
   - Summarise the manuscript in 3-5 sentences: what's working, what isn't, where it stands.
   - Identify 3-5 genuine strengths (specific, not generic praise).
   - Identify 3-5 major areas for improvement.
   - Rate readiness: "early_draft" | "needs_work" | "nearly_ready" | "publication_ready"
   - List recommended next steps in priority order.

2. PLOT STRUCTURE
   - Does the story arc have a clear setup, escalating tension, climax, and resolution?
   - Are there plot holes — events that are promised but not delivered, or outcomes that
     have no cause?
   - Does the climax deliver on the story's central question?
   - Does the resolution satisfy? Does it feel earned?

3. PACING
   - Overall: does the story move at the right speed for its genre and tone?
   - Per chapter: is each chapter too slow, well-paced, too fast, or uneven?
   - Which chapters or scenes could be trimmed without losing essential story?
   - Which moments need more space to land properly?

4. CHARACTER ARCS
   - For each significant character: what is their arc? Do they change and grow?
   - Are motivations clearly established and consistent?
   - Is each arc resolved by the end?
   - Are there characters who feel underdeveloped or whose presence feels unjustified?

5. POINT OF VIEW
   - Is POV used consistently within chapters?
   - Are POV switches between chapters handled with enough clarity?
   - Are there passages where POV drifts unexpectedly?

6. CHAPTER-BY-CHAPTER ASSESSMENT
   For EVERY chapter, assess:
   - Its purpose: what role does it serve in the larger story?
   - Hook quality: does it start in a way that pulls the reader in?
   - Ending quality: does it end with a reason to keep reading?
   - POV consistency within the chapter.
   - Show vs tell balance.
   - Overall pacing of the chapter.
   - Any specific flags or observations.

7. STRUCTURAL RECOMMENDATIONS
   - Are there chapters that should be reordered?
   - Are there chapters that could be merged or split?
   - Are there scenes that should be cut entirely?
   - Are there structural gaps that need new material?

8. THEME AND THROUGHLINE
   - What are the central themes? Are they developed consistently?
   - Is there a clear thematic throughline from beginning to end?
   - Where does the thematic thread drop or become inconsistent?

9. OPENING AND ENDING
   - Does the opening hook the right kind of reader for this book?
   - Does the ending satisfy and feel thematically complete?

═══════════════════════════════════════════════════════
OUTPUT FORMAT — RESPOND WITH VALID JSON ONLY
═══════════════════════════════════════════════════════

Return ONLY the JSON below. No preamble, no markdown fences, no explanation outside the JSON.
All string values should be substantive — avoid generic phrases like "well done" or "could be improved"
without specifics. Be honest, direct, and specific.

{{
  "book_title": "{book_title}",
  "book_author": "{book_author}",
  "generated_at": "<ISO 8601 timestamp>",
  "overall_assessment": {{
    "summary": "3-5 sentence overall assessment of the manuscript",
    "strengths": ["specific strength 1", "specific strength 2", "specific strength 3"],
    "weaknesses": ["specific weakness 1", "specific weakness 2", "specific weakness 3"],
    "readiness_level": "early_draft | needs_work | nearly_ready | publication_ready",
    "recommended_next_steps": ["first priority", "second priority", "third priority"]
  }},
  "plot_analysis": {{
    "arc_assessment": "Is the overall story arc working? What's missing?",
    "plot_holes": [
      {{
        "severity": "high|medium|low",
        "description": "what's missing or contradictory",
        "chapters_affected": [1, 5],
        "suggested_approach": "how to address it"
      }}
    ],
    "climax_assessment": "Does the climax deliver on the story's central question?",
    "resolution_assessment": "Does the ending satisfy? Does it feel earned?"
  }},
  "pacing_analysis": {{
    "overall_pacing": "description of pacing strengths and issues",
    "chapter_pacing": [
      {{
        "chapter_id": "01",
        "assessment": "too_slow | well_paced | too_fast | uneven",
        "notes": "specific observations about this chapter's pacing"
      }}
    ],
    "suggested_cuts": ["chapters or scenes that could be trimmed, with reason"],
    "suggested_expansions": ["moments that need more space, with reason"]
  }},
  "character_analysis": [
    {{
      "character_name": "Name",
      "arc_assessment": "description of their arc — what changes, what doesn't",
      "arc_complete": true,
      "motivation_clear": true,
      "issues": ["any problems with this character's portrayal or arc"],
      "chapters_present": [1, 3, 5]
    }}
  ],
  "chapter_assessments": [
    {{
      "chapter_id": "01",
      "title": "Chapter title",
      "purpose": "What role this chapter serves in the story",
      "hook_quality": "strong | adequate | weak | missing",
      "ending_quality": "strong | adequate | weak | missing",
      "pov_consistency": "consistent | minor_issues | major_issues",
      "show_vs_tell": "mostly_showing | balanced | mostly_telling",
      "pacing": "too_slow | well_paced | too_fast | uneven",
      "notes": "specific observations",
      "flags": ["anything that needs author attention"]
    }}
  ],
  "structural_recommendations": [
    {{
      "priority": "high|medium|low",
      "type": "reorder | merge | split | cut | expand | rewrite",
      "description": "what specifically to do",
      "chapters_affected": [3, 4],
      "rationale": "why this would improve the book"
    }}
  ],
  "theme_analysis": {{
    "identified_themes": ["theme 1", "theme 2"],
    "theme_consistency": "Are themes developed consistently throughout?",
    "thematic_gaps": "Where does the thematic thread drop or become inconsistent?"
  }}
}}

Include an entry in "chapter_assessments" for EVERY chapter in the manuscript.
The chapter IDs in this manuscript are: {json.dumps(chapter_ids)}

═══════════════════════════════════════════════════════
FULL NOVEL TEXT FOLLOWS:
═══════════════════════════════════════════════════════

{full_book}
"""
    return prompt


# ── Convert the JSON report into a readable Markdown file ────────────────────
def report_to_markdown(report: dict, chapters: list[dict]) -> str:
    book_title  = report.get("book_title", "Untitled")
    book_author = report.get("book_author", "")
    generated   = report.get("generated_at", datetime.now().isoformat())

    try:
        dt = datetime.fromisoformat(generated)
        generated_fmt = dt.strftime("%B %d, %Y at %H:%M")
    except Exception:
        generated_fmt = generated

    lines = []
    lines.append(f"# Developmental Edit Report")
    lines.append(f"## {book_title}")
    if book_author:
        lines.append(f"*by {book_author}*")
    lines.append(f"*Generated: {generated_fmt}*\n")
    lines.append("---\n")

    # ── Overall Assessment ────────────────────────────────────────────────────
    oa = report.get("overall_assessment", {})
    lines.append("## Overall Assessment\n")
    lines.append(oa.get("summary", "") + "\n")

    readiness = oa.get("readiness_level", "")
    readiness_icons = {
        "early_draft":        "🔴 Early Draft",
        "needs_work":         "🟡 Needs Work",
        "nearly_ready":       "🟢 Nearly Ready",
        "publication_ready":  "✅ Publication Ready",
    }
    if readiness:
        lines.append(f"**Readiness:** {readiness_icons.get(readiness, readiness)}\n")

    strengths = oa.get("strengths", [])
    if strengths:
        lines.append("### Strengths")
        for s in strengths:
            lines.append(f"- {s}")
        lines.append("")

    weaknesses = oa.get("weaknesses", [])
    if weaknesses:
        lines.append("### Areas for Improvement")
        for w in weaknesses:
            lines.append(f"- {w}")
        lines.append("")

    next_steps = oa.get("recommended_next_steps", [])
    if next_steps:
        lines.append("### Recommended Next Steps")
        for i, step in enumerate(next_steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    # ── Plot Analysis ─────────────────────────────────────────────────────────
    pa = report.get("plot_analysis", {})
    lines.append("---\n## Plot Analysis\n")
    if pa.get("arc_assessment"):
        lines.append(f"**Story Arc:** {pa['arc_assessment']}\n")
    if pa.get("climax_assessment"):
        lines.append(f"**Climax:** {pa['climax_assessment']}\n")
    if pa.get("resolution_assessment"):
        lines.append(f"**Resolution:** {pa['resolution_assessment']}\n")

    plot_holes = pa.get("plot_holes", [])
    if plot_holes:
        lines.append("### Plot Holes\n")
        sev_order = ["high", "medium", "low"]
        sev_icon  = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for hole in sorted(plot_holes, key=lambda x: sev_order.index(x.get("severity", "low"))):
            icon = sev_icon.get(hole.get("severity", "low"), "⚪")
            chs  = ", ".join(str(c) for c in hole.get("chapters_affected", []))
            lines.append(f"#### {icon} {hole.get('description', '')}")
            if chs:
                lines.append(f"**Chapters:** {chs}")
            if hole.get("suggested_approach"):
                lines.append(f"**Approach:** {hole['suggested_approach']}")
            lines.append("")

    # ── Pacing Analysis ───────────────────────────────────────────────────────
    pac = report.get("pacing_analysis", {})
    lines.append("---\n## Pacing Analysis\n")
    if pac.get("overall_pacing"):
        lines.append(pac["overall_pacing"] + "\n")

    chapter_pacing = pac.get("chapter_pacing", [])
    if chapter_pacing:
        lines.append("### Per-Chapter Pacing\n")
        lines.append("| Chapter | Assessment | Notes |")
        lines.append("|---------|-----------|-------|")
        pacing_icons = {
            "too_slow":   "🐢 Too Slow",
            "well_paced": "✅ Well Paced",
            "too_fast":   "⚡ Too Fast",
            "uneven":     "🌊 Uneven",
        }
        for cp in chapter_pacing:
            assessment = pacing_icons.get(cp.get("assessment", ""), cp.get("assessment", ""))
            notes      = cp.get("notes", "").replace("|", "\\|")
            lines.append(f"| Ch {cp.get('chapter_id', '?')} | {assessment} | {notes} |")
        lines.append("")

    cuts = pac.get("suggested_cuts", [])
    if cuts:
        lines.append("### Suggested Cuts")
        for c in cuts:
            lines.append(f"- {c}")
        lines.append("")

    expansions = pac.get("suggested_expansions", [])
    if expansions:
        lines.append("### Suggested Expansions")
        for e in expansions:
            lines.append(f"- {e}")
        lines.append("")

    # ── Character Analysis ────────────────────────────────────────────────────
    chars = report.get("character_analysis", [])
    if chars:
        lines.append("---\n## Character Analysis\n")
        for char in chars:
            name         = char.get("character_name", "Unknown")
            arc_complete = "✅" if char.get("arc_complete") else "❌"
            mot_clear    = "✅" if char.get("motivation_clear") else "❌"
            chs          = ", ".join(str(c) for c in char.get("chapters_present", []))
            lines.append(f"### {name}")
            lines.append(f"**Arc complete:** {arc_complete}  |  **Motivation clear:** {mot_clear}")
            if chs:
                lines.append(f"**Present in chapters:** {chs}")
            if char.get("arc_assessment"):
                lines.append(f"\n{char['arc_assessment']}\n")
            issues = char.get("issues", [])
            if issues:
                lines.append("**Issues:**")
                for issue in issues:
                    lines.append(f"- {issue}")
            lines.append("")

    # ── Chapter-by-Chapter Assessment ─────────────────────────────────────────
    ch_assessments = report.get("chapter_assessments", [])
    if ch_assessments:
        lines.append("---\n## Chapter-by-Chapter Assessment\n")

        quality_icon = {"strong": "🟢", "adequate": "🟡", "weak": "🔴", "missing": "⛔"}
        pov_icon     = {"consistent": "✅", "minor_issues": "🟡", "major_issues": "🔴"}
        stt_icon     = {"mostly_showing": "✅", "balanced": "🟡", "mostly_telling": "🔴"}

        lines.append("| Chapter | Hook | Ending | POV | Show/Tell | Pacing | Flags |")
        lines.append("|---------|------|--------|-----|-----------|--------|-------|")
        for ca in ch_assessments:
            ch_id   = ca.get("chapter_id", "?")
            hook    = quality_icon.get(ca.get("hook_quality", ""), "⚪")
            ending  = quality_icon.get(ca.get("ending_quality", ""), "⚪")
            pov     = pov_icon.get(ca.get("pov_consistency", ""), "⚪")
            stt     = stt_icon.get(ca.get("show_vs_tell", ""), "⚪")
            pacing  = pacing_icons.get(ca.get("pacing", ""), ca.get("pacing", ""))  # type: ignore
            flags   = len(ca.get("flags", []))
            flag_str = f"🚩 {flags}" if flags else "—"
            lines.append(f"| Ch {ch_id} | {hook} | {ending} | {pov} | {stt} | {pacing} | {flag_str} |")
        lines.append("")

        # Detailed per-chapter notes
        lines.append("### Chapter Notes\n")
        for ca in ch_assessments:
            ch_id   = ca.get("chapter_id", "?")
            title   = ca.get("title", "")
            purpose = ca.get("purpose", "")
            notes   = ca.get("notes", "")
            flags   = ca.get("flags", [])
            heading = f"#### Chapter {ch_id}"
            if title:
                heading += f": {title}"
            lines.append(heading)
            if purpose:
                lines.append(f"**Purpose:** {purpose}")
            if notes:
                lines.append(f"\n{notes}\n")
            if flags:
                lines.append("**Flags:**")
                for flag in flags:
                    lines.append(f"- 🚩 {flag}")
            lines.append("")

    # ── Structural Recommendations ────────────────────────────────────────────
    struct_recs = report.get("structural_recommendations", [])
    if struct_recs:
        lines.append("---\n## Structural Recommendations\n")
        sev_order = ["high", "medium", "low"]
        sev_icon  = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        type_labels = {
            "reorder": "Reorder",
            "merge":   "Merge",
            "split":   "Split",
            "cut":     "Cut",
            "expand":  "Expand",
            "rewrite": "Rewrite",
        }
        for rec in sorted(struct_recs, key=lambda x: sev_order.index(x.get("priority", "low"))):
            icon  = sev_icon.get(rec.get("priority", "low"), "⚪")
            rtype = type_labels.get(rec.get("type", ""), rec.get("type", ""))
            chs   = ", ".join(str(c) for c in rec.get("chapters_affected", []))
            lines.append(f"### {icon} {rtype}: {rec.get('description', '')}")
            if chs:
                lines.append(f"**Chapters:** {chs}")
            if rec.get("rationale"):
                lines.append(f"**Why:** {rec['rationale']}")
            lines.append("")

    # ── Theme Analysis ────────────────────────────────────────────────────────
    themes = report.get("theme_analysis", {})
    if themes:
        lines.append("---\n## Theme Analysis\n")
        identified = themes.get("identified_themes", [])
        if identified:
            lines.append(f"**Themes:** {', '.join(identified)}\n")
        if themes.get("theme_consistency"):
            lines.append(f"**Consistency:** {themes['theme_consistency']}\n")
        if themes.get("thematic_gaps"):
            lines.append(f"**Gaps:** {themes['thematic_gaps']}\n")

    lines.append("---")
    lines.append("*Generated by Cassian Pipeline — Developmental Editor*")

    return "\n".join(lines)


# ── Save per-chapter assessment files ─────────────────────────────────────────
def save_chapter_assessments(report: dict, chapters: list[dict]):
    """
    Extracts the per-chapter data from the main report and writes one
    chapter_XX_assessment.json file per chapter to output/dev_editing/.
    """
    # Build a lookup from chapter_id → chapter metadata
    ch_meta = {}
    for ch in chapters:
        ch_id = str(ch.get("chapter_id", ch.get("chapter_number", "")))
        ch_meta[ch_id] = ch

    # Build a lookup from chapter_id → pacing assessment
    pacing_by_id = {}
    for cp in report.get("pacing_analysis", {}).get("chapter_pacing", []):
        pacing_by_id[str(cp.get("chapter_id", ""))] = cp

    # Build a lookup for characters present per chapter
    chars_by_chapter: dict[str, list] = {}
    for char in report.get("character_analysis", []):
        name = char.get("character_name", "")
        for ch_num in char.get("chapters_present", []):
            key = str(ch_num)
            chars_by_chapter.setdefault(key, []).append(name)

    for ca in report.get("chapter_assessments", []):
        ch_id   = str(ca.get("chapter_id", ""))
        meta    = ch_meta.get(ch_id, {})
        pacing  = pacing_by_id.get(ch_id, {})
        chars   = chars_by_chapter.get(ch_id, [])

        assessment = {
            "chapter_id":        ch_id,
            "title":             ca.get("title", meta.get("title", "")),
            "word_count":        meta.get("word_count", 0),
            "purpose":           ca.get("purpose", ""),
            "hook_quality":      ca.get("hook_quality", ""),
            "ending_quality":    ca.get("ending_quality", ""),
            "pov_consistency":   ca.get("pov_consistency", ""),
            "show_vs_tell":      ca.get("show_vs_tell", ""),
            "pacing":            pacing.get("assessment", ca.get("pacing", "")),
            "characters_present": chars,
            "plot_points":       [],   # populated by user or a later agent
            "flags":             ca.get("flags", []),
            "notes":             ca.get("notes", ""),
        }

        if ch_id == "epilogue":
            out_path = OUTPUT_DIR / "epilogue_assessment.json"
        else:
            try:
                num = int(ch_id)
                out_path = OUTPUT_DIR / f"chapter_{num:02d}_assessment.json"
            except ValueError:
                out_path = OUTPUT_DIR / f"chapter_{ch_id}_assessment.json"

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(assessment, f, indent=2, ensure_ascii=False)

    ok(f"Per-chapter assessment files saved → output/dev_editing/")


# ── Strip markdown fences if Gemini wrapped the JSON ─────────────────────────
def strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    print()
    print("═" * 62)
    head("  AGENT 3a — DEVELOPMENTAL EDITOR")
    head("  Cassian Publishing Pipeline")
    print("═" * 62)
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()

    # API key: try config.json first, fall back to env var
    api_key = (
        config.get("gemini", {}).get("api_key", "")
        or os.environ.get("GEMINI_API_KEY", "")
    )
    model_name = (
        config.get("gemini", {}).get("models", {}).get("text", "")
        or "gemini-2.5-pro"
    )
    book_title = config.get("book", {}).get("title", "Untitled")

    if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
        err("No Gemini API key found!")
        err("Set GEMINI_API_KEY env var or add it to config.json.")
        sys.exit(1)

    # Load chapters
    info("Loading chapters...")
    chapters = load_chapters()
    total_words = sum(c.get("word_count", 0) for c in chapters)
    ok(f"Loaded {len(chapters)} chapters  ({total_words:,} words total)\n")

    # Load world rules
    info("Loading world rules...")
    world_rules = load_world_rules()
    if world_rules:
        ok(f"Loaded {len(world_rules)} world rules.\n")
    else:
        warn("No world rules found — developmental edit will proceed without them.\n")

    # Load consistency summary
    info("Loading consistency report (if available)...")
    consistency_summary = load_consistency_summary()
    if consistency_summary:
        ok("Consistency report summary loaded — will be included as context.\n")
    else:
        warn("No consistency report found — that's fine, continuing without it.\n")

    # Configure Gemini
    info(f"Connecting to Gemini ({model_name})...")
    client = genai.Client(api_key=api_key)
    ok("Connected.\n")

    # Build prompt
    info("Building prompt (compiling full manuscript text)...")
    prompt = build_prompt(chapters, config, world_rules, consistency_summary)
    word_count_prompt = len(prompt.split())
    ok(f"Prompt ready — {word_count_prompt:,} words being sent to Gemini.\n")

    # Send to Gemini
    info(f'Sending "{book_title}" to Gemini for developmental analysis...')
    info("(This typically takes 1-3 minutes for a full novel)\n")
    start_time = time.time()

    response = None
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=65536
            )
        )
        elapsed = round(time.time() - start_time, 1)
        ok(f"Response received in {elapsed}s\n")

    except Exception as e:
        warn(f"Model '{model_name}' failed: {e}")
        warn("Retrying with gemini-2.5-flash...")
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=65536
                )
            )
            elapsed = round(time.time() - start_time, 1)
            ok(f"Response received in {elapsed}s (via Flash fallback)\n")
        except Exception as e2:
            err(f"Gemini API error: {e2}")
            err("Check your API key and internet connection.")
            sys.exit(1)

    # Parse the JSON response
    info("Parsing Gemini's developmental report...")
    raw_text = strip_fences(response.text)

    try:
        report = json.loads(raw_text)
        ok("Report parsed successfully.\n")
    except json.JSONDecodeError as e:
        warn(f"Gemini response wasn't clean JSON: {e}")
        warn("Saving raw response so you can inspect it...")
        raw_path = OUTPUT_DIR / "raw_gemini_response.txt"
        raw_path.write_text(raw_text, encoding='utf-8')
        warn(f"Raw response saved → {raw_path.name}")
        err("Fix the JSON manually and re-run, or adjust the prompt.")
        sys.exit(1)

    # Stamp the generation time if Gemini left it as a placeholder
    if not report.get("generated_at") or "timestamp" in report.get("generated_at", "").lower():
        report["generated_at"] = datetime.now().isoformat()

    # Save JSON report
    report_json_path = OUTPUT_DIR / "dev_report.json"
    with open(report_json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    ok("JSON report saved → dev_report.json")

    # Save readable Markdown report
    md_text = report_to_markdown(report, chapters)
    report_md_path = OUTPUT_DIR / "dev_report.md"
    report_md_path.write_text(md_text, encoding='utf-8')
    ok("Readable report saved → dev_report.md")

    # Save per-chapter assessment files
    info("Writing per-chapter assessment files...")
    save_chapter_assessments(report, chapters)

    # Final summary
    oa         = report.get("overall_assessment", {})
    readiness  = oa.get("readiness_level", "unknown")
    strengths  = len(oa.get("strengths", []))
    weaknesses = len(oa.get("weaknesses", []))
    struct_rec = len(report.get("structural_recommendations", []))
    ch_count   = len(report.get("chapter_assessments", []))

    print()
    print("═" * 62)
    ok("DEVELOPMENTAL EDIT COMPLETE")
    print(f"     Readiness level      : {readiness}")
    print(f"     Strengths identified : {strengths}")
    print(f"     Areas to improve     : {weaknesses}")
    print(f"     Structural recs      : {struct_rec}")
    print(f"     Chapters assessed    : {ch_count}")
    print()
    print("     Open this file to read the full report:")
    print("     output/dev_editing/dev_report.md")
    print()
    print("  Next step: review the report and run Copy & Line Editor")
    print("═" * 62)
    print()


if __name__ == "__main__":
    run()
