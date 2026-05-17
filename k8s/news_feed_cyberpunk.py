"""
News Feed Aggregator mit lokalem LLM
=====================================
Liest RSS-Feeds, fasst neue Artikel auf Deutsch zusammen, speichert sie
in der DB und schreibt pro Feed eine Block-Datei für die HTMX-Shell.
"""

import feedparser
import requests
import datetime
import html
import re
import psycopg2
import os

from pathlib import Path

# ─────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────

LLM_BASE_URL      = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
API_KEY           = "none"
LLM_MODEL         = "unknown"
SUMMARY_LANGUAGE  = "German"

MAX_ARTICLES_PER_FEED = 5

OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "news_zusammenfassung.html")
BLOCK_DIR   = Path(OUTPUT_FILE).parent / "blocks"


# ─────────────────────────────────────────────
# DATENBANK
# ─────────────────────────────────────────────

def init_llm_config():
    global LLM_BASE_URL, API_KEY, LLM_MODEL, SUMMARY_LANGUAGE
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT url, api_key, summary_language FROM llm_config LIMIT 1")
            row = cur.fetchone()
            if row and row[0]:
                LLM_BASE_URL     = row[0]
                API_KEY          = row[1] or "none"
                SUMMARY_LANGUAGE = row[2] or "German"
                print(f"  LLM-Config aus DB: {LLM_BASE_URL}, language: {SUMMARY_LANGUAGE}")
        finally:
            conn.close()
    except Exception as e:
        print(f"  LLM-Config DB-Fehler, nutze Env-Var: {e}")
    try:
        res = requests.get(f"{LLM_BASE_URL}/models").json()
        raw = res["data"][0]["aliases"][0]
        LLM_MODEL = re.sub(r"[^a-zA-Z0-9-]", "", raw)
        print(f"  LLM-Modell: {LLM_MODEL}")
    except Exception as e:
        print(f"  LLM-Modell konnte nicht ermittelt werden: {e}")


def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST"),
        dbname=os.environ.get("DB_NAME"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        port=os.environ.get("DB_PORT"),
    )

def get_sources_from_db() -> list:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, url, sync_interval_minutes, last_fetched_at, max_articles, category
            FROM sources
            WHERE enabled = true
            ORDER BY name
        """)
        return [
            {
                "id":                    row[0],
                "name":                  row[1],
                "url":                   row[2],
                "sync_interval_minutes": row[3],
                "last_fetched_at":       row[4],
                "max_articles":          row[5],
                "category":              row[6],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()

def update_last_fetched(source_url: str):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sources SET last_fetched_at = now() WHERE url = %s",
            (source_url,),
        )
        conn.commit()
    finally:
        conn.close()

def store_new_articles(source_id: str, rss_articles: list) -> int:
    """Summarizes and stores only articles not yet in DB. Returns count of new articles."""
    if not rss_articles:
        return 0

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        urls = [a["url"] for a in rss_articles]
        # Nur Artikel mit vorhandener Summary als "fertig" betrachten —
        # leere Summary (LLM-Leerantwort) wird beim nächsten Sync neu versucht.
        cur.execute(
            "SELECT url FROM articles WHERE url = ANY(%s) AND summary_llm IS NOT NULL AND summary_llm != ''",
            (urls,),
        )
        existing = {row[0] for row in cur.fetchall()}

        new_count = 0
        for art in rss_articles:
            if art["url"] in existing:
                continue

            print(f"    → Zusammenfasse: {art['title'][:60]}...")
            summary, category, title_translated = summarize_and_classify(art["title"], art["text"])
            llm_error = summary.startswith("[Fehler")

            cur.execute(
                """INSERT INTO articles
                       (source_id, title_original, url, summary_llm, category,
                        title_translated, llm_error, published_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (url) DO UPDATE
                     SET summary_llm       = EXCLUDED.summary_llm,
                         category          = EXCLUDED.category,
                         title_translated  = EXCLUDED.title_translated,
                         llm_error         = EXCLUDED.llm_error
                     WHERE articles.summary_llm IS NULL OR articles.summary_llm = ''""",
                (source_id, art["title"], art["url"], summary, category or None,
                 title_translated or None, llm_error, art["published_at"]),
            )
            conn.commit()
            new_count += 1

        # Backfill: classify existing articles that have summaries but no category yet
        cur.execute(
            "SELECT id, title_original FROM articles "
            "WHERE source_id = %s AND category IS NULL "
            "  AND summary_llm IS NOT NULL AND summary_llm != '' "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 20",
            (source_id,)
        )
        backfill_rows = cur.fetchall()
        if backfill_rows:
            print(f"    → Backfill {len(backfill_rows)} Artikel ohne Kategorie...")
            for art_id, art_title in backfill_rows:
                if not art_title:
                    continue
                cat = classify_title(art_title)
                if cat:
                    cur.execute("UPDATE articles SET category = %s WHERE id = %s", (cat, art_id))
            conn.commit()

        # Backfill: translate titles that have no translated title yet
        cur.execute(
            "SELECT id, title_original FROM articles "
            "WHERE source_id = %s AND title_translated IS NULL "
            "  AND summary_llm IS NOT NULL AND summary_llm != '' "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 20",
            (source_id,)
        )
        title_rows = cur.fetchall()
        if title_rows:
            print(f"    → Backfill {len(title_rows)} Titel ohne Übersetzung...")
            for art_id, art_title in title_rows:
                if not art_title:
                    continue
                translated = translate_title(art_title)
                cur.execute("UPDATE articles SET title_translated = %s WHERE id = %s", (translated, art_id))
            conn.commit()

        # Clean up stranded articles: NULL/empty summary AND no longer in current RSS feed.
        # These will never be re-processed, so they just block display slots.
        if rss_articles:
            current_urls = [a["url"] for a in rss_articles]
            cur.execute(
                "DELETE FROM articles WHERE source_id = %s "
                "  AND (summary_llm IS NULL OR summary_llm = '') "
                "  AND url != ALL(%s)",
                (source_id, current_urls),
            )
            if cur.rowcount:
                print(f"    → {cur.rowcount} verwaiste Artikel bereinigt")
            conn.commit()

        return new_count
    finally:
        conn.close()

def load_block_articles(source_id: str, limit: int = MAX_ARTICLES_PER_FEED) -> list:
    """Loads the latest articles for a source from DB."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, title_original, url, summary_llm, llm_error, category, title_translated
               FROM articles
               WHERE source_id = %s
                 AND summary_llm IS NOT NULL AND summary_llm != ''
               ORDER BY COALESCE(published_at, fetched_at) DESC
               LIMIT %s""",
            (source_id, limit),
        )
        return [
            {
                "id":               str(row[0]),
                "title":            row[1],
                "link":             row[2],
                "summary":          row[3],
                "llm_error":        row[4],
                "category":         row[5],
                "title_translated": row[6],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def update_feed_categories(source_id) -> list:
    """Aggregates distinct article categories and stores them in sources.category."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT category FROM articles "
            "WHERE source_id = %s AND category IS NOT NULL AND category != '' "
            "ORDER BY category",
            (source_id,)
        )
        cats = [row[0] for row in cur.fetchall()]
        cur.execute(
            "UPDATE sources SET category = %s WHERE id = %s",
            (",".join(cats) if cats else None, source_id)
        )
        conn.commit()
        return cats
    finally:
        conn.close()


