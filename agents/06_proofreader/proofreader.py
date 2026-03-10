"""
PROOFREADER AGENT — agents/06_proofreader/proofreader.py

Final surface-level quality pass before layout.
Flags typos, repeated words, homophones, punctuation issues,
capitalization problems, formatting quirks, and continuity errors.

ADVISORY ONLY — this agent does NOT modify chapter files.
The user reviews flags in the UI and fixes them in the Workbench.

Input:
  - Chapter text (priority: workbench working copy → edited → ingested)
  - config.json for API key and model
  - CASSIAN_PROJECT_DIR env var

Output:
  - output/proofreading/proofread_report.json    — full summary report
  - output/proofreading/proofread_report.md      — human-readable markdown
  - output/proofreading/chapter_{key}_issues.json — per-chapter issue files

Usage:
  python agents/06_proofreader/proofreader.py                 # all chapters
  python agents/06_proofreader/proofreader.py --chapter 01   # single chapter
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types


# ── Constants ──────────────────────────────────────────────────────────────────

WORD_SPLIT_THRESHOLD = 8000   # Split chapters longer than this into two halves
MAX_RETRIES          = 3
RETRY_TEMPERATURES   = [0.2, 0.4, 0.6]
RATE_LIMIT_DELAY     = 1.5    # seconds between API calls


# ── Paths ───────────────────────────────────────────────────────────────────────

def get_project_dir() -> Path:
    raw = os.environ.get("CASSIAN_PROJECT_DIR", "").strip()
    if not raw:
        sys.exit("ERROR: CASSIAN_PROJECT_DIR environment variable is not set.")
    p = Path(raw)
    if not p.exists():
        sys.exit(f"ERROR: Project directory not found: {p}")
    return p


def get_chapter_text_path(project_dir: Path, key: str) -> tuple[Path | None, str]:
    """
    Return (path, source_label) for the most up-to-date chapter file.
    Priority: workbench working copy → edited → ingested
    """
    candidates = [
        (project_dir / "output" / "workbench" / f"chapter_{key}_working.json", "workbench"),
        (project_dir / "output" / "editing"   / f"chapter_{key}_edited.json",  "edited"),
        (project_dir / "output" / "ingested"  / f"chapter_{key}.json",         "ingested"),
    ]
    for path, label in candidates:
        if path.exists():
            return path, label
    return None, "none"


def load_chapter(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def chapter_to_plain_text(data: dict) -> tuple[str, int]:
    """
    Convert chapter JSON to plain text for Gemini.
    Returns (text, word_count).
    """
    paragraphs = data.get("paragraphs", [])
    lines = []
    for para in paragraphs:
        text = para.get("text", "").strip()
        if text:
            lines.append(text)
    full_text = "\n\n".join(lines)
    word_count = len(full_text.split())
    return full_text, word_count


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config(project_dir: Path) -> dict:
    config_path = project_dir / "config.json"
    if not config_path.exists():
        sys.exit(f"ERROR: config.json not found at {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_api_key(config: dict) -> str:
    key = (
        config.get("gemini", {}).get("api_key")
        or config.get("api_key")
        or os.environ.get("GEMINI_API_KEY", "")
    )
    if not key:
        sys.exit("ERROR: No Gemini API key found in config.json or GEMINI_API_KEY env var.")
    return key


def get_fast_model(config: dict) -> str:
    return (
        config.get("gemini", {}).get("models", {}).get("fast")
        or "gemini-2.5-flash"
    )


# ── Gemini ─────────────────────────────────────────────────────────────────────

PROOFREADING_PROMPT = """You are a professional book proofreader performing a final quality check.

Read the following chapter text and identify ALL surface-level issues. Look for:

