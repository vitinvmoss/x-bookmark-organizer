# x-bookmark-organizer - FREE local X/Twitter bookmark organizer (Windows)

Local-first pipeline: **COLLECT -> DATABASE/JSON -> ANALYZE -> AI CATEGORIES -> REVIEW -> CLASSIFY -> APPROVAL -> CREATE FOLDERS -> ADD -> VERIFY -> LOG**.

- FREE: stdlib Python only, no paid API. AI via your existing OpenCode free models (OpenAI-compatible) or local Ollama, with offline heuristic fallback.
- No password, no manual cookie copy. Primary import = passive userscript export (same shape as SaveBox `collector.user.js`), re-imported locally. Live GraphQL replay supported via discovered query IDs (never hard-coded).
- Never deletes bookmarks. Default ADD-ONLY. Resume-safe + local change log.
- Windows supported.

## How X bookmark folders work (the important part)

X's web client drives bookmark folders through internal GraphQL operations:

- `Bookmarks` - paginated bookmark timeline
- `BookmarkFoldersSlice` - list your folders
- `BookmarkFolderTimeline` - bookmarks inside one folder
- `createBookmarkFolder` - create a folder
- `bookmarkTweetToFolder` - add a bookmark to a folder
- `RemoveTweetFromBookmarkFolder` - remove a bookmark from a folder (never the bookmark itself)
- `EditBookmarkFolder` - rename a folder

The query IDs for these change (rotate) when X deploys, so **nothing here hard-codes them**. They are read from `data/session.json` (from your live session) or discovered by scanning X's JS bundles. `DeleteBookmark` is **blocked in code** (`requests.assert_no_delete` raises if it ever sees it).

Safety semantics implemented:
- `bookmarkTweetToFolder` ADDS; it does not remove from another folder. Default is ADD-ONLY.
- A true MOVE (only with `--reorganize`) = add to new -> verify -> then remove from old.
- Never deletes the original bookmark.
## Reuse map (what we took from each project)

| Project | Reused idea / code pattern | Not reused |
|---|---|---|
| **SaveBox** | In-page fetch so X attaches its own anti-bot headers; dynamic queryId+features+bearer discovery (fallback only); passive `collector.user.js` interceptor (matches operation name, not hardcoded ID); pacing ~2.5-3s + 429 backoff; atomic writes; `status_id + content_hash` idempotency | Linux-only profile paths + POSIX CDP pipe client (does not work on Windows). Replaced with userscript-import + stdlib bundle-scrape discovery |
| **xarchive** | `background.js` passive header/queryId capture; strict bundle regex (both key orders, no guessing); header builder (fresh `ct0`, replay transaction-id unchanged) + features replay + rate-limit handling; jittered delay + exponential backoff + cooldown + pagination guards; tombstone/visibility-wrapper parsing; folder phases (Slice -> Timeline) | Chrome-extension APIs. Reimplemented as file-based `data/session.json` + stdlib `urllib` |
| **xmarks** | Bookmarks variables/features shape; cursor pagination + dedup; bearer constant (public, not secret) | Hard-coded query ID (we discover), manual cookie paste, macOS-only paths |
| **Siftly** | Zero-cost entity extraction (hashtags/urls/mentions/tools/tweetType/media); multi-format parser normalization; batched categorization (20/batch) | Next.js/Prisma/Anthropic-SDK stack, hard-coded default categories (you forbade this - we discover categories from your data) |

## Quick start (copy-paste, Windows PowerShell)

```powershell
cd C:\Users\User\Documents\x-bookmark-organizer
python --version   # 3.10+ ok

# 0) Try with synthetic data first (no X needed):
python xb.py --kb data demo --n 200
python xb.py --kb data dry-run --engine heuristic
# -> prints proposed folders + counts. Nothing touches X.

# 1) Real import via one-click exporter (recommended on Windows):
#  a) Install Tampermonkey/Violentmonkey, add assets\collector.user.js
#  b) Open https://x.com/i/bookmarks, scroll to bottom, click Download
#  c) Import it:
python xb.py --kb data import --fixture C:\path\to\bookmarks_raw.jsonl
#  (also accepts xarchive combined JSON: --fixture archive.json --format xarchive)

# 2) Build + preview (DRY RUN - nothing written to X):
python xb.py --kb data normalize
python xb.py --kb data analyze
python xb.py --kb data categories --max-categories 20
python xb.py --kb data classify
python xb.py --kb data propose        # review data\proposal.json
python xb.py --kb data plan-writes    # how many folders/bookmarks to write
python xb.py --kb data apply          # DRY: refuses without --approve
```
## AI setup (free, via your existing OpenCode config)

