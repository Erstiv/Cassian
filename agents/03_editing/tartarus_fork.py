"""
╔══════════════════════════════════════════════════════════════════╗
║  TARTARUS FORK — Restructure for 6 deaths                       ║
║  One Thousand Perfect Sighs                                     ║
║                                                                  ║
║  What this does:                                                 ║
║    The current manuscript has 9 Tartarus challenges,            ║
║    each resulting in exactly one death (9 deaths total).        ║
║    Critics note this becomes rhythmically predictable.          ║
║                                                                  ║
║    This script rewrites chapters 13–23 so that:                 ║
║      • 6 challenges result in actual deaths                     ║
║      • 3 challenges result in near-misses, sacrifice,           ║
║        injury, or unexpected survival                           ║
║      • The team count still works mathematically                ║
║      • The emotional climax of Chapter 21's litany still lands  ║
║                                                                  ║
║    PHASE 1 — PLANNING:                                          ║
║      Gemini reads all Tartarus chapters together and proposes   ║
║      a restructure plan. You review and approve before          ║
║      anything is rewritten.                                     ║
║                                                                  ║
║    PHASE 2 — EXECUTION:                                         ║
║      Rewrites each chapter according to the approved plan.      ║
║      Saves to output/editing/ (your v1 backup is safe in        ║
║      output/editing_v1_nine_deaths/)                            ║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/03_editing/tartarus_fork.py                    ║
║    python agents/03_editing/tartarus_fork.py --plan-only        ║
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
BASE_DIR     = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH  = BASE_DIR / "config.json"
EDITING_DIR  = BASE_DIR / "output" / "editing"
BACKUP_DIR   = BASE_DIR / "output" / "editing_v1_nine_deaths"
FORK_LOG_DIR = BASE_DIR / "output" / "tartarus_fork"

# Tartarus sequence — chapters 13 through 23
TARTARUS_CHAPTERS = list(range(13, 24))  # 13, 14, 15 ... 23


# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):    print(f"{Fore.GREEN}  ✓ {msg}{Style.RESET_ALL}")
def info(msg):  print(f"{Fore.CYAN}  → {msg}{Style.RESET_ALL}")
def warn(msg):  print(f"{Fore.YELLOW}  ⚠ {msg}{Style.RESET_ALL}")
def err(msg):   print(f"{Fore.RED}  ✗ {msg}{Style.RESET_ALL}")
def head(msg):  print(f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}")
def bold(msg):  print(f"\n{Style.BRIGHT}{msg}{Style.RESET_ALL}")


def load_config() -> dict:
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)


def load_tartarus_chapters() -> list[dict]:
    """Load the Tartarus sequence from the edited output."""
    chapters = []
    for n in TARTARUS_CHAPTERS:
        path = EDITING_DIR / f"chapter_{str(n).zfill(2)}_edited.json"
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                chapters.append(json.load(f))
        else:
            warn(f"Chapter {n} not found in editing output — skipping.")
    return chapters


# ══════════════════════════════════════════════════════════════════════════════
#  PHASE 1 — PLANNING
# ══════════════════════════════════════════════════════════════════════════════

def build_planning_prompt(chapters: list[dict], config: dict) -> str:
    world_rules = json.dumps(config.get("world_rules", {}), indent=2)

    # Build a chapter-by-chapter summary for the planning prompt
    chapter_summaries = []
    for ch in chapters:
        ch_id = ch.get("chapter_id", "?")
        title = ch.get("title", "Untitled")
        text  = ch.get("full_text", "")
        # Truncate for planning — we just need enough to understand structure
        preview = text[:3000] + ("\n[...continues...]" if len(text) > 3000 else "")
        chapter_summaries.append(
            f"--- CHAPTER {ch_id}: \"{title}\" ---\n{preview}"
        )

    all_chapters_text = "\n\n".join(chapter_summaries)

    return f"""You are a developmental editor working on "One Thousand Perfect Sighs" by Elliot.

━━━ THE PROBLEM ━━━
The Tartarus sequence (Chapters 13–23) currently has 9 challenges with exactly one death
per challenge, for a total of 9 deaths. This creates a predictable, metronomic rhythm that
dulls the emotional impact. The team enters with approximately 17 people.

