"""
╔══════════════════════════════════════════════════════════════════╗
║  AGENT 4 — ILLUSTRATION                                         ║
║  One Thousand Perfect Sighs                                     ║
║                                                                  ║
║  What this does:                                                 ║
║    For each chapter, Agent 4:                                   ║
║      1. Reads the edited chapter text (Agent 3 output)          ║
║      2. Uses Gemini to identify the strongest visual moment     ║
║         and write a detailed image generation prompt            ║
║      3. Calls Imagen 3 to generate the chapter-header image    ║
║      4. Opens the image on your Mac for preview                 ║
║      5. Waits for your approval — y/n/s                         ║
║         y = approved, move on                                   ║
║         n = regenerate with a variation                         ║
║         s = skip this chapter, flag for later                   ║
║      6. Converts approved images to CMYK TIFF at 300 DPI       ║
║                                                                  ║
║  Style reference:                                               ║
║    Drop a reference image into input/ and set                   ║
║    illustration.style_reference_image in config.json            ║
║    The agent will use it to keep all chapters consistent.       ║
║                                                                  ║
║  Input:   output/editing/chapter_XX_edited.json                 ║
║  Output:  output/illustrations/prompts/chapter_XX_prompt.json   ║
║           output/illustrations/images/chapter_XX.tif  (CMYK)   ║
║           output/illustrations/illustration_manifest.json       ║
║                                                                  ║
║  How to run:                                                     ║
║    python agents/04_illustration/illustrate.py                  ║
║    python agents/04_illustration/illustrate.py --chapter 01     ║
║    python agents/04_illustration/illustrate.py --prompts-only   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import sys
import time
import base64
import subprocess
import platform
from pathlib import Path
from datetime import datetime

from google import genai
from google.genai import types
from colorama import init, Fore, Style
init(autoreset=True)

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False


# ── Paths ─────────────────────────────────────────────────────────────────────
import os
BASE_DIR = (
    Path(os.environ['CASSIAN_PROJECT_DIR'])
    if 'CASSIAN_PROJECT_DIR' in os.environ
    else Path(__file__).resolve().parent.parent.parent
)
CONFIG_PATH    = BASE_DIR / "config.json"
EDITING_DIR    = BASE_DIR / "output" / "editing"
INGESTED_DIR   = BASE_DIR / "output" / "ingested"   # fallback if editing not done
OUTPUT_DIR     = BASE_DIR / "output" / "illustrations"
PROMPTS_DIR    = OUTPUT_DIR / "prompts"
IMAGES_DIR     = OUTPUT_DIR / "images"
INPUT_DIR      = BASE_DIR / "input"


# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):    print(f"{Fore.GREEN}  ✓ {msg}{Style.RESET_ALL}")
def info(msg):  print(f"{Fore.CYAN}  → {msg}{Style.RESET_ALL}")
def warn(msg):  print(f"{Fore.YELLOW}  ⚠ {msg}{Style.RESET_ALL}")
def err(msg):   print(f"{Fore.RED}  ✗ {msg}{Style.RESET_ALL}")
def head(msg):  print(f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}")
def flag(msg):  print(f"{Fore.YELLOW}  🚩 {msg}{Style.RESET_ALL}")
def bold(msg):  print(f"{Style.BRIGHT}{msg}{Style.RESET_ALL}")


def load_config() -> dict:
    """Load config.json if it exists. Returns {} if missing/unreadable."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def load_chapters(target_chapter: str = None) -> list[dict]:
    """
    Load chapters from editing output if available, otherwise fall back to ingested.
    """
    source_dir = EDITING_DIR
    suffix     = "_edited"

    # Check if editing output exists
    if not any(EDITING_DIR.glob("chapter_*_edited.json")):
        warn("No edited chapters found — falling back to ingested chapters.")
        warn("Run Agent 3 first for best results.")
        source_dir = INGESTED_DIR
        suffix     = ""

    if target_chapter:
        if target_chapter == "epilogue":
            paths = [source_dir / f"epilogue{suffix}.json"]
            if not paths[0].exists():
                paths = [source_dir / "epilogue.json"]
        else:
            padded = target_chapter.zfill(2)
            paths  = [source_dir / f"chapter_{padded}{suffix}.json"]
            if not paths[0].exists():
                paths = [source_dir / f"chapter_{padded}.json"]
    else:
        paths = sorted(source_dir.glob(f"chapter_*{suffix}.json"))
        epilogue = source_dir / f"epilogue{suffix}.json"
        if not epilogue.exists():
            epilogue = source_dir / "epilogue.json"
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
#  STEP 1 — SCENE ANALYSIS & PROMPT GENERATION (Gemini)
# ══════════════════════════════════════════════════════════════════════════════

