"""
╔══════════════════════════════════════════════════════════════════╗
║  AGENT 2 — CONSISTENCY EDITOR                                    ║
║  Cassian Publishing Pipeline                                     ║
║                                                                  ║
║  What this does:                                                 ║
║    Feeds your entire manuscript to Gemini in one pass and asks  ║
║    it to flag anything inconsistent across chapters —           ║
║    character names, timelines, place names, tone drift, and     ║
║    violations of your established world rules.                   ║
║                                                                  ║
║  Input:   output/ingested/chapter_XX.json (all chapters)        ║
║           output/consistency/world_rules_export.json (optional) ║
║  Output:  output/consistency/consistency_report.json            ║
║           output/consistency/consistency_report.md  (readable)  ║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/02_consistency/consistency.py                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import sys
import time
from pathlib import Path
from datetime import datetime

from google import genai
from google.genai import types
from colorama import init, Fore, Style
init(autoreset=True)


# ── Paths ─────────────────────────────────────────────────────────────────────
import os
BASE_DIR = (
    Path(os.environ['CASSIAN_PROJECT_DIR'])
    if 'CASSIAN_PROJECT_DIR' in os.environ
    else Path(__file__).resolve().parent.parent.parent
)
CONFIG_PATH  = BASE_DIR / "config.json"
INGESTED_DIR = BASE_DIR / "output" / "ingested"
OUTPUT_DIR   = BASE_DIR / "output" / "consistency"


# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):    print(f"{Fore.GREEN}  ✓ {msg}{Style.RESET_ALL}")
def info(msg):  print(f"{Fore.CYAN}  → {msg}{Style.RESET_ALL}")
def warn(msg):  print(f"{Fore.YELLOW}  ⚠ {msg}{Style.RESET_ALL}")
def err(msg):   print(f"{Fore.RED}  ✗ {msg}{Style.RESET_ALL}")
def head(msg):  print(f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}")


# ── Load config ───────────────────────────────────────────────────────────────
def load_config() -> dict:
    """
    Load config.json if it exists.  Returns {} if missing or unreadable,
    so callers can fall back to env vars.
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


# ── Load all ingested chapters, sorted by chapter number ─────────────────────
def load_chapters() -> list[dict]:
    # Pick up chapter_XX.json files AND epilogue.json
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

    # Sort: numbered chapters by chapter_number, epilogue always last
    def sort_key(c):
        if c.get("chapter_id") == "epilogue":
            return 9999
        return c.get("chapter_number") or 0

    chapters.sort(key=sort_key)
    return chapters


