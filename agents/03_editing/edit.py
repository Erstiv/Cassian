"""
╔══════════════════════════════════════════════════════════════════╗
║  AGENT 3 — EDITING                                              ║
║  One Thousand Perfect Sighs                                     ║
║                                                                  ║
║  What this does:                                                 ║
║    Goes through each chapter and applies edits in two tiers:    ║
║                                                                  ║
║    TIER 1 — AUTO-FIXED (done automatically):                    ║
║      • Name corrections (Karpov → Tsarasov, Kora Mori → etc.)  ║
║      • Location standardisation (→ Vorkuta, Komi Republic)      ║
║      • Quote corrections (Chronos notebook quote)               ║
║      • Sigh number corrections (72 → 250, 431 → 370, etc.)     ║
║      • "thirty-three years" / "forty-five years" clarification  ║
║                                                                  ║
║    TIER 2 — AI-ASSISTED (Gemini rewrites with your voice):      ║
║      • Daniil's visual descriptions → frequency/resonance       ║
║      • Prose polish at your chosen creativity level             ║
║      • Seam repairs between chapters 3/4, 7/8, 8/9             ║
║      • Cane additions to early chapters                         ║
║      • Cassandra → Pax + brief rename justification             ║
║                                                                  ║
║    TIER 3 — FLAGGED FOR YOU (not auto-edited):                  ║
║      • Tartarus structure (9 losses → possible 6 deaths)        ║
║      • POV framing device / prologue                            ║
║      • River / Will Way name decision                           ║
║      • Any edit that changes plot or character arc              ║
║      • Countdown timeline fix in Chapter 2 (Cassandra's data)  ║
║                                                                  ║
║  Input:   output/ingested/chapter_XX.json                       ║
║           output/consistency/consistency_report.json            ║
║  Output:  output/editing/chapter_XX_edited.json                 ║
║           output/editing/changelog.md  (every change logged)    ║
║           output/editing/flags_for_review.md  (your todo list)  ║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/03_editing/edit.py                             ║
║    python agents/03_editing/edit.py --chapter 01  (one only)   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import re
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
CONFIG_PATH      = BASE_DIR / "config.json"
INGESTED_DIR     = BASE_DIR / "output" / "ingested"
CONSISTENCY_PATH = BASE_DIR / "output" / "consistency" / "consistency_report.json"
OUTPUT_DIR       = BASE_DIR / "output" / "editing"
PROPOSALS_DIR    = BASE_DIR / "output" / "editing_proposals"


# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):    print(f"{Fore.GREEN}  ✓ {msg}{Style.RESET_ALL}")
def info(msg):  print(f"{Fore.CYAN}  → {msg}{Style.RESET_ALL}")
def warn(msg):  print(f"{Fore.YELLOW}  ⚠ {msg}{Style.RESET_ALL}")
def err(msg):   print(f"{Fore.RED}  ✗ {msg}{Style.RESET_ALL}")
def head(msg):  print(f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}")
def flag(msg):  print(f"{Fore.YELLOW}  🚩 {msg}{Style.RESET_ALL}")


def load_config() -> dict:
    """Load config.json if it exists. Returns {} if missing/unreadable."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def load_consistency_report() -> dict:
    if CONSISTENCY_PATH.exists():
        with open(CONSISTENCY_PATH, 'r') as f:
            return json.load(f)
    return {}


def load_chapters(target_chapter: str = None) -> list[dict]:
    """Load all chapter JSONs, or just one if target_chapter is specified."""
    if target_chapter:
        if target_chapter == "epilogue":
            paths = [INGESTED_DIR / "epilogue.json"]
        else:
            paths = [INGESTED_DIR / f"chapter_{target_chapter.zfill(2)}.json"]
    else:
        paths = sorted(INGESTED_DIR.glob("chapter_*.json"))
        epilogue = INGESTED_DIR / "epilogue.json"
        if epilogue.exists():
            paths = list(paths) + [epilogue]

    chapters = []
    for p in paths:
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                chapters.append(json.load(f))
        else:
            warn(f"File not found: {p.name}")

    chapters.sort(key=lambda c: 9999 if c.get("chapter_id") == "epilogue"
                                else (c.get("chapter_number") or 0))
    return chapters


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 1 — AUTOMATIC TEXT REPLACEMENTS
#  These are safe, mechanical fixes that don't require AI judgment.
# ══════════════════════════════════════════════════════════════════════════════