1. TYPOS — misspelled words, missing letters, transposed letters
2. REPEATED WORDS — "the the", "was was", accidental word duplication
3. HOMOPHONES — wrong word used (their/there/they're, its/it's, your/you're, etc.)
4. PUNCTUATION — missing periods, double spaces, mismatched quotes, em-dash vs en-dash issues, missing commas in compound sentences
5. CAPITALIZATION — inconsistent capitalization of proper nouns, start of sentences
6. FORMATTING — inconsistent paragraph spacing markers, orphaned dialogue tags
7. CONTINUITY — a character's name spelled differently than earlier (e.g., "Daniil" vs "Danill"), inconsistent place name spelling

For each issue found, report:
- The exact text containing the issue (10-20 words of context, with the problematic word/phrase in **bold**)
- The issue category (one of: typo, repeated_word, homophone, punctuation, capitalization, formatting, continuity)
- A brief explanation of the problem
- A suggested fix

RESPOND IN THIS EXACT JSON FORMAT (no markdown fences, no commentary):
{
  "issues": [
    {
      "paragraph_index": 0,
      "context": "the exact surrounding text with the **issue** highlighted",
      "category": "typo",
      "explanation": "Brief description of the problem",
      "suggestion": "The corrected text"
    }
  ],
  "summary": {
    "total_issues": 5,
    "by_category": {"typo": 2, "punctuation": 2, "repeated_word": 1},
    "quality_rating": "good",
    "notes": "Optional overall note about chapter quality"
  }
}

If no issues are found, return: {"issues": [], "summary": {"total_issues": 0, "by_category": {}, "quality_rating": "excellent", "notes": "No issues found."}}

Quality rating scale:
- excellent: 0 issues
- good: 1-3 minor issues
- fair: 4-8 issues or any significant ones
- needs_work: 9+ issues

CHAPTER TEXT:
{chapter_text}"""


def call_gemini(client, model: str, chapter_text: str, temperature: float = 0.2) -> dict | None:
    """
    Call Gemini with the proofreading prompt.
    Returns parsed JSON dict or None on failure.
    """
    prompt = PROOFREADING_PROMPT.replace("{chapter_text}", chapter_text)

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=8192,
            ),
        )
        raw = response.text.strip()

        # Strip markdown code fences if Gemini wrapped it anyway
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw   = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        return json.loads(raw)

    except json.JSONDecodeError as exc:
        print(f"    [WARN] JSON parse failed: {exc}")
        return None
    except Exception as exc:
        print(f"    [WARN] Gemini call failed: {exc}")
        return None


def proofread_chapter(client, model: str, chapter_text: str, word_count: int) -> dict:
    """
    Proofread a single chapter. Splits long chapters in two halves.
    Retries up to MAX_RETRIES times with increasing temperature on JSON errors.
    Returns the standardized issues dict.
    """
    if word_count > WORD_SPLIT_THRESHOLD:
        print(f"    Chapter is {word_count} words — splitting into two halves for Gemini.")
        words     = chapter_text.split()
        mid       = len(words) // 2
        half_a    = " ".join(words[:mid])
        half_b    = " ".join(words[mid:])
        result_a  = _proofread_single_pass(client, model, half_a)
        time.sleep(RATE_LIMIT_DELAY)
        result_b  = _proofread_single_pass(client, model, half_b)

        # Offset paragraph_index for second half (approximate — we don't know exact split point)
        combined_issues = result_a.get("issues", [])
        offset          = len(chapter_text.split("\n\n")) // 2
        for issue in result_b.get("issues", []):
            issue_copy = dict(issue)
            issue_copy["paragraph_index"] = issue.get("paragraph_index", 0) + offset
            combined_issues.append(issue_copy)

        # Merge summaries
        by_cat_a = result_a.get("summary", {}).get("by_category", {})
        by_cat_b = result_b.get("summary", {}).get("by_category", {})
        merged_by_cat: dict[str, int] = {}
        for cat, cnt in {**by_cat_a, **by_cat_b}.items():
            merged_by_cat[cat] = merged_by_cat.get(cat, 0) + cnt

        total = len(combined_issues)
        rating = _compute_rating(total)

        return {
            "issues": combined_issues,
            "summary": {
                "total_issues":  total,
                "by_category":   merged_by_cat,
                "quality_rating": rating,
                "notes":         "Split chapter — merged from two proofreading passes.",
            },
        }
    else:
        return _proofread_single_pass(client, model, chapter_text)


def _proofread_single_pass(client, model: str, text: str) -> dict:
    """Single Gemini call with retry logic."""
    for attempt in range(MAX_RETRIES):
        temp   = RETRY_TEMPERATURES[min(attempt, len(RETRY_TEMPERATURES) - 1)]
        result = call_gemini(client, model, text, temperature=temp)
        if result is not None:
            return result
        print(f"    [WARN] Attempt {attempt + 1}/{MAX_RETRIES} failed. Retrying...")
        time.sleep(1.5)

    # All retries failed — return an error entry
    return {
        "issues": [{
            "paragraph_index": 0,
            "context":         "(Proofreading failed after retries)",
            "category":        "error",
            "explanation":     "Gemini returned invalid JSON on all retry attempts.",
            "suggestion":      "Re-run the proofreader for this chapter.",
        }],
        "summary": {
            "total_issues":   1,
            "by_category":    {"error": 1},
            "quality_rating": "error",
            "notes":          "Agent could not parse Gemini response.",
        },
    }


def _compute_rating(issue_count: int) -> str:
    if issue_count == 0:  return "excellent"
    if issue_count <= 3:  return "good"
    if issue_count <= 8:  return "fair"
    return "needs_work"


# ── Chapter discovery ──────────────────────────────────────────────────────────

def discover_chapters(project_dir: Path) -> list[str]:
    """
    Return sorted list of chapter keys that have at least one source file.
    Keys are the numeric/string portion: "01", "02", ... "epilogue" etc.
    """
    keys_found: set[str] = set()

    for subdir, pattern, strip_suffix in [
        ("output/workbench", "chapter_*_working.json", "_working"),
        ("output/editing",   "chapter_*_edited.json",  "_edited"),
        ("output/ingested",  "chapter_*.json",          ""),
    ]:
        search_dir = project_dir / subdir
        if not search_dir.exists():
            continue
        for f in search_dir.glob(pattern):
            stem = f.stem  # e.g. "chapter_01" or "chapter_01_edited"
            # Strip prefix "chapter_"
            without_prefix = stem.replace("chapter_", "", 1)
            # Strip suffix
            if strip_suffix and without_prefix.endswith(strip_suffix.lstrip("_")):
                without_prefix = without_prefix[: -len(strip_suffix.lstrip("_")) - 1]
            keys_found.add(without_prefix)

    # Sort: numeric keys first, then alphabetic (epilogue, prologue, etc.)
    def sort_key(k: str):
        try:
            return (0, int(k))
        except ValueError:
            return (1, k)

    return sorted(keys_found, key=sort_key)


# ── Output writers ─────────────────────────────────────────────────────────────

def save_chapter_issues(project_dir: Path, chapter_key: str, chapter_data: dict,
                        source: str, word_count: int, result: dict) -> None:
    out_dir = project_dir / "output" / "proofreading"
    out_dir.mkdir(parents=True, exist_ok=True)

    out = {
        "chapter_key":   chapter_key,
        "title":         chapter_data.get("title", f"Chapter {chapter_key}"),
        "source":        source,
        "word_count":    word_count,
        "issues":        result.get("issues", []),
        "summary":       result.get("summary", {}),
        "proofread_at":  datetime.now(timezone.utc).isoformat(),
    }

    path = out_dir / f"chapter_{chapter_key}_issues.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


def save_summary_report(project_dir: Path, chapter_results: list[dict]) -> dict:
    """
    Build and write the summary proofread_report.json.
    chapter_results: list of per-chapter issue dicts (as saved to disk).
    """
    out_dir = project_dir / "output" / "proofreading"
    out_dir.mkdir(parents=True, exist_ok=True)

    total_issues = 0
    by_category: dict[str, int] = {}
    chapters_summary = []

    for cr in chapter_results:
        summary     = cr.get("summary", {})
        count       = summary.get("total_issues", 0)
        total_issues += count

        for cat, cnt in summary.get("by_category", {}).items():
            by_category[cat] = by_category.get(cat, 0) + cnt

        chapters_summary.append({
            "chapter_key":    cr["chapter_key"],
            "title":          cr.get("title", ""),
            "issue_count":    count,
            "quality_rating": summary.get("quality_rating", "unknown"),
        })

    overall_rating = _compute_rating(total_issues)

    report = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "total_chapters": len(chapter_results),
        "total_issues":   total_issues,
        "by_category":    by_category,
        "chapters":       chapters_summary,
        "overall_rating": overall_rating,
    }

    report_path = out_dir / "proofread_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Also write human-readable markdown
    md_lines = [
        f"# Proofreading Report",
        f"",
        f"Generated: {report['generated_at'][:16].replace('T', ' ')} UTC",
        f"Chapters:  {report['total_chapters']}",
        f"Issues:    {report['total_issues']}",
        f"Rating:    {report['overall_rating'].upper()}",
        f"",
        f"## By Category",
        f"",
    ]
    for cat, cnt in sorted(by_category.items(), key=lambda x: -x[1]):
        md_lines.append(f"- **{cat.replace('_', ' ').title()}**: {cnt}")

    md_lines += ["", "## Chapters", ""]
    for ch in chapters_summary:
        rating_icon = {"excellent": "✅", "good": "🟡", "fair": "🟠", "needs_work": "🔴"}.get(
            ch["quality_rating"], "❓"
        )
        md_lines.append(
            f"- {rating_icon} Ch {ch['chapter_key']}: \"{ch['title']}\" — "
            f"{ch['issue_count']} issue{'s' if ch['issue_count'] != 1 else ''} "
            f"({ch['quality_rating']})"
        )

    md_path = out_dir / "proofread_report.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    return report


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cassian Proofreader Agent")
    parser.add_argument("--chapter", metavar="KEY",
                        help="Proofread a single chapter (e.g. --chapter 01)")
    args = parser.parse_args()

    project_dir = get_project_dir()
    config      = load_config(project_dir)
    api_key     = get_api_key(config)
    model_name  = get_fast_model(config)

    print(f"\n  Cassian Proofreader")
    print(f"  Project: {project_dir}")
    print(f"  Model:   {model_name}")

    client = genai.Client(api_key=api_key)

    # Which chapters to process?
    if args.chapter:
        keys = [args.chapter]
        print(f"  Mode:    single chapter ({args.chapter})\n")
    else:
        keys = discover_chapters(project_dir)
        print(f"  Mode:    all chapters ({len(keys)} found)\n")

    if not keys:
        sys.exit("ERROR: No chapters found. Run the intake agent first.")

    # Load any existing per-chapter results to merge (for partial re-runs)
    # We'll rebuild the summary from scratch at the end.
    all_chapter_results: list[dict] = []

    for key in keys:
        print(f"  ── Chapter {key} ──")

        # Find the chapter source file
        path, source = get_chapter_text_path(project_dir, key)
        if path is None:
            print(f"    [SKIP] No source file found for chapter {key}")
            continue

        print(f"    Source: {source}")

        # Load and convert to plain text
        try:
            chapter_data = load_chapter(path)
        except Exception as exc:
            print(f"    [ERROR] Could not load chapter file: {exc}")
            continue

        chapter_text, word_count = chapter_to_plain_text(chapter_data)
        print(f"    Words:  {word_count}")

        if not chapter_text.strip():
            print(f"    [SKIP] Chapter has no text content.")
            continue

        # Call Gemini
        print(f"    Proofreading…")
        result = proofread_chapter(client, model_name, chapter_text, word_count)

        issue_count = result.get("summary", {}).get("total_issues", 0)
        rating      = result.get("summary", {}).get("quality_rating", "unknown")
        print(f"    Issues: {issue_count}  Rating: {rating}")

        # Save per-chapter file
        save_chapter_issues(project_dir, key, chapter_data, source, word_count, result)

        all_chapter_results.append({
            "chapter_key": key,
            "title":       chapter_data.get("title", f"Chapter {key}"),
            "summary":     result.get("summary", {}),
        })

        # Rate limit between chapters
        time.sleep(RATE_LIMIT_DELAY)

    # If we only ran a single chapter, we need to merge with any existing
    # per-chapter files to produce an accurate summary report.
    if args.chapter:
        # Load all existing per-chapter issue files to build a complete summary
        all_chapter_results = _load_all_chapter_results(project_dir)

    # Write summary report
    if all_chapter_results:
        report = save_summary_report(project_dir, all_chapter_results)
        print(f"\n  ✅ Done — {report['total_issues']} total issues across {report['total_chapters']} chapters.")
        print(f"     Overall rating: {report['overall_rating']}")
        print(f"     Report saved: output/proofreading/proofread_report.json")
    else:
        print("\n  [WARN] No chapters were processed — no summary report written.")


def _load_all_chapter_results(project_dir: Path) -> list[dict]:
    """Load all existing chapter_*_issues.json files for summary report generation."""
    proof_dir = project_dir / "output" / "proofreading"
    if not proof_dir.exists():
        return []

    results = []

    def sort_key(p: Path):
        # Extract key from "chapter_01_issues.json"
        stem = p.stem  # "chapter_01_issues"
        k    = stem.replace("chapter_", "").replace("_issues", "")
        try:
            return (0, int(k))
        except ValueError:
            return (1, k)

    for path in sorted(proof_dir.glob("chapter_*_issues.json"), key=sort_key):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            results.append({
                "chapter_key": data.get("chapter_key", "?"),
                "title":       data.get("title", ""),
                "summary":     data.get("summary", {}),
            })
        except Exception:
            continue

    return results


if __name__ == "__main__":
    main()