No new keys needed. `xbookmark/ai.py` auto-reads your OpenCode providers
(`%USERPROFILE%\.config\opencode\config.json`) and picks a free endpoint:

```powershell
python xb.py --kb data ai-status     # confirms what was detected
# override if needed:
$env:XBO_AI_BASE="https://api.apinex.bond/v1"
$env:XBO_AI_KEY="sk-apx-..."
$env:XBO_AI_MODEL="apinex/free/gemini-3.8-flash"
# fully local alternative (free):
#   ollama pull llama3.1 && ollama serve
#   $env:XBO_AI_BACKEND="ollama"
# offline fallback (no AI at all):
python xb.py --kb data categories --engine heuristic
python xb.py --kb data classify --engine heuristic
```

Only bookmark *text snippets* (batched, truncated) go to the configured
inference endpoint. The raw database stays local.

## Finding current GraphQL operation IDs (do not hard-code)

```powershell
python xb.py --kb data query-ids --discover   # scans x.com bundles (no code run)
python xb.py --kb data query-ids --show       # prints stored IDs
# paste a DevTools Network "graphql/<id>/<Operation>" URL (takes ownership):
python xb.py --kb data query-ids --add "https://x.com/i/api/graphql/XXXX/Bookmarks"
```

To capture query IDs AND auth passively: open DevTools -> Network ->
filter `graphql` while scrolling bookmarks/folders, then populate
`data/session.json` with the `queryId_<Operation>` values plus
`cookie_header` / `ct0` (copy from the request headers). The mutation
operations are discovered the same way. Proposal/dry-run works without any
## Files layout

