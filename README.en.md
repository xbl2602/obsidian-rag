# Obsidian RAG — Search Your Own Notes, Locally

> Search your Obsidian notes by meaning, on your own computer. Notes never leave your machine.
>
> **No dedicated GPU required.** CPU-only works, just slower the first time; page-image search can be turned off with one switch without affecting text search.

<!-- Placeholders: dark theme only, 1600x900 suggested. Drop files at the same paths, no README edits needed. -->
<p align="center">
  <img src="docs/screenshots/00-hero-guiweb-dark.png" width="860" alt="Home (dark) + full-library graph" />
  <br/>
  <em>Fig.0 placeholder: home (dark), replace <code>docs/screenshots/00-hero-guiweb-dark.png</code></em>
</p>

<p align="center">
  <a href="docs/demo/30s-search.mp4">▶ 30s demo video placeholder (hosting TBD)</a>
  <br/>
  <em>Suggested content: type a question → find related notes → expand links → open the source</em>
</p>

- [1. Core features](#1-core-features)
- [2. How it is different](#2-how-it-is-different)
- [3. Gallery](#3-gallery)
- [4. Setup (from zero)](#4-setup-from-zero)
- [5. Daily use](#5-daily-use)
- [6. Common switches](#6-common-switches)
- [7. FAQ](#7-faq)

---

## 1. Core features

### 1.1 Multiple libraries + fine-grained control

Keep tech notes, reading notes, and work docs in separate libraries, managed independently. Each library can include folders, exclude files, down to a single file.

- Choose what new files mean by default (follow / include / exclude).
- A single file can be pulled back in even if its folder is excluded, and vice versa; the more specific rule wins.
- What you see in the UI and what indexing actually uses is one and the same rule set.
- AI changes need two-step confirmation; your own clicks in the UI don't, with a warning on conflicts.
- One implementation: `selection_in` / `selection_out` + `selection_new_files`, decided solely by `library.decide_included`; file enumeration goes through the single `collect_md_files` funnel.

<p align="center">
  <img src="docs/screenshots/01-library-selection.png" width="820" alt="Libraries + folder/file picker (dark)" />
  <br/><em>Fig.1 placeholder: libraries + picker (dark), replace <code>docs/screenshots/01-library-selection.png</code></em>
</p>

### 1.2 Reads your file types

Markdown, plain text, PDF, and Word are indexed by default. Text PDFs are read directly (`pymupdf4llm`); image-based PDFs (scans, mixed slide-plus-scan decks) are handled as a whole book, so image pages are never silently dropped.

- Without image recognition on, image-based files are honestly recorded as "skipped this round" and picked up automatically later; no full rebuild needed.
- Cloud (MinerU cloud) or fully-local (MinerU local) recognition available (see optional setup below).

<p align="center">
  <img src="docs/screenshots/02-extract-scan-status.png" width="820" alt="Files not yet indexed and why (dark)" />
  <br/><em>Fig.2 placeholder, replace <code>docs/screenshots/02-extract-scan-status.png</code></em>
</p>

### 1.3 Finds by meaning

Matches both wording (BM25 + jieba Chinese tokenization) and meaning (BGE-M3 vectors); the two rankings fuse (RRF, equal weights by default) and a reranker keeps the best few passages (candidate pool of 50). A small-chunk hit brings back its full section. Low-confidence hits are labeled, very weak ones are withheld rather than pushed.

> Measured (2026-09-09, real `agents` library, 134 chunks): query `agent skill`, top 3 — top hit `[来源] agents/ui-agent.md (## 工具闸门) [块 2/4] [置信度 0.20·中相关]`, next two at 0.06 / 0.04 labeled weak. First run loads models (~tens of seconds, cuda), then millisecond-scale.

### 1.4 Keeps the knowledge base healthy

- A per-file panel shows what got indexed and why not; click a row to open the source; the same reasons are queryable via `index_failures`.
- Follow links both ways (`note_relations`): from a hit, expand related notes.
- Finds near-duplicate notes (`find_duplicates`, MinHash + LSH) as suggestions only; nothing is auto-deleted.
- Export/import (checksummed) for moving machines without recomputing everything.
- Stale caches and leftovers from deleted libraries are pruned automatically; skipped if the round had errors.

<p align="center">
  <img src="docs/screenshots/03-file-detail-panel.png" width="820" alt="Per-file indexing detail (dark)" />
  <br/><em>Fig.3 placeholder, replace <code>docs/screenshots/03-file-detail-panel.png</code></em>
</p>

### 1.5 Search by page image (optional)

PDFs can be found by page (WEMM page vectors, service on `:9101`): handy when you remember what a page looked like but not the words. The page library follows text indexing automatically.

- The image service starts on demand and quits when idle (VRAM released after 5 min idle, exits after 30 min).
- Only one big model holds the graphics card at a time (VRAM arbitration: WEMM waits for ≥5.5GB free; text search preempts), and probe failures never block; CPU-only machines can simply turn this off.

<p align="center">
  <img src="docs/screenshots/04-wemm-navigate.png" width="820" alt="Find by page (dark)" />
  <br/><em>Fig.4 placeholder: page search + preview (dark), replace <code>docs/screenshots/04-wemm-navigate.png</code></em>
</p>

### 1.6 Humans and AI have separate powers + two desktop apps

AI-triggered work touches plain text plus formats you approved (`agent_formats`); unapproved files stay frozen (kept, not deleted). Approve once, revoke anytime.

Two desktop apps share the same data: the new one (pywebview shell + dark glass + full-library graph, `python guiweb/app.py`, recommended) and the classic one (Flet, `python gui/app.py`). The UI only observes — no model loading, no direct writes; the classic app must quit via `gui/stop.py`.

<p align="center">
  <img src="docs/screenshots/05-guiweb-graph.png" width="820" alt="Full-library graph (dark)" />
  <br/><em>Fig.5 placeholder, replace <code>docs/screenshots/05-guiweb-graph.png</code></em>
</p>

---

## 2. How it is different

| Aspect | This project | Common alternatives |
|---|---|---|
| Privacy | Notes and images stay on your machine | Uploads sources or data to a cloud |
| Honesty | Unreadable image pages say so; no half-baked results | Text pages kept, image pages silently lost |
| Low maintenance | Turn recognition on and old gaps fill in next round | Every setting change needs a full rebuild |
| Control | Down to a single file; file picks beat folder excludes | Global block/allow lists only |
| AI safety | Two-step confirm for scope changes; unapproved formats frozen | AI can reshape everything |
| UI | Observer-only; no leftover processes on quit | Models run inside the UI, leftovers linger |

---

## 3. Gallery

Convention: **dark theme only**, 1600x900 suggested. All placeholders; drop screenshots at the same paths.

| # | Content | Path |
|---|---|---|
| Fig.0 | Home (dark) + graph | `docs/screenshots/00-hero-guiweb-dark.png` |
| Fig.1 | Libraries + picker | `docs/screenshots/01-library-selection.png` |
| Fig.2 | Skipped files and reasons | `docs/screenshots/02-extract-scan-status.png` |
| Fig.3 | Per-file detail | `docs/screenshots/03-file-detail-panel.png` |
| Fig.4 | Page search + preview | `docs/screenshots/04-wemm-navigate.png` |
| Fig.5 | Full-library graph | `docs/screenshots/05-guiweb-graph.png` |
| Fig.6 | Settings | `docs/screenshots/06-settings.png` |
| Fig.7 | Classic app | `docs/screenshots/07-flet-classic.png` |

<p align="center">
  <img src="docs/screenshots/06-settings.png" width="820" alt="Settings (dark)" />
  <br/><em>Fig.6 placeholder: settings (dark), replace <code>docs/screenshots/06-settings.png</code></em>
  <br/><br/>
  <img src="docs/screenshots/07-flet-classic.png" width="820" alt="Classic app (dark)" />
  <br/><em>Fig.7 placeholder, replace <code>docs/screenshots/07-flet-classic.png</code></em>
</p>

Short videos (**hosting TBD**, placeholders for now):

1. `docs/demo/30s-search.mp4` — search → expand links → open source.
2. `docs/demo/60s-library-select.mp4` — new library → include/exclude → index → check detail.

---

## 4. Setup (from zero)

> Main path ends at "text is searchable". Image recognition, page search, and AI wiring are optional below; newcomers can skip them.
> Windows + PowerShell is primary; on Linux replace `.venv\Scripts\` with `.venv/bin/`.

### 4.1 Prepare

- Windows 10/11, Python 3.14, ≥4GB free (models auto-download on first search; allow a few minutes).
- A notes folder. This README uses the bundled `demo-vault/` as the example; replace with your Vault path.

```powershell
$env:PYTHONIOENCODING = "utf-8"
python --version  # expect 3.14
```

### 4.2 Download

```powershell
git clone <repo-url> obsidian-rag
cd obsidian-rag
```

### 4.3 Environment

```powershell
python -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
# With NVIDIA GPU:
.venv\Scripts\pip install torch==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
# CPU only:
# .venv\Scripts\pip install torch==2.11.0
```

No red errors means done.

### 4.4 Register the demo library

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python library.py add ".\demo-vault" --name demo
.venv\Scripts\python library.py list
```

Add your own library by running `add` again with your Vault path.

### 4.5 Index

```powershell
.venv\Scripts\python index.py --library demo
```

It scans → reads files → builds the index. Image-based PDFs without recognition on are recorded as skipped; that is expected and they fill in later.

### 4.6 Verify search

```powershell
.venv\Scripts\python -c "from retriever import hybrid_search; print(hybrid_search('a word from the demo vault', top_k=3))"
```

First run downloads models (seconds to minutes). A `[来源] … [置信度 …]` line plus body text means success; see the measured example in §1.3.

### 4.7 Open an app (either)

New app (recommended):

```powershell
.venv\Scripts\python guiweb/app.py
```

Classic app (use the dedicated start/quit, otherwise processes linger):

```powershell
Start-Process -FilePath "$PWD\.venv\Scripts\pythonw.exe" -ArgumentList "gui\app.py" -WorkingDirectory "$PWD"
# Quit:
.venv\Scripts\python gui/stop.py
```

### 4.8 Optional: image recognition

- Cloud: register, get a key → turn on scanned-file recognition in settings and paste the key. Previously skipped files fill in next round.
- Local: stays offline and free, but needs a separate environment and a multi-GB model download; best with a GPU and many files. Newcomers skip for now; see `AI_GUIDE.md` when needed.

### 4.9 Optional: page-image search switch

On by default. Best with a GPU; without one, or if it feels slow, turn it off in settings. Text search is unaffected.

### 4.10 Optional: let an AI search (generic template)

This project is a standard stdio retrieval service. Three things suffice: **which Python to run (command) + which file (args) + where the repo is (cwd)**. Generic shape for any agent tool:

```json
{
  "type": "stdio",
  "command": "C:\\path\\to\\obsidian-rag\\.venv\\Scripts\\python.exe",
  "args": ["server.py"],
  "cwd": "C:\\path\\to\\obsidian-rag"
}
```

Once connected, the AI gets 9 tools: search (`search_knowledge`), list libraries (`list_libraries`), reindex (`reindex_knowledge`), relations (`note_relations`), find by page (`navigate_knowledge`), read full text (`read_document`), duplicates (`find_duplicates`), failures (`index_failures`), image-service status (`wemm_status`). Reconnect after settings changes.

---

## 5. Daily use

```powershell
# Index all libraries
.venv\Scripts\python index.py
# See what failed
.venv\Scripts\python -c "import server; print(server.index_failures())"
# Move machines without recomputing
.venv\Scripts\python export.py --library demo --output data/export/demo.zip
.venv\Scripts\python import.py --yes data/export/demo.zip
```

In the app: edit picker → start indexing → check per-file detail → search one and expand relations.

---

## 6. Common switches

All in the settings page, same file as editing `data/config.json` by hand; last save wins.

| Switch (settings label) | Default | Notes |
|---|---|---|
| Indexed formats | md,pdf,docx | Which extensions enter the library |
| AI-allowed formats | text-like | What AI-triggered runs may touch; rest frozen |
| Image recognition | off | off / cloud / local |
| Page-image search | on | Turn off without a GPU |
| New files default | follow | New files count as included or excluded |
| Rerank | on | Off = faster, slightly less accurate |

---

## 7. FAQ

1. **No GPU?** Yes. Install the CPU build; first run is slow, then fine; turn page-image search off.
2. **Scanned PDF shows empty?** Recognition is off by default, so it is recorded as skipped. Turn it on and it fills in next round; check the detail panel for the reason.
3. **AI says it can't change scope?** Expected: AI scope changes need two-step confirmation. Click it yourself in the UI.
4. **Rebuild after settings changes?** Text gaps fill in automatically next round; only model or chunk-size changes need a full rebuild, and the UI tells you.
5. **App won't quit cleanly?** Classic app must quit via `gui/stop.py`, never kill the PID; the new app quits by closing the window.

---

*Chinese version in `README.md`. Images and videos are placeholders under `docs/screenshots/` and `docs/demo/`.*
