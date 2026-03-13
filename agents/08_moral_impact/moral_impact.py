"""
MORAL IMPACT EDITOR — agents/08_moral_impact/moral_impact.py

AI-powered ethical and social impact analysis. Reads each chapter and
flags content that might make the world a worse place — and suggests
ways the author could make it better.

ADVISORY ONLY — this agent does NOT modify chapter files.
The author reviews flags in the UI and acknowledges or ignores them.

Input:
  - Chapter text (priority: workbench working copy → edited → ingested)
  - config.json for API key and model
  - CASSIAN_PROJECT_DIR env var
  - World Rules export (if exists) for intentional character context

Output:
  - output/moral_impact/moral_impact_report.json     — full summary report
  - output/moral_impact/moral_impact_report.md        — human-readable markdown
  - output/moral_impact/chapter_{key}_concerns.json   — per-chapter concern files
  - output/moral_impact/world_rules_context.json      — world rules snapshot (optional)

Usage:
  python agents/08_moral_impact/moral_impact.py                  # all chapters
  python agents/08_moral_impact/moral_impact.py --chapter 01    # single chapter
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


def get_text_model(config: dict) -> str:
    """Use the text model (gemini-2.5-pro) for nuanced ethical analysis."""
    return (
        config.get("gemini", {}).get("models", {}).get("text")
        or config.get("gemini", {}).get("models", {}).get("fast")
        or "gemini-2.5-pro"
    )


# ── World Rules context ─────────────────────────────────────────────────────────

def export_world_rules_context(project_dir: Path) -> str:
    """
    Load world rules JSON and extract a plain-text summary for the prompt.
    Returns empty string if no world rules exist.
    """
    world_rules_path = project_dir / "output" / "world_rules" / "world_rules.json"
    if not world_rules_path.exists():
        return ""

    try:
        with open(world_rules_path, "r", encoding="utf-8") as f:
            wr = json.load(f)
    except Exception:
        return ""

    lines = []

    # Characters
    chars = wr.get("characters", [])
    if chars:
        lines.append("CHARACTERS:")
        for c in chars[:20]:
            name  = c.get("name", "Unknown")
            desc  = c.get("description", "")
            lines.append(f"  - {name}: {desc}")

    # Locations
    locs = wr.get("locations", [])
    if locs:
        lines.append("LOCATIONS:")
        for loc in locs[:10]:
            name = loc.get("name", "")
            desc = loc.get("description", "")
            lines.append(f"  - {name}: {desc}")

    # Notes / rules
    notes = wr.get("notes", "")
    if notes:
        lines.append(f"WORLD NOTES: {notes}")

    genre = wr.get("genre", "")

    summary = "\n".join(lines)

    # Save snapshot so route can reference it
    out_dir = project_dir / "output" / "moral_impact"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "summary":     summary,
        "genre":       genre,
    }
    with open(out_dir / "world_rules_context.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    return summary


def get_genre(project_dir: Path) -> str:
    """Try to get the genre from world rules or config."""
    wr_path = project_dir / "output" / "world_rules" / "world_rules.json"
    if wr_path.exists():
        try:
            with open(wr_path, "r", encoding="utf-8") as f:
                wr = json.load(f)
            g = wr.get("genre", "")
            if g:
                return g
        except Exception:
            pass

    cfg_path = project_dir / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("genre", "Literary fiction")
        except Exception:
            pass

    return "Literary fiction"


# ── Gemini ─────────────────────────────────────────────────────────────────────

MORAL_IMPACT_PROMPT = """You are a thoughtful ethical reviewer performing a moral and social impact analysis of a book chapter.

Your role is NOT to censor or police. Your role is to help the author ensure their book is a positive force in the world — or at minimum, not an unintentionally harmful one. Authors may intentionally depict difficult, dark, or morally complex content. Your job is to ensure they're doing so thoughtfully and with awareness.

