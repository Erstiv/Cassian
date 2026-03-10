"""
Quick fix: re-run the Tartarus fork rewrite for Chapter 21 only.
Chapter 21 failed during the main fork run due to a JSON parsing error.
This script retries it with the same plan.
"""

import json
import sys
from pathlib import Path
from datetime import datetime

from google import genai
from google.genai import types
from colorama import init, Fore, Style
init(autoreset=True)

BASE_DIR    = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
EDITING_DIR = BASE_DIR / "output" / "editing"
PLAN_PATH   = BASE_DIR / "output" / "tartarus_fork" / "tartarus_restructure_plan.json"

def ok(msg):   print(f"{Fore.GREEN}  ✓ {msg}{Style.RESET_ALL}")
def info(msg): print(f"{Fore.CYAN}  → {msg}{Style.RESET_ALL}")
def err(msg):  print(f"{Fore.RED}  ✗ {msg}{Style.RESET_ALL}")

config    = json.load(open(CONFIG_PATH))
plan      = json.load(open(PLAN_PATH))
api_key   = config["gemini"]["api_key"]
model     = config["gemini"]["models"]["text"]
world_rules = json.dumps(config.get("world_rules", {}), indent=2)

# Load Chapter 21
ch_path = EDITING_DIR / "chapter_21_edited.json"
chapter = json.load(open(ch_path, encoding='utf-8'))

# Get Chapter 21 plan entry
ch21_plan = next((c for c in plan["challenges"] if c["chapter"] == 21), None)
if not ch21_plan:
    err("No plan entry for Chapter 21 found.")
    sys.exit(1)

info(f"Chapter 21 plan: {ch21_plan['proposed_outcome'].upper()}")
info(f"Detail: {ch21_plan['proposed_detail']}")
print()

prompt = f"""You are rewriting Chapter 21 of "One Thousand Perfect Sighs" by Elliot.

━━━ AUTHORIAL VOICE — PRESERVE ABOVE ALL ELSE ━━━
Lyrical, melancholic, precise. Soviet history, mythological weight, personal grief.
Change only what the restructure requires. Preserve sentences, dialogue, and events
unrelated to the challenge outcome.

━━━ THIS CHAPTER'S CHANGE ━━━
Outcome: {ch21_plan['proposed_outcome'].upper()}
What happens: {ch21_plan['proposed_detail']}
Deaths this challenge: {ch21_plan['deaths_this_challenge']}
Team count after this chapter: {ch21_plan['running_team_count']}

━━━ LITANY ADJUSTMENT ━━━
{plan.get('chapter_21_litany_adjustment', '')}

The litany names the six who have fallen so far in the correct order.
Deaths to this point: Suna Reed, Sev Aris, Santos Osa, Metta, and now Will Way.
Ubuntu has not yet died (that is Chapter 22).

━━━ WORLD RULES ━━━
{world_rules}

━━━ OUTPUT FORMAT ━━━
Return ONLY valid JSON. Use only standard JSON — no special characters, no backslashes
in text except for newlines (\\n). No Unicode escape sequences.

{{
  "chapter_id": "21",
  "rewritten_text": "THE COMPLETE REWRITTEN CHAPTER",
  "changes_made": ["change 1", "change 2"],
  "team_count_end": {ch21_plan['running_team_count']},
  "outcome_achieved": "{ch21_plan['proposed_outcome']}"
}}

Return ONLY the JSON. No preamble. No markdown fences.

━━━ CURRENT CHAPTER TEXT ━━━
{chapter.get('full_text', '')}
"""

info("Sending Chapter 21 to Gemini...")
client   = genai.Client(api_key=api_key)
response = client.models.generate_content(
    model=model,
    contents=prompt,
    config=types.GenerateContentConfig(temperature=0.5, max_output_tokens=65536)
)

raw = response.text.strip()
# Strip markdown fences if present
if "```" in raw:
    raw = raw.split("```")[1]
    if raw.startswith("json"):
        raw = raw[4:]
    raw = raw.strip()

try:
    result = json.loads(raw)
except json.JSONDecodeError as e:
    err(f"JSON parse error: {e}")
    err("Saving raw response to output/tartarus_fork/chapter_21_raw.txt for inspection")
    (BASE_DIR / "output" / "tartarus_fork" / "chapter_21_raw.txt").write_text(raw)
    sys.exit(1)

# Save
chapter["full_text"] = result["rewritten_text"]
chapter.setdefault("editing_metadata", {})
chapter["editing_metadata"].update({
    "tartarus_fork":   True,
    "fork_applied_at": datetime.now().isoformat(),
    "fork_outcome":    result.get("outcome_achieved", ""),
    "fork_changes":    result.get("changes_made", []),
    "team_count_end":  result.get("team_count_end", "?"),
})

json.dump(chapter, open(ch_path, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)

ok("Chapter 21 rewritten and saved.")
for c in result.get("changes_made", []):
    print(f"     • {c}")