def build_scene_prompt(chapter: dict, config: dict, chapter_name: str = '') -> str:
    """Ask Gemini to identify the best visual moment and write an image prompt."""

    ill_config  = config.get("illustration", {})
    ch_id       = chapter.get("chapter_id", "?")
    title       = chapter.get("title", "Untitled")
    text        = chapter.get("full_text", "")

    # Key world facts only — keep prompt lean to avoid token issues
    world_summary = (
        "Setting: Vorkuta, Komi Republic, Russia. Year: 2033. Soviet brutalist architecture, "
        "frozen tundra, underground concrete tunnels, Cold War industrial machinery.\n"
        "Daniil Tsarasov: Russian/Slavic man. Blind since 1979. Appears 70 but is 105. "
        "White hair, weathered Eastern European face, dark heavy coat, plain wooden walking cane. "
        "He is a Soviet engineer and scientist — NOT a warrior, NOT a soldier. "
        "His cane is an ordinary old man's walking cane. NOT a sword, NOT a weapon, NOT a staff.\n"
        "Marcus Wright: American scientist, mid-40s, methodical, practical clothing.\n"
        "The Machine: a vast underground Soviet resonance device, exhaling since 1946. "
        "Brutalist concrete, Soviet-era dials and meters, massive iron components, steam vents.\n"
        "Tartarus: a mythological underworld beneath the facility — darkness and depth, not fire.\n"
        "Tone: lyrical, melancholic, mythological weight meets Cold War history. "
        "Russian literary tradition, not Asian or feudal."
    )

    chapter_name_line = f'Chapter title: "{chapter_name}"' if chapter_name else ''

    if len(text) > 6000:
        text = text[:6000] + "\n\n[... chapter continues ...]"

    # Mandatory style suffix — appended to EVERY image prompt after the scene description.
    # The scene contents come first so Imagen locks in the subject before reading style.
    style_prefix = (
        "Chaotic highly textured digital impasto painting, violent fractured brushstrokes, "
        "thick paint applied with a palette knife, raw gestural energy, high contrast. "
        "No text or letters."
    )

    # Palette: each chapter gets its own bold expressive palette.
    # Red/orange/white/black are the book's signature — always present as an accent/ember/glow.
    palette_guidance = (
        "COLOUR PALETTE — make it bold and specific:\n"
        "Choose a dominant palette that fits this chapter's emotional register. "
        "Do NOT default to all-red/orange — that is the accent, not the base.\n\n"
        "Examples of what bold chapter palettes look like:\n"
        "- A winter isolation chapter: dominant deep indigo and steel blue, "
        "icy white highlights, one searing orange ember glowing in the dark\n"
        "- A grief chapter: dominant ash-white and cold charcoal, "
        "bone pale and grey-green, with a crack of deep red bleeding through\n"
        "- An underground/myth chapter: dominant violet-black and obsidian, "
        "tarnished silver catching a molten orange glow from below\n"
        "- A memory/longing chapter: dominant warm ochre and faded sepia, "
        "dusty amber light, with a piercing white highlight cutting through\n"
        "- A dread/surveillance chapter: dominant institutional grey and olive, "
        "harsh cold white, with a deep crimson warning light somewhere\n\n"
        "The red/orange/white/black signature MUST appear somewhere — "
        "as an ember, a glow on metal, a reflected fire, a crack of light. "
        "It threads through all 31 images. But it is an accent, not the whole painting."
    )

    return f"""You are a visual development artist for the sci-fi novel "One Thousand Perfect Sighs."
{chapter_name_line}

WORLD CONTEXT:
{world_summary}

YOUR TASK: Read the chapter text below. Then respond in the exact format shown.

RULES:
- First write a one-sentence factual summary of what happens in this chapter (name the character and action)
- Choose ONE visual moment from the chapter to illustrate — it must come from the actual text
- Write the IMAGE_PROMPT with scene contents first, style last
- NO split panels, diptychs, or side-by-side images
- NOT a lone figure before a beam of light, NOT a figure arms-wide silhouetted
- If Daniil appears: describe him as "an elderly blind Russian man, white hair, dark heavy coat, plain wooden walking cane" — never vague

{palette_guidance}

IMAGE_PROMPT STRUCTURE — use this exact sandwich format, one paragraph:

PART A — Style anchor (start with these exact words):
"Chaotic impasto painting of "

PART B — Scene (immediately after, no line break, ~50 words):
Describe exactly what is in the scene: the specific subject, setting, characters with FULL
descriptions (age, nationality, clothing — never just "a figure" or "an old man"),
the composition, the dominant colours, the lighting quality.
If Daniil appears: "an elderly blind Russian man with white hair and dark heavy coat
holding a plain wooden walking cane" — always this specific.

PART C — Style reinforcement (end with these exact words):
"Violent fractured brushstrokes, thick impasto palette knife texture, raw gestural energy,
high contrast, searing red and orange embers visible somewhere in the scene. No text."

Total: 80-120 words. One paragraph. No line breaks.

RESPOND IN THIS FORMAT:

CHAPTER_SUMMARY:
[one factual sentence: who does what, where]

SCENE:
[2-3 sentences: the moment chosen and why it works visually]

IMAGE_PROMPT:
["Chaotic impasto painting of " + scene description (~50 words) + "Violent fractured brushstrokes, thick impasto palette knife texture, raw gestural energy, high contrast, searing red and orange embers visible somewhere in the scene. No text."]

NEGATIVE_PROMPT:
samurai, katana, sword, weapon, warrior, armour, feudal, ninja, split panel, diptych, multiple panels, [add any chapter-specific terms]

MOOD:
[3-5 tags]

CHARACTERS:
[names or NONE]

CHAPTER TEXT:
{text}
"""