━━━ THE GOAL ━━━
Restructure the Tartarus sequence so there are only 6 deaths total across the 9 challenges.
The other 3 challenges should have varied, unexpected outcomes: near-deaths, survivals that
cost something, unexpected sacrifice, injury, or a moment where the rules of Tartarus
are subverted in a way that costs differently.

The restructure should:
• Keep total team deaths at exactly 6 (not 5, not 7)
• Vary the rhythm so no two consecutive challenges feel the same
• Make each survival feel as earned as each death — not a reprieve, a different kind of loss
• Preserve the emotional weight of Chapter 21's litany of the lost
• Ensure the team count is mathematically consistent across all 11 chapters
• Keep the author's voice, the lyrical tone, and existing plot structure intact

━━━ WORLD RULES ━━━
{world_rules}

━━━ YOUR TASK — PHASE 1: PLANNING ONLY ━━━
Read the chapters below. Then produce a restructure plan in JSON.

Do NOT rewrite the chapters yet. Just plan.

Return ONLY valid JSON:

{{
  "restructure_rationale": "2–3 sentences on your overall approach and why it serves the story",
  "team_starting_count": <number>,
  "challenges": [
    {{
      "chapter": <number>,
      "challenge_number": <1–9>,
      "current_outcome": "Who died and how, in the current manuscript",
      "proposed_outcome": "death | near_death | survival_with_cost | double_death | unexpected",
      "proposed_detail": "What happens instead — be specific. Name the character(s). Describe the moment.",
      "deaths_this_challenge": <0, 1, or 2>,
      "running_team_count": <team size after this challenge>
    }}
  ],
  "total_deaths": 6,
  "surviving_team_members": ["name1", "name2"],
  "chapter_21_litany_adjustment": "How the litany in Ch 21 changes — which 6 names are spoken vs the current 9",
  "chapter_30_adjustment": "How Ch 30's reference to losses changes",
  "editor_notes": "Anything the author should know before approving this plan"
}}

Return ONLY the JSON. No preamble.

━━━ THE TARTARUS CHAPTERS ━━━
{all_chapters_text}
"""


def get_restructure_plan(chapters: list[dict], config: dict, client, model: str) -> dict:
    info("Sending all Tartarus chapters to Gemini for planning...")
    info("(This may take a minute — it's reading ~11 chapters at once)")

    prompt   = build_planning_prompt(chapters, config)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.5,
            max_output_tokens=8192
        )
    )

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def display_plan(plan: dict):
    """Print the restructure plan clearly for author review."""
    print()
    print("═" * 62)
    bold("  TARTARUS RESTRUCTURE PLAN")
    print("═" * 62)

    print(f"\n  {plan.get('restructure_rationale', '')}\n")
    print(f"  Team starts with: {plan.get('team_starting_count', '?')} people")
    print(f"  Total deaths:     {plan.get('total_deaths', '?')} (down from 9)\n")

    print(f"  {'─' * 58}")
    print(f"  {'CH':<4} {'CHALLENGE':<12} {'OUTCOME':<22} {'TEAM':<6}")
    print(f"  {'─' * 58}")

    for ch in plan.get("challenges", []):
        ch_num    = ch.get("chapter", "?")
        challenge = ch.get("challenge_number", "?")
        outcome   = ch.get("proposed_outcome", "?").upper()
        count     = ch.get("running_team_count", "?")
        print(f"  {ch_num:<4} #{challenge:<11} {outcome:<22} {count:<6}")
        detail = ch.get("proposed_detail", "")
        if detail:
            # Word-wrap detail at ~54 chars
            words = detail.split()
            line  = "       "
            for w in words:
                if len(line) + len(w) > 60:
                    print(f"  {Fore.CYAN}{line}{Style.RESET_ALL}")
                    line = "       " + w + " "
                else:
                    line += w + " "
            if line.strip():
                print(f"  {Fore.CYAN}{line}{Style.RESET_ALL}")
        print()

    survivors = plan.get("surviving_team_members", [])
    if survivors:
        print(f"  Surviving team: {', '.join(survivors)}")

    litany = plan.get("chapter_21_litany_adjustment", "")
    if litany:
        print(f"\n  Ch 21 litany: {litany}")

    notes = plan.get("editor_notes", "")
    if notes:
        print(f"\n  Editor notes: {notes}")

    print()
    print("═" * 62)


def get_approval(plan: dict) -> bool:
    """Ask the author to approve the plan before execution."""
    print()
    print(f"  {Fore.YELLOW}Review the plan above carefully.{Style.RESET_ALL}")
    print(f"  Your v1 (nine deaths) is safely backed up in:")
    print(f"  output/editing_v1_nine_deaths/")
    print()
    print(f"  {Fore.YELLOW}y{Style.RESET_ALL} = approve and start rewriting")
    print(f"  {Fore.YELLOW}n{Style.RESET_ALL} = cancel (nothing will be changed)")
    print()

    try:
        answer = input("  Approve this plan? [y/n]: ").strip().lower()
        return answer == "y"
    except (EOFError, KeyboardInterrupt):
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  PHASE 2 — EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def build_rewrite_prompt(chapter: dict, chapter_plan: dict, full_plan: dict, config: dict) -> str:
    world_rules  = json.dumps(config.get("world_rules", {}), indent=2)
    ch_id        = chapter.get("chapter_id", "?")
    title        = chapter.get("title", "Untitled")
    text         = chapter.get("full_text", "")
    outcome      = chapter_plan.get("proposed_outcome", "")
    detail       = chapter_plan.get("proposed_detail", "")
    team_after   = chapter_plan.get("running_team_count", "?")
    deaths_here  = chapter_plan.get("deaths_this_challenge", 0)
    rationale    = full_plan.get("restructure_rationale", "")

    return f"""You are rewriting Chapter {ch_id} ("{title}") of "One Thousand Perfect Sighs" by Elliot.

