-- Schema of feed-reader-llm, applied by `migrate.py` before the app starts.
-- Every statement is idempotent: the file runs on every start and only
-- creates or adds what is missing. Parameters (psycopg2 pyformat): llm_url,
-- llm_model. A literal percent sign must be written as two.

CREATE TABLE IF NOT EXISTS sources (
  id                    SERIAL PRIMARY KEY,
  name                  TEXT NOT NULL,
  url                   TEXT NOT NULL UNIQUE,
  enabled               BOOLEAN NOT NULL DEFAULT true,
  last_fetched_at       TIMESTAMPTZ,
  sync_interval_minutes INTEGER NOT NULL DEFAULT 360,
  max_articles          INTEGER NOT NULL DEFAULT 5,
  category              TEXT
);
ALTER TABLE sources ADD COLUMN IF NOT EXISTS sync_interval_minutes INTEGER NOT NULL DEFAULT 360;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS max_articles INTEGER NOT NULL DEFAULT 5;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS category TEXT;

CREATE TABLE IF NOT EXISTS llm_config (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  url              TEXT NOT NULL DEFAULT '',
  api_key          TEXT NOT NULL DEFAULT 'none',
  summary_language TEXT NOT NULL DEFAULT 'German',
  model            TEXT NOT NULL DEFAULT ''
);
ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS url TEXT NOT NULL DEFAULT '';
ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS api_key TEXT NOT NULL DEFAULT 'none';
ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS summary_language TEXT NOT NULL DEFAULT 'German';
ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
INSERT INTO llm_config (url, api_key, summary_language, model)
SELECT %(llm_url)s, 'none', 'German', %(llm_model)s
WHERE NOT EXISTS (SELECT 1 FROM llm_config);
-- llm_config holds exactly one row. Older versions could insert duplicates
-- (all rows were updated together, so they are identical): keep the row
-- that SELECT ... LIMIT 1 returned, then let the index refuse a second one.
DELETE FROM llm_config
WHERE id <> (SELECT id FROM llm_config ORDER BY ctid LIMIT 1);
CREATE UNIQUE INDEX IF NOT EXISTS llm_config_single_row ON llm_config ((true));

CREATE TABLE IF NOT EXISTS articles (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id        INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  title_original   TEXT,
  url              TEXT NOT NULL UNIQUE,
  summary_llm      TEXT,
  category         TEXT,
  title_translated TEXT,
  llm_error        BOOLEAN NOT NULL DEFAULT false,
  published_at     TIMESTAMPTZ,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS title_translated TEXT;

CREATE TABLE IF NOT EXISTS _migrations (id TEXT PRIMARY KEY);

-- One-time data migrations: each runs once, tracked in _migrations.

-- v1.9.0: categories are reassigned per article by the LLM.
WITH m AS (
  INSERT INTO _migrations (id) VALUES ('v1.9.0-cat-reset')
  ON CONFLICT DO NOTHING RETURNING id
), a AS (
  UPDATE articles SET category = NULL WHERE EXISTS (SELECT 1 FROM m)
)
UPDATE sources SET category = NULL WHERE EXISTS (SELECT 1 FROM m);

-- v1.15.0: existing deployments take the model from LLM_MODEL once;
-- afterwards the CONF LLM dialog owns it (an empty model stays empty).
WITH m AS (
  INSERT INTO _migrations (id) VALUES ('v1.15.0-llm-model')
  ON CONFLICT DO NOTHING RETURNING id
)
UPDATE llm_config SET model = %(llm_model)s
WHERE model = '' AND EXISTS (SELECT 1 FROM m);
