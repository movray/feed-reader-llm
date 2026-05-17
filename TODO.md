# TODO

## UI — Main Overview

- [x] **Browser-local timestamp** — replace the static generated time in the header with the
      current browser time (JavaScript `Date` on page load), so it always shows the viewer's
      local time instead of the server's generation time

- [x] **Last-synced time per block** — show the `last_fetched_at` timestamp from the `sources`
      table inside each feed block (e.g. below the block title); update it after each sync

## UI — Sidebar

- [x] **Category sidebar** — auto-classify feeds via LLM (Sicherheit/Technologie/KI/DevOps/…),
      store in `sources.category`; JS sidebar filters visible blocks by category

- [ ] **Article count per category** — show count badge next to each sidebar button
      (e.g. `// SECURITY (4)`); pure JS, no backend changes needed

## UI — Article Cards

- [x] **Per-article delete button** — × button (visible on hover) removes the article from DB
      and DOM; deleted articles are re-fetched and re-processed on the next sync

- [x] **Category badge on article card** — small colored pill showing the article's category
      directly on the card (e.g. `[SECURITY]`); helps orient while scrolling without using
      the sidebar filter

- [ ] **Retry button per article** — ↻ button next to × that resets `summary_llm = NULL` and
      triggers a feed sync; useful for bad translations without deleting the article

- [ ] **Read / unread tracking** — `articles.read_at TIMESTAMPTZ`; set when user clicks the
      article link; unread articles highlighted, read articles dimmed; filter option in sidebar
      or header

## Article Archiving

- [ ] **Article starring / archiving** — star button on each article card sets
      `articles.starred BOOLEAN`; starred articles are never deleted by sync cleanup;
      `// STARRED` filter in sidebar shows only saved articles

- [ ] **Global search over archived articles** — search input in header runs ILIKE query over
      `title_translated` and `summary_llm` filtered to `starred = true`; results shown inline

## Config Panel

- [ ] **Category override per feed** — allow manual override of the LLM-assigned category
      via a dropdown in the config table row; writes to `sources.category` in the DB

- [ ] **Editable category list** — add a "Categories" section in the config panel with an
      editable list of available categories (add / rename / delete)

- [x] **Per-feed sync interval** — numeric input per feed row sets `sources.sync_interval_minutes`;
      generator respects this value on scheduled runs

- [x] **Per-feed max articles** — numeric input per feed row sets `sources.max_articles` (default 5,
      max 20); controls how many articles are fetched and shown per block

- [x] **LLM config** — CONF LLM button opens URL + API key form; stored in `llm_config` DB table;
      generator reads from DB on startup, falls back to `LLM_BASE_URL` env var

- [x] **Configurable summary language** — language selector in CONF LLM dialog; LLM summarizes
      and translates titles into the selected language; language change resets all articles
      for re-translation
