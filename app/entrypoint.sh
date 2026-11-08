#!/bin/sh
# Entrypoint of the feed-reader-llm image. The first argument picks the role:
#   migrate    create or update the database schema, then exit
#   generator  fetch feeds and write the HTML (loop, every 60 s)
#   api        Flask API for the config panel (gunicorn)
#   healthcheck-generator  exit 0 while the generator loop is alive
# Anything else is executed as is (e.g. sh for debugging).
set -eu

HTML_DIR="${HTML_DIR:-/html}"
export OUTPUT_FILE="${OUTPUT_FILE:-$HTML_DIR/index.html}"
HEARTBEAT="$HTML_DIR/.generator-heartbeat"
cd /app

case "${1:-}" in
  migrate)
    exec python migrate.py
    ;;

  generator)
    # A full sync at start; then the scheduled run every 60 s, which only
    # processes feeds whose interval has elapsed. The API writes the trigger
    # files .sync-now and .sync-feed-<id> into HTML_DIR.
    mkdir -p "$HTML_DIR"
    touch "$HTML_DIR/.sync-now"
    while true; do
      date +%s > "$HEARTBEAT"
      if [ -f "$HTML_DIR/.sync-now" ]; then
        rm -f "$HTML_DIR/.sync-now"
        echo "[$(date)] Full sync (forced)..."
        python news_feed_cyberpunk.py --force || echo "[$(date)] Full sync failed"
      else
        for f in "$HTML_DIR"/.sync-feed-*; do
          [ -f "$f" ] || continue
          feed_id="${f#"$HTML_DIR"/.sync-feed-}"
          rm -f "$f"
          echo "[$(date)] Feed sync: $feed_id"
          python news_feed_cyberpunk.py --feed "$feed_id" || echo "[$(date)] Feed sync failed: $feed_id"
        done
        python news_feed_cyberpunk.py || echo "[$(date)] Scheduled sync failed"
      fi
      sleep 60
    done
    ;;

  healthcheck-generator)
    # Alive while the loop wrote its heartbeat in the last 10 minutes.
    last="$(cat "$HEARTBEAT" 2>/dev/null || echo 0)"
    [ $(( $(date +%s) - last )) -lt "${GENERATOR_MAX_AGE_SECONDS:-600}" ]
    ;;

  api)
    exec gunicorn \
      --bind "0.0.0.0:${PORT:-5000}" \
      --workers "${GUNICORN_WORKERS:-2}" \
      --threads "${GUNICORN_THREADS:-4}" \
      --timeout "${GUNICORN_TIMEOUT:-60}" \
      --access-logfile - \
      --no-control-socket \
      api_server:app
    ;;

  *)
    exec "$@"
    ;;
esac
