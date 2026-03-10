#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# download_fonts.sh — Pre-download all Google Fonts used by Cassian
#
# Run this ONCE during setup on Hetzner (or any deployment machine).
# After running, the app serves all fonts locally — no Google CDN needed.
#
# Usage:
#   cd /path/to/Cassian
#   bash scripts/download_fonts.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CASSIAN_DIR="$(dirname "$SCRIPT_DIR")"
FONTS_DIR="$CASSIAN_DIR/agents/05_layout/fonts"
STATIC_FONTS_DIR="$CASSIAN_DIR/app/static/fonts"

echo "=== Cassian Font Downloader ==="
echo "Fonts dir:  $FONTS_DIR"
echo "Static dir: $STATIC_FONTS_DIR"
echo ""

mkdir -p "$FONTS_DIR" "$STATIC_FONTS_DIR"

# ── Google Fonts API (no key needed for this endpoint) ────────────────────────
# We use the direct download URLs from fonts.google.com
# Format: https://fonts.google.com/download?family=Font+Name

download_google_font() {
    local family_name="$1"
    local dir_name="$2"
    local target="$FONTS_DIR/$dir_name"

    if [ -d "$target" ] && ls "$target"/*.ttf 1>/dev/null 2>&1; then
        echo "  ✓ $family_name — already downloaded"
        return 0
    fi

    echo "  ↓ Downloading $family_name..."
    local tmp_zip="/tmp/font_${dir_name}.zip"
    local url="https://fonts.google.com/download?family=${family_name// /+}"

    if curl -sL -o "$tmp_zip" "$url" && [ -s "$tmp_zip" ]; then
        mkdir -p "$target"
        unzip -qo "$tmp_zip" -d "$target" 2>/dev/null || true
        rm -f "$tmp_zip"
        local count
        count=$(find "$target" -name "*.ttf" -o -name "*.otf" 2>/dev/null | wc -l | tr -d ' ')
        echo "    → $count font files"
    else
        echo "    ✗ Failed to download $family_name"
        rm -f "$tmp_zip"
        return 1
    fi
}

echo "── Book Fonts (for layout/PDF) ──"
download_google_font "EB Garamond"        "EB_Garamond"
download_google_font "Merriweather"       "Merriweather"
download_google_font "Libre Baskerville"  "Libre_Baskerville"
download_google_font "Crimson Text"       "Crimson_Text"
download_google_font "Lora"               "Lora"
download_google_font "Playfair Display"   "Playfair_Display"
download_google_font "Source Serif 4"     "Source_Serif_4"
download_google_font "Cormorant Garamond" "Cormorant_Garamond"
download_google_font "PT Serif"           "PT_Serif"
download_google_font "Spectral"           "Spectral"
download_google_font "Alegreya"           "Alegreya"

echo ""
echo "── UI Fonts (for the web interface) ──"
download_google_font "Inter"              "Inter"

echo ""

# ── Copy UI fonts to app/static/fonts for web serving ─────────────────────────
echo "── Copying UI fonts to static directory ──"

copy_font_to_static() {
    local src_dir="$1"
    local family="$2"

    if [ ! -d "$FONTS_DIR/$src_dir" ]; then
        echo "  ✗ $family not found in $FONTS_DIR/$src_dir"
        return 1
    fi

    # Prefer variable fonts (smaller, more flexible), fall back to static
    local copied=0
    for f in "$FONTS_DIR/$src_dir"/*VariableFont*.ttf "$FONTS_DIR/$src_dir"/*.ttf; do
        [ -f "$f" ] || continue
        local basename
        basename=$(basename "$f")
        cp "$f" "$STATIC_FONTS_DIR/$basename"
        copied=$((copied + 1))
    done

    # Also check /static subfolder (Google's format)
    if [ -d "$FONTS_DIR/$src_dir/static" ]; then
        for f in "$FONTS_DIR/$src_dir/static"/*.ttf; do
            [ -f "$f" ] || continue
            local basename
            basename=$(basename "$f")
            cp "$f" "$STATIC_FONTS_DIR/$basename"
            copied=$((copied + 1))
        done
    fi

    echo "  ✓ $family — $copied files"
}

copy_font_to_static "Inter"       "Inter"
copy_font_to_static "EB_Garamond" "EB Garamond"

echo ""
echo "=== Done! All fonts are now available locally. ==="
echo "The app will serve them from:"
echo "  Book fonts:  $FONTS_DIR/{family}/"
echo "  UI fonts:    $STATIC_FONTS_DIR/"
