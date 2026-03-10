# One Thousand Perfect Sighs — Pipeline Setup Guide

Welcome! This guide walks you through getting the pipeline running on your computer step by step. You don't need to know how to code — just follow the steps in order.

---

## What you need before starting

- Python installed on your computer (version 3.11 or newer)
  - To check: open a terminal and type `python --version`
  - If you don't have it: download from https://python.org/downloads
- Your Gemini API key (from https://aistudio.google.com)
- Your 31 chapter .docx files

---

## Step 1 — Download this project

If you haven't already, move the `book-pipeline` folder somewhere you can find it easily, like your Desktop or Documents folder.

---

## Step 2 — Open a Terminal in the project folder

**On Mac:**
1. Open Finder and navigate to the `book-pipeline` folder
2. Right-click the folder → "New Terminal at Folder"

**On Windows:**
1. Open the `book-pipeline` folder in File Explorer
2. Click the address bar at the top, type `cmd`, press Enter

You should see a terminal with the path ending in `book-pipeline`.

---

## Step 3 — Install the required tools

In the terminal, type this and press Enter:

```
pip install -r requirements.txt
```

This downloads all the Python tools the pipeline needs. It may take a minute.

---

## Step 4 — Add your Gemini API key

1. Open the file `config.json` in any text editor (Notepad, TextEdit, VS Code)
2. Find this line:
   ```
   "api_key": "YOUR_GEMINI_API_KEY_HERE",
   ```
3. Replace `YOUR_GEMINI_API_KEY_HERE` with your actual key (keep the quotes)
4. Save the file

---

## Step 5 — Add your chapter files

Copy all 31 of your chapter `.docx` files into this folder:

```
book-pipeline/
  input/
    chapters/       ← put all 31 files in here
```

Name them so they sort in the right order, for example:
- `Chapter_01.docx`
- `Chapter_02.docx`
- ... and so on up to `Chapter_31.docx`

---

## Step 6 — Run the Ingestion Agent

In the terminal, type:

```
python agents/01_ingestion/ingest.py
```

You'll see coloured output as it processes each chapter. When it's done, check the folder:

```
book-pipeline/
  output/
    ingested/
      chapter_01.json    ← one JSON file per chapter
      chapter_02.json
      ...
      ingestion_summary.json   ← a summary of the whole book
      images/                  ← any embedded images, extracted
```

---

## What's a JSON file?

Think of it as a structured index card for each chapter. It contains the chapter title, all the text, word count, and image references — in a format that the other agents can instantly read and pass between each other. You can open any `.json` file in a text editor to see what's inside.

---

## Project Structure

```
book-pipeline/
├── config.json              ← all your settings live here
├── requirements.txt         ← list of tools Python needs
├── SETUP.md                 ← this file
│
├── input/
│   └── chapters/            ← YOUR 31 .DOCX FILES GO HERE
│
├── output/
│   ├── ingested/            ← Agent 1 output (JSON files)
│   ├── consistency/         ← Agent 2 output (issues report)
│   ├── editing/             ← Agent 3 output (edited chapters)
│   ├── illustrations/       ← Agent 4 output (prompts + images)
│   ├── formatting/          ← Agent 5 output (formatted book)
│   └── final/               ← Agent 6 output (print-ready PDF)
│
└── agents/
    ├── 01_ingestion/        ← DONE ✓
    │   └── ingest.py
    ├── 02_consistency/      ← coming next
    ├── 03_editing/          ← coming next
    ├── 04_illustration/     ← coming next
    ├── 05_formatting/       ← coming next
    └── 06_qc/               ← coming next
```

---

## Customising the pipeline

Open `config.json` to change:

- **Editing creativity level** (1–5): how aggressively Agent 3 edits your prose
- **Illustration style**: describe the visual style for all 31 illustrations
- **Style reference image**: upload a picture as visual inspiration
- **Print format**: choose hardcover, trade paperback, or mass market paperback
- **Fonts and margins**: all Lulu-compliant settings are pre-filled

---

## Deploying to Hetzner (later)

Once the pipeline is working locally, we'll:
1. Push the code to GitHub
2. Use Coolify on your Hetzner server to deploy it
3. Set it up so it runs on demand or on a schedule

We'll cover this once all 6 agents are built and tested.

---

*Built for: One Thousand Perfect Sighs | Pipeline v1.0*