# ── Load world rules from the pre-exported JSON file ─────────────────────────
def load_world_rules() -> list[dict]:
    """
    Reads the world rules export that runner.py writes before calling this agent.
    Returns an empty list if the file doesn't exist (e.g. running standalone).
    """
    export_path = OUTPUT_DIR / "world_rules_export.json"
    if not export_path.exists():
        return []
    try:
        with open(export_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


# ── Build the prompt we'll send to Gemini ────────────────────────────────────
def build_prompt(chapters: list[dict], config: dict, world_rules: list[dict]) -> str:
    """
    Assembles the full manuscript text plus detailed instructions into one
    large prompt for Gemini. Fully generic — works for any book.
    """
    book_title  = config.get("book", {}).get("title", "Untitled")
    book_author = config.get("book", {}).get("author", "Unknown Author")

    # Build a compact version of the book: chapter heading + full text
    book_text_parts = []
    for ch in chapters:
        ch_id  = ch.get("chapter_id", ch.get("chapter_number", "?"))
        title  = ch.get("title", "Untitled")
        text   = ch.get("full_text", "")
        book_text_parts.append(
            f"═══ CHAPTER {ch_id}: {title} ═══\n\n{text}"
        )

    full_book = "\n\n\n".join(book_text_parts)

    # Build the world rules block
    if world_rules:
        grouped: dict[str, list] = {}
        for rule in world_rules:
            cat = rule.get("category", "rule")
            grouped.setdefault(cat, []).append(rule)

        rules_text_parts = []
        for cat, rules in grouped.items():
            rules_text_parts.append(f"  [{cat.upper()}]")
            for r in rules:
                rules_text_parts.append(f"  • {r['title']}: {r['content']}")
                if r.get("rule_data"):
                    rules_text_parts.append(
                        f"    Extra data: {json.dumps(r['rule_data'])}"
                    )

        rules_block = "\n".join(rules_text_parts)
        world_rules_section = f"""═══════════════════════════════════════════════════════
AUTHOR'S ESTABLISHED WORLD RULES — READ BEFORE ANYTHING ELSE
═══════════════════════════════════════════════════════
The following are confirmed, intentional facts about this novel's universe.
Check consistency AGAINST these rules. Do NOT flag them as errors.
If the text VIOLATES these rules, that IS the error — report it under "world_rule_violations".

{rules_block}
"""
    else:
        world_rules_section = """═══════════════════════════════════════════════════════
WORLD RULES
═══════════════════════════════════════════════════════
No world rules have been defined for this project.
Return an empty array for "world_rule_violations".
"""

    prompt = f"""You are a professional fiction editor performing a consistency audit of a
novel called "{book_title}" by {book_author}.

{world_rules_section}

Below is the complete text of the novel, divided into chapters.
Read it carefully and produce a detailed consistency report.

YOUR TASK — check for ALL of the following:

1. CHARACTER CONSISTENCY
   - Spelling or capitalisation of character names
   - Character traits, abilities, or backstory that contradict themselves
   - Characters who appear, disappear, or change without explanation
   - Physical descriptions (eye colour, hair colour, age) that change unexpectedly

2. WORLD & SETTING CONSISTENCY
   - Place names spelled differently across chapters
   - Distances, directions, or geography that contradict each other
   - Technology, rules, or world-building that changes without explanation
   - Organisations, factions, or objects with inconsistent names or details

3. TIMELINE & PLOT CONSISTENCY
   - Events referenced out of order
   - Time passing inconsistently (e.g. a journey takes 3 days in ch.2, 1 day in ch.7)
   - Characters knowing things they shouldn't know yet
   - Plot events that contradict earlier or later chapters

4. TONE & STYLE
   - Chapters that feel dramatically different in voice or register from the rest
   - Sudden unexplained shifts in narrative perspective (POV)
   - Chapters where the prose quality notably drops or spikes without narrative reason

5. WORLD RULE VIOLATIONS
   - Check whether the text contradicts any of the author's established world rules listed above
   - Only populate this section if world rules were provided; otherwise return an empty array
   - Do NOT flag the rules themselves as issues — only flag places where the text violates them

6. STRUCTURAL NOTES
   - Chapter transitions that feel abrupt or mismatched
   - Pacing: chapters that are notably too long or too short relative to their dramatic weight
   - Unclear POV chapter opens, unannounced timeline jumps, or anything likely to confuse a reader
   - Useful structural observations are welcome even if not strictly "wrong"

FORMAT YOUR RESPONSE AS VALID JSON with this exact structure:

{{
  "summary": "2-3 sentence overview of the novel's overall consistency",
  "total_issues_found": <number>,
  "severity_counts": {{
    "high": <number of serious issues that need fixing>,
    "medium": <number of moderate issues worth addressing>,
    "low": <number of minor notes>
  }},
  "character_issues": [
    {{
      "severity": "high|medium|low",
      "character": "character name",
      "issue": "clear description of the inconsistency",
      "chapter_first": <chapter number where it first appears>,
      "chapter_conflict": <chapter number where it conflicts>,
      "suggested_fix": "brief suggestion"
    }}
  ],
  "world_issues": [
    {{
      "severity": "high|medium|low",
      "element": "place/tech/faction name",
      "issue": "description",
      "chapters_affected": [<list of chapter numbers>],
      "suggested_fix": "brief suggestion"
    }}
  ],
  "timeline_issues": [
    {{
      "severity": "high|medium|low",
      "issue": "description",
      "chapters_affected": [<list of chapter numbers>],
      "suggested_fix": "brief suggestion"
    }}
  ],
  "tone_issues": [
    {{
      "severity": "high|medium|low",
      "chapter": <chapter number>,
      "issue": "description",
      "suggested_fix": "brief suggestion"
    }}
  ],
  "world_rule_violations": [
    {{
      "severity": "high|medium|low",
      "rule_title": "the world rule that was violated",
      "rule_category": "character|location|timeline|rule|terminology|style_decision|genre_default",
      "issue": "description of how the text violates this rule",
      "chapters_affected": [<list of chapter numbers>],
      "suggested_fix": "brief suggestion"
    }}
  ],
  "structural_notes": [
    {{
      "severity": "high|medium|low",
      "issue": "any structural observation (POV shifts, pacing, chapter transitions)",
      "chapters_affected": [<list of chapter numbers>],
      "suggested_fix": "brief suggestion"
    }}
  ],
  "positive_observations": [
    "things the novel does consistently well (list 3-5 genuine strengths)"
  ]
}}

Return ONLY the JSON. No preamble, no explanation outside the JSON.

═══════════════════════════════════════════════════════
FULL NOVEL TEXT FOLLOWS:
═══════════════════════════════════════════════════════

{full_book}
"""
    return prompt


# ── Convert the JSON report into a readable Markdown file ────────────────────
def report_to_markdown(report: dict, chapters: list[dict], config: dict) -> str:
    """
    Turns the structured JSON report into a nicely formatted
    Markdown document you can read in any text editor.
    """
    book_title  = config.get("book", {}).get("title", "Untitled")
    book_author = config.get("book", {}).get("author", "")

    lines = []
    lines.append("# Consistency Report")
    lines.append(f"## {book_title}")
    if book_author:
        lines.append(f"*by {book_author}*")
    lines.append(f"*Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}*\n")

    lines.append("---\n")
    lines.append("## Summary\n")
    lines.append(report.get("summary", "") + "\n")

    sc    = report.get("severity_counts", {})
    total = report.get("total_issues_found", 0)
    lines.append(f"**Total issues found:** {total}  ")
    lines.append(
        f"🔴 High: {sc.get('high', 0)}  |  "
        f"🟡 Medium: {sc.get('medium', 0)}  |  "
        f"🟢 Low: {sc.get('low', 0)}\n"
    )

    def severity_icon(s):
        return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s, "⚪")

    SEVERITY_ORDER = ["high", "medium", "low"]

    # Character issues
    char_issues = report.get("character_issues", [])
    if char_issues:
        lines.append("---\n## Character Issues\n")
        for i in sorted(char_issues, key=lambda x: SEVERITY_ORDER.index(x.get("severity", "low"))):
            icon = severity_icon(i.get("severity", "low"))
            lines.append(f"### {icon} {i.get('character', 'Unknown')}")
            lines.append(f"**Issue:** {i.get('issue', '')}")
            lines.append(f"**Chapters:** {i.get('chapter_first')} → {i.get('chapter_conflict')}")
            lines.append(f"**Suggested fix:** {i.get('suggested_fix', '')}\n")

    # World issues
    world_issues = report.get("world_issues", [])
    if world_issues:
        lines.append("---\n## World & Setting Issues\n")
        for i in sorted(world_issues, key=lambda x: SEVERITY_ORDER.index(x.get("severity", "low"))):
            icon = severity_icon(i.get("severity", "low"))
            chs  = i.get("chapters_affected", [])
            lines.append(f"### {icon} {i.get('element', 'Unknown')}")
            lines.append(f"**Issue:** {i.get('issue', '')}")
            lines.append(f"**Chapters affected:** {', '.join(str(c) for c in chs)}")
            lines.append(f"**Suggested fix:** {i.get('suggested_fix', '')}\n")

    # Timeline issues
    timeline_issues = report.get("timeline_issues", [])
    if timeline_issues:
        lines.append("---\n## Timeline & Plot Issues\n")
        for i in sorted(timeline_issues, key=lambda x: SEVERITY_ORDER.index(x.get("severity", "low"))):
            icon = severity_icon(i.get("severity", "low"))
            chs  = i.get("chapters_affected", [])
            lines.append(f"### {icon} Timeline Issue")
            lines.append(f"**Issue:** {i.get('issue', '')}")
            lines.append(f"**Chapters affected:** {', '.join(str(c) for c in chs)}")
            lines.append(f"**Suggested fix:** {i.get('suggested_fix', '')}\n")

    # Tone issues
    tone_issues = report.get("tone_issues", [])
    if tone_issues:
        lines.append("---\n## Tone & Style Issues\n")
        for i in sorted(tone_issues, key=lambda x: SEVERITY_ORDER.index(x.get("severity", "low"))):
            icon = severity_icon(i.get("severity", "low"))
            lines.append(f"### {icon} Chapter {i.get('chapter', '?')}")
            lines.append(f"**Issue:** {i.get('issue', '')}")
            lines.append(f"**Suggested fix:** {i.get('suggested_fix', '')}\n")

    # World Rule Violations
    rule_violations = report.get("world_rule_violations", [])
    if rule_violations:
        lines.append("---\n## World Rule Violations\n")
        for i in sorted(rule_violations, key=lambda x: SEVERITY_ORDER.index(x.get("severity", "low"))):
            icon = severity_icon(i.get("severity", "low"))
            chs  = i.get("chapters_affected", [])
            lines.append(f"### {icon} {i.get('rule_title', 'Unknown Rule')}")
            lines.append(f"**Category:** {i.get('rule_category', '')}")
            lines.append(f"**Issue:** {i.get('issue', '')}")
            lines.append(f"**Chapters affected:** {', '.join(str(c) for c in chs)}")
            lines.append(f"**Suggested fix:** {i.get('suggested_fix', '')}\n")

    # Structural Notes
    structural_notes = report.get("structural_notes", [])
    if structural_notes:
        lines.append("---\n## Structural Notes\n")
        for i in sorted(structural_notes, key=lambda x: SEVERITY_ORDER.index(x.get("severity", "low"))):
            icon = severity_icon(i.get("severity", "low"))
            chs  = i.get("chapters_affected", [])
            ch_label = f" (ch. {', '.join(str(c) for c in chs)})" if chs else ""
            lines.append(f"### {icon} Structural Note{ch_label}")
            lines.append(f"**Issue:** {i.get('issue', '')}")
            lines.append(f"**Suggested fix:** {i.get('suggested_fix', '')}\n")

    # Positives
    positives = report.get("positive_observations", [])
    if positives:
        lines.append("---\n## What's Working Well\n")
        for p in positives:
            lines.append(f"- {p}")
        lines.append("")

    lines.append("---")
    lines.append("*Report generated by Cassian Pipeline — Consistency Editor*")

    return "\n".join(lines)