PROJECT CONTEXT:
Genre: {genre}
World Rules / Character descriptions: {world_rules_summary}

Read the following chapter and analyze for:

1. VIOLENCE_IMPACT — Is violence depicted gratuitously, or does it serve the narrative? Could it desensitize or glorify? Does it show consequences? Note: depicting violence is not inherently harmful — gratuitous or consequence-free violence is the concern.
2. TRAUMA_PORTRAYAL — Is trauma handled with care and nuance? Are traumatic experiences used as plot devices without addressing their real impact on characters? Are there potential triggers that lack appropriate narrative framing?
3. HARMFUL_IDEOLOGIES — Does the text inadvertently promote or normalize harmful ideologies (supremacism, dehumanization, authoritarianism) without critical examination? Characters may hold these views — but does the narrative challenge them?
4. EXPLOITATION — Are vulnerable populations (children, elderly, disabled, marginalized groups) depicted in exploitative ways? Is suffering used for entertainment without purpose?
5. MISINFORMATION_RISK — Does the narrative present false or misleading information as fact? Could fictional claims be mistaken for real-world truth in ways that could cause harm (medical, scientific, historical)?
6. EMPATHY_AND_HUMANITY — Does the narrative encourage empathy? Are "villains" or antagonists given humanity, or are groups dehumanized? Are there missed opportunities to foster understanding?
7. CONSTRUCTIVE_MESSAGING — Could the narrative inspire positive change? Are there opportunities to model healthy relationships, resilience, or moral growth that the author might explore?
8. ENVIRONMENTAL_AWARENESS — Does the narrative normalize environmental destruction? Are there opportunities to weave in environmental consciousness without being preachy?

For each concern, classify its severity:
- NOTE: informational, no action likely needed — just something to be aware of
- CONSIDER: worth thinking about, may or may not need changes
- FLAG: should be reviewed carefully, likely needs attention

RESPOND IN THIS EXACT JSON FORMAT (no markdown fences):
{{
  "concerns": [
    {{
      "paragraph_index": 0,
      "context": "the surrounding text with the **relevant passage** highlighted",
      "category": "violence_impact",
      "severity": "consider",
      "explanation": "Why this might be a concern for the world",
      "suggestion": "How the author might address this, if they choose to"
    }}
  ],
  "strengths": [
    "Positive moral/social impact elements worth celebrating"
  ],
  "summary": {{
    "total_concerns": 3,
    "by_severity": {{"note": 1, "consider": 1, "flag": 1}},
    "by_category": {{"violence_impact": 1, "trauma_portrayal": 1, "empathy_and_humanity": 1}},
    "overall_assessment": "Brief overall assessment of the chapter's moral and social impact"
  }}
}}

If no concerns are found, return empty concerns array with a positive overall_assessment celebrating what the chapter does well.

IMPORTANT GUIDELINES:
- Be thoughtful, not reactionary. Dark themes handled well can make the world BETTER.
- Distinguish between depicting harm (which can build empathy) and promoting harm.
- Fiction that makes readers uncomfortable can be powerful and positive.
- The goal is not sanitization — it's intentionality. Help the author be deliberate.
- Celebrate strengths generously. Note when the author handles difficult material well.
- Avoid false positives: not every conflict is a concern, not every villain's speech needs a flag.

