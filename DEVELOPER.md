# Developer Guide

This document covers the internal architecture, component interactions, and conventions for working on feed-reader-llm. For features and deployment see [README.md](README.md).

---

## Project layout

```
k8s/
  news_feed_cyberpunk.py   # generator — fetches feeds, calls LLM, writes HTML
  api_server.py            # Flask API — config panel, block serving, sync triggers
  nginx-default.conf       # nginx server config
  nginx-loading.html       # loading page shown before first generator run
  kustomization.yaml       # Kustomize — generates all ConfigMaps from source files
  deployment.yaml          # Deployment + init-db init container (owns DB schema)
  service.yaml
  ingress.yaml
```

All files under `k8s/` are source files edited directly. There is no code generation step — Kustomize embeds the Python scripts and nginx files into ConfigMaps at deploy time (ArgoCD runs `kustomize build` on every sync).

---

## Kubernetes architecture

```
Pod: news-feed (namespace: feedreader)
│
├── init-db (postgres:16-alpine)          runs to completion before app containers start
│     └── creates / migrates all tables via psql heredoc
│
├── nginx (nginx:alpine) :80
│     ├── serves /html/index.html and /html/blocks/*.html from the shared volume
│     ├── proxies /api/ → localhost:5000 (api-server)
│     └── returns loading.html on 403/404 (before index.html exists)
│
├── generator (python:3.12-slim)
│     ├── shell loop: sleeps 60 s, runs generator every minute
│     ├── writes index.html shell + /html/blocks/<id>.html per feed
│     └── liveness probe: /html/.generator-heartbeat must be < 10 min old
│
└── api-server (python:3.12-slim) :5000
      ├── Flask API for the config panel and block serving
      ├── writes trigger files to signal the generator
      ├── readiness probe: GET /api/sources :5000 (holds traffic until Flask is up)
      └── liveness probe: GET /api/sources :5000 (restarts on deadlock)
```

**Shared volume:** `emptyDir` mounted at `/html` by all three app containers. This is the only communication channel between generator and api-server — no sockets, no queues.

**ConfigMaps:**
- `news-feed-script` — contains `news_feed_cyberpunk.py` and `api_server.py`, mounted at `/scripts`
- `nginx-config` — contains `default.conf` and `loading.html`

Both ConfigMap names include a content hash (Kustomize `configMapGenerator`). Any committed change to the source files produces a new hash → Kubernetes triggers an automatic rolling update.

---

## Render architecture

The page is built from a static shell and independent per-feed block files.

**On each sync:**

1. Generator calls `write_shell()` — writes `index.html` with one HTMX loading slot per enabled feed:
   ```html
   <div id="feed-{id}" hx-get="/api/block/{id}" hx-trigger="load, every 5s" ...>
     <!-- loading spinner -->
   </div>
   ```
2. Generator processes feeds one by one. After each feed it calls `write_block()` which writes `/html/blocks/{id}.html`.
3. The browser polls `/api/block/{id}` every 5 s. The endpoint returns the block HTML once the file exists, or a self-polling placeholder while it is still missing.
4. Once a block is delivered, HTMX swaps the slot content in-place — other feeds are unaffected.

**Shell rebuilds** also happen in `api_server.py` via `_rebuild_shell()` after every toggle/add/delete, so the feed list stays correct across page reloads without a full generator run. `_rebuild_shell()` patches only the `#feed-list` section of `index.html` in-place.

---

## Sync triggers

Generator and api-server communicate via trigger files in `/html/`:

| File | Written by | Effect |
|---|---|---|
| `/html/.sync-now` | `api_server` (`/api/sync`) | Full sync of all enabled feeds (`--force`), ignoring per-feed intervals |
| `/html/.sync-feed-<id>` | `api_server` (`/api/sync/<id>`) | Single-feed sync; api-server also deletes the block file to show loading state |

The generator loop sleeps 60 s between iterations. On each tick it checks trigger files first; if none are present it runs the generator normally — the generator then checks each feed's `sync_interval_minutes` against `last_fetched_at` and skips feeds not yet due.

---

## Database schema

PostgreSQL database `feedconfig`. The service name (`feedreader-db-cluster-rw`) and credentials secret (`feeduser-credentials`) are configured in `deployment.yaml` and should be adjusted to match your cluster setup.

### Schema ownership

**`init-db` (init container in `deployment.yaml`) owns all DDL.** It runs `CREATE TABLE IF NOT EXISTS` with the full current schema plus `ALTER TABLE IF NOT EXISTS` for backwards compatibility on existing deployments. This is the single source of truth — when adding a new column, only `deployment.yaml` needs updating.

**`api_server._migrate()` owns one-time data migrations only**, tracked via the `_migrations` table. Use `_run_once(id, stmts)` for any migration that must run exactly once against live data.

### Tables

**`sources`** — one row per RSS feed
```
id                    SERIAL PRIMARY KEY
name                  TEXT
url                   TEXT UNIQUE
enabled               BOOLEAN DEFAULT true
last_fetched_at       TIMESTAMPTZ
sync_interval_minutes INTEGER DEFAULT 360
max_articles          INTEGER DEFAULT 5
category              TEXT   -- comma-separated, derived from articles.category after each sync
```