# ── Update pipeline status in each chapter JSON ───────────────────────────────
def mark_chapters_checked(chapters: list[dict], issues_by_chapter: dict):
    """
    Goes back to each chapter_XX.json and updates its pipeline_status
    to show that consistency checking is done.
    """
    for ch in chapters:
        ch_id     = ch.get("chapter_id", "")
        num       = ch.get("chapter_number") or 0
        ch_issues = issues_by_chapter.get(num, [])

        ch["pipeline_status"]["consistency_checked"] = True
        ch["pipeline_status"]["consistency_issues"]  = ch_issues

        if ch_id == "epilogue":
            output_path = INGESTED_DIR / "epilogue.json"
        else:
            output_path = INGESTED_DIR / f"chapter_{num:02d}.json"

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(ch, f, indent=2, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    print()
    print("═" * 62)
    head("  AGENT 2 — CONSISTENCY EDITOR")
    head("  Cassian Publishing Pipeline")
    print("═" * 62)
    print()

    # Setup
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()

    # API key: try config.json first, fall back to env var
    api_key = (
        config.get("gemini", {}).get("api_key", "")
        or os.environ.get("GEMINI_API_KEY", "")
    )
    model_name = (
        config.get("gemini", {}).get("models", {}).get("text", "")
        or "gemini-2.5-flash"
    )
    book_title = config.get("book", {}).get("title", "Untitled")

    if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
        err("No Gemini API key found!")
        err("Set GEMINI_API_KEY env var or add it to config.json.")
        sys.exit(1)

    # Load chapters
    info("Loading chapters...")
    chapters = load_chapters()
    ok(f"Loaded {len(chapters)} chapters  ({sum(c['word_count'] for c in chapters):,} words total)\n")

    # Load world rules
    info("Loading world rules...")
    world_rules = load_world_rules()
    if world_rules:
        ok(f"Loaded {len(world_rules)} world rules.\n")
    else:
        warn("No world rules file found — world rule violation check will be skipped.\n")

    # Configure Gemini
    info(f"Connecting to Gemini ({model_name})...")
    client = genai.Client(api_key=api_key)
    ok("Connected.\n")

    # Build prompt
    info("Building prompt (compiling full manuscript text)...")
    prompt = build_prompt(chapters, config, world_rules)
    word_count_prompt = len(prompt.split())
    ok(f"Prompt ready — {word_count_prompt:,} words being sent to Gemini.\n")

    # Send to Gemini
    info(f'Sending "{book_title}" to Gemini... (this may take 30–90 seconds)\n')
    start_time = time.time()

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
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
                    temperature=0.2,
                    max_output_tokens=65536
                )
            )
            elapsed = round(time.time() - start_time, 1)
            ok(f"Response received in {elapsed}s\n")
        except Exception as e2:
            err(f"Gemini API error: {e2}")
            err("Check your API key and internet connection.")
            return

    # Parse the JSON response
    info("Parsing Gemini's report...")
    raw_text = response.text.strip()

    # Strip markdown code fences if Gemini wrapped the JSON in them
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        report = json.loads(raw_text)
        ok("Report parsed successfully.\n")
    except json.JSONDecodeError as e:
        warn(f"Gemini response wasn't clean JSON: {e}")
        warn("Saving raw response so you can inspect it...")
        raw_path = OUTPUT_DIR / "raw_gemini_response.txt"
        raw_path.write_text(raw_text, encoding='utf-8')
        warn(f"Raw response saved → {raw_path.name}")
        return

    # Save JSON report
    report_json_path = OUTPUT_DIR / "consistency_report.json"
    with open(report_json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    ok("JSON report saved → consistency_report.json")

    # Save readable Markdown report
    md_text = report_to_markdown(report, chapters, config)
    report_md_path = OUTPUT_DIR / "consistency_report.md"
    report_md_path.write_text(md_text, encoding='utf-8')
    ok("Readable report saved → consistency_report.md")

    # Update chapter JSONs with consistency status
    info("Updating chapter files with consistency status...")
    issues_by_chapter: dict[int, list] = {}
    all_issues = (
        report.get("character_issues", []) +
        report.get("world_issues", []) +
        report.get("timeline_issues", []) +
        report.get("tone_issues", []) +
        report.get("world_rule_violations", []) +
        report.get("structural_notes", [])
    )
    for issue in all_issues:
        affected = []
        if "chapters_affected" in issue:
            affected = issue["chapters_affected"]
        elif "chapter_first" in issue:
            affected = [issue["chapter_first"], issue.get("chapter_conflict")]
        elif "chapter" in issue:
            affected = [issue["chapter"]]
        for ch_num in affected:
            if ch_num:
                issues_by_chapter.setdefault(int(ch_num), []).append(
                    issue.get("issue", "")
                )

    mark_chapters_checked(chapters, issues_by_chapter)
    ok("Chapter files updated.\n")

    # Final summary
    sc    = report.get("severity_counts", {})
    total = report.get("total_issues_found", 0)

    print("═" * 62)
    print()
    ok("CONSISTENCY CHECK COMPLETE")
    print(f"     Total issues found : {total}")
    print(f"     🔴 High severity   : {sc.get('high', 0)}")
    print(f"     🟡 Medium          : {sc.get('medium', 0)}")
    print(f"     🟢 Low / notes     : {sc.get('low', 0)}")
    print()
    print("     Open this file to read the full report:")
    print("     output/consistency/consistency_report.md")
    print()
    print("  Next step: run Agent 3 (Developmental Editor)")
    print("═" * 62)
    print()


if __name__ == "__main__":
    run()