# Each entry: (pattern, replacement, description)
# Pattern can be a plain string or a regex string (use r"..." prefix in description)
AUTO_FIXES = [
    # ── Name corrections ───────────────────────────────────────────────────
    ("Daniil Karpov",     "Daniil Tsarasov",   "Name: Karpov → Tsarasov"),
    ("Karpov",            "Tsarasov",           "Name: Karpov → Tsarasov (surname only)"),
    ("Kora Mori",         "Kora Komorebi",      "Name: Kora Mori → Kora Komorebi"),
    ("Cassandra",         "Pax",                "Name: Cassandra → Pax (AI assistant)"),

    # ── Location corrections ───────────────────────────────────────────────
    ("Krasnoyarsk Region","Komi Republic",      "Location: Krasnoyarsk → Komi Republic"),
    ("Ural Region",       "Komi Republic",      "Location: Ural → Komi Republic"),
    ("Krasnoyarsk",       "Vorkuta",            "Location: Krasnoyarsk → Vorkuta"),

    # ── Chronos notebook quote ─────────────────────────────────────────────
    ("He wanted me to know that he is counting.",
     "He is coming.",
     "Quote: Chronos notebook corrected to canonical version"),

    # ── Underground duration phrasing ──────────────────────────────────────
    ("thirty-three years underground",
     "thirty-three years underground with Nadya — forty-five in total",
     "Duration: 33yrs clarified as time with Nadya only"),
    ("33 years underground",
     "33 years underground with Nadya (45 years in total)",
     "Duration: 33yrs clarified (numeric form)"),

    # ── Sigh / Action numbers ──────────────────────────────────────────────
    ("Action 72",         "Action 250",         "Sigh: Bhola Cyclone corrected to ~250"),
    ("Action 431",        "Action 370",         "Sigh: Typhoon Tip corrected to ~370"),
    ("Action 527",        "Action 750",         "Sigh: Tsunami corrected to ~750"),

    # ── Kora camera location ───────────────────────────────────────────────
    ("lost most of her equipment in the approach",
     "lost most of her equipment inside Tartarus",
     "Kora: camera destroyed inside Tartarus, not in the approach"),

    # ── Daniil age phrasing (appearance vs chronological) ─────────────────
    ("He is seventy years old",
     "He looked seventy years old",
     "Daniil: appearance vs chronological age clarified"),
    ("he is seventy years old",
     "he looked seventy years old",
     "Daniil: appearance vs chronological age clarified"),
    ("he was seventy years old",
     "he looked seventy years old",
     "Daniil: appearance vs chronological age clarified"),

    # ── 1991 extraction — 'staff' reference ───────────────────────────────
    ("your staff will be extracted",
     "you will be extracted",
     "1991 extraction: Daniil was alone, no staff"),
    ("his staff will be extracted",
     "he will be extracted",
     "1991 extraction: Daniil was alone, no staff"),
]


def apply_auto_fixes(text: str) -> tuple[str, list[str]]:
    """
    Apply all Tier 1 mechanical fixes to a block of text.
    Returns the fixed text and a list of changes that were made.
    """
    changes = []
    for pattern, replacement, description in AUTO_FIXES:
        if pattern in text:
            text = text.replace(pattern, replacement)
            changes.append(description)
    return text, changes


def fix_paragraphs(paragraphs: list[dict]) -> tuple[list[dict], list[str]]:
    """Run Tier 1 fixes across all paragraphs in a chapter."""
    all_changes = []
    fixed = []
    for para in paragraphs:
        new_text, changes = apply_auto_fixes(para["text"])
        fixed.append({**para, "text": new_text})
        all_changes.extend(changes)
    # Deduplicate change descriptions
    return fixed, list(dict.fromkeys(all_changes))


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 2 — AI-ASSISTED REWRITES (Gemini)
# ══════════════════════════════════════════════════════════════════════════════