# ─────────────────────────────────────────────
# LLM ZUSAMMENFASSUNG
# ─────────────────────────────────────────────

def _extract_category_word(raw: str) -> str:
    """Extract a single clean title-cased word from messy LLM category output."""
    # Strip common LLM meta-prefixes ("Category: ...", "Topic: ...")
    raw = re.sub(r'^(category|topic|theme|tag)\s*[:=]\s*', '', raw, flags=re.IGNORECASE).strip()
    # Split on any non-alphabetic character to get tokens
    tokens = [t for t in re.split(r'[^a-zA-ZÀ-ɏ]+', raw) if t]
    if not tokens:
        return ""
    first = tokens[0]
    # Split CamelCase (e.g. "AIAgeVerification" → "AI Age Verification")
    spaced = re.sub(r'(?<=[A-ZÀ-ɏ])(?=[A-ZÀ-ɏ][a-zÀ-ɏ])|(?<=[a-zÀ-ɏ])(?=[A-ZÀ-ɏ])', ' ', first)
    word = spaced.split()[0] if spaced.split() else first
    return word[0].upper() + word[1:].lower() if word else ""


def _parse_llm_response(text: str) -> tuple:
    """Parse CATEGORY:/TITLE:/SUMMARY: format, returning (summary, category, title_translated)."""
    category         = ""
    title_translated = ""
    summary_lines    = []
    in_summary       = False
    for line in text.strip().splitlines():
        upper = line.upper()
        if not category and upper.startswith("CATEGORY:"):
            category = _extract_category_word(line[9:].strip())
        elif not title_translated and upper.startswith("TITLE:"):
            title_translated = line[6:].strip()
        elif upper.startswith("SUMMARY:"):
            summary_lines.append(line[8:].strip())
            in_summary = True
        elif in_summary:
            summary_lines.append(line)
    summary = "\n".join(summary_lines).strip()
    if not summary:
        summary = text  # fallback: use whole response
    return summary, category, title_translated