def parse_scene_response(raw: str, ch_id: str) -> dict:
    """
    Parse the structured text response from Gemini into a dict.
    Uses section headers instead of JSON — immune to JSON parse errors.
    """
    sections = {
        "CHAPTER_SUMMARY": "",
        "SCENE":           "",
        "IMAGE_PROMPT":    "",
        "NEGATIVE_PROMPT": "",
        "MOOD":            "",
        "CHARACTERS":      "",
    }

    current = None
    lines   = raw.splitlines()
    buffer  = []

    for line in lines:
        stripped = line.strip()
        matched  = False
        for key in sections:
            if stripped.startswith(f"{key}:"):
                if current and buffer:
                    sections[current] = " ".join(
                        l.strip() for l in buffer if l.strip()
                    ).strip()
                current = key
                buffer  = [stripped[len(key)+1:].strip()]
                matched = True
                break
        if not matched and current:
            buffer.append(line)

    if current and buffer:
        sections[current] = " ".join(
            l.strip() for l in buffer if l.strip()
        ).strip()

    mood_tags  = [t.strip() for t in sections["MOOD"].split(",") if t.strip()]
    characters = [c.strip() for c in sections["CHARACTERS"].split(",")
                  if c.strip() and c.strip().upper() != "NONE"]

    return {
        "chapter_id":         ch_id,
        "chapter_summary":    sections["CHAPTER_SUMMARY"],
        "selected_scene":     sections["SCENE"],
        "image_prompt":       sections["IMAGE_PROMPT"],
        "negative_prompt":    sections["NEGATIVE_PROMPT"],
        "mood_tags":          mood_tags,
        "characters_present": characters,
    }