def needs_ai_edit(chapter: dict, world_rules: dict) -> list[str]:
    """
    Decide what AI editing tasks are needed for this chapter.
    Returns a list of task descriptions for the prompt.
    """
    tasks = []
    ch_num = chapter.get("chapter_number") or 0
    text   = chapter.get("full_text", "")

    # Daniil visual descriptions (only in Chs 1-3 where blindness errors occur)
    if ch_num in [1, 2, 3]:
        visual_words = ["read", "saw", "watch", "look", "screen", "sign", "see"]
        if any(w in text.lower() for w in visual_words):
            tasks.append(
                "BLINDNESS FIX: Daniil is blind since 1979 and perceives the world through "
                "frequency and resonance — a synesthetic awareness of the machine's field. "
                "Rewrite any passages where he 'reads', 'sees', 'watches', or processes visual "
                "information. Replace with sensory descriptions: vibrations, frequencies, the "
                "weight of sound, the texture of resonance. Do NOT make it feel clinical — "
                "keep the lyrical, melancholic tone of his voice."
            )

    # Cane establishment (Chs 1, 2, 3 — before it appears in Ch 7)
    if ch_num in [1, 2, 3] and "cane" not in text.lower():
        tasks.append(
            "CANE ADDITION: Daniil uses a cane at all times. Add one brief, natural mention "
            "of the cane — a single detail woven into an existing action, not a standalone "
            "sentence. Something like 'his cane finding the familiar rhythm of the floor' or "
            "'the cane tapping ahead of him'. Match the chapter's existing pace and tone."
        )

    # Seam repairs
    if ch_num == 4:
        tasks.append(
            "SEAM REPAIR (Chapter 3 → 4): Chapter 3 ends with Daniil falling asleep on the "
            "plane. Chapter 4 opens with him remembering 1991. The current opening of Chapter 4 "
            "says 'He could not sleep' — this directly contradicts Chapter 3. Rewrite the "
            "opening lines of this chapter so the memory sequence feels like it emerges FROM "
            "sleep or a half-dreaming state, not in spite of wakefulness."
        )

    if ch_num == 8:
        tasks.append(
            "SEAM REPAIR (Chapter 7 → 8): Chapter 7 is the coffee/diner conversation. "
            "Chapter 8 opens with 'The briefing took four hours.' Check whether the opening "
            "of this chapter repeats information already covered in Chapter 7. If so, trim "
            "the repetition so Chapter 8 picks up naturally where Chapter 7 left off."
        )

    # Prose polish (all chapters, based on creativity level)
    tasks.append("PROSE POLISH: Apply light prose improvements per the creativity level below.")

    return tasks


def build_edit_prompt(chapter: dict, tasks: list[str], config: dict) -> str:
    """Build the editing prompt for Gemini.

    Requests paragraph-level output so changes can be reviewed and approved
    individually rather than accepting or rejecting the entire chapter at once.
    """
    creativity_level  = config.get("editing", {}).get("creativity_level", 3)
    creativity_guides = config.get("editing", {}).get("_creativity_guide", {})
    creativity_desc   = creativity_guides.get(str(creativity_level), "Prose polish and improvements.")
    world_rules       = json.dumps(config.get("world_rules", {}), indent=2)

    ch_id      = chapter.get("chapter_id", "?")
    title      = chapter.get("title", "Untitled")
    paragraphs = chapter.get("paragraphs", [])

    tasks_text = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(tasks))

    # Number the paragraphs so Gemini can reference them by index
    para_lines = []
    for i, p in enumerate(paragraphs):
        text = p.get("text", "").strip()
        if text:
            para_lines.append(f"[{i}] {text}")
    paragraphs_text = "\n\n".join(para_lines)

    return f"""You are editing Chapter {ch_id} ("{title}") of a novel by Elliot.

AUTHORIAL VOICE: Preserve it above all else. Lyrical, melancholic, precise but not cold.
When in doubt, do LESS. Preserve the author's sentences. Do not change dialogue, character
names, proper nouns, or plot events.

CREATIVITY LEVEL: {creativity_level}/5
{creativity_desc}

WORLD RULES:
{world_rules}

YOUR TASKS:
{tasks_text}

OUTPUT FORMAT - READ CAREFULLY:
Return ONLY valid JSON. Do NOT rewrite the entire chapter.
Return ONLY the paragraphs you actually changed.

For each changed paragraph:
- "index": the [N] number shown before the paragraph
- "original": the exact original paragraph text (copy it verbatim)
- "proposed": your improved version
- "change_type": one of: prose_polish | blindness_fix | seam_repair | cane_addition | continuity | other
- "reason": one sentence explaining what you changed and why

For anything you noticed but chose NOT to change, add to "flagged_items".

{{
  "chapter_id": "{ch_id}",
  "paragraph_edits": [
    {{
      "index": 0,
      "original": "exact original text",
      "proposed": "your improved version",
      "change_type": "prose_polish",
      "reason": "one sentence explaining the change"
    }}
  ],
  "flagged_items": ["Description of anything needing a manual or structural decision"],
  "edit_confidence": "high|medium|low"
}}

Return ONLY the JSON. No preamble. No explanation outside the JSON.

CHAPTER PARAGRAPHS (numbered for reference):
{paragraphs_text}
"""