━━━ AUTHORIAL VOICE — PRESERVE THIS ABOVE ALL ELSE ━━━
This novel has a distinctive lyrical, melancholic voice. Soviet history, mythological weight,
personal grief. Precise but not cold. Emotional but not sentimental.
Preserve the author's sentences wherever possible. Change only what the restructure requires.
Do not alter dialogue, character names, or events unrelated to the Tartarus challenge outcome.

━━━ OVERALL RESTRUCTURE RATIONALE ━━━
{rationale}

━━━ THIS CHAPTER'S SPECIFIC CHANGE ━━━
Outcome type:  {outcome}
What happens:  {detail}
Deaths in this challenge: {deaths_here}
Team count after this chapter: {team_after}

━━━ WORLD RULES ━━━
{world_rules}

━━━ YOUR TASK ━━━
Rewrite Chapter {ch_id} so the Tartarus challenge plays out as described above.
• If outcome is DEATH: the specified character(s) die, make it feel earned
• If outcome is NEAR_DEATH: the character comes within a breath of dying — show that cost
• If outcome is DOUBLE_DEATH: two characters die, find the right dramatic rhythm
• If outcome is SURVIVAL_WITH_COST: survival feels worse than death in some way
• If outcome is UNEXPECTED: subvert the formula — surprise us, but make it feel true

Keep the team count mathematically correct (ends at {team_after}).
Keep the chapter's existing structure — same scenes, same arc. Rewrite the challenge
and its aftermath. Leave everything else intact.

━━━ OUTPUT FORMAT ━━━
Return ONLY valid JSON:

{{
  "chapter_id": "{ch_id}",
  "rewritten_text": "THE COMPLETE REWRITTEN CHAPTER — full text",
  "changes_made": ["Brief description of each change"],
  "team_count_end": {team_after},
  "outcome_achieved": "{outcome}"
}}

Return ONLY the JSON. No preamble.

