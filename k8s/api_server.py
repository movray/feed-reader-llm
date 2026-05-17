"""
Kleiner Flask-API-Server für das Feed-Reader Config-Panel.
Stellt HTMX-kompatible Endpunkte bereit um RSS-Quellen zu verwalten.
"""

import os
import html
import requests as http_requests
import psycopg2
import feedparser
from pathlib import Path
from flask import Flask, request

app = Flask(__name__)

OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "news_zusammenfassung.html")
BLOCK_DIR   = Path(OUTPUT_FILE).parent / "blocks"

SUMMARY_LANGUAGES = [
    ("German",     "Deutsch"),
    ("English",    "English"),
    ("French",     "Français"),
    ("Spanish",    "Español"),
    ("Italian",    "Italiano"),
    ("Portuguese", "Português"),
    ("Dutch",      "Nederlands"),
    ("Polish",     "Polski"),
    ("Turkish",    "Türkçe"),
]


def get_db():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST"),
        dbname=os.environ.get("DB_NAME"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        port=os.environ.get("DB_PORT"),
    )


def _run_once(migration_id: str, stmts: list):
    """Run SQL statements exactly once, tracked via _migrations table."""
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM _migrations WHERE id = %s", (migration_id,))
        if cur.fetchone():
            return
        for stmt in stmts:
            cur.execute(stmt)
        cur.execute("INSERT INTO _migrations (id) VALUES (%s)", (migration_id,))
        conn.commit()
        print(f"Migration {migration_id}: done")
    except Exception as e:
        print(f"Migration {migration_id}: {e}")
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


def _migrate():
    # Schema (DDL) is owned by the init-db init container in deployment.yaml.
    # Only one-time data migrations belong here via _run_once.
    conn = None
    try:
        conn = get_db()
        conn.cursor().execute("CREATE TABLE IF NOT EXISTS _migrations (id TEXT PRIMARY KEY)")
        conn.commit()
    except Exception as e:
        print(f"Migration: _migrations table → {e}")
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

    _run_once("v1.9.0-cat-reset", [
        "UPDATE articles SET category = NULL",
        "UPDATE sources SET category = NULL",
    ])

_migrate()


def _loading_slot(feed_id: str, feed_name: str, category: str = "") -> str:
    name_e = html.escape(feed_name)
    cat_attr = f' data-categories="{html.escape(category)}"' if category else ''
    return (
        f'<div id="feed-{feed_id}"{cat_attr}>'
        f'<div hx-get="/api/block/{feed_id}" hx-trigger="load" hx-swap="outerHTML">'
        f'<div class="source-block">'
        f'<div class="source-title">{name_e}</div>'
        f'<div class="article loading-article">'
        f'<span class="loading-msg">&#x27F3; Loading...</span>'
        f'</div></div></div></div>'
    )


def render_row(feed_id: int, name: str, url: str, enabled: bool, interval: int = 360, max_articles: int = 5) -> str:
    status_label = "ENABLED" if enabled else "DISABLED"
    row_class    = "cfg-row-enabled" if enabled else "cfg-row-disabled"
    btn_class    = "toggle-btn-on" if enabled else "toggle-btn-off"
    url_e  = html.escape(url)
    name_e = html.escape(name)
    return f"""<tr class="{row_class}">
  <td class="cfg-name">
    {name_e}
    <div class="cfg-url-sub"><a href="{url_e}" target="_blank">{url_e}</a></div>
  </td>
  <td class="cfg-interval">
    <input class="cfg-input cfg-input-interval" type="number" min="1" max="10080"
           name="interval" value="{interval}"
           hx-post="/api/interval"
           hx-vals='{{"url": "{url_e}"}}'
           hx-trigger="change"
           hx-swap="none">
    <span class="interval-unit">min</span>
  </td>
  <td class="cfg-interval">
    <input class="cfg-input cfg-input-interval" type="number" min="1" max="20"
           name="max_articles" value="{max_articles}"
           hx-post="/api/max-articles"
           hx-vals='{{"url": "{url_e}"}}'
           hx-trigger="change"
           hx-swap="none">
    <span class="interval-unit">art</span>
  </td>
  <td class="cfg-actions">
    <button class="toggle-btn {btn_class}"
            hx-post="/api/toggle"
            hx-vals='{{"url": "{url_e}"}}'
            hx-target="closest tr"
            hx-swap="outerHTML">
      {status_label}
    </button>
    <button class="delete-btn"
            hx-post="/api/delete"
            hx-vals='{{"url": "{url_e}"}}'
            hx-target="closest tr"
            hx-swap="outerHTML"
            hx-confirm="[{name_e}] wirklich löschen?">
      DEL
    </button>
  </td>
</tr>"""