def get_scene_analysis(chapter: dict, config: dict, client, model_name: str,
                       chapter_name: str = '') -> dict:
    """Call Gemini to analyse the chapter and get a scene + prompt. Retries up to 3 times."""
    ch_id  = chapter.get("chapter_id", "?")
    prompt = build_scene_prompt(chapter, config, chapter_name=chapter_name)

    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4 + (attempt * 0.1),  # vary temp on retries
                    max_output_tokens=4096
                )
            )
            text = getattr(response, "text", None)
            if text:
                return parse_scene_response(text.strip(), str(ch_id))
            # Diagnose why response is empty
            try:
                candidates = getattr(response, 'candidates', [])
                if candidates:
                    finish_reason = getattr(candidates[0], 'finish_reason', 'unknown')
                    safety = getattr(candidates[0], 'safety_ratings', [])
                    warn(f"Empty response (attempt {attempt}/3) — finish_reason: {finish_reason}")
                    if safety:
                        warn(f"  Safety ratings: {safety}")
                else:
                    warn(f"Empty response (attempt {attempt}/3) — no candidates returned")
            except Exception as diag_e:
                warn(f"Empty response (attempt {attempt}/3) — could not diagnose: {diag_e}")
            time.sleep(3)
        except Exception as e:
            warn(f"Scene analysis error (attempt {attempt}/3): {e}")
            time.sleep(3)

    # Fallback: generate a safe atmospheric prompt — no chapter text, no characters
    warn(f"Using fallback prompt for Chapter {ch_id} (Gemini did not respond)")
    return {
        "chapter_id":         str(ch_id),
        "chapter_summary":    "",
        "selected_scene":     f"Abstract environment for Chapter {ch_id}",
        "image_prompt":       (
            f"Extreme close-up of corroded Soviet industrial machinery: rusted iron gears, "
            f"frost-covered bolts, cracked enamel dials, peeling paint on steel plate. "
            f"No people, no figures, no living beings anywhere. Pure object and texture. "
            f"Slate grey and deep rust with cold blue-white frost. "
            f"Highly textured digital impasto painting, expressive fractured brushstrokes, "
            f"thick paint applied with a palette knife, intense gestural energy, high contrast. "
            f"No text or letters."
        ),
        "negative_prompt":    (
            "person, people, human, figure, silhouette, warrior, samurai, soldier, "
            "katana, sword, weapon, armour, feudal, ninja, face, body, "
            "corridor, hallway, tunnel with light at end, "
            "text, letters, photorealistic, cartoon"
        ),
        "mood_tags":          ["atmospheric", "melancholic", "industrial"],
        "characters_present": [],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — IMAGE GENERATION (Imagen 3)
# ══════════════════════════════════════════════════════════════════════════════

def load_style_reference(config: dict) -> bytes | None:
    """Load the style reference image if one is configured."""
    ref_path = config.get("illustration", {}).get("style_reference_image")
    if not ref_path:
        return None

    full_path = BASE_DIR / ref_path
    if full_path.exists():
        with open(full_path, 'rb') as f:
            return f.read()
    else:
        warn(f"Style reference image not found: {ref_path}")
        return None


def get_imagen_client(config: dict):
    """
    Create a Vertex AI client for Imagen 3 image generation.
    Uses Application Default Credentials (set up via gcloud auth application-default login).
    """
    va = config.get("vertex_ai", {})
    project  = va.get("project_id", va.get("project_number", ""))
    location = va.get("location", "us-central1")
    return genai.Client(vertexai=True, project=project, location=location)


def generate_image(
    image_prompt: str,
    negative_prompt: str,
    config: dict,
    client,          # text client (unused here — imagen uses its own)
    attempt: int = 1
) -> bytes | None:
    """
    Generate the chapter-header image using Imagen 3 via Vertex AI.
    Returns raw PNG bytes, or None if generation fails.
    """
    ill_config   = config.get("illustration", {})
    model_name   = ill_config.get("model", "imagen-3.0-generate-001")
    aspect_ratio = ill_config.get("aspect_ratio", "1:1")

    # Add variation hint on retries
    if attempt > 1:
        image_prompt = f"{image_prompt} [Variation {attempt}: use a different composition or lighting angle]"

    # Hard-coded negative terms that must always be excluded regardless of what Gemini wrote.
    # These prevent Imagen from defaulting to common visual clichés unrelated to the novel.
    hard_negatives = (
        "split panel, diptych, triptych, multiple panels, comic strip, "
        "vertical dividing lines, panel borders, panel separators, "
        "bordered sections, framed panels, composite image, "
        "samurai, katana, sword, warrior, armour, feudal, Asian warrior, ninja, "
        "Japanese aesthetic, medieval weapon, fantasy weapon, "
        "photorealistic, cartoon, anime, text, letters, words, watermark"
    )

    # Merge hard negatives with any chapter-specific negatives from Gemini
    combined_negatives = hard_negatives
    if negative_prompt:
        combined_negatives = f"{hard_negatives}, {negative_prompt}"

    full_prompt = f"{image_prompt} Avoid: {combined_negatives}"

    imagen_client = get_imagen_client(config)

    response = imagen_client.models.generate_images(
        model=model_name,
        prompt=full_prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio=aspect_ratio,
            safety_filter_level="block_only_high",
            person_generation="allow_adult",
        )
    )

    if response.generated_images:
        return response.generated_images[0].image.image_bytes
    return None