**`articles`** — one row per article URL (deduplicated)
```
id               UUID PRIMARY KEY DEFAULT gen_random_uuid()
source_id        INTEGER REFERENCES sources(id) ON DELETE CASCADE
title_original   TEXT
url              TEXT UNIQUE
summary_llm      TEXT   -- NULL = not yet summarized; retried on next sync
category         TEXT   -- single English word assigned by LLM (e.g. "Technology")
title_translated TEXT   -- NULL = not yet translated; retried on next sync
llm_error        BOOLEAN DEFAULT false
published_at     TIMESTAMPTZ
fetched_at       TIMESTAMPTZ DEFAULT now()
```

**`llm_config`** — single-row table for LLM connection settings
```
id               UUID PRIMARY KEY DEFAULT gen_random_uuid()
url              TEXT DEFAULT ''          -- populated from LLM_BASE_URL env var on first deploy
api_key          TEXT DEFAULT 'none'
summary_language TEXT DEFAULT 'German'
```
Always read with `SELECT ... LIMIT 1`. Write with UPDATE-then-INSERT upsert (never assume a fixed id).
The initial URL comes from the generator's `LLM_BASE_URL` env var (single source of truth — init-db passes it to psql via `-v`).

**`_migrations`** — tracks one-time data migrations
```
id TEXT PRIMARY KEY   -- e.g. 'v1.9.0-cat-reset'
```

### Adding a new column

1. Add the column to the `CREATE TABLE` statement in `deployment.yaml` (init-db).
2. Add an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` below the `CREATE TABLE` in the same block (for existing deployments).
3. That's it — no changes needed in the Python files unless the code references the column.

---

## LLM integration

**Config loading** (`init_llm_config` in `news_feed_cyberpunk.py`):
1. Read `url`, `api_key`, `summary_language` from `llm_config` table (`LIMIT 1`).
2. Fall back to `LLM_BASE_URL` environment variable if the table read fails.
3. Detect the active model via `GET /v1/models` — uses the first alias of the first result.

**Summarization call** (`summarize_and_classify`):
One API call per article produces a three-line response:
```
CATEGORY: Technology
TITLE: Translated article title in the configured language
SUMMARY: 2-3 sentence summary in the configured language.
```
`_parse_llm_response()` extracts all three fields. If the format is not recognised, the full response is used as the summary and category/title fall back to defaults.

**Backfill calls** (run on each sync, max 20 articles per feed):
- `classify_title(title)` — title-only call (10 tokens) for articles that have a summary but no category.
- `translate_title(title)` — title-only call for articles that have a summary but no translated title.

---

## Category system

Categories are assigned per article, not per feed.

1. During summarization, the LLM picks a free-form English word (e.g. `Security`, `Politics`, `Art`). No fixed vocabulary.
2. `_extract_category_word()` normalises the raw LLM output — handles CamelCase, slashes, multi-word phrases.
3. After each feed sync, `update_feed_categories()` aggregates all distinct `articles.category` values for that feed and writes them as a comma-separated string to `sources.category`.
4. The sidebar JS reads `data-categories` attributes on the feed wrapper divs (`<div id="feed-{id}">`). This attribute is set at shell-write time from `sources.category` and updated immediately when a block loads via an inline `<script>` tag in each block file.

The `data-categories` attribute lives on the **outer wrapper div**, not inside the HTMX-swapped block content, so it survives every HTMX innerHTML swap.

---

## Known pitfalls

**HTMX 2.x event bubbling** — `htmx:afterRequest` fires on both the requesting element and `document.body`. Always guard element-scoped handlers:
```javascript
element.addEventListener('htmx:afterRequest', (event) => {
  if (event.detail.elt !== element) return;
  // ...
});
```

**`sources.id` is SERIAL (integer), not UUID** — Flask routes must use `<block_id>` not `<int:block_id>`, since the same route also handles UUID-shaped parameters in some contexts. Don't mix up `sources.id` with `articles.id` (which is UUID).

**`llm_config.id` is UUID** — never use `WHERE id = 1` or `INSERT ... VALUES (1, ...)`. Use `SELECT ... LIMIT 1` to read and an UPDATE-then-INSERT upsert to write.

**`_migrate()` is not the place for DDL** — schema changes go in `deployment.yaml` init-db. `_migrate()` is for one-time data operations only.

**Block files persist across pod restarts** — the `/html` volume is an `emptyDir`, so it starts empty on every pod start. The generator writes a fresh shell and blocks on its first run. During that window nginx serves `loading.html`.

---

## Local development

```bash
pip install feedparser requests psycopg2-binary flask

# Set DB and LLM env vars, then:
python k8s/news_feed_cyberpunk.py          # full sync
python k8s/news_feed_cyberpunk.py --feed 3 # single feed by source id
python k8s/api_server.py                   # Flask dev server on :5000
```

The generator writes `index.html` and `blocks/` relative to `OUTPUT_FILE` (defaults to `news_zusammenfassung.html` in the working directory).