━━━ CURRENT CHAPTER TEXT ━━━
{text}
"""


def rewrite_chapter(chapter: dict, chapter_plan: dict, full_plan: dict,
                    config: dict, client, model: str) -> dict | None:
    prompt = build_rewrite_prompt(chapter, chapter_plan, full_plan, config)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.6,
            max_output_tokens=65536
        )
    )

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def save_rewritten_chapter(chapter: dict, result: dict):
    ch_id    = chapter.get("chapter_id", "?")
    out_path = EDITING_DIR / f"chapter_{str(ch_id).zfill(2)}_edited.json"

    updated = {
        **chapter,
        "full_text": result.get("rewritten_text", chapter.get("full_text", "")),
        "editing_metadata": {
            **chapter.get("editing_metadata", {}),
            "tartarus_fork":    True,
            "fork_applied_at":  datetime.now().isoformat(),
            "fork_outcome":     result.get("outcome_achieved", ""),
            "fork_changes":     result.get("changes_made", []),
            "team_count_end":   result.get("team_count_end", "?"),
        }
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)

    ok(f"Saved → {out_path.name}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(plan_only: bool = False):
    print()
    print("═" * 62)
    head("  TARTARUS FORK — Six Deaths Restructure")
    head("  One Thousand Perfect Sighs")
    print("═" * 62)
    print()
    info("Your v1 backup (nine deaths) lives in:")
    info("output/editing_v1_nine_deaths/")
    info("Nothing will be overwritten without your approval.\n")

    FORK_LOG_DIR.mkdir(parents=True, exist_ok=True)

    config    = load_config()
    api_key   = config["gemini"]["api_key"]
    model     = config["gemini"]["models"]["text"]

    info("Loading Tartarus chapters (13–23)...")
    chapters = load_tartarus_chapters()
    ok(f"Loaded {len(chapters)} chapter(s)\n")

    info("Connecting to Gemini...")
    client = genai.Client(api_key=api_key)
    ok(f"Connected ({model})\n")

    # ── PHASE 1: Planning ──────────────────────────────────────────────────
    bold("  PHASE 1 — PLANNING")
    print()

    try:
        plan = get_restructure_plan(chapters, config, client, model)
    except Exception as e:
        err(f"Planning failed: {e}")
        return

    ok("Plan received from Gemini")

    # Save plan to file regardless
    plan_path = FORK_LOG_DIR / "tartarus_restructure_plan.json"
    with open(plan_path, 'w', encoding='utf-8') as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    ok(f"Plan saved → {plan_path.name}")

    # Display it
    display_plan(plan)

    if plan_only:
        info("--plan-only mode: stopping here. Review the plan file and run without --plan-only to execute.")
        return

    # ── Get approval ───────────────────────────────────────────────────────
    approved = get_approval(plan)
    if not approved:
        warn("Cancelled. No chapters have been changed.")
        info("Your v1 is still intact in output/editing_v1_nine_deaths/")
        return

    # ── PHASE 2: Execution ─────────────────────────────────────────────────
    bold("  PHASE 2 — REWRITING")
    print()

    # Build a lookup from chapter number → plan entry
    plan_by_chapter = {
        ch.get("chapter"): ch
        for ch in plan.get("challenges", [])
    }

    rewrite_log = []

    for chapter in chapters:
        ch_id  = chapter.get("chapter_id", "?")
        title  = chapter.get("title", "Untitled")
        ch_num = chapter.get("chapter_number", 0)

        print(f"  {'─' * 58}")
        info(f"Rewriting Chapter {ch_id}: \"{title}\"")

        chapter_plan = plan_by_chapter.get(ch_num)
        if not chapter_plan:
            warn(f"No plan entry for Chapter {ch_num} — skipping (keeping original)")
            continue

        outcome = chapter_plan.get("proposed_outcome", "")
        info(f"Outcome: {outcome.upper()} — {chapter_plan.get('proposed_detail', '')[:60]}...")

        try:
            result = rewrite_chapter(chapter, chapter_plan, plan, config, client, model)
            save_rewritten_chapter(chapter, result)

            for change in result.get("changes_made", []):
                print(f"       • {change}")

        except Exception as e:
            err(f"Rewrite failed for Chapter {ch_id}: {e}")
            warn("Keeping original — your v1 backup is unaffected.")

        rewrite_log.append({
            "chapter_id": ch_id,
            "outcome":    outcome,
            "plan":       chapter_plan
        })

        time.sleep(1)

    # Save execution log
    log_path = FORK_LOG_DIR / "tartarus_fork_log.json"
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump({
            "forked_at": datetime.now().isoformat(),
            "plan":      plan,
            "chapters_rewritten": rewrite_log
        }, f, indent=2, ensure_ascii=False)

    # Summary
    print()
    print("═" * 62)
    ok("TARTARUS FORK COMPLETE")
    print(f"     Chapters rewritten   : {len(rewrite_log)}")
    print(f"     Total deaths (fork)  : {plan.get('total_deaths', '?')}")
    print()
    print("  To compare versions:")
    print("  output/editing/              ← six-deaths fork (current)")
    print("  output/editing_v1_nine_deaths/ ← original nine deaths")
    print()
    print("  Next step: run Agent 4 (Illustration) when ready")
    print("═" * 62)
    print()


if __name__ == "__main__":
    plan_only = "--plan-only" in sys.argv
    run(plan_only=plan_only)