CHAPTER TEXT:
{chapter_text}"""


def call_gemini(client, model: str, chapter_text: str, genre: str,
                world_rules_summary: str, temperature: float = 0.2) -> dict | None:
    """
    Call Gemini with the moral impact prompt.
    Returns parsed JSON dict or None on failure.
    """
    prompt = MORAL_IMPACT_PROMPT.format(
        genre=genre,
        world_rules_summary=world_rules_summary or "No world rules available.",
        chapter_text=chapter_text,
    )

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


def analyze_chapter(client, model: str, chapter_text: str, word_count: int,
                    genre: str, world_rules_summary: str) -> dict:
    """
    Analyze a single chapter. Splits long chapters in two halves.
    Retries up to MAX_RETRIES times with increasing temperature on JSON errors.
    """
    if word_count > WORD_SPLIT_THRESHOLD:
        print(f"    Chapter is {word_count} words — splitting into two halves for Gemini.")
        words    = chapter_text.split()
        mid      = len(words) // 2
        half_a   = " ".join(words[:mid])
        half_b   = " ".join(words[mid:])
        result_a = _analyze_single_pass(client, model, half_a, genre, world_rules_summary)
        time.sleep(RATE_LIMIT_DELAY)
        result_b = _analyze_single_pass(client, model, half_b, genre, world_rules_summary)

        # Offset paragraph_index for second half
        combined_concerns = result_a.get("concerns", [])
        offset            = len(chapter_text.split("\n\n")) // 2
        for concern in result_b.get("concerns", []):
            c_copy = dict(concern)
            c_copy["paragraph_index"] = concern.get("paragraph_index", 0) + offset
            combined_concerns.append(c_copy)

        # Merge strengths (deduplicate)
        strengths_a = result_a.get("strengths", [])
        strengths_b = result_b.get("strengths", [])
        combined_strengths = list(dict.fromkeys(strengths_a + strengths_b))

        # Merge summaries
        by_sev_a  = result_a.get("summary", {}).get("by_severity", {})
        by_sev_b  = result_b.get("summary", {}).get("by_severity", {})
        by_cat_a  = result_a.get("summary", {}).get("by_category", {})
        by_cat_b  = result_b.get("summary", {}).get("by_category", {})

        merged_by_sev: dict[str, int] = {}
        for k, v in {**by_sev_a, **by_sev_b}.items():
            merged_by_sev[k] = merged_by_sev.get(k, 0) + v

        merged_by_cat: dict[str, int] = {}
        for k, v in {**by_cat_a, **by_cat_b}.items():
            merged_by_cat[k] = merged_by_cat.get(k, 0) + v

        total = len(combined_concerns)
        assess_a = result_a.get("summary", {}).get("overall_assessment", "")
        assess_b = result_b.get("summary", {}).get("overall_assessment", "")
        overall  = f"{assess_a} {assess_b}".strip() if assess_b else assess_a

        return {
            "concerns":  combined_concerns,
            "strengths": combined_strengths,
            "summary": {
                "total_concerns":    total,
                "by_severity":       merged_by_sev,
                "by_category":       merged_by_cat,
                "overall_assessment": overall or "Split chapter — merged from two analysis passes.",
            },
        }
    else:
        return _analyze_single_pass(client, model, chapter_text, genre, world_rules_summary)


def _analyze_single_pass(client, model: str, text: str,
                         genre: str, world_rules_summary: str) -> dict:
    """Single Gemini call with retry logic."""
    for attempt in range(MAX_RETRIES):
        temp   = RETRY_TEMPERATURES[min(attempt, len(RETRY_TEMPERATURES) - 1)]
        result = call_gemini(client, model, text, genre, world_rules_summary, temperature=temp)
        if result is not None:
            return result
        print(f"    [WARN] Attempt {attempt + 1}/{MAX_RETRIES} failed. Retrying...")
        time.sleep(1.5)

    # All retries failed — return an error entry
    return {
        "concerns": [{
            "paragraph_index": 0,
            "context":         "(Analysis failed after retries)",
            "category":        "error",
            "severity":        "flag",
            "explanation":     "Gemini returned invalid JSON on all retry attempts.",
            "suggestion":      "Re-run the moral impact editor for this chapter.",
        }],
        "strengths": [],
        "summary": {
            "total_concerns":    1,
            "by_severity":       {"flag": 1},
            "by_category":       {"error": 1},
            "overall_assessment": "Agent could not parse Gemini response.",
        },
    }


# ── Chapter discovery ──────────────────────────────────────────────────────────

def discover_chapters(project_dir: Path) -> list[str]:
    """
    Return sorted list of chapter keys that have at least one source file.
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
            stem             = f.stem
            without_prefix   = stem.replace("chapter_", "", 1)
            if strip_suffix and without_prefix.endswith(strip_suffix.lstrip("_")):
                without_prefix = without_prefix[: -len(strip_suffix.lstrip("_")) - 1]
            keys_found.add(without_prefix)

    def sort_key(k: str):
        try:
            return (0, int(k))
        except ValueError:
            return (1, k)

    return sorted(keys_found, key=sort_key)


