# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [1.14.0] — 2026-05-06

### Added
- Liveness probe for generator container: heartbeat file (`/html/.generator-heartbeat`) written at the start of each loop iteration; Kubernetes restarts the container if the file is older than 10 minutes
- Readiness and liveness probes for api-server: HTTP GET `/api/sources` on port 5000; Kubernetes holds traffic until Flask is up and restarts on deadlock

### Changed
- Config panel width increased to fit all elements without overflow
- `CONF LLM` button now uses the same muted-start / cyan-glow-on-hover style as the `[CONFIG]` button in the page header
- Resource limits increased: generator `256Mi/200m → 768Mi/1 CPU`, api-server `64Mi/50m → 384Mi/500m`
- `LLM_BASE_URL` is now defined once (generator env var); init-db reads it via psql `-v` for the initial `llm_config` row — no more duplicate hardcoded IP

---

## [1.13.0] — 2026-05-06

### Fixed
- Per-feed `sync_interval_minutes` shorter than 6 h were silently ignored — the shell loop fired a full sync only every 6 h, so a feed configured for e.g. 30 min was still only synced every 6 h

### Changed
- Generator shell loop now sleeps 60 s instead of 5 s and drops the hardcoded 6 h `NEXT_RUN` timer; the generator runs every minute and decides per-feed whether a sync is due based on `sync_interval_minutes` and `last_fetched_at`

---

## [1.12.0] — 2026-05-06

### Fixed
- `llm_config` table was never created on a fresh cluster deploy — `ALTER TABLE` and `INSERT` statements in both `init-db` and `api_server._migrate()` failed silently, leaving the CONF LLM feature non-functional on first install; added `CREATE TABLE IF NOT EXISTS llm_config` to both migration paths

### Changed
- Schema ownership consolidated: `init-db` init container is now the single source of truth for all DDL (`CREATE TABLE`, `ALTER TABLE`); `api_server._migrate()` reduced to one-time data migrations via `_run_once` only
- `articles` `CREATE TABLE` in `init-db` updated to include all current columns (`category`, `title_translated`); duplicate `ALTER TABLE llm_config ... summary_language` removed
- nginx config and loading page extracted from the hand-maintained `configmap-nginx.yaml` into `k8s/nginx-default.conf` and `k8s/nginx-loading.html`; Kustomize now generates the `nginx-config` ConfigMap with a content hash — changes to either file trigger an automatic pod rollout

### Removed
- `k8s/configmap-nginx.yaml` — replaced by Kustomize configMapGenerator

---

## [1.11.0] — 2026-05-06

### Changed
- Python scripts (`news_feed_cyberpunk.py`, `api_server.py`) moved into `k8s/` and embedded into the ConfigMap via Kustomize `configMapGenerator` — no manual generation step required anymore
- ConfigMap name now includes a content hash; any change to the Python scripts automatically triggers a pod rollout via ArgoCD

### Removed
- `scripts/generate-configmap.sh` — replaced by Kustomize
- `k8s/configmap-news-feed-script.yaml` — generated at deploy time by Kustomize

---

## [1.10.0] — 2026-05-05

### Added
- **Configurable summary language** — language selector in the CONF LLM dialog (German, English, French, Spanish, Italian, Portuguese, Dutch, Polish, Turkish); stored in `llm_config.summary_language`
- **Translated article titles** — the LLM now translates the article title into the selected language in the same API call as the summary; stored in `articles.title_translated` and shown in the feed blocks instead of the original RSS title
- `translate_title()` backfill: existing articles without a translated title are translated on the next sync (up to 20 per feed, title-only call)
- Changing the summary language resets all `summary_llm` and `title_translated` values to `NULL` so everything is re-generated in the new language on the next sync

### Changed
- LLM response format extended to three lines: `CATEGORY:` / `TITLE:` / `SUMMARY:`
- Summary prompt no longer hardcodes German — language is read from `llm_config.summary_language` at generator startup

---

## [1.9.0] — 2026-05-05

### Changed
- **Per-article LLM categorization** — categories are now assigned per article during summarization, not per feed; the LLM chooses a free-form English word (e.g. Politics, Security, Art, Finance) in the same API call that produces the German summary
- Category vocabulary is no longer fixed — the LLM picks any word that fits the article; no predefined slug list
- `articles.category` column stores the per-article category; `sources.category` is now derived by aggregating distinct article categories after each sync
- Sidebar categories are built from all distinct values in `articles.category` and sorted alphabetically; `CAT_LABELS` / `CAT_ORDER` removed
- `data-categories` on each feed wrapper is updated immediately when its block loads via an inline script, so the sidebar reflects freshly classified articles without waiting for the next full shell rebuild

### Added
- `_extract_category_word()` parser handles CamelCase, slash-separated and multi-word LLM output — extracts a clean single word in all cases
- `classify_title()` — lightweight backfill call (title only, 10 tokens) that classifies existing articles which have a summary but no category; runs for up to 20 articles per feed on each sync
- `_migrations` table for tracking one-time DB migrations; `_run_once()` helper in `api_server.py`
- One-time migration `v1.9.0-cat-reset` resets all `articles.category` and `sources.category` values to `NULL` so every article gets re-classified cleanly with the new system

### Removed
- `classify_feed()` and `save_categories()` — feed-level classification replaced by per-article classification
- Fixed category slug list (`FEED_CATEGORIES`)

---

## [1.8.0] — 2026-05-04

