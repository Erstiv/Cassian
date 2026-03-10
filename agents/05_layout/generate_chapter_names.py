"""
╔══════════════════════════════════════════════════════════════════╗
║  CHAPTER NAME GENERATOR                                         ║
║  One Thousand Perfect Sighs                                      ║
║                                                                  ║
║  Reads each edited chapter and asks Gemini to create an         ║
║  evocative 3-6 word chapter title based on the content.        ║
║                                                                  ║
║  Output:  output/formatting/chapter_names.json                  ║
║                                                                  ║
║  Run this BEFORE layout.py. The layout agent reads the JSON    ║
║  and uses the names as chapter subtitles and in running headers.║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/05_layout/generate_chapter_names.py            ║
║    python agents/05_layout/generate_chapter_names.py --redo 04  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

from google import genai
from google.genai import types
from colorama import init, Fore, Style
init(autoreset=True)

def green(s):  return Fore.GREEN  + str(s) + Style.RESET_ALL
def yellow(s): return Fore.YELLOW + str(s) + Style.RESET_ALL
def cyan(s):   return Fore.CYAN   + str(s) + Style.RESET_ALL
def red(s):    return Fore.RED    + str(s) + Style.RESET_ALL
def bold(s):   return Style.BRIGHT + str(s) + Style.RESET_ALL

import os
BASE_DIR = (
    Path(os.environ['CASSIAN_PROJECT_DIR'])
    if 'CASSIAN_PROJECT_DIR' in os.environ
    else Path(__file__).resolve().parent.parent.parent
)
CONFIG_PATH    = BASE_DIR / "config.json"
EDITING_DIR    = BASE_DIR / "output" / "editing"
OUTPUT_PATH    = BASE_DIR / "output" / "formatting" / "chapter_names.json"

EXCERPT_WORDS  = 700   # words sent to Gemini per chapter

SYSTEM_PROMPT = """\
You are a literary editor working on a science fiction novel called \
"One Thousand Perfect Sighs". The novel is a serious, atmospheric literary \
sci-fi with Soviet/Russian historical elements, set in 2033. \
The prose is precise, melancholic, and occasionally mythological in register. \
It follows Daniil Tsarasov, an ageing Soviet scientist who built a machine \
called Chronos that has been slowly exhaling for 87 years, and Marcus Wright, \
an American scientist who discovers Daniil's work.\
"""

CHAPTER_PROMPT = """\
Read this chapter excerpt and create a single evocative chapter title.

Requirements:
- 3 to 6 words long
- Poetic and atmospheric — like the chapter titles of great literary fiction
- Hints at the emotional core or key event without summarising the plot
- Matches the tone: serious, mythological, melancholic
- Should feel like it belongs in a book like "The Road" or "Never Let Me Go"
- Do NOT start with "The " unless it feels genuinely essential
- Do NOT use colons in the title
- Return ONLY the chapter title — no quotes, no punctuation at the end,
  no explanation, no alternatives

Chapter excerpt ({word_count} words):
---
{excerpt}
---

Chapter title:\
"""


def get_excerpt(full_text: str, max_words: int = EXCERPT_WORDS) -> str:
    """Return the first max_words words of the chapter body text."""
    words = full_text.split()
    return ' '.join(words[:max_words])


def load_existing(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def generate_name(client, model: str, excerpt: str) -> str:
    """Call Gemini and return a clean chapter title string."""
    word_count = len(excerpt.split())
    prompt = CHAPTER_PROMPT.format(excerpt=excerpt, word_count=word_count)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.85,
            max_output_tokens=40,
        ),
    )
    name = response.text.strip()
    # Strip any surrounding quotes the model might add
    name = name.strip('"\'').strip()
    # Remove trailing punctuation (period, comma)
    name = name.rstrip('.,;:')
    return name


def main():
    print()
    print(bold(cyan('╔══════════════════════════════════════════════════════════════════╗')))
    print(bold(cyan('║  CHAPTER NAME GENERATOR — One Thousand Perfect Sighs            ║')))
    print(bold(cyan('╚══════════════════════════════════════════════════════════════════╝')))
    print()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--redo', metavar='KEY',
        help='Re-generate name for a specific chapter key only (e.g. --redo 04 or --redo epilogue)'
    )
    parser.add_argument(
        '--force-all', action='store_true',
        help='Re-generate all names even if chapter_names.json already exists'
    )
    args = parser.parse_args()

    # ── Config ────────────────────────────────────────────────────────────────
    if not CONFIG_PATH.exists():
        print(red(f'  ✗  config.json not found'))
        sys.exit(1)
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))

    api_key = config['gemini']['api_key']
    model   = config['gemini']['models']['fast']   # gemini-2.5-flash — fast + cheap
    print(green(f'  ✓  Config loaded — using model: {model}'))

    client = genai.Client(api_key=api_key)

    # ── Load existing names (so we can skip already-done chapters) ───────────
    existing = load_existing(OUTPUT_PATH)
    if existing and not args.force_all and not args.redo:
        print(yellow(f'  ℹ  Found existing chapter_names.json ({len(existing)} entries)'))
        print(yellow('     Only generating missing entries. Use --force-all to redo all.'))

    reading_order = config['book']['reading_order']
    names = dict(existing)   # start from existing, fill in gaps

    print()

    for seq, chapter_key in enumerate(reading_order, start=1):
        key_str = str(chapter_key)

        # Filter for --redo
        if args.redo:
            # Normalise: "04" → "4", "epilogue" → "epilogue"
            redo_norm = args.redo.lstrip('0') or '0'
            key_norm  = key_str.lstrip('0') or '0'
            if redo_norm != key_norm and args.redo != key_str:
                continue

        # Skip if already done (unless forced)
        if not args.force_all and not args.redo and key_str in names and names[key_str]:
            label = f'Epilogue' if chapter_key == 'epilogue' else f'Chapter {seq}'
            print(green(f'  ✓  {label}: "{names[key_str]}" (cached)'))
            continue

        # Load chapter data
        if chapter_key == 'epilogue':
            fname = 'epilogue_edited.json'
            label = 'Epilogue'
        else:
            fname  = f'chapter_{int(chapter_key):02d}_edited.json'
            label  = f'Chapter {seq}'

        path = EDITING_DIR / fname
        if not path.exists():
            print(red(f'  ✗  {label}: edited file not found ({fname})'))
            continue

        chapter_data = json.loads(path.read_text(encoding='utf-8'))
        excerpt = get_excerpt(chapter_data.get('full_text', ''))

        print(f'  {cyan("►")}  {label} ... ', end='', flush=True)

        try:
            name = generate_name(client, model, excerpt)
            names[key_str] = name
            print(green(f'"{name}"'))
        except Exception as e:
            print(red(f'ERROR: {e}'))
            if key_str not in names:
                names[key_str] = ''

        time.sleep(0.4)   # gentle rate limiting

    # ── Save ──────────────────────────────────────────────────────────────────
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(names, indent=2, ensure_ascii=False),
        encoding='utf-8'
    )

    filled = sum(1 for v in names.values() if v)
    print()
    print(bold(green(f'  ✓  {filled}/{len(reading_order)} chapter names saved → output/formatting/chapter_names.json')))
    print()


if __name__ == '__main__':
    main()