# ── Output writers ─────────────────────────────────────────────────────────────

def save_chapter_concerns(project_dir: Path, chapter_key: str, chapter_data: dict,
                          source: str, word_count: int, result: dict) -> None:
    out_dir = project_dir / "output" / "moral_impact"
    out_dir.mkdir(parents=True, exist_ok=True)

    out = {
        "chapter_key":  chapter_key,
        "title":        chapter_data.get("title", f"Chapter {chapter_key}"),
        "source":       source,
        "word_count":   word_count,
        "concerns":     result.get("concerns", []),
        "strengths":    result.get("strengths", []),
        "summary":      result.get("summary", {}),
        "analyzed_at":  datetime.now(timezone.utc).isoformat(),
    }

    path = out_dir / f"chapter_{chapter_key}_concerns.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


def save_summary_report(project_dir: Path, chapter_results: list[dict]) -> dict:
    """
    Build and write moral_impact_report.json and moral_impact_report.md.
    chapter_results: list of per-chapter concern dicts (as saved to disk).
    """
    out_dir = project_dir / "output" / "moral_impact"
    out_dir.mkdir(parents=True, exist_ok=True)

    total_concerns  = 0
    by_severity:  dict[str, int] = {}
    by_category:  dict[str, int] = {}
    chapters_summary = []
    all_assessments  = []

    for cr in chapter_results:
        summary       = cr.get("summary", {})
        count         = summary.get("total_concerns", 0)
        total_concerns += count

        for sev, cnt in summary.get("by_severity", {}).items():
            by_severity[sev] = by_severity.get(sev, 0) + cnt

        for cat, cnt in summary.get("by_category", {}).items():
            by_category[cat] = by_category.get(cat, 0) + cnt

        assessment = summary.get("overall_assessment", "")
        if assessment:
            all_assessments.append(assessment)

        chapters_summary.append({
            "chapter_key":   cr["chapter_key"],
            "title":         cr.get("title", ""),
            "concern_count": count,
            "strengths":     cr.get("strengths", []),
        })

    # Overall assessment: pick the most representative one (first non-empty)
    overall_assessment = all_assessments[0] if all_assessments else "No assessment available."

    report = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "total_chapters":    len(chapter_results),
        "total_concerns":    total_concerns,
        "by_severity":       by_severity,
        "by_category":       by_category,
        "chapters":          chapters_summary,
        "overall_assessment": overall_assessment,
    }

    report_path = out_dir / "moral_impact_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Human-readable markdown
    sev_icons = {"note": "🔵", "consider": "🟡", "flag": "🔴"}
    md_lines = [
        "# Moral & Social Impact Report",
        "",
        f"Generated: {report['generated_at'][:16].replace('T', ' ')} UTC",
        f"Chapters:  {report['total_chapters']}",
        f"Concerns:  {report['total_concerns']}",
        "",
        "## By Severity",
        "",
    ]
    for sev in ["flag", "consider", "note"]:
        count = by_severity.get(sev, 0)
        if count:
            md_lines.append(f"- {sev_icons.get(sev, '●')} **{sev.title()}**: {count}")

    md_lines += ["", "## By Category", ""]
    for cat, cnt in sorted(by_category.items(), key=lambda x: -x[1]):
        if cat != "error":
            md_lines.append(f"- **{cat.replace('_', ' ').title()}**: {cnt}")

    md_lines += ["", "## Chapters", ""]
    for ch in chapters_summary:
        count_str = f"{ch['concern_count']} concern{'s' if ch['concern_count'] != 1 else ''}"
        strengths_str = f", {len(ch['strengths'])} strength{'s' if len(ch['strengths']) != 1 else ''}" if ch['strengths'] else ""
        icon = "✅" if ch['concern_count'] == 0 else "📋"
        md_lines.append(
            f"- {icon} Ch {ch['chapter_key']}: \"{ch['title']}\" — {count_str}{strengths_str}"
        )

    md_path = out_dir / "moral_impact_report.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    return report