### Added
- **Multi-category feeds** — each feed can now belong to multiple categories simultaneously; clicking a sidebar category shows all feeds tagged with it
- **Article-title-based classification** — feeds are classified using actual article titles fetched during sync, not just the feed name/URL; far more accurate
- `CAT_LABELS` translation table in the sidebar JS lays groundwork for a configurable display language without touching internal category keys

### Changed
- Category sidebar buttons now follow a fixed logical order (Security → Technology → AI → DevOps → Science → Economy → News → Other) instead of alphabetical
- Categories stored as comma-separated slugs (`"Security,Technology"`) in `sources.category`; old single-value entries are reset on api_server startup so all feeds get re-classified with the new approach
- Removed `_CATEGORY_ALIASES` and `_CATEGORY_MIGRATION` dicts — classification is now purely content-driven, no language mapping needed
- Classification happens inside the per-feed sync loop (after articles are fetched), not as a separate pre-pass

---

## [1.7.0] — 2026-05-04

### Added
- **Category sidebar** — auto-generated from LLM classifications; click any category to filter the feed view; "ALL" resets the filter
- **LLM config via DB and config panel** — `llm_config` table stores URL and API key; editable via "CONF LLM" button; LLM connection is tested automatically after saving
- **Retry empty LLM summaries** — articles with an empty `summary_llm` are re-submitted to the LLM on the next sync instead of being silently cached

### Changed
- All UI text translated to English
- Feed URL shown below the feed name in the config panel instead of as a separate column
- Category names changed from German to English slugs (Security, Technology, AI, DevOps, Science, Economy, News, Other)

### Fixed
- `_migrate()` in `api_server.py` now runs each schema statement in its own transaction — a failure in one step no longer blocks subsequent migrations
- `llm_config` table with UUID primary key handled correctly (no hardcoded `id=1` assumptions)

---

## [1.6.0] — 2026-05-04

### Added
- **Configurable max articles per feed** — `sources.max_articles` column (1–20); editable per feed in the config panel
- **Per-feed last-sync timestamp** — each feed block shows when it was last updated
- **Browser-local timestamps** — header date/time and footer timestamp rendered in the browser's local timezone instead of the server's

---

## [1.5.0] — 2026-05-04

### Added
- **Per-feed sync interval** — `sources.sync_interval_minutes` column; generator respects the interval on scheduled runs and skips feeds that synced recently
- **Per-feed sync button** — each block has a ↻ button that triggers an immediate resync of that feed only (via `/html/.sync-feed-<id>` trigger file)
- Shell (`index.html`) is rebuilt by `api_server.py` after every toggle/add/delete so the feed list stays correct across page reloads without a full generator run

### Fixed
- `htmx:afterRequest` on SYNC NOW button scoped with `event.detail.elt===this` guard to prevent capturing unrelated HTMX requests bubbling to `document.body`

---

## [1.4.0] — 2026-05-03

### Changed
- **Streaming render architecture** — generator no longer writes a single monolithic `index.html`; instead it writes a lightweight shell with HTMX loading slots and individual block files under `/html/blocks/<id>.html`
- Browser polls `/api/block/<id>` every 5 seconds until the block is ready; feed blocks appear live as the generator finishes them

---

## [1.3.0] — 2026-05-03

### Changed
- Reorganised all Kubernetes manifests into `k8s/` — one file per resource (`configmap-nginx.yaml`, `configmap-news-feed-script.yaml`, `deployment.yaml`, `service.yaml`, `ingress.yaml`)
- `configmap-news-feed-script.yaml` is now committed directly; no manual generation step required on first deploy
- Replaced `create_configmap.sh` with `scripts/generate-configmap.sh` (run after changing Python source files, then commit the result)
- Moved `create_configmap.sh` script to `scripts/` and updated output path accordingly

### Added
- Init container (`postgres:16-alpine`) in the Deployment that automatically creates the `sources` table on first deploy using `CREATE TABLE IF NOT EXISTS` — no manual database setup required

---

## [1.2.0] — 2026-04-19

### Added
- **Sync Now button** — `[SYNC NOW]` in the page header triggers an immediate feed refresh
- Sync works via a trigger file (`/html/.sync-now`); the generator loop detects it within 30 seconds
- Status badge polls every 5 seconds and disappears once the sync completes
- Config panel now uses vanilla `fetch()` for reliable data loading (independent of HTMX CDN timing)

---

## [1.1.0] — 2026-04-19

### Added
- **Config panel** — `[CONFIG]` button opens a cyberpunk-styled overlay panel
- **Toggle enable/disable** per source — updates `enabled` flag in DB instantly via HTMX
- **Delete source** — removes entry from DB with browser confirm dialog
- **Add source** — form with URL reachability validation via feedparser; name auto-detected from feed title
- Flask `api-server` sidecar container on port 5000; nginx proxies `/api/` to it
- HTMX loaded from CDN for toggle/delete/add interactions

---

## [1.0.0] — 2026-04-19

### Added
- Initial release
- Fetches RSS feeds from PostgreSQL `sources` table (`enabled = true`)
- Summarizes articles in German using a local OpenAI-compatible LLM
- Cyberpunk-themed HTML output with scanline animation and glitch effects
- Kubernetes deployment: nginx + generator sidecar, ArgoCD-managed
- `create_configmap.sh` to bundle Python scripts into a K8s ConfigMap
- `last_fetched_at` timestamp updated after each successful feed fetch