def save_png(image_bytes: bytes, path: Path):
    """Save raw PNG bytes to disk."""
    with open(path, 'wb') as f:
        f.write(image_bytes)


def convert_to_cmyk_tiff(png_path: Path, tiff_path: Path, dpi: int = 300):
    """
    Convert a PNG (sRGB) to CMYK TIFF at the specified DPI.
    Requires Pillow. If Pillow not available, saves PNG instead.
    """
    if not PILLOW_AVAILABLE:
        warn("Pillow not installed — saving as PNG (not CMYK TIFF). Run: pip3 install Pillow --only-binary :all:")
        png_path.rename(tiff_path.with_suffix('.png'))
        return

    img       = Image.open(png_path)
    cmyk_img  = img.convert('CMYK')
    cmyk_img.save(str(tiff_path), dpi=(dpi, dpi), compression='tiff_lzw')
    ok(f"Converted to CMYK TIFF @ {dpi} DPI")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 3 — APPROVAL LOOP
# ══════════════════════════════════════════════════════════════════════════════

def open_image_for_preview(image_path: Path):
    """Open the image in the system default viewer."""
    try:
        if platform.system() == "Darwin":        # macOS
            subprocess.run(["open", str(image_path)])
        elif platform.system() == "Linux":
            subprocess.run(["xdg-open", str(image_path)])
        elif platform.system() == "Windows":
            subprocess.run(["start", str(image_path)], shell=True)
    except Exception as e:
        warn(f"Could not auto-open image: {e}")
        info(f"Open manually: {image_path}")


