"""
News Feed Aggregator mit lokalem LLM
=====================================
Liest RSS-Feeds, fasst Artikel auf Deutsch zusammen
und speichert das Ergebnis als HTML-Seite.

Benötigte Pakete:
    pip install feedparser requests

Verwendung:
    python news_feed.py
"""

import feedparser
import requests
import json
import datetime
import html
from pathlib import Path

# ─────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────

LLM_BASE_URL = "http://192.168.5.7:8080/v1"
LLM_MODEL    = "bartowski_Qwen2.5-Coder-14B-Instruct-GGUF_Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf"
API_KEY      = "none"

# Maximale Anzahl Artikel pro Feed
MAX_ARTICLES_PER_FEED = 5

# Ausgabedatei
OUTPUT_FILE = "news_zusammenfassung.html"

# RSS-Feeds
RSS_FEEDS = [
    {"name": "Tagesschau",           "url": "https://www.tagesschau.de/xml/rss2/"},
    {"name": "Spiegel Online",       "url": "https://www.spiegel.de/schlagzeilen/index.rss"},
    {"name": "Zeit Online",          "url": "https://newsfeed.zeit.de/index"},
    {"name": "Heise Online",         "url": "https://www.heise.de/rss/heise-atom.xml"},
    {"name": "Süddeutsche",          "url": "https://rss.sueddeutsche.de/rss/Topthemen"},
    # Golem.de – themenspezifische Feeds
    {"name": "Golem – KI",           "url": "https://rss.golem.de/rss.php?ms=ki&feed=RSS2.0"},
    {"name": "Golem – Security",     "url": "https://rss.golem.de/rss.php?ms=security&feed=RSS2.0"},
    {"name": "Golem – Software",     "url": "https://rss.golem.de/rss.php?ms=software&feed=RSS2.0"},
    {"name": "Golem – Software-Entwiklung", "url": "https://rss.golem.de/rss.php?ms=softwareentwicklung&feed=RSS2.0"},
    {"name": "Golem – Internet",     "url": "https://rss.golem.de/rss.php?feed=RSS2.0&tp=inet"},
    {"name": "Golem – Politik Recht", "url": "https://rss.golem.de/rss.php?ms=politik-recht&feed=RSS2.0"},
    {"name": "Golem – Open Source",  "url": "https://rss.golem.de/rss.php?ms=open-source&feed=RSS2.0"},
    {"name": "Golem – Mobil",        "url": "https://rss.golem.de/rss.php?ms=mobil&feed=RSS2.0"},
    # Linux & Sysadmin
    {"name": "LWN.net",              "url": "https://lwn.net/headlines/newrss"},
    {"name": "Phoronix",             "url": "https://www.phoronix.com/rss.php"},
    {"name": "It's FOSS",            "url": "https://itsfoss.com/feed/"},
    {"name": "Linux Journal",        "url": "https://www.linuxjournal.com/node/feed"},
    # DevOps & Cloud
    {"name": "DevOps.com",           "url": "https://devops.com/feed/"},
    {"name": "Docker Blog",          "url": "https://www.docker.com/feed/"},
    {"name": "Kubernetes Blog",      "url": "https://kubernetes.io/feed.xml"},
    # Security
    {"name": "Krebs on Security",    "url": "https://krebsonsecurity.com/feed/"},
    {"name": "Schneier on Security", "url": "https://www.schneier.com/feed/atom/"},
    {"name": "BSI Cybersecurity",    "url": "https://wid.cert-bund.de/content/public/securityAdvisory/rss"},
    {"name": "The Hacker News",      "url": "https://feeds.feedburner.com/TheHackersNews"},
    {"name": "Hacker News Top",      "url": "https://news.ycombinator.com/rss"},
    # Wirtschaft
    {"name": "Handelsblatt",         "url": "https://www.handelsblatt.com/contentexport/feed/schlagzeilen"},
    {"name": "Heise iX",             "url": "https://www.heise.de/ix/news/news-atom.xml"},
    # Tagesgeschehen
    {"name": "Deutschlandfunk",      "url": "https://www.deutschlandfunk.de/die-nachrichten.353.de.rss"},
    {"name": "NDR Info",             "url": "https://www.ndr.de/nachrichten/info/podcast4226.xml"},
    {"name": "The Guardian",         "url": "https://www.theguardian.com/europe/rss"},
]

# ─────────────────────────────────────────────
# LLM ZUSAMMENFASSUNG
# ─────────────────────────────────────────────

