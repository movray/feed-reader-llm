# Developer Guide

This document covers the internal architecture, component interactions, and conventions for working on feed-reader-llm. For features and deployment see [README.md](README.md).

---

## Project layout

```
app/
  news_feed_cyberpunk.py   # generator — fetches feeds, calls LLM, writes HTML
  api_server.py            # Flask API — config panel, block serving, sync triggers
  migrate.py               # applies schema.sql, waits for the database
  schema.sql               # the whole DB schema and one-time data migrations
  entrypoint.sh            # image entrypoint: migrate | generator | api | healthcheck-generator
  requirements.in          # direct dependencies
  requirements.txt         # lockfile with hashes (make lock)
charts/feed-reader-llm/    # Helm chart
  files/nginx-default.conf # nginx server config (also used by docker-compose.yml)
  files/nginx-loading.html # loading page shown before the first generator run
Dockerfile                 # one image for all app roles
docker-compose.yml         # local test stack incl. PostgreSQL
Makefile                   # lock, image, scan, up, down, lint, template
.github/workflows/         # ci (checks, scan, smoke test, edge image), release, vuln (weekly)
archive/                   # old standalone scripts, not used
```

---

## Kubernetes architecture

```
Pod: <release>-feed-reader-llm
│
├── migrate (feed-reader-llm image)       init container, runs to completion first
│     └── waits for the DB, applies app/schema.sql (idempotent, under an advisory lock)
│
├── nginx (nginxinc/nginx-unprivileged) :8080
│     ├── serves /html/index.html and /html/blocks/*.html from the shared volume
│     ├── proxies /api/ → 127.0.0.1:5000 (api)
│     ├── /healthz for its probes
│     └── returns loading.html on 403/404 (before index.html exists)
│
├── generator (feed-reader-llm image)
│     ├── shell loop: runs the generator every 60 s
│     ├── writes index.html shell + /html/blocks/<id>.html per feed
│     └── liveness probe: /html/.generator-heartbeat must be < 10 min old
│
└── api (feed-reader-llm image) :5000
      ├── Flask API under gunicorn (2 workers × 4 threads)
      ├── writes trigger files to signal the generator
      ├── readiness probe: GET /api/sources (holds traffic until the DB answers)
      └── liveness probe: GET /api/sources (restarts on deadlock)

Optional (postgresql.enabled=true): StatefulSet <release>-feed-reader-llm-postgresql
with the official postgres image, for testing.
```

**Shared volume:** `emptyDir` (or a PVC with `html.persistence.enabled`) mounted at `/html` by
all three app containers. This is the only communication channel between generator and API —
no sockets, no queues. Hence `replicas: 1` and the `Recreate` strategy.

**Security:** every container runs as non-root with a read-only root filesystem and no
capabilities; writable paths are emptyDirs (`/tmp`, `/html`).

**nginx config** lives in `charts/feed-reader-llm/files/` and becomes a ConfigMap; a checksum
annotation restarts the pod when it changes.

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

PostgreSQL database `feedconfig`. Host, name and credentials come from the chart values
(`database.*`, or the built-in `postgresql.*`).

### Schema ownership

**`app/schema.sql` owns the whole schema**, applied by `app/migrate.py` (the `migrate` init
container) on every start, in one transaction. It runs `CREATE TABLE IF NOT EXISTS` with the
full current schema plus `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for existing deployments.

**One-time data migrations** go at the end of `schema.sql` as a statement guarded by an
`INSERT INTO _migrations ... ON CONFLICT DO NOTHING RETURNING id` CTE, so they run exactly once
(see `v1.9.0-cat-reset`). The file is executed with psycopg2 parameters (`%(llm_url)s`,
`%(llm_model)s`): a literal `%` must be written as `%%`.

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
model            TEXT DEFAULT ''          -- empty = detect from /models; set once from LLM_MODEL
```
Always read with `SELECT ... LIMIT 1`. Write with UPDATE-then-INSERT upsert (never assume a fixed id).
The initial URL and model come from `LLM_BASE_URL` / `LLM_MODEL` (chart: `llm.baseUrl`, `llm.model`); `migrate.py` inserts them while the table is empty.