# ══════════════════════════════════════════════════════════════════════════════
#  TIER 3 — FLAGS FOR MANUAL REVIEW
# ══════════════════════════════════════════════════════════════════════════════

# These are known issues that require authorial decisions — not auto-fixed.
MANUAL_FLAGS = [
    {
        "item": "Tartarus structure: 9 losses vs 6 deaths",
        "chapters": "12–22",
        "detail": (
            "Currently every Tartarus challenge results in a character loss (9 total). "
            "The critic suggests restructuring: some characters nearly die without dying, "
            "one challenge causes two deaths, varying the rhythm. This changes plot and pacing — "
            "your decision. If you want this, tell Agent 3 to execute it in a future run."
        )
    },
    {
        "item": "POV framing device — 'The City 40 Dossier' prologue",
        "chapters": "Before Chapter 9",
        "detail": (
            "The shift from 3rd-person (Chs 1–8) to 1st-person choral (Ch 9+) is intentional "
            "but currently has no framing device. The critic suggests a prologue — perhaps compiled "
            "by Wright and Axis — establishing these testimonials as an official dossier. "
            "Needs writing from scratch: your call."
        )
    },
    {
        "item": "River / Will Way — name decision",
        "chapters": "4, 18, 21",
        "detail": (
            "'River' (Ch 4, 1991 flashback) and 'Will Way / Wu Wei' (Chs 18, 21) are the same "
            "person. Currently unexplained. Options: (a) use one name throughout, "
            "(b) add a line in Ch 4 or 18 noting River was a former codename. Your choice."
        )
    },
    {
        "item": "Climax countdown — Cassandra's Chapter 2 data",
        "chapters": "2",
        "detail": (
            "Cassandra currently says Event 1000 is '4 days away' (Timeline A). "
            "The correct timeline is C: Event 998 is 3 days away, Event 1000 is ~20 days away. "
            "This is flagged rather than auto-fixed because it touches the AI character's "
            "core data presentation and may affect surrounding dialogue."
        )
    },
    {
        "item": "Tartarus: six-deaths rhythm note from critic",
        "chapters": "12–22",
        "detail": (
            "The critic's specific suggestion: Ch 1 of Tartarus — near-death but survives. "
            "Ch 2 — two die at once. Ch 3 — something unexpected (injury, sacrifice, choice). "
            "Full notes are in the critique documents. Review before instructing Agent 3 to execute."
        )
    },
]


# ══════════════════════════════════════════════════════════════════════════════
#  CHANGELOG AND REPORT WRITERS
# ══════════════════════════════════════════════════════════════════════════════

