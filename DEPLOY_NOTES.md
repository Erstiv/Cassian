# Cassian — Hetzner Deployment Notes

## Pre-populated Google Fonts
After deploying to Hetzner, run once to download all book fonts locally:
```bash
bash scripts/download_fonts.sh
```
This downloads all 11 book fonts + Inter from Google Fonts and copies UI fonts to `app/app/static/fonts/`. After running, the font manager and base UI will serve fonts locally without hitting Google CDN.