def approval_loop(
    chapter: dict,
    scene_analysis: dict,
    config: dict,
    client,
    prompts_only: bool = False
) -> dict:
    """
    Full generation + approval cycle for one chapter.

    If an approved image already exists for this chapter, shows it first so the
    user can compare old vs new before deciding.

    Returns a result dict describing what happened.
    """
    ch_id    = chapter.get("chapter_id", "?")
    title    = chapter.get("title", "Untitled")
    ill_cfg  = config.get("illustration", {})
    max_tries = ill_cfg.get("max_regeneration_attempts", 3)
    dpi       = ill_cfg.get("image_format", {}).get("dpi", 300)

    image_prompt    = scene_analysis.get("image_prompt", "").strip()
    negative_prompt = scene_analysis.get("negative_prompt", "").strip()

    # Guard: if image_prompt is empty, use a fallback so Imagen doesn't reject it
    if not image_prompt:
        warn("Image prompt was empty — using atmospheric fallback")
        image_prompt = (
            f"Epic painterly illustration, square format. Atmospheric mythological scene "
            f"evoking Chapter {ch_id} of 'One Thousand Perfect Sighs'. "
            f"Soviet underground industrial environment, deep blacks, amber and crimson light, "
            f"painterly gestural brushwork, high contrast, cinematic. No text."
        )

    if prompts_only:
        ok(f"Prompt saved (prompts-only mode)")
        return {
            "chapter_id": ch_id,
            "status":     "prompt_only",
            "scene":      scene_analysis.get("selected_scene", ""),
            "prompt":     image_prompt,
        }

    # ── Paths ───────────────────────────────────────────────────────────────
    preview_new = IMAGES_DIR / f"chapter_{str(ch_id).zfill(2)}_preview_NEW.png"
    preview_old = IMAGES_DIR / f"chapter_{str(ch_id).zfill(2)}_preview_OLD.png"

    if ch_id == "epilogue":
        final_tiff = IMAGES_DIR / "epilogue.tif"
    else:
        final_tiff = IMAGES_DIR / f"chapter_{str(ch_id).zfill(2)}.tif"

    # ── Check for existing approved image ───────────────────────────────────
    has_existing = final_tiff.exists()
    if has_existing:
        # Convert existing TIF → PNG preview so user can see the old image
        try:
            if PILLOW_AVAILABLE:
                old_img = Image.open(final_tiff)   # Image is imported at top via: from PIL import Image
                if old_img.mode == 'CMYK':
                    old_img = old_img.convert('RGB')
                old_img.save(str(preview_old), 'PNG')
                info(f"Existing image found — showing OLD version for Chapter {ch_id} first...")
                open_image_for_preview(preview_old)
                time.sleep(1.5)
                print()
                print(f"  {Fore.CYAN}  ↑ That is the CURRENT saved image for Chapter {ch_id}.{Style.RESET_ALL}")
                print(f"  {Fore.CYAN}    Generating a NEW candidate now — compare them before deciding.{Style.RESET_ALL}")
                print()
        except Exception as e:
            warn(f"Could not preview existing image: {e}")
            warn(f"  (old image is at: {final_tiff})")

    approved = False
    kept_old = False
    attempt  = 0
    answer   = ""

    while not approved and not kept_old and attempt < max_tries:
        attempt += 1
        info(f"Generating new image (attempt {attempt}/{max_tries})...")

        try:
            image_bytes = generate_image(image_prompt, negative_prompt, config, client, attempt)
        except Exception as e:
            err(f"Image generation failed: {e}")
            break

        if not image_bytes:
            warn("No image returned by Imagen — skipping.")
            break

        # Save new preview PNG
        save_png(image_bytes, preview_new)
        ok(f"New image generated → opening for comparison...")
        open_image_for_preview(preview_new)
        time.sleep(1.5)

        # Scene info
        print()
        print(f"  {'─' * 58}")
        bold(f"  Chapter {ch_id}: \"{title}\"")
        summary = scene_analysis.get('chapter_summary', '')
        if summary:
            print(f"  Story: {summary}")
        print(f"  Scene: {scene_analysis.get('selected_scene', '')}")
        print(f"  Mood:  {', '.join(scene_analysis.get('mood_tags', []))}")
        print(f"  {'─' * 58}")
        print()

        if has_existing:
            # Compare mode — user has seen both old and new
            print(f"  {Fore.YELLOW}  y{Style.RESET_ALL} = use NEW image (replace the old one)")
            print(f"  {Fore.YELLOW}  k{Style.RESET_ALL} = keep OLD image (discard new, move on)")
            print(f"  {Fore.YELLOW}  n{Style.RESET_ALL} = generate another new variation")
            print(f"  {Fore.YELLOW}  s{Style.RESET_ALL} = skip (keep old, flag for later)")
            print()
            try:
                answer = input(f"  Your choice [y/k/n/s]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                warn("Input interrupted — keeping old image.")
                answer = "k"

            if answer == "y":
                approved = True
                ok(f"Using NEW image! Converting to CMYK TIFF...")
                convert_to_cmyk_tiff(preview_new, final_tiff, dpi=dpi)
                ok(f"Replaced → {final_tiff.name}")
            elif answer == "k":
                kept_old = True
                ok(f"Keeping existing image for Chapter {ch_id}.")
            elif answer == "s":
                kept_old = True   # treat skip as "keep old" for status purposes
                answer = "s"
                warn(f"Skipped — keeping old image, flagged for later.")
            else:
                warn(f"Generating another variation...")
        else:
            # No existing image — standard approval
            print(f"  {Fore.YELLOW}  y{Style.RESET_ALL} = approve and continue")
            print(f"  {Fore.YELLOW}  n{Style.RESET_ALL} = generate a new variation")
            print(f"  {Fore.YELLOW}  s{Style.RESET_ALL} = skip this chapter (flag for later)")
            print()
            try:
                answer = input(f"  Your choice [y/n/s]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                warn("Input interrupted — skipping chapter.")
                answer = "s"

            if answer == "y":
                approved = True
                ok(f"Approved! Converting to CMYK TIFF...")
                convert_to_cmyk_tiff(preview_new, final_tiff, dpi=dpi)
                ok(f"Saved → {final_tiff.name}")
            elif answer == "s":
                warn(f"Skipped — flagged for manual review.")
                break
            else:
                warn(f"Regenerating with a new variation...")

    # ── Clean up preview PNGs ────────────────────────────────────────────────
    for p in (preview_new, preview_old):
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    # ── Determine status ────────────────────────────────────────────────────
    if approved:
        status = "approved"
    elif kept_old and answer != "s":
        status = "kept_existing"
    elif answer == "s":
        status = "skipped"
    else:
        status = "failed"

    return {
        "chapter_id": ch_id,
        "status":     status,
        "scene":      scene_analysis.get("selected_scene", ""),
        "prompt":     image_prompt,
        "output":     str(final_tiff) if (approved or kept_old) else None,
        "attempts":   attempt,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MANIFEST WRITER
# ══════════════════════════════════════════════════════════════════════════════

def write_manifest(results: list[dict], path: Path):
    """Write a summary of all illustration results."""
    approved     = [r for r in results if r.get("status") == "approved"]
    kept         = [r for r in results if r.get("status") == "kept_existing"]
    skipped      = [r for r in results if r.get("status") == "skipped"]
    failed       = [r for r in results if r.get("status") == "failed"]

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total":          len(results),
            "approved_new":   len(approved),
            "kept_existing":  len(kept),
            "skipped":        len(skipped),
            "failed":         len(failed),
        },
        "illustrations": results
    }

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def write_manifest_md(results: list[dict], path: Path):
    """Human-readable version of the manifest."""
    lines = [
        "# Illustration Manifest",
        "## One Thousand Perfect Sighs",
        f"*Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}*\n",
        "---\n",
    ]

    for r in results:
        ch_id  = r.get("chapter_id", "?")
        status = r.get("status", "?")
        scene  = r.get("scene", "")
        output = r.get("output", "")

        icon = ("✅" if status == "approved" else
                "🔒" if status == "kept_existing" else
                "🚩" if status == "skipped" else "❌")
        lines.append(f"## {icon} Chapter {ch_id} — {status.upper()}")
        if scene:
            lines.append(f"**Scene:** {scene}\n")
        if output:
            lines.append(f"**File:** `{Path(output).name}`\n")
        lines.append("")

    path.write_text("\n".join(lines), encoding='utf-8')


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(target_chapter: str = None, prompts_only: bool = False):
    print()
    print("═" * 62)
    head("  AGENT 4 — ILLUSTRATION")
    head("  One Thousand Perfect Sighs")
    print("═" * 62)
    print()

    if prompts_only:
        info("Mode: PROMPTS ONLY — images will not be generated")
    else:
        info("Mode: FULL — prompts + image generation + approval")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    config     = load_config()
    ill_cfg    = config.get("illustration", {})

    # ── Load evocative chapter names (from Agent 5's generate_chapter_names.py) ─
    chapter_names_path = BASE_DIR / "output" / "formatting" / "chapter_names.json"
    chapter_names = {}
    if chapter_names_path.exists():
        try:
            chapter_names = json.loads(chapter_names_path.read_text(encoding='utf-8'))
            ok(f"Chapter names loaded ({len(chapter_names)} entries) — using in prompts")
        except Exception as e:
            warn(f"Could not load chapter names: {e}")
    else:
        warn("chapter_names.json not found — run generate_chapter_names.py first for richer prompts")
    # API key: try config.json first, fall back to env var
    api_key = (
        config.get("gemini", {}).get("api_key", "")
        or os.environ.get("GEMINI_API_KEY", "")
    )
    text_model = (
        config.get("gemini", {}).get("models", {}).get("text", "")
        or "gemini-2.5-flash"
    )

    if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
        err("No Gemini API key found!")
        err("Set GEMINI_API_KEY env var or add it to config.json.")
        return

    # Style reference notice
    style_ref = ill_cfg.get("style_reference_image")
    if style_ref:
        ok(f"Style reference: {style_ref}")
    else:
        warn("No style reference image set. Using text style description only.")
        warn("To add one: generate a style image, save it to input/, then set")
        warn("  illustration.style_reference_image in config.json")
    print()

    info("Loading chapters...")
    chapters = load_chapters(target_chapter)
    ok(f"Loaded {len(chapters)} chapter(s)\n")

    info("Connecting to Gemini...")
    client = genai.Client(api_key=api_key)
    ok(f"Connected ({text_model})\n")

    if not PILLOW_AVAILABLE and not prompts_only:
        warn("Pillow not installed — images will be saved as PNG, not CMYK TIFF.")
        warn("Install with: pip3 install Pillow --only-binary :all:\n")

    results = []

    for chapter in chapters:
        ch_id = chapter.get("chapter_id", "?")
        title = chapter.get("title", "Untitled")

        print(f"  {'─' * 58}")
        info(f"Chapter {ch_id}: \"{title}\"")
        print()

        # Look up chapter name (key = int string or "epilogue")
        key_str    = 'epilogue' if ch_id == 'epilogue' else str(int(ch_id)) if str(ch_id).isdigit() else str(ch_id)
        chap_name  = chapter_names.get(key_str, '')
        if chap_name:
            info(f'Chapter name: "{chap_name}"')

        # Step 1: Scene analysis
        info("Analysing chapter for visual moment...")
        try:
            scene_analysis = get_scene_analysis(chapter, config, client, text_model,
                                                chapter_name=chap_name)
            summary = scene_analysis.get('chapter_summary', '')
            if summary:
                ok(f"Chapter summary: {summary}")
            ok(f"Scene identified: {scene_analysis.get('selected_scene', '')[:80]}...")
        except Exception as e:
            err(f"Scene analysis failed: {e}")
            results.append({"chapter_id": ch_id, "status": "failed", "error": str(e)})
            continue

        # Save the prompt to file regardless of mode
        prompt_path = PROMPTS_DIR / (
            "epilogue_prompt.json" if ch_id == "epilogue"
            else f"chapter_{str(ch_id).zfill(2)}_prompt.json"
        )
        with open(prompt_path, 'w', encoding='utf-8') as f:
            json.dump(scene_analysis, f, indent=2, ensure_ascii=False)
        ok(f"Prompt saved → {prompt_path.name}")

        # Step 2: Image generation + approval
        result = approval_loop(chapter, scene_analysis, config, client, prompts_only)
        results.append(result)

        print()
        time.sleep(1)

    # Write manifests
    manifest_path    = OUTPUT_DIR / "illustration_manifest.json"
    manifest_md_path = OUTPUT_DIR / "illustration_manifest.md"
    write_manifest(results, manifest_path)
    write_manifest_md(results, manifest_md_path)

    # Summary
    approved     = sum(1 for r in results if r.get("status") == "approved")
    kept         = sum(1 for r in results if r.get("status") == "kept_existing")
    skipped      = sum(1 for r in results if r.get("status") == "skipped")
    failed       = sum(1 for r in results if r.get("status") == "failed")
    prompts      = sum(1 for r in results if r.get("status") == "prompt_only")

    print()
    print("═" * 62)
    ok("ILLUSTRATION COMPLETE")
    print(f"     Chapters processed  : {len(results)}")
    if prompts_only:
        print(f"     Prompts saved       : {prompts}")
    else:
        print(f"     New images approved : {approved}")
        if kept:
            print(f"     Kept existing       : {kept}")
        print(f"     Chapters skipped    : {skipped}")
        print(f"     Failed              : {failed}")
    print()
    print("  Output files:")
    print("  output/illustrations/prompts/   ← scene prompts (one per chapter)")
    if not prompts_only:
        print("  output/illustrations/images/    ← CMYK TIFFs (approved images)")
    print("  output/illustrations/illustration_manifest.md")
    print()
    if skipped or failed:
        flag(f"{skipped + failed} chapter(s) need attention — see illustration_manifest.md")
    print("  Next step: run Agent 5 (Layout / Formatting)")
    print("═" * 62)
    print()


if __name__ == "__main__":
    target       = None
    prompts_only = "--prompts-only" in sys.argv

    # Support both: --chapter epilogue  AND  --epilogue
    if "--epilogue" in sys.argv:
        target = "epilogue"
    elif "--chapter" in sys.argv:
        idx = sys.argv.index("--chapter")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]

    run(target_chapter=target, prompts_only=prompts_only)