def summarize_and_classify(title: str, text: str) -> tuple:
    """Summarize and translate article title+summary, assign free-form English category."""
    prompt = (
        f"Translate the title and summarize the following news article in {SUMMARY_LANGUAGE}. "
        f"Also choose ONE single English word as the topic category.\n\n"
        f"Respond in exactly this format — no other text:\n"
        f"CATEGORY: Technology\n"
        f"TITLE: Translated article title\n"
        f"SUMMARY: 2-3 sentence summary.\n\n"
        f"Replace 'Technology' with one word from examples like: "
        f"Politics, Security, Sport, Science, Finance, Health, AI, Business, Military, Law\n"
        f"Translate TITLE and SUMMARY to {SUMMARY_LANGUAGE}.\n\n"
        f"Article title: {title}\n\n"
        f"Article content: {text[:2000]}"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 350,
        "temperature": 0.3,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    try:
        response = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()
        return _parse_llm_response(raw)
    except Exception as e:
        return f"[Fehler bei der Zusammenfassung: {e}]", "", ""


def translate_title(title: str) -> str:
    """Translate article title to SUMMARY_LANGUAGE (for backfill)."""
    prompt = (
        f"Translate this news headline to {SUMMARY_LANGUAGE}. "
        f"Reply with only the translated headline, no explanation.\n\n{title}"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 80,
        "temperature": 0.1,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip() or title
    except Exception:
        return title


def classify_title(title: str) -> str:
    """Quick classification: title → single English category word (for backfill)."""
    prompt = (
        f"Classify this news article with ONE English word.\n"
        f"Examples: Technology, Politics, Security, Sport, Science, Finance, Health, AI, Military, Law\n\n"
        f"Title: {title}\n\n"
        f"Reply with the single category word only:"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 10,
        "temperature": 0.1,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        return _extract_category_word(raw)
    except Exception:
        return ""


# ─────────────────────────────────────────────
# RSS ABRUF
# ─────────────────────────────────────────────

def fetch_feed(feed_info: dict) -> list:
    print(f"  → Lade Feed: {feed_info['name']} ...")
    limit = feed_info.get("max_articles", MAX_ARTICLES_PER_FEED)
    try:
        parsed = feedparser.parse(feed_info["url"])
        articles = []
        for entry in parsed.entries[:limit]:
            title   = entry.get("title", "Kein Titel")
            link    = entry.get("link", "#")
            summary = entry.get("summary", entry.get("description", ""))
            clean   = re.sub(r"<[^>]+>", "", summary).strip()

            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            published_at = (
                datetime.datetime(*pub[:6], tzinfo=datetime.timezone.utc) if pub else None
            )

            articles.append({
                "title": title,
                "url": link,
                "text": clean,
                "published_at": published_at,
            })
        return articles
    except Exception as e:
        print(f"    ✗ Fehler beim Laden: {e}")
        return []


# ─────────────────────────────────────────────
# HTML — SHELL SEITE
# ─────────────────────────────────────────────

HTML_SHELL = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NEWS // FEED — {date}</title>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:      #020408;
      --bg2:     #060d14;
      --bg3:     #0a1520;
      --cyan:    #00f5ff;
      --pink:    #ff006e;
      --green:   #00ff9d;
      --red:     #ff2d2d;
      --dim:     #1a2a3a;
      --text:    #9ef7d5;
      --muted:   #4a6a7a;
      --mono:    'Roboto Mono bold', monospace;
      --display: 'Orbitron', monospace;
      --scanline: repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(0,245,255,0.015) 2px,
        rgba(0,245,255,0.015) 4px
      );
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--mono);
      min-height: 100vh;
      overflow-x: hidden;
      padding: 0 0 4rem;
    }}

    body::before {{
      content: '';
      position: fixed;
      inset: 0;
      background: var(--scanline);
      pointer-events: none;
      z-index: 1000;
      animation: scanmove 8s linear infinite;
    }}

    @keyframes scanmove {{
      0%   {{ background-position: 0 0; }}
      100% {{ background-position: 0 200px; }}
    }}

    body::after {{
      content: '';
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(rgba(0,245,255,0.04) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0,245,255,0.04) 1px, transparent 1px);
      background-size: 40px 40px;
      pointer-events: none;
      z-index: 0;
    }}

    .page-layout {{
      position: relative;
      z-index: 1;
      max-width: 1140px;
      margin: 0 auto;
      display: flex;
      align-items: flex-start;
    }}

    .sidebar {{
      width: 155px;
      flex-shrink: 0;
      border-right: 1px solid var(--dim);
      padding: 1.5rem 0 4rem;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
    }}

    .sidebar-label {{
      font-size: 9px;
      letter-spacing: 0.18em;
      color: var(--muted);
      padding: 0 1rem 0.75rem;
    }}

    .cat-btn {{
      display: block;
      width: 100%;
      text-align: left;
      padding: 0.4rem 1rem;
      background: none;
      border: none;
      border-left: 2px solid transparent;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      cursor: pointer;
      transition: color 0.15s, border-color 0.15s;
    }}
    .cat-btn:hover {{ color: var(--text); }}
    .cat-btn.active {{
      color: var(--cyan);
      border-left-color: var(--cyan);
      text-shadow: 0 0 8px rgba(0,245,255,0.3);
    }}

    .wrap {{ flex: 1; min-width: 0; padding: 0 1.5rem; }}

    /* ─── HEADER ─────────────────────────────── */
    header {{
      border-bottom: 1px solid var(--dim);
      padding: 1.25rem 1.5rem;
      position: relative;
      display: flex;
      align-items: center;
      gap: 1.5rem;
      background: linear-gradient(90deg, rgba(0,245,255,0.04) 0%, transparent 60%);
      flex-wrap: wrap;
    }}

    header::after {{
      content: '';
      position: absolute;
      bottom: -2px; left: 0;
      width: 40%; height: 2px;
      background: linear-gradient(90deg, var(--cyan), transparent);
    }}

    .logo {{
      font-family: var(--display);
      font-size: 22px;
      font-weight: 900;
      letter-spacing: 0.12em;
      color: var(--cyan);
      text-shadow: 0 0 20px rgba(0,245,255,0.6), 0 0 60px rgba(0,245,255,0.2);
      animation: flicker 6s infinite;
      flex-shrink: 0;
    }}

    .logo span {{ color: var(--pink); text-shadow: 0 0 20px rgba(255,0,110,0.6); }}

    @keyframes flicker {{
      0%,100% {{ opacity: 1; }}
      92%      {{ opacity: 1; }}
      93%      {{ opacity: 0.4; }}
      94%      {{ opacity: 1; }}
      96%      {{ opacity: 0.7; }}
      97%      {{ opacity: 1; }}
    }}

    .header-meta {{
      display: flex;
      gap: 1.5rem;
      font-size: 11px;
      color: var(--muted);
      flex-wrap: wrap;
    }}

    .header-meta .val {{ color: var(--green); }}

    .dot {{
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--green);
      display: inline-block;
      margin-right: 6px;
      box-shadow: 0 0 6px var(--green);
      animation: pdot 2s infinite;
    }}

    @keyframes pdot {{
      0%,100% {{ opacity: 1; box-shadow: 0 0 6px var(--green); }}
      50%      {{ opacity: 0.3; box-shadow: none; }}
    }}

    /* ─── STATUS BAR ─────────────────────────── */
    .statusbar {{
      background: var(--bg2);
      border-bottom: 1px solid var(--dim);
      padding: 0.4rem 1.5rem;
      font-size: 11px;
      color: var(--muted);
      display: flex;
      gap: 2rem;
      overflow-x: auto;
      white-space: nowrap;
    }}

    .statusbar .prompt {{ color: var(--green); }}

    /* ─── SECTION LABEL ──────────────────────── */
    .section-label {{
      font-size: 10px;
      letter-spacing: 0.18em;
      color: var(--muted);
      padding: 1.5rem 0 0.75rem;
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}

    .section-label::after {{
      content: '';
      flex: 1;
      height: 1px;
      background: linear-gradient(90deg, var(--dim), transparent);
    }}

    /* ─── SOURCE BLOCK ───────────────────────── */
    .source-block {{ margin-bottom: 2.5rem; }}

    .source-title {{
      font-family: var(--display);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.12em;
      color: var(--cyan);
      text-shadow: 0 0 12px rgba(0,245,255,0.4);
      padding: 0.6rem 1rem;
      border-left: 3px solid var(--cyan);
      background: rgba(0,245,255,0.04);
      margin-bottom: 1px;
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}

    .source-title::before {{
      content: '[RSS]';
      font-size: 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-weight: 400;
      letter-spacing: 0.08em;
    }}

    /* ─── ARTICLE CARD ───────────────────────── */
    .article {{
      background: #060d14;
      border: 1px solid var(--dim);
      border-left: 2px solid var(--pink);
      border-top: none;
      padding: 1rem 1.25rem;
      margin-bottom: 1px;
      position: relative;
      overflow: hidden;
      transition: background-color 0.15s;
    }}

    .article::after {{
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 1px;
      background: linear-gradient(90deg, var(--pink), transparent 60%);
      opacity: 0;
      transition: opacity 0.2s;
    }}

    .article:hover {{
      background-color: #0a1520;
      background-image: repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(0,245,255,0.12) 2px,
        rgba(0,245,255,0.12) 4px
      );
    }}

    .article:hover::after {{ opacity: 1; }}

    .article::before {{
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 1px;
      background: linear-gradient(90deg, var(--pink), transparent 60%);
      opacity: 0;
      transition: opacity 0.2s;
    }}

    .article:hover::before {{ opacity: 1; }}

    .article h3 {{
      font-family: var(--display);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      margin-bottom: 0.5rem;
      line-height: 1.4;
    }}

    .article h3 a {{
      color: var(--green);
      text-decoration: none;
      transition: color 0.15s;
    }}

    .article:hover h3 a {{
      animation: glitch 0.3s steps(2) 1;
      color: var(--green);
    }}

    @keyframes glitch {{
      0%   {{ text-shadow: 2px 0 var(--pink), -2px 0 var(--cyan); }}
      25%  {{ text-shadow: -2px 0 var(--pink), 2px 0 var(--cyan); }}
      50%  {{ text-shadow: 2px 0 var(--cyan), -2px 0 var(--pink); }}
      75%  {{ text-shadow: -2px 0 var(--cyan), 2px 0 var(--pink); }}
      100% {{ text-shadow: none; color: var(--cyan); }}
    }}

    .article h3 a:hover {{
      color: var(--cyan);
      text-shadow: 0 0 10px rgba(0,245,255,0.4);
    }}

    .summary {{
      font-size: 12px;
      color: var(--text);
      line-height: 1.7;
    }}

    .error {{
      font-size: 12px;
      color: var(--red);
      font-style: italic;
    }}

    /* ─── ARTICLE CATEGORY LABEL ────────────── */
    .art-cat {{
      position: absolute;
      top: 0.35rem;
      right: 0.5rem;
      font-size: 9px;
      letter-spacing: 0.15em;
      color: var(--cyan);
      font-family: var(--mono);
      white-space: nowrap;
      padding-bottom: 3px;
    }}
    /* horizontal line: ~1 char left of text-left → article right edge */
    .art-cat::after {{
      content: '';
      position: absolute;
      bottom: 0;
      left: -1ch;
      right: -0.5rem;
      height: 1px;
      background: var(--cyan);
      box-shadow: 0 0 6px rgba(0,245,255,0.9), 0 0 12px rgba(0,245,255,0.5);
      pointer-events: none;
    }}
    /* diagonal: 6px gap then 45° up-left to article top (clipped by overflow:hidden) */
    .art-cat::before {{
      content: '';
      position: absolute;
      bottom: 0;
      right: calc(100% + 4px);
      width: 2rem;
      height: 2rem;
      background: linear-gradient(45deg,
        transparent calc(50% - 1px),
        var(--cyan) 50%,
        transparent calc(50% + 1px));
      filter: drop-shadow(0 0 4px rgba(0,245,255,0.9)) drop-shadow(0 0 8px rgba(0,245,255,0.5));
      pointer-events: none;
    }}

    /* ─── ARTICLE DELETE BUTTON ─────────────── */
    .article-del-btn {{
  display: block;
  width: auto;
  margin-left: auto;
  text-align: right;

  position: relative;
  right: -0.7rem;

  padding: 0.4rem 0 0 0;
  margin-top: 0.6rem;

  background: none;
  border: none;
  border-top: 1px solid transparent;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  cursor: pointer;
  opacity: 0;
  transition: color 0.15s, opacity 0.15s, border-color 0.15s;
    }}
    .article:hover .article-del-btn {{ opacity: 1; border-top-color: var(--dim); }}
    .article-del-btn:hover {{ color: var(--pink); border-top-color: rgba(255,0,110,0.35); }}

    /* ─── LOADING PLACEHOLDER ────────────────── */
    .loading-article {{
      border-left-color: var(--dim);
    }}

    .loading-msg {{
      font-size: 11px;
      color: var(--muted);
      letter-spacing: 0.15em;
      animation: pdot 1.5s infinite;
    }}

    /* ─── FOOTER ─────────────────────────────── */
    footer {{
      margin-top: 3rem;
      padding: 1rem 1.5rem;
      border-top: 1px solid var(--dim);
      text-align: center;
      font-size: 11px;
      color: var(--muted);
      position: relative;
    }}

    footer::before {{
      content: '';
      position: absolute;
      top: -2px; left: 50%;
      transform: translateX(-50%);
      width: 30%; height: 1px;
      background: linear-gradient(90deg, transparent, var(--pink), transparent);
    }}

    footer span {{ color: var(--pink); }}

    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--dim); }}
    ::-webkit-scrollbar-thumb:hover {{ background: var(--cyan); }}

    @media (max-width: 700px) {{
      .logo {{ font-size: 16px; }}
      .header-meta {{ display: none; }}
      .page-layout {{ display: block; }}
      .sidebar {{
        width: 100%; height: auto; position: static;
        border-right: none; border-bottom: 1px solid var(--dim);
        display: flex; flex-wrap: wrap; gap: 0.3rem;
        padding: 0.6rem 1rem;
      }}
      .sidebar-label {{ display: none; }}
      .cat-btn {{
        border-left: none;
        border: 1px solid var(--dim);
        padding: 0.2rem 0.6rem;
        width: auto;
      }}
      .cat-btn.active {{ border-color: var(--cyan); }}
    }}

    /* ─── CONFIG BUTTON ─────────────────────── */
    .cfg-btn {{
      margin-left: auto;
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.12em;
      color: var(--muted);
      background: none;
      border: 1px solid var(--dim);
      padding: 0.35rem 0.8rem;
      cursor: pointer;
      transition: color 0.15s, border-color 0.15s, box-shadow 0.15s;
      flex-shrink: 0;
    }}
    .cfg-btn:hover {{
      color: var(--cyan);
      border-color: var(--cyan);
      box-shadow: 0 0 8px rgba(0,245,255,0.3);
    }}

    /* ─── CONFIG PANEL ──────────────────────── */
    .cfg-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      z-index: 2000;
      background: rgba(2,4,8,0.85);
      backdrop-filter: blur(2px);
    }}
    .cfg-overlay.open {{ display: flex; align-items: flex-start; justify-content: center; padding-top: 5rem; }}

    .cfg-panel {{
      width: min(calc((820px + 2.75rem) * 1.08), 92vw);
      max-height: 70vh;
      background: var(--bg2);
      border: 1px solid var(--cyan);
      box-shadow: 0 0 40px rgba(0,245,255,0.15), 0 0 80px rgba(0,245,255,0.05);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}

    .cfg-header {{
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: 0.75rem 1.25rem;
      border-bottom: 1px solid var(--dim);
      background: linear-gradient(90deg, rgba(0,245,255,0.06) 0%, transparent 60%);
      flex-shrink: 0;
    }}

    .cfg-header-title {{
      font-family: var(--display);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.14em;
      color: var(--cyan);
      text-shadow: 0 0 12px rgba(0,245,255,0.4);
    }}

    .cfg-close {{
      margin-left: auto;
      background: none;
      border: none;
      color: var(--muted);
      font-size: 18px;
      cursor: pointer;
      line-height: 1;
      padding: 0.2rem 0.4rem;
      transition: color 0.15s;
    }}
    .cfg-close:hover {{ color: var(--pink); }}

    .cfg-body {{
      overflow-y: auto;
      flex: 1;
    }}

    .cfg-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}

    .cfg-table thead tr {{
      background: var(--bg3);
      border-bottom: 1px solid var(--dim);
    }}

    .cfg-table th {{
      text-align: left;
      padding: 0.5rem 1.25rem;
      font-size: 10px;
      letter-spacing: 0.15em;
      color: var(--muted);
      font-family: var(--mono);
      font-weight: 400;
    }}

    .cfg-table tbody tr {{
      border-bottom: 1px solid var(--dim);
      transition: background 0.1s;
    }}
    .cfg-table tbody tr:hover {{ background: rgba(0,245,255,0.04); }}

    .cfg-name {{
      padding: 0.65rem 1.25rem;
      color: var(--green);
      white-space: nowrap;
      width: 1%;
    }}

    .cfg-url-sub {{
      font-size: 10px;
      color: var(--muted);
      margin-top: 0.25rem;
      word-break: break-all;
      font-weight: 400;
      letter-spacing: 0;
    }}
    .cfg-url-sub a {{
      color: var(--muted);
      text-decoration: none;
      transition: color 0.15s;
    }}
    .cfg-url-sub a:hover {{
      color: var(--cyan);
      text-shadow: 0 0 6px rgba(0,245,255,0.3);
    }}

    .cfg-row-disabled {{ opacity: 0.45; }}

    .cfg-actions {{
      padding: 0.65rem 1.25rem;
      white-space: nowrap;
      width: 1%;
    }}

    .cfg-interval {{
      padding: 0.65rem 1.25rem;
      white-space: nowrap;
      width: 1%;
    }}
    .cfg-input-interval {{
      width: 62px;
      text-align: right;
    }}
    .interval-unit {{
      font-size: 10px;
      color: var(--muted);
      margin-left: 0.3rem;
    }}

    .toggle-btn {{
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: 0.12em;
      background: none;
      border: 1px solid;
      padding: 0.25rem 0.6rem;
      width: 5.5rem;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .toggle-btn-on {{
      color: var(--green);
      border-color: var(--green);
    }}
    .toggle-btn-on:hover {{
      background: rgba(0,255,157,0.1);
      box-shadow: 0 0 8px rgba(0,255,157,0.3);
    }}
    .toggle-btn-off {{
      color: var(--muted);
      border-color: var(--dim);
    }}
    .toggle-btn-off:hover {{
      color: var(--pink);
      border-color: var(--pink);
      box-shadow: 0 0 8px rgba(255,0,110,0.2);
    }}

    .htmx-request .toggle-btn {{ opacity: 0.4; pointer-events: none; }}

    .delete-btn {{
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: 0.12em;
      background: none;
      border: 1px solid var(--dim);
      color: var(--muted);
      padding: 0.25rem 0.6rem;
      cursor: pointer;
      transition: all 0.15s;
      margin-left: 0.4rem;
    }}
    .delete-btn:hover {{
      color: var(--pink);
      border-color: var(--pink);
      box-shadow: 0 0 8px rgba(255,0,110,0.2);
    }}

    .cfg-add-btn {{
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: 0.12em;
      color: var(--green);
      background: none;
      border: 1px solid var(--green);
      padding: 0.25rem 0.7rem;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .cfg-add-btn:hover {{
      background: rgba(0,255,157,0.08);
      box-shadow: 0 0 8px rgba(0,255,157,0.3);
    }}
    .cfg-add-btn.cfg-ghost {{
      color: var(--muted);
      border-color: var(--dim);
    }}
    .cfg-add-btn.cfg-ghost:hover {{
      color: var(--cyan);
      border-color: var(--cyan);
      background: none;
      box-shadow: 0 0 8px rgba(0,245,255,0.3);
    }}

    .add-form {{
      padding: 0.75rem 1.25rem;
      border-bottom: 1px solid var(--dim);
      background: rgba(0,245,255,0.02);
      display: flex;
      gap: 0.6rem;
      align-items: center;
      flex-wrap: wrap;
    }}

    .cfg-input {{
      font-family: var(--mono);
      font-size: 11px;
      background: var(--bg3);
      border: 1px solid var(--dim);
      color: var(--text);
      padding: 0.35rem 0.7rem;
      outline: none;
      transition: border-color 0.15s;
    }}
    .cfg-input:focus {{ border-color: var(--cyan); box-shadow: 0 0 6px rgba(0,245,255,0.2); }}
    .cfg-input-name {{ width: 180px; }}
    .cfg-input-url {{ flex: 1; min-width: 220px; }}

    .add-error {{
      width: 100%;
      color: var(--red);
      font-size: 11px;
      letter-spacing: 0.05em;
      padding-top: 0.2rem;
    }}

    /* ─── BLOCK TIMESTAMP ──────────────────── */
    .source-ts {{
      font-size: 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-weight: 400;
      letter-spacing: 0.05em;
    }}

    /* ─── BLOCK SYNC BUTTON ─────────────────── */
    .block-sync-btn {{
      margin-left: auto;
      font-family: var(--mono);
      font-size: 11px;
      background: none;
      border: 1px solid var(--dim);
      color: var(--muted);
      padding: 0.2rem 0.55rem;
      cursor: pointer;
      transition: all 0.15s;
      letter-spacing: 0.08em;
      flex-shrink: 0;
    }}
    .block-sync-btn:hover {{
      color: var(--cyan);
      border-color: var(--cyan);
      box-shadow: 0 0 6px rgba(0,245,255,0.3);
    }}
    .htmx-request .block-sync-btn {{ opacity: 0.4; pointer-events: none; }}

    /* ─── SYNC BUTTON ───────────────────────── */
    .sync-btn {{
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.12em;
      color: var(--cyan);
      background: none;
      border: 1px solid var(--cyan);
      padding: 0.35rem 0.8rem;
      cursor: pointer;
      transition: all 0.15s;
      flex-shrink: 0;
    }}
    .sync-btn:hover {{
      background: rgba(0,245,255,0.08);
      box-shadow: 0 0 10px rgba(0,245,255,0.3);
    }}
    .sync-badge-running {{
      font-size: 11px;
      color: var(--green);
      letter-spacing: 0.1em;
      animation: pdot 1.2s infinite;
    }}
    .sync-badge-idle {{ display: none; }}
  </style>
  <script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"></script>
</head>
<body>

<header>
  <div class="logo">NEWS<span>//</span>FEED</div>
  <div class="header-meta">
    <span><span class="dot"></span><span class="val">LIVE</span></span>
    <span>DATE <span class="val" id="hdr-date">{date}</span></span>
    <span>TIME <span class="val" id="hdr-time">{time} Uhr</span></span>
    <span>LLM <span class="val">{LLM_MODEL}</span></span>
  </div>
  <span id="sync-status"></span>
  <button class="sync-btn"
          hx-post="/api/sync"
          hx-target="#sync-status"
          hx-swap="innerHTML"
          hx-on::after-request="if(event.detail.elt===this) setTimeout(() => location.reload(), 500)">
    [SYNC NOW]
  </button>
  <button class="cfg-btn" onclick="openConfig()">[CONFIG]</button>
</header>

<!-- Config Overlay -->
<div class="cfg-overlay" id="cfgOverlay" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="cfg-panel">
    <div class="cfg-header">
      <span class="cfg-header-title">// RSS SOURCES</span>
      <button class="cfg-add-btn" onclick="loadAddForm()">+ ADD</button>
      <button class="cfg-add-btn cfg-ghost" onclick="loadLlmForm()">CONF LLM</button>
      <button class="cfg-close" onclick="document.getElementById('cfgOverlay').classList.remove('open')">&#x2715;</button>
    </div>
    <div id="add-form-area"></div>
    <div id="llm-form-area"></div>
    <div class="cfg-body">
      <table class="cfg-table">
        <thead>
          <tr><th>NAME / URL</th><th>INTERVAL</th><th>ARTICLES</th><th>ACTIONS</th></tr>
        </thead>
        <tbody id="cfg-tbody">
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
function cfgFetch(url, options) {{
  return fetch(url, options).then(function(r) {{
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.text();
  }});
}}
function htmxProcess(el) {{
  if (typeof htmx !== 'undefined') htmx.process(el);
}}
function loadSources() {{
  var tbody = document.getElementById('cfg-tbody');
  tbody.innerHTML = '<tr><td colspan="4" style="padding:1rem 1.25rem;color:var(--muted)">&#x27F3; Loading...</td></tr>';
  cfgFetch('/api/sources').then(function(h) {{
    tbody.innerHTML = h;
    htmxProcess(tbody);
  }}).catch(function(e) {{
    tbody.innerHTML = '<tr><td colspan="4" class="add-error" style="padding:1rem 1.25rem">&#x26A0; ' + e.message + '</td></tr>';
  }});
}}
function openConfig() {{
  document.getElementById('cfgOverlay').classList.add('open');
  loadSources();
}}
function loadAddForm() {{
  document.getElementById('llm-form-area').innerHTML = '';
  cfgFetch('/api/add-form').then(function(h) {{
    var area = document.getElementById('add-form-area');
    area.innerHTML = h;
    htmxProcess(area);
  }});
}}
function loadLlmForm() {{
  document.getElementById('add-form-area').innerHTML = '';
  cfgFetch('/api/llm-form').then(function(h) {{
    var area = document.getElementById('llm-form-area');
    area.innerHTML = h;
    htmxProcess(area);
  }});
}}
document.addEventListener('DOMContentLoaded', function() {{
  var now = new Date();
  var p = function(n) {{ return String(n).padStart(2, '0'); }};
  var d = p(now.getDate()) + '.' + p(now.getMonth()+1) + '.' + now.getFullYear();
  var t = p(now.getHours()) + ':' + p(now.getMinutes());
  document.getElementById('hdr-date').textContent = d;
  document.getElementById('hdr-time').textContent = t + ' Uhr';
  document.getElementById('lbl-date').textContent = '// NEWS DIGEST — ' + d;
  document.getElementById('ftr-ts').textContent = d + ' ' + t + ' Uhr';
}});

var _activeCat = 'ALL';
function buildSidebar() {{
  var cats = new Set();
  document.querySelectorAll('#feed-list > [data-categories]').forEach(function(b) {{
    var raw = b.dataset.categories || '';
    raw.split(',').forEach(function(c) {{ c = c.trim(); if (c) cats.add(c); }});
    var visible = _activeCat === 'ALL' || raw.split(',').some(function(c) {{ return c.trim() === _activeCat; }});
    b.style.display = visible ? '' : 'none';
  }});
  var sb = document.getElementById('sidebar-cats');
  if (!sb) return;
  sb.innerHTML = '';
  function makeBtn(label, cat) {{
    var btn = document.createElement('button');
    btn.className = 'cat-btn' + (_activeCat === cat ? ' active' : '');
    btn.dataset.cat = cat;
    btn.textContent = label;
    btn.onclick = function() {{ _activeCat = cat; buildSidebar(); }};
    sb.appendChild(btn);
  }}
  makeBtn('// ALL', 'ALL');
  Array.from(cats).sort().forEach(function(cat) {{
    makeBtn('// ' + cat.toUpperCase(), cat);
  }});
}}
document.addEventListener('DOMContentLoaded', buildSidebar);
document.addEventListener('htmx:afterSwap', buildSidebar);
</script>

<div class="statusbar">
  <span class="prompt">llm@rss-aggregator</span>
  <span>:~$ fetch --all-feeds | summarize --model {LLM_MODEL}</span>
  <span>&nbsp;|&nbsp;</span>
  <span style="color:#00ff9d">LIVE</span>
</div>

<div class="page-layout">
  <aside class="sidebar">
    <div class="sidebar-label">// CATEGORIES</div>
    <div id="sidebar-cats"></div>
  </aside>

  <div class="wrap">
    <div class="section-label" id="lbl-date">// NEWS DIGEST — {date}</div>

    <div id="feed-list">
      {feed_slots}
    </div>

  <footer>
    Generated with <span>Python + RSS + local LLM</span> &nbsp;|&nbsp; <span id="ftr-ts">{date} {time}</span>
  </footer>
  </div>
</div>

</body>
</html>"""


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


def write_shell(feeds: list, output_path: str):
    slots = "\n".join(_loading_slot(f["id"], f["name"], f.get("category") or "") for f in feeds)
    if not slots:
        slots = '<p style="color:var(--muted);font-size:12px;padding:2rem 0">No feeds configured. Add feeds via [CONFIG].</p>'
    now = datetime.datetime.now()
    Path(output_path).write_text(
        HTML_SHELL.format(
            date=now.strftime("%d.%m.%Y"),
            time=now.strftime("%H:%M"),
            LLM_MODEL=LLM_MODEL,
            feed_slots=slots,
        ),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────
# HTML — FEED BLOCK
# ─────────────────────────────────────────────

def write_block(feed_id: str, feed_name: str, articles: list):
    articles_html = ""
    for art in articles:
        css_class     = "error" if art["llm_error"] else "summary"
        display_title = art.get("title_translated") or art["title"]
        del_btn = (
            f'<button class="article-del-btn"'
            f' hx-post="/api/article/{art["id"]}/delete"'
            f' hx-target="closest .article"'
            f' hx-swap="outerHTML">REMOVE</button>'
        )
        cat_badge = (
            f'<span class="art-cat">{html.escape(art["category"].upper())}</span>'
            if art.get("category") else ""
        )
        articles_html += (
            f'<div class="article">'
            f'{cat_badge}'
            f'<h3><a href="{html.escape(art["link"])}" target="_blank">{html.escape(display_title)}</a></h3>'
            f'<p class="{css_class}">{html.escape(art["summary"] or "")}</p>'
            f'{del_btn}'
            f'</div>'
        )
    if not articles_html:
        articles_html = '<div class="article loading-article"><span class="loading-msg">No articles available.</span></div>'

    # Collect categories from articles and update outer wrapper via inline script
    cats = sorted({a["category"] for a in articles if a.get("category")})
    cats_js = html.escape(",".join(cats))
    update_script = (
        f'<script>(function(){{'
        f'var w=document.getElementById("feed-{feed_id}");'
        f'if(w){{w.dataset.categories="{cats_js}";'
        f'if(typeof buildSidebar==="function")buildSidebar();}}'
        f'}})()</script>'
    )

    ts = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    sync_btn = (
        f'<button class="block-sync-btn"'
        f' hx-post="/api/sync/{feed_id}"'
        f' hx-target="#feed-{feed_id}"'
        f' hx-swap="innerHTML">&#x27F3;</button>'
    )
    block = (
        f'<div class="source-block">'
        f'<div class="source-title">{html.escape(feed_name)}'
        f'<span class="source-ts">SYNC {ts}</span>'
        f'{sync_btn}</div>'
        f'{articles_html}'
        f'</div>'
        f'{update_script}'
    )
    BLOCK_DIR.mkdir(parents=True, exist_ok=True)
    (BLOCK_DIR / f"{feed_id}.html").write_text(block, encoding="utf-8")


# ─────────────────────────────────────────────
# HAUPTPROGRAMM
# ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed",  default=None,  help="Nur diesen Feed (UUID) verarbeiten")
    parser.add_argument("--force", action="store_true", help="Intervall-Prüfung überspringen")
    args = parser.parse_args()

    print("=" * 50)
    print("  News Feed Aggregator mit lokalem LLM")
    print("=" * 50)

    init_llm_config()
    all_feeds = get_sources_from_db()

    if args.feed:
        feeds = [f for f in all_feeds if str(f["id"]) == args.feed]
        print(f"  Single feed: {feeds[0]['name'] if feeds else args.feed}")
    else:
        feeds = all_feeds
        print(f"  {len(feeds)} sources loaded from DB")
        write_shell(feeds, OUTPUT_FILE)
        print(f"  Shell written: {OUTPUT_FILE}")

    for feed_info in feeds:
        # Interval check only on scheduled full syncs — skip for --force and --feed.
        if not args.feed and not args.force:
            last = feed_info.get("last_fetched_at")
            if last is not None:
                interval_min = feed_info.get("sync_interval_minutes", 360)
                elapsed_min = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() / 60
                if elapsed_min < interval_min:
                    remaining = int(interval_min - elapsed_min)
                    print(f"\n  [{feed_info['name']}] Skipped — next sync in {remaining} min")
                    continue

        print(f"\n  [{feed_info['name']}]")
        rss_articles = fetch_feed(feed_info)

        new_count = store_new_articles(feed_info["id"], rss_articles)
        print(f"    {new_count} new articles stored")

        cats = update_feed_categories(feed_info["id"])
        if cats:
            print(f"    Categories: {', '.join(cats)}")

        db_articles = load_block_articles(feed_info["id"], feed_info.get("max_articles", MAX_ARTICLES_PER_FEED))
        write_block(feed_info["id"], feed_info["name"], db_articles)
        print(f"    Block written ({len(db_articles)} articles from DB)")

        update_last_fetched(feed_info["url"])

    print("\n" + "=" * 50)
    print("  Fertig.")
    print("=" * 50)


if __name__ == "__main__":
    main()