**`_migrations`** — tracks one-time data migrations
```
id TEXT PRIMARY KEY   -- e.g. 'v1.9.0-cat-reset'
```

### Adding a new column

1. Add the column to the `CREATE TABLE` statement in `app/schema.sql`.
2. Add an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` below the `CREATE TABLE` in the same block (for existing deployments).
3. That's it — no changes needed in the Python files unless the code references the column.

---

## LLM integration

**Config loading** (`init_llm_config` in `news_feed_cyberpunk.py`):
1. Read `url`, `api_key`, `summary_language`, `model` from `llm_config` table (`LIMIT 1`).
2. Fall back to the `LLM_BASE_URL` and `LLM_MODEL` environment variables if the table read fails.
3. Without a model, `detect_model()` asks `GET /models`: the entry with `status.value == "loaded"`
   (llama-server router mode lists all its models with a status), otherwise the first one. The
   `id` is used unchanged — router model names contain `/`, `:` and `.`.

**Failed calls** — `summarize_and_classify()` returns a `[Fehler …]` text and the article is
stored with `llm_error = true`. Such articles count as not summarized: the next sync calls the LLM
again, the backfills skip them, and the stranded-article cleanup removes them once they left the
feed.

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

**Router model names** — never sanitize the model id (e.g. with a regex): the router only knows
the exact name. Escape it with `html.escape` where it is rendered.

**`llm_config.id` is UUID** — never use `WHERE id = 1` or `INSERT ... VALUES (1, ...)`. Use `SELECT ... LIMIT 1` to read and an UPDATE-then-INSERT upsert to write.

**No schema changes in the Python code** — DDL and one-time data migrations go in `app/schema.sql` only. The API and the generator assume the schema exists.

**Block files do not persist across pod restarts** — the `/html` volume is an `emptyDir` by default, so it starts empty on every pod start. The generator writes a fresh shell and blocks on its first run. During that window nginx serves `loading.html`.

---

## Local development

```bash
make up        # docker compose: PostgreSQL, migrate, generator, API, nginx → http://localhost:8080
make down      # stop (docker compose down -v also removes the database)
make scan      # build the image, Trivy gate as in the release
make lint      # helm lint (both database modes)
make lock      # regenerate app/requirements.txt after changing app/requirements.in
```

The LLM for the compose stack: `LLM_BASE_URL=http://host.docker.internal:11434/v1 make up`
(read on the first start only; afterwards the CONF LLM dialog).

Without Docker:

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r app/requirements.txt
export DB_HOST=... DB_PORT=5432 DB_NAME=feedconfig DB_USER=... DB_PASSWORD=...
export OUTPUT_FILE=$PWD/html/index.html
python app/migrate.py                       # schema
python app/news_feed_cyberpunk.py           # full sync
python app/news_feed_cyberpunk.py --feed 3  # single feed by source id
python app/api_server.py                    # Flask dev server on :5000
```

The generator writes `index.html` and `blocks/` relative to `OUTPUT_FILE`.

---

## Releasing

1. Add a section `## [X.Y.Z] — YYYY-MM-DD` to `CHANGELOG.md`.
2. Set `version` and `appVersion` in `charts/feed-reader-llm/Chart.yaml` to `X.Y.Z`.
3. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.

`release.yml` then builds the image for amd64 and arm64, scans it with Trivy (no HIGH or
CRITICAL finding, unfixed ones included), pushes exactly the scanned image to
`ghcr.io/movray/feed-reader-llm:X.Y.Z` and `:latest`, the chart to
`oci://ghcr.io/movray/charts/feed-reader-llm`, signs both with cosign, adds build provenance and
creates the GitHub release from the CHANGELOG section. Run the workflow by hand
(workflow_dispatch) for a dry run that publishes nothing.

`vuln.yml` scans `latest` and `edge` weekly; a red run means: rebuild or update and release.