def summarize(title: str, text: str) -> str:
    """Sendet einen Artikel an das lokale LLM und gibt eine deutsche Zusammenfassung zurück."""
    prompt = (
        f"Fasse den folgenden Nachrichtenartikel in 2-3 Sätzen auf Deutsch zusammen. "
        f"Antworte NUR mit der Zusammenfassung, ohne Einleitung oder Erklärung.\n\n"
        f"Titel: {title}\n\n"
        f"Inhalt: {text[:2000]}"  # max. 2000 Zeichen für den Kontext
    )

    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
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
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[Fehler bei der Zusammenfassung: {e}]"


# ─────────────────────────────────────────────
# RSS ABRUF
# ─────────────────────────────────────────────

def fetch_feed(feed_info: dict) -> list:
    """Ruft einen RSS-Feed ab und gibt eine Liste von Artikeln zurück."""
    print(f"  → Lade Feed: {feed_info['name']} ...")
    try:
        parsed = feedparser.parse(feed_info["url"])
        articles = []
        for entry in parsed.entries[:MAX_ARTICLES_PER_FEED]:
            title   = entry.get("title", "Kein Titel")
            link    = entry.get("link", "#")
            summary = entry.get("summary", entry.get("description", ""))
            # HTML-Tags aus der Beschreibung entfernen
            import re
            clean = re.sub(r"<[^>]+>", "", summary).strip()
            articles.append({"title": title, "link": link, "text": clean})
        return articles
    except Exception as e:
        print(f"    ✗ Fehler beim Laden: {e}")
        return []


# ─────────────────────────────────────────────
# HTML AUSGABE
# ─────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>News Zusammenfassung – {date}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f5f7;
      color: #1a1a2e;
      padding: 2rem;
    }}
    h1 {{
      font-size: 1.8rem;
      margin-bottom: 0.25rem;
      color: #0f3460;
    }}
    .subtitle {{ color: #666; margin-bottom: 2rem; font-size: 0.9rem; }}
    .source-block {{ margin-bottom: 2.5rem; }}
    .source-title {{
      font-size: 1.2rem;
      font-weight: 700;
      color: #e94560;
      border-bottom: 2px solid #e94560;
      padding-bottom: 0.4rem;
      margin-bottom: 1rem;
    }}
    .article {{
      background: #fff;
      border-radius: 8px;
      padding: 1rem 1.25rem;
      margin-bottom: 0.8rem;
      box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    }}
    .article h3 {{ font-size: 1rem; margin-bottom: 0.4rem; }}
    .article h3 a {{ color: #0f3460; text-decoration: none; }}
    .article h3 a:hover {{ text-decoration: underline; }}
    .summary {{ font-size: 0.92rem; color: #444; line-height: 1.6; }}
    .error {{ color: #c0392b; font-style: italic; }}
    footer {{ margin-top: 3rem; text-align: center; color: #aaa; font-size: 0.8rem; }}
  </style>
</head>
<body>
  <h1>📰 News Zusammenfassung</h1>
  <p class="subtitle">Erstellt am {date} um {time} Uhr – zusammengefasst von lokalem LLM (Qwen2.5-Coder)</p>
  {content}
  <footer>Generiert mit Python + RSS + lokalem LLM</footer>
</body>
</html>"""


def build_html(results: list) -> str:
    content_blocks = []
    for source in results:
        articles_html = ""
        for art in source["articles"]:
            summary_class = "error" if art["summary"].startswith("[Fehler") else "summary"
            articles_html += f"""
    <div class="article">
      <h3><a href="{html.escape(art['link'])}" target="_blank">{html.escape(art['title'])}</a></h3>
      <p class="{summary_class}">{html.escape(art['summary'])}</p>
    </div>"""

        content_blocks.append(f"""
  <div class="source-block">
    <div class="source-title">{html.escape(source['name'])}</div>
    {articles_html}
  </div>""")

    now = datetime.datetime.now()
    return HTML_TEMPLATE.format(
        date=now.strftime("%d.%m.%Y"),
        time=now.strftime("%H:%M"),
        content="\n".join(content_blocks),
    )


# ─────────────────────────────────────────────
# HAUPTPROGRAMM
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  News Feed Aggregator mit lokalem LLM")
    print("=" * 50)

    all_results = []

    for feed_info in RSS_FEEDS:
        articles = fetch_feed(feed_info)
        summarized = []

        for i, art in enumerate(articles, 1):
            print(f"    [{i}/{len(articles)}] Zusammenfasse: {art['title'][:60]}...")
            art["summary"] = summarize(art["title"], art["text"])
            summarized.append(art)

        all_results.append({"name": feed_info["name"], "articles": summarized})

    print("\nErstelle HTML-Ausgabe ...")
    html_output = build_html(all_results)

    output_path = Path(OUTPUT_FILE)
    output_path.write_text(html_output, encoding="utf-8")
    print(f"✓ Fertig! Gespeichert als: {output_path.resolve()}")
    print("=" * 50)


if __name__ == "__main__":
    main()