def render_add_form(error: str = "") -> str:
    error_html = f'<span class="add-error">&#x26A0; {html.escape(error)}</span>' if error else ""
    return f"""<div class="add-form">
  <form hx-post="/api/add"
        hx-target="#add-form-area"
        hx-swap="innerHTML">
    <input class="cfg-input cfg-input-name" type="text" name="name"
           placeholder="Name (optional, derived from feed)">
    <input class="cfg-input cfg-input-url" type="url" name="url"
           placeholder="https://example.com/feed.rss" required>
    <button class="toggle-btn toggle-btn-on" type="submit">ADD</button>
    <button class="toggle-btn toggle-btn-off" type="button"
            onclick="document.getElementById('add-form-area').innerHTML=''">CANCEL</button>
  </form>
  {error_html}
</div>"""


@app.get("/api/sources")
def sources():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, url, enabled, sync_interval_minutes, max_articles FROM sources ORDER BY name")
        rows = cur.fetchall()
        return "".join(render_row(fid, name, url, enabled, interval, max_art) for fid, name, url, enabled, interval, max_art in rows)
    finally:
        conn.close()


@app.get("/api/add-form")
def add_form():
    return render_add_form()


@app.post("/api/add")
def add():
    url  = request.form.get("url", "").strip()
    name = request.form.get("name", "").strip()

    if not url:
        return render_add_form(error="URL is required.")

    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        return render_add_form(error=f"Feed unreachable or invalid RSS feed: {url}")

    if not name:
        name = feed.feed.get("title", url)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sources (name, url, enabled) VALUES (%s, %s, true) "
            "ON CONFLICT (url) DO NOTHING RETURNING id, name, url, enabled, sync_interval_minutes",
            (name, url),
        )
        conn.commit()
        row = cur.fetchone()
        if row is None:
            return render_add_form(error="This URL already exists in the database.")
        fid, fname, furl, fenabled, finterval = row
        Path(f"{SYNC_FEED_PREFIX}{fid}").touch()
        table_row = f'<tr hx-swap-oob="beforeend:#cfg-tbody">{render_row(fid, fname, furl, fenabled, finterval)}</tr>'
        feed_slot  = f'<div hx-swap-oob="beforeend:#feed-list">{_loading_slot(str(fid), fname)}</div>'
        # category wird beim nächsten Generator-Lauf automatisch klassifiziert
        _rebuild_shell()
        return table_row + feed_slot
    except Exception as e:
        return render_add_form(error=f"DB error: {e}")
    finally:
        conn.close()


@app.post("/api/toggle")
def toggle():
    url  = request.form.get("url")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sources SET enabled = NOT enabled WHERE url = %s "
            "RETURNING id, name, url, enabled, sync_interval_minutes, max_articles, category",
            (url,),
        )
        conn.commit()
        fid, fname, furl, fenabled, finterval, fmax, fcat = cur.fetchone()
        table_row = render_row(fid, fname, furl, fenabled, finterval, fmax)
        if fenabled:
            Path(f"{SYNC_FEED_PREFIX}{fid}").touch()
            oob = f'<div hx-swap-oob="beforeend:#feed-list">{_loading_slot(str(fid), fname, fcat or "")}</div>'
        else:
            (BLOCK_DIR / f"{fid}.html").unlink(missing_ok=True)
            oob = f'<div id="feed-{fid}" hx-swap-oob="delete"></div>'
        _rebuild_shell()
        return table_row + oob
    finally:
        conn.close()


@app.post("/api/delete")
def delete():
    url  = request.form.get("url")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sources WHERE url = %s RETURNING id", (url,))
        conn.commit()
        row = cur.fetchone()
        if row:
            (BLOCK_DIR / f"{row[0]}.html").unlink(missing_ok=True)
    finally:
        conn.close()
    _rebuild_shell()
    return ""