```
xb.py                        CLI (all commands)
xbookmark/
  util.py                    constants + Windows-safe helpers
  store.py                   local JSON/JSONL store + change log
  parser.py, tweet_extract.py, pages.py   GraphQL tweet/folder parsing
  collect.py                 import envelopes (userscript / xarchive)
  normalize.py               raw -> bookmark.json (dedup by tweet_id)
  analyze.py                 cheap local stats (domains/hashtags/authors)
  categories.py              AI (or heuristic) discovers 10-25 categories
  classify.py                batched classification with confidence
  propose.py                 dry-run proposal (folders + counts)
  writes.py                  plan write operations
  apply_phase.py             create folders -> add -> verify (ADD-ONLY)
  discovery.py, requests.py  live query-ID discovery + GraphQL calls
  ai.py                      FREE AI bridge (OpenCode config / Ollama)
  demo.py                    synthetic data for offline testing
assets/collector.user.js     one-click exporter (SaveBox-style)
tests/test_smoke.py          smoke + discovery + no-delete tests
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Start the app (stabilized UI)

Windows Command Prompt / PowerShell:

```powershell
cd C:\Users\User\Documents\x-bookmark-organizer
python app.py --no-browser
REM then open http://127.0.0.1:8765/
```

Git Bash:

```bash
cd /c/Users/User/Documents/x-bookmark-organizer
python app.py --no-browser
```

## What the views mean

- **Unsorted = no topic category.** The bookmark has no LOCAL folder
  assigned yet (or its only folders were deleted).
- **Needs Review = uncertain or not yet reviewed.** Needs Review contains
  bookmarks whose classification is uncertain, usually because confidence
  is below 0.60, or because the bookmark has not been classified yet.
  Review a bookmark to accept or change its categories and intent.
  Each card shows why it needs review, the confidence value/level,
  current categories, alternative categories, and the classifier reason.
  Actions: accept as-is, change category (folder chips), add/remove
  category, change intent, mark reviewed/done. Marking reviewed removes
  it from Needs Review but keeps it under All.
- **Possible Duplicates = possible duplicate records.** Same URL or
  highly similar text. Read-only; nothing is auto-deleted.

## How local categories work

- Folders/categories are **LOCAL folders in this app, not X folders**.
  Nothing is written to X; only `data/<kb>/library.json` changes.
- Discover Categories proposes folders from YOUR data (AI names the
  offline clusters, heuristic fallback when the AI 502s). Edit names /
  descriptions, press ✕ to delete a proposal row, then Accept.
- The sidebar **Folders (local only)** manager lets you rename or delete
  an accepted folder. Deleting asks for confirmation, never deletes
  bookmarks: bookmarks left with no folder become Unsorted, bookmarks in
  other folders keep those folders.
- No X writes, no X bookmark deletion, no X Premium requirement.

## How AI fallback works (free endpoint may 502)

- AI timeout is bounded (`XBO_AI_TIMEOUT`, default 30s, capped 5–60s).
- One attempt per batch — no uncontrolled retries.
- Any AI failure (HTTP 502, timeout, bad JSON) is captured per batch and
  filled with the offline heuristic so the run still completes.
- Results report `mode`: `ai`, `heuristic`, or `mixed`, plus per-batch
  `ai_errors` and a human-readable `note`. The UI shows this after
  Classify All / Discover.
- Classify All saves after every batch (`library.json` + append to
  `classifications.jsonl`), so re-running resumes: classified records
  are skipped, manual/reviewed corrections are never overwritten unless
  you tick “re-classify everything”.

## How to use Classify All

1. Run Discover Categories → Accept Category Structure first.
2. Click **Classify All** (uses AI when available, else heuristic).
3. The status line shows `done [mode]: X classified, Y skipped,
   Z unsorted (N batches)` — wait for it; the button disables while
   running but the server saves each batch, so an interrupted run is
   safe to retry (it resumes).
4. Review the Needs Review queue, then export/backup.

## How to back up and restore

- UI: **Backup ZIP** downloads all core KB files
  (`bookmarks.json`, `library.json`, `categories.json`,
  `category_structure.json`, `feedback.jsonl`, `classifications.jsonl`,
  …). **Export CSV / Markdown** downloads the joined view.
- Manual backup: copy the whole `data/` directory.
- Restore: stop the app, replace `data/` with the backup copy, restart
  with `python app.py --no-browser`.

## Importing your real X bookmarks

1. Install Tampermonkey/Violentmonkey, add `assets\collector.user.js`.
2. Open `https://x.com/i/bookmarks`, scroll to the bottom, click Download.
3. `python xb.py --kb data import --fixture C:\path\to\bookmarks_raw.jsonl`
4. `python xb.py --kb data normalize`
5. Open the UI (`python app.py --no-browser`), Discover → Accept →
   Classify All → review → export/backup. Local-only throughout.

## Safety recap

- Dry-run default. `apply` needs `--approve`.
- ADD-ONLY default. `--reorganize` needed to ever call `RemoveTweetFromBookmarkFolder`.
- `DeleteBookmark` is never called (asserted in code).
- Every proposed/completed op appends to `data/change_log.jsonl`; resume skips done ops.
- MOVE = add -> verify -> (only if `--reorganize`) remove from old folder.
of this.

## Public deployment (Render, production server)

Local `python app.py` is unchanged for offline dev. The publicly deployable
target is `server.py` (Flask + gunicorn):

- Production start: `gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
- Dependencies: `requirements.txt` (Flask, gunicorn, Werkzeug)
- Health: `GET /healthz` (also `/health`), used by `render.yaml`
- Config: `render.yaml` (free plan, persistent disk `/var/data`)
- Data dir: `DATA_DIR` env (default `data`; on Render `/var/data`).
  `meta.db` (SQLite upload history) lives there too.

### Environment variables

| Var | Purpose | Default |
|---|---|---|
| `LLM_PROVIDER` | provider mode: `auto`/`gemini`/`groq`/`openrouter`/`heuristic` | `auto` |
| `LLM_FALLBACK_PROVIDERS` | deterministic order tried in `auto` mode | `gemini,groq,openrouter` |
| `LLM_ALLOW_HEURISTIC_FALLBACK` | allow the offline heuristic as the final `auto` fallback | `false` |
| `GEMINI_API_KEY` | Google Gemini key (server-side only) | empty |
| `GEMINI_MODEL` | Gemini model | `gemini-3.8-flash` |
| `GEMINI_TIMEOUT` | per-request timeout s (5–60) | `25` |
| `GROQ_API_KEY` / `GROQ_MODEL` | Groq (OpenAI-compatible) | `llama-3.1-8b-instant` |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | OpenRouter (OpenAI-compatible) | `openai/gpt-oss-20b:free` |
| `OPENROUTER_SITE_URL` | optional OpenRouter `HTTP-Referer` attribution | empty |
| `OPENROUTER_APP_NAME` | optional OpenRouter `X-Title` attribution | empty |
| `SESSION_SECRET` | Flask session signing secret (required in prod) | — |
| `APP_USERNAME` | single-account login name | — |
| `APP_PASSWORD_HASH` | werkzeug hash, e.g. `python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('pw'))"` | — |
| `APP_BASE_URL` | public URL (links) | — |
| `DATA_DIR` | KB + sqlite dir (`/var/data` on Render) | `data` |
| `PORT` | web port | `8765` |