def write_changelog(all_changes: list[dict], path: Path):
    lines = [
        "# Editing Changelog",
        "## One Thousand Perfect Sighs",
        f"*Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}*\n",
        "---\n",
        "This log records every change made by Agent 3. "
        "Tier 1 = automatic. Tier 2 = AI-assisted. Tier 3 = flagged only.\n",
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

        t2 = entry.get("tier2_changes", [])
        if t2:
            lines.append("**Tier 2 — AI-assisted edits:**")
            for c in t2:
                lines.append(f"- {c}")
            lines.append("")

        flags = entry.get("flagged_items", [])
        if flags:
            lines.append("**🚩 Flagged for manual review:**")
            for f in flags:
                lines.append(f"- {f}")
            lines.append("")

        confidence = entry.get("confidence", "")
        if confidence:
            lines.append(f"*Edit confidence: {confidence}*\n")

        lines.append("---\n")

    path.write_text("\n".join(lines), encoding='utf-8')


def write_flags_report(all_chapter_flags: list, path: Path):
    lines = [
        "# Items Flagged for Your Review",
        "## One Thousand Perfect Sighs",
        f"*Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}*\n",
        "---\n",
        "These items were NOT changed by Agent 3. They require a creative decision from you.",
        "Once you've decided, you can tell Agent 3 to execute them in a targeted re-run.\n",
        "---\n",
        "## Known Structural Flags\n"
    ]

    for flag_item in MANUAL_FLAGS:
        lines.append(f"### 🚩 {flag_item['item']}")
        lines.append(f"**Chapters:** {flag_item['chapters']}")
        lines.append(f"{flag_item['detail']}\n")

    if any(e.get("flagged_items") for e in all_chapter_flags):
        lines.append("---\n## Flags Raised During Editing\n")
        for entry in all_chapter_flags:
            flags = entry.get("flagged_items", [])
            if flags:
                ch_id = entry.get("chapter_id", "?")
                lines.append(f"### Chapter {ch_id}")
                for f in flags:
                    lines.append(f"- 🚩 {f}")
                lines.append("")

    path.write_text("\n".join(lines), encoding='utf-8')


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(target_chapter: str = None):
    print()
    print("═" * 62)
    head("  AGENT 3 — EDITING")
    head("  One Thousand Perfect Sighs")
    print("═" * 62)
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    config      = load_config()
    world_rules = config.get("world_rules", {})
    # API key: try config.json first, fall back to env var
    api_key = (
        config.get("gemini", {}).get("api_key", "")
        or os.environ.get("GEMINI_API_KEY", "")
    )
    model_name = (
        config.get("gemini", {}).get("models", {}).get("text", "")
        or "gemini-2.5-flash"
    )

    if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
        err("No Gemini API key found!")
        err("Set GEMINI_API_KEY env var or add it to config.json.")
        sys.exit(1)

    info("Loading chapters...")
    chapters = load_chapters(target_chapter)
    ok(f"Loaded {len(chapters)} chapter(s)\n")

    info("Connecting to Gemini...")
    client = genai.Client(api_key=api_key)
    ok(f"Connected ({model_name})\n")

    all_changes = []

    for chapter in chapters:
        ch_id  = chapter.get("chapter_id", "?")
        title  = chapter.get("title", "Untitled")
        ch_num = chapter.get("chapter_number") or 0

        print(f"  {'─' * 58}")
        info(f"Chapter {ch_id}: \"{title}\"")

        # ── TIER 1: Automatic fixes ────────────────────────────────────────
        paragraphs = chapter.get("paragraphs", [])
        fixed_paragraphs, t1_changes = fix_paragraphs(paragraphs)

        full_text_fixed, _ = apply_auto_fixes(chapter.get("full_text", ""))

        if t1_changes:
            ok(f"Tier 1: {len(t1_changes)} automatic fix(es)")
            for c in t1_changes:
                print(f"       • {c}")
        else:
            print(f"       Tier 1: nothing to fix")

        # ── TIER 2: AI-assisted edits (paragraph-level proposals) ────────
        ai_tasks = needs_ai_edit(chapter, world_rules)
        chapter_for_ai = {**chapter, "full_text": full_text_fixed, "paragraphs": fixed_paragraphs}

        paragraph_edits = []   # list of {index, original, proposed, change_type, reason}
        flagged         = []
        confidence      = "tier-1-only"

        info(f"Tier 2: sending to Gemini ({len(ai_tasks)} task(s))...")
        try:
            prompt   = build_edit_prompt(chapter_for_ai, ai_tasks, config)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=65536
                )
            )

            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            result          = json.loads(raw)
            paragraph_edits = result.get("paragraph_edits", [])
            flagged         = result.get("flagged_items", [])
            confidence      = result.get("edit_confidence", "medium")

            ok(f"Tier 2 complete — {len(paragraph_edits)} paragraph(s) proposed (confidence: {confidence})")
            for ed in paragraph_edits:
                print(f"       • [{ed.get('index','?')}] {ed.get('change_type','edit')}: {ed.get('reason','')}")
            for f in flagged:
                flag(f)

        except Exception as e:
            warn(f"Tier 2 AI edit failed: {e}")
            warn("Saving Tier 1 fixes only — no proposals generated.")

        # ── Save tier-1 chapter to output/editing/ (base version, pending approval) ──
        # full_text and paragraphs here are tier-1 only.
        # Tier-2 proposals are stored separately and applied after user review.
        edited_chapter = {
            **chapter,
            "full_text":  full_text_fixed,
            "paragraphs": fixed_paragraphs,
            "pipeline_status": {
                **chapter.get("pipeline_status", {}),
                "editing_complete":         False,   # set True after proposals reviewed
                "proposals_pending":        len(paragraph_edits) > 0,
                "editing_creativity_level": config.get("editing", {}).get("creativity_level", 3)
            },
            "editing_metadata": {
                "edited_at":     datetime.now().isoformat(),
                "tier1_changes": t1_changes,
                "tier2_changes": [],           # filled in after approval
                "flagged_items": flagged,
                "confidence":    confidence
            }
        }

        if ch_id == "epilogue":
            out_path      = OUTPUT_DIR   / "epilogue_edited.json"
            proposal_path = PROPOSALS_DIR / "epilogue_proposals.json"
        else:
            out_path      = OUTPUT_DIR   / f"chapter_{str(ch_id).zfill(2)}_edited.json"
            proposal_path = PROPOSALS_DIR / f"chapter_{str(ch_id).zfill(2)}_proposals.json"

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(edited_chapter, f, indent=2, ensure_ascii=False)
        ok(f"Base (tier-1) → {out_path.name}")

        # ── Save proposals to output/editing_proposals/ ───────────────────
        # Each entry: {index, original, proposed, change_type, reason, approved: null}
        # approved=null means pending review; true=accepted; false=rejected
        proposals_doc = {
            "chapter_id":       ch_id,
            "title":            title,
            "generated_at":     datetime.now().isoformat(),
            "edit_confidence":  confidence,
            "flagged_items":    flagged,
            "proposals_count":  len(paragraph_edits),
            "paragraphs": [
                {
                    "index":       ed.get("index"),
                    "original":    ed.get("original", ""),
                    "proposed":    ed.get("proposed", ""),
                    "change_type": ed.get("change_type", "other"),
                    "reason":      ed.get("reason", ""),
                    "approved":    None    # null = awaiting review
                }
                for ed in paragraph_edits
            ]
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

        # Small pause between chapters to be kind to the API
        time.sleep(1)

    # ── Write summary reports ──────────────────────────────────────────────
    changelog_path = OUTPUT_DIR / "changelog.md"
    flags_path     = OUTPUT_DIR / "flags_for_review.md"

    write_changelog(all_changes, changelog_path)
    write_flags_report(all_changes, flags_path)

    ok(f"Changelog → {changelog_path.name}")
    ok(f"Flags report → {flags_path.name}")

    # Summary
    total_t1    = sum(len(e.get("tier1_changes", [])) for e in all_changes)
    total_props = sum(e.get("proposals_count", 0) for e in all_changes)
    total_f     = sum(len(e.get("flagged_items", [])) for e in all_changes)

    print()
    print("=" * 62)
    ok("EDITING COMPLETE")
    print(f"     Chapters processed       : {len(all_changes)}")
    print(f"     Tier 1 (auto) fixes      : {total_t1}")
    print(f"     Tier 2 proposals pending : {total_props}")
    print(f"     Items flagged            : {total_f + len(MANUAL_FLAGS)}")
    print()
    print("  Next: open Cassian and review proposals before continuing.")
    print("  output/editing/          — tier-1 base versions (ready)")
    print("  output/editing_proposals/ — AI proposals (awaiting your review)")
    print("  output/editing/flags_for_review.md — manual decisions needed")
    print("=" * 62)
    print()


if __name__ == "__main__":
    # Optional: pass --chapter 01 to run on one chapter only
    target = None
    if "--chapter" in sys.argv:
        idx = sys.argv.index("--chapter")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]

    run(target_chapter=target)