# ── Load existing results ───────────────────────────────────────────────────────

def _load_all_chapter_results(project_dir: Path) -> list[dict]:
    """Load all existing chapter_*_concerns.json files for summary generation."""
    div_dir = project_dir / "output" / "moral_impact"
    if not div_dir.exists():
        return []

    results = []

    def sort_key(p: Path):
        stem = p.stem
        k    = stem.replace("chapter_", "").replace("_concerns", "")
        try:
            return (0, int(k))
        except ValueError:
            return (1, k)

    for path in sorted(div_dir.glob("chapter_*_concerns.json"), key=sort_key):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            results.append({
                "chapter_key": data.get("chapter_key", "?"),
                "title":       data.get("title", ""),
                "strengths":   data.get("strengths", []),
                "summary":     data.get("summary", {}),
            })
        except Exception:
            continue

    return results


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cassian Moral Impact Editor")
    parser.add_argument("--chapter", metavar="KEY",
                        help="Analyze a single chapter (e.g. --chapter 01)")
    args = parser.parse_args()

    project_dir = get_project_dir()
    config      = load_config(project_dir)
    api_key     = get_api_key(config)
    model_name  = get_text_model(config)
    genre       = get_genre(project_dir)

    print(f"\n  Cassian Moral Impact Editor")
    print(f"  Project: {project_dir}")
    print(f"  Model:   {model_name}")
    print(f"  Genre:   {genre}")

    # Export world rules context
    print(f"  Loading world rules context…")
    world_rules_summary = export_world_rules_context(project_dir)
    if world_rules_summary:
        print(f"  World rules: found and exported.")
    else:
        print(f"  World rules: none found — proceeding without character context.")

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

    all_chapter_results: list[dict] = []

    for key in keys:
        print(f"  ── Chapter {key} ──")

        path, source = get_chapter_text_path(project_dir, key)
        if path is None:
            print(f"    [SKIP] No source file found for chapter {key}")
            continue

        print(f"    Source: {source}")

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

        print(f"    Analyzing…")
        result = analyze_chapter(client, model_name, chapter_text, word_count,
                                 genre, world_rules_summary)

        concern_count = result.get("summary", {}).get("total_concerns", 0)
        strength_count = len(result.get("strengths", []))
        print(f"    Concerns: {concern_count}   Strengths: {strength_count}")

        save_chapter_concerns(project_dir, key, chapter_data, source, word_count, result)

        all_chapter_results.append({
            "chapter_key": key,
            "title":       chapter_data.get("title", f"Chapter {key}"),
            "strengths":   result.get("strengths", []),
            "summary":     result.get("summary", {}),
        })

        time.sleep(RATE_LIMIT_DELAY)

    # For single-chapter runs, merge with any existing per-chapter files
    if args.chapter:
        all_chapter_results = _load_all_chapter_results(project_dir)

    if all_chapter_results:
        report = save_summary_report(project_dir, all_chapter_results)
        print(f"\n  ✅ Done — {report['total_concerns']} total concerns across {report['total_chapters']} chapters.")
        print(f"     Report saved: output/moral_impact/moral_impact_report.json")
    else:
        print("\n  [WARN] No chapters were processed — no summary report written.")


if __name__ == "__main__":
    main()
