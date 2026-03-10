"""
╔══════════════════════════════════════════════════════════════════╗
║  UTILITY — RENUMBER CHAPTERS                                    ║
║  One Thousand Perfect Sighs                                     ║
║                                                                  ║
║  Renames your 31 source .docx files from the original mixed     ║
║  numbering (1–28 + 4b/5b/6b) to clean sequential numbers:      ║
║  chapter_01.docx through chapter_30.docx + epilogue.docx        ║
║                                                                  ║
║  Run with --preview to see what will change before committing.  ║
║                                                                  ║
║  How to run:                                                     ║
║    python utils/renumber_chapters.py --preview   (safe, look)  ║
║    python utils/renumber_chapters.py             (actually do)  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import sys
import shutil
from pathlib import Path
from colorama import init, Fore, Style
init(autoreset=True)

BASE_DIR    = Path(__file__).resolve().parent.parent
CHAPTERS_DIR = BASE_DIR / "input" / "chapters"

# ── The complete renaming map ─────────────────────────────────────────────────
# Left side:  current filename (exactly as it is on disk)
# Right side: new clean filename
#
# Reading order: 1, 2, 3, [4b→4], [4→5], 5, [5b→7], 6, [6b→9], 7–27, [28→Epilogue]

RENAME_MAP = {
    "chapter 1.docx":   "chapter_01.docx",
    "chapter 2.docx":   "chapter_02.docx",
    "chapter 3.docx":   "chapter_03.docx",
    "Chapter 4b.docx":  "chapter_04.docx",   # was 4b, now Chapter 4
    "chapter 4.docx":   "chapter_05.docx",   # was 4,  now Chapter 5
    "chapter 5.docx":   "chapter_06.docx",
    "chapter 5b.docx":  "chapter_07.docx",   # was 5b, now Chapter 7
    "chapter 6.docx":   "chapter_08.docx",
    "chapter 6b.docx":  "chapter_09.docx",   # was 6b, now Chapter 9
    "chapter 7.docx":   "chapter_10.docx",
    "chapter 8.docx":   "chapter_11.docx",
    "chapter 9.docx":   "chapter_12.docx",
    "chapter 10.docx":  "chapter_13.docx",
    "chapter 11.docx":  "chapter_14.docx",
    "chapter 12.docx":  "chapter_15.docx",
    "chapter 13.docx":  "chapter_16.docx",
    "chapter 14.docx":  "chapter_17.docx",
    "chapter 15.docx":  "chapter_18.docx",
    "chapter 16.docx":  "chapter_19.docx",
    "chapter 17.docx":  "chapter_20.docx",
    "chapter 18.docx":  "chapter_21.docx",
    "chapter 19.docx":  "chapter_22.docx",
    "chapter 20.docx":  "chapter_23.docx",
    "chapter 21.docx":  "chapter_24.docx",
    "chapter 22.docx":  "chapter_25.docx",
    "chapter 23.docx":  "chapter_26.docx",
    "chapter 24.docx":  "chapter_27.docx",
    "chapter 25.docx":  "chapter_28.docx",
    "chapter 26.docx":  "chapter_29.docx",
    "chapter 27.docx":  "chapter_30.docx",
    "chapter 28.docx":  "epilogue.docx",      # Chapter 28 becomes the Epilogue
}


def run(preview_only: bool = False):
    mode = "PREVIEW MODE — no files will be changed" if preview_only else "RENAMING FILES"

    print()
    print("═" * 62)
    print(f"  RENUMBER CHAPTERS — {mode}")
    print("═" * 62)
    print()

    # Check every source file exists before doing anything
    missing = []
    for old_name in RENAME_MAP:
        if not (CHAPTERS_DIR / old_name).exists():
            missing.append(old_name)

    if missing:
        print(f"{Fore.RED}  ✗ These files were not found in input/chapters/:{Style.RESET_ALL}")
        for m in missing:
            print(f"      {m}")
        print()
        print("  Please check the filenames and try again.")
        return

    # Show the full rename plan
    print(f"  {'OLD FILENAME':<30}  →  NEW FILENAME")
    print(f"  {'─' * 30}     {'─' * 22}")
    for old_name, new_name in RENAME_MAP.items():
        old_path = CHAPTERS_DIR / old_name
        new_path = CHAPTERS_DIR / new_name

        # Flag if the new name already exists (would be overwritten)
        conflict = " ⚠ FILE EXISTS" if new_path.exists() and old_name != new_name else ""
        print(f"  {Fore.YELLOW}{old_name:<30}{Style.RESET_ALL}  →  {Fore.GREEN}{new_name}{Style.RESET_ALL}{Fore.RED}{conflict}{Style.RESET_ALL}")

    print()

    if preview_only:
        print(f"  {Fore.CYAN}Preview complete. Run without --preview to apply these renames.{Style.RESET_ALL}")
        print()
        return

    # Confirm before proceeding
    print("  This will rename all 31 files listed above.")
    answer = input("  Type YES to continue: ").strip()
    if answer != "YES":
        print(f"\n  {Fore.YELLOW}Cancelled — no files were changed.{Style.RESET_ALL}\n")
        return

    print()

    # Do a two-phase rename to avoid collisions
    # (e.g. "chapter 4.docx" → "chapter_05.docx" must not clobber anything)
    # Phase 1: rename everything to a temp name
    temp_paths = {}
    for old_name in RENAME_MAP:
        old_path = CHAPTERS_DIR / old_name
        temp_path = CHAPTERS_DIR / f"__temp__{old_name}"
        old_path.rename(temp_path)
        temp_paths[old_name] = temp_path

    # Phase 2: rename from temp to final name
    for old_name, new_name in RENAME_MAP.items():
        temp_path = temp_paths[old_name]
        new_path  = CHAPTERS_DIR / new_name
        temp_path.rename(new_path)
        print(f"  {Fore.GREEN}✓{Style.RESET_ALL} {old_name:<30} → {new_name}")

    print()
    print("═" * 62)
    print(f"  {Fore.GREEN}✓ All 31 files renamed successfully.{Style.RESET_ALL}")
    print()
    print("  Next step: re-run ingestion to rebuild the JSON files:")
    print("  python agents/01_ingestion/ingest.py")
    print("═" * 62)
    print()


if __name__ == "__main__":
    preview = "--preview" in sys.argv
    run(preview_only=preview)