def _rebuild_shell():
    shell_path = Path(OUTPUT_FILE)
    if not shell_path.exists():
        return
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, category FROM sources WHERE enabled=true ORDER BY name")
        feeds = cur.fetchall()
    finally:
        conn.close()
    slots = "\n".join(_loading_slot(str(fid), fname, cat or "") for fid, fname, cat in feeds)
    if not slots:
        slots = '<p style="color:var(--muted);font-size:12px;padding:2rem 0">No feeds configured. Add feeds via [CONFIG].</p>'
    content = shell_path.read_text(encoding="utf-8")
    marker = '<div id="feed-list">'
    end_marker = '</div>\n\n  <footer>'
    idx = content.find(marker)
    idx_end = content.find(end_marker, idx)
    if idx == -1 or idx_end == -1:
        return
    shell_path.write_text(
        content[:idx + len(marker)] + '\n    ' + slots + '\n  ' + content[idx_end:],
        encoding="utf-8",
    )


@app.post("/api/article/<article_id>/delete")
def delete_article(article_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM articles WHERE id = %s", (article_id,))
        conn.commit()
    finally:
        conn.close()
    return ""


@app.get("/api/block/<block_id>")
def block(block_id):
    block_file = BLOCK_DIR / f"{block_id}.html"
    if block_file.exists():
        return block_file.read_text(encoding="utf-8")

    # Block noch nicht fertig — Feed-Name aus DB holen und Loading-Placeholder zurückgeben
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sources WHERE id = %s", (block_id,))
        row = cur.fetchone()
        name_e = html.escape(row[0]) if row else f"Feed {block_id}"
    finally:
        conn.close()

    return (
        f'<div hx-get="/api/block/{block_id}" hx-trigger="every 5s" hx-swap="outerHTML">'
        f'<div class="source-block">'
        f'<div class="source-title">{name_e}</div>'
        f'<div class="article loading-article">'
        f'<span class="loading-msg">&#x27F3; Loading...</span>'
        f'</div></div></div>'
    )


SYNC_TRIGGER     = Path("/html/.sync-now")
SYNC_FEED_PREFIX = "/html/.sync-feed-"


@app.post("/api/sync/<feed_id>")
def sync_feed(feed_id):
    # Block-Datei löschen damit Polling sofort Loading-State zeigt
    (BLOCK_DIR / f"{feed_id}.html").unlink(missing_ok=True)
    # Per-Feed Trigger schreiben
    Path(f"{SYNC_FEED_PREFIX}{feed_id}").touch()

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sources WHERE id = %s", (feed_id,))
        row = cur.fetchone()
        name_e = html.escape(row[0]) if row else f"Feed {feed_id}"
    finally:
        conn.close()

    return (
        f'<div hx-get="/api/block/{feed_id}" hx-trigger="load" hx-swap="outerHTML">'
        f'<div class="source-block">'
        f'<div class="source-title">{name_e}</div>'
        f'<div class="article loading-article">'
        f'<span class="loading-msg">&#x27F3; Loading...</span>'
        f'</div></div></div>'
    )


@app.post("/api/sync")
def sync():
    SYNC_TRIGGER.touch()
    return _sync_badge(pending=True)


@app.get("/api/sync/status")
def sync_status():
    return _sync_badge(pending=SYNC_TRIGGER.exists())


def _sync_badge(pending: bool) -> str:
    if pending:
        return (
            '<span class="sync-badge sync-badge-running"'
            ' hx-get="/api/sync/status" hx-trigger="every 5s"'
            ' hx-target="#sync-status" hx-swap="innerHTML">&#x27F3; SYNC RUNNING...</span>'
        )
    return '<span class="sync-badge sync-badge-idle">&#x25A0; IDLE</span>'


@app.post("/api/interval")
def set_interval():
    url = request.form.get("url", "")
    try:
        minutes = max(1, min(10080, int(request.form.get("interval", 360))))
    except (TypeError, ValueError):
        minutes = 360
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sources SET sync_interval_minutes = %s WHERE url = %s",
            (minutes, url),
        )
        conn.commit()
    finally:
        conn.close()
    return "", 204