Generate the hash locally and paste only the hash into Render env —
never commit passwords. `render.yaml` contains no secrets.

### AI providers

Providers are isolated behind a small adapter interface in
`xbookmark/llm.py` (Gemini, Groq, OpenRouter, plus the offline
heuristic), so new providers can be added without touching
`categories.py`.

- **`LLM_PROVIDER=auto`** (recommended): try only the providers that
  actually have an API key, in the deterministic
  `LLM_FALLBACK_PROVIDERS` order (default `gemini,groq,openrouter`).
  A provider is skipped when its key is missing. One provider is
  considered successful only when the HTTP call succeeds, JSON parses,
  the required structure exists, strict validation passes, and at least
  three valid categories remain. On a retryable failure
  (429/500/502/503/504/timeout) or invalid structured output, discovery
  moves to the next configured provider.
- **Explicit mode** (`LLM_PROVIDER=gemini`, `=groq`, `=openrouter`,
  `=heuristic`) still works: exactly that provider is used and no other
  provider is invoked.
- Each provider performs **at most one controlled retry** for transient
  errors. Non-retryable 400/401/403/404 / invalid-model errors fail fast
  and let auto mode continue. The whole fallback chain runs once per
  discovery attempt (bounded).
- **The offline heuristic never runs automatically** unless
  `LLM_ALLOW_HEURISTIC_FALLBACK=true` (it is then the final entry in the
  auto chain) or the user explicitly chooses “Use heuristic fallback”.
  A heuristic proposal is always labeled as **not AI-generated**.
- Cloud AI is **opt-in and off by default**. Ticking “Use cloud AI”
  sends compact/truncated bookmark lines to the selected provider.
- “Test AI connection” uses a tiny harmless prompt (never the
  collection). In `auto` mode it tests the configured chain in order and
  stops at the first success, reporting each attempt
  (e.g. `Gemini — 503 · Groq — OK · OpenRouter — not attempted`).
- **Secrets** travel server-side only and are never logged: diagnostics
  retain only provider, model, HTTP status, error kind, a truncated
  error message, retry count, duration, and success.
- A successful discovery reports which provider actually produced the
  result and the fallback chain. If every configured provider fails the
  UI shows each attempt and offers **Retry AI** / **Use heuristic
  fallback** — it never silently substitutes heuristic categories.

### Phone workflow

1. Desktop: export bookmarks with `assets/collector.user.js` → `bookmarks_raw.jsonl`.
2. Phone: open the deployed site → log in → **📤 Upload** → pick the file.
3. Discover Categories → Accept → Classify All (heuristic or opt-in cloud).
4. Work through Needs Review → search/organize → Export/Backup ZIP.

### Free-tier limitations (honest)

- Without a persistent disk/database, Render's free ephemeral filesystem
  **loses uploads on restart/redeploy**. This repo configures a 1 GB
  persistent disk (`/var/data`); the disk itself requires a paid Render
  plan — on the pure free plan, treat the library as ephemeral and keep
  Backup ZIPs after every session.
- Gemini free tier has quotas/rate limits; the app surfaces 429/quota as
  safe errors and heuristic-fills those batches (run stays resumable).

### Tests + smoke

```powershell
python -m unittest discover -s tests -v
$env:APP_USERNAME="you"; $env:APP_PASSWORD_HASH="<hash>"
$env:SESSION_SECRET="<32+ chars>"; python server.py  # then open /healthz
```