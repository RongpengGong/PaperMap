# PaperMap · a local knowledge map of your papers

[中文](README.md)

PaperMap runs on your own computer. Drop paper PDFs into a folder and it will:

1. **Detect new papers** – read the DOI, verify it with Crossref / OpenAlex, and skip duplicates (renamed or moved copies, preprint vs. published version);
2. **Write a paper card** – one-sentence takeaway, study design, methods and software, key findings, limitations;
3. **Classify by research paradigm** – single-cell, spatial transcriptomics, wet-lab mechanism, clinical cohort, prediction model… (multi-label; the taxonomy is editable);
4. **Build a knowledge map** – one page per field with key papers, common tools, method progress and conflicting results between papers;
5. **Spot new directions** – when something does not fit the taxonomy, it collects a few papers, asks whether to create a new field, and writes a **study handbook**.

Everything is plain Markdown that opens in [Obsidian](https://obsidian.md), but you can also just use PaperMap's built-in web pages.

> **Privacy**: the text of each new paper (title, abstract, key sections) is sent to **the AI provider you choose** (e.g. DeepSeek). PDFs in excluded sub-folders (default `_private`, `drafts`) are never read or sent. PaperMap ships with no keys and sends nothing to its author.

## Install

**Download (recommended for non-programmers):** on macOS drag `PaperMap.app` into Applications and open it; on Windows double-click `PaperMap\PaperMap.exe`. On macOS, if you see "cannot verify developer", allow it under System Settings → Privacy & Security, and allow access to Documents/Downloads if asked. Your browser opens the settings page. PaperMap has no window of its own: it lives in the **menu bar (macOS) / notification area (Windows)** – the icon's menu has Open PaperMap, Open knowledge folder, Start at login, and Quit.

**With Python ≥ 3.10:** `pip install .` (or `uv tool install .`) in the source folder, then run `papermap`.

## First use

1. **Connect a model** in Settings → AI services → Models and accounts. For a ChatGPT plan, select **Continue with ChatGPT**, authorize plan usage in the system browser, select an available model and its reasoning effort, then save. For a Claude subscription, install [Claude Code](https://code.claude.com/docs/en/quickstart) and use its own account login from Settings, then select the model and effort. Each subscription remembers its own choice. The default option uses the model’s normal behavior. PaperMap sends the paper text through the selected service. See [ChatGPT plan eligibility](https://developers.openai.com/siwc/quickstart). For API access, choose *DeepSeek official* and paste a key from <https://platform.deepseek.com/api_keys>, or *Custom* with Base URL, key, protocol (OpenAI Chat Completions / OpenAI Responses / Anthropic Messages / Gemini API) and model name. Click **Test connection**, then save.
2. **Inbox folder** – open Settings → Library → Import and folders. The default is `~/PaperMap/inbox`. You can point it at Zotero's storage folder (`~/Zotero/storage`) – PaperMap only reads, never moves or renames files – or your downloads folder. Several folders are allowed.
3. **Add PDFs** – sub-folders are scanned too. Large batches (5+ papers by default) wait for you to confirm a cost estimate on the home page; interrupted runs resume where they stopped.

## Pages

Papers · Knowledge map (blue = method, orange = topic, grey = paper) · Fields (hub pages; "Consolidate now" rewrites overview/disagreements with citations) · Handbooks (part A only cites your library with DOIs; part B is labelled LLM background to verify) · Tools · Review (uncertain labels, unverified DOIs, proposed fields: establish / merge / reject) · Taxonomy editor · Costs · Settings.

Your own notes ("Relevance to me" in cards, "My notes" in field pages and handbooks) are never overwritten.

## Start at login

`papermap autostart install | status | uninstall` (macOS launchd, Windows Task Scheduler, Linux systemd). Then open <http://127.0.0.1:8765>. Quit from the bottom of the Settings page.

## Where data lives

`~/PaperMap/` – `config.yaml`, `papermap.db`, optional `taxonomy.yaml`, `logs/`, and `knowledge/` (papers, fields, handbooks, concepts, _pending) which you can open as an Obsidian vault. Back up the knowledge folder and database. The configuration may contain API keys. When moving to a new device, exclude the ChatGPT credential folder (`chatgpt/`) and sign in again. Optional git versioning of `knowledge/` is in Settings.

## Development

```bash
uv venv && uv pip install -e ".[dev,build]"
pytest
python packaging/build.py   # build on each target OS
```