def _test_llm(url: str, api_key: str) -> str:
    try:
        r = http_requests.get(
            f"{url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            model = data[0].get("id") or data[0].get("aliases", ["?"])[0]
            return f"&#x2713; Connected &mdash; Model: {html.escape(str(model))}"
        return "&#x2713; Connected (no models returned)"
    except Exception as e:
        return f"&#x26A0; Connection test failed: {html.escape(str(e))}"


def _lang_options(current: str) -> str:
    parts = []
    for val, native in SUMMARY_LANGUAGES:
        sel   = ' selected' if val == current else ''
        label = f"{val} — {native}" if native != val else val
        parts.append(f'<option value="{html.escape(val)}"{sel}>{html.escape(label)}</option>')
    return "\n".join(parts)


def render_llm_form(url: str = "", api_key: str = "", summary_language: str = "German",
                    error: str = "", success: str = "", test_result: str = "") -> str:
    url_e    = html.escape(url)
    ak_e     = html.escape(api_key)
    msg_html = ""
    if error:
        msg_html = f'<div class="add-error" style="margin-top:.5rem">&#x26A0; {html.escape(error)}</div>'
    elif success:
        msg_html = f'<div class="add-error" style="color:var(--green);margin-top:.5rem">&#x2713; {html.escape(success)}</div>'
    test_html = f'<div class="add-error" style="margin-top:.25rem;color:{"var(--green)" if test_result.startswith("&#x2713;") else "var(--red)"}">{test_result}</div>' if test_result else ""
    return f"""<div class="add-form">
  <form hx-post="/api/llm-config"
        hx-target="#llm-form-area"
        hx-swap="innerHTML">
    <input class="cfg-input" style="width:300px" type="text" name="url"
           placeholder="http://host:port/v1" value="{url_e}" required>
    <input class="cfg-input" style="width:180px" type="password" name="api_key"
           placeholder="API Key (or 'none')" value="{ak_e}">
    <select class="cfg-input" name="summary_language" style="width:180px">
      {_lang_options(summary_language)}
    </select>
    <button class="toggle-btn toggle-btn-on" type="submit">SAVE</button>
    <button class="toggle-btn toggle-btn-off" type="button"
            onclick="document.getElementById('llm-form-area').innerHTML=''">CANCEL</button>
  </form>
  {msg_html}{test_html}
</div>"""


@app.get("/api/llm-form")
def llm_form():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT url, api_key, summary_language FROM llm_config LIMIT 1")
        row             = cur.fetchone()
        url             = row[0] if row else ""
        api_key         = row[1] if row else ""
        summary_language = row[2] if row else "German"
    except Exception:
        url, api_key, summary_language = "", "", "German"
    finally:
        conn.close()
    return render_llm_form(url, api_key, summary_language)


@app.post("/api/llm-config")
def save_llm_config():
    url              = request.form.get("url", "").strip()
    api_key          = request.form.get("api_key", "none").strip() or "none"
    summary_language = request.form.get("summary_language", "German").strip()
    if not url:
        return render_llm_form(error="URL is required.")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT summary_language FROM llm_config LIMIT 1")
        row           = cur.fetchone()
        old_language  = row[0] if row else None
        cur.execute(
            "UPDATE llm_config SET url = %s, api_key = %s, summary_language = %s",
            (url, api_key, summary_language),
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO llm_config (url, api_key, summary_language) VALUES (%s, %s, %s)",
                (url, api_key, summary_language),
            )
        if old_language and old_language != summary_language:
            cur.execute("UPDATE articles SET summary_llm = NULL, title_translated = NULL")
            print(f"Language changed {old_language} → {summary_language}: articles reset for re-translation")
        conn.commit()
    except Exception as e:
        return render_llm_form(url, api_key, summary_language, error=f"DB error: {e}")
    finally:
        conn.close()
    test_result = _test_llm(url, api_key)
    return render_llm_form(url, api_key, summary_language, success="Saved. Active on next sync.", test_result=test_result)


@app.post("/api/max-articles")
def set_max_articles():
    url = request.form.get("url", "")
    try:
        count = max(1, min(20, int(request.form.get("max_articles", 5))))
    except (TypeError, ValueError):
        count = 5
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sources SET max_articles = %s WHERE url = %s",
            (count, url),
        )
        conn.commit()
    finally:
        conn.close()
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
