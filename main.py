"""Policy Dossier: fetch news, summarise, rebuild the site, post to X.

Run:  python main.py
Env:  ANTHROPIC_API_KEY, and for X posting: X_API_KEY, X_API_SECRET,
      X_ACCESS_TOKEN, X_ACCESS_SECRET. Set AUTO_POST=true to actually tweet.
"""
import os, json, re, html, hashlib, pathlib, datetime, calendar, urllib.request
import feedparser
from anthropic import Anthropic

ROOT = pathlib.Path(__file__).parent
SITE = ROOT / "docs"
STATE = ROOT / "state.json"

# Nigeria first, then global. Verify each URL still works (see README).
FEEDS = {
    "Nigeria": [
        ("Channels TV", "https://www.channelstv.com/feed/"),
        ("Nairametrics", "https://nairametrics.com/feed/"),
        ("Premium Times", "https://www.premiumtimesng.com/feed"),
        ("Vanguard", "https://www.vanguardngr.com/feed/"),
        ("Punch", "https://punchng.com/feed/"),
    ],
    "Global": [
        ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("DW", "https://rss.dw.com/rdf/rss-en-world"),
    ],
}
NEW_PER_RUN = {"Nigeria": 6, "Global": 3}   # new stories summarised per run
SHOW = {"Nigeria": 12, "Global": 8}         # stories shown on the site
MAX_TWEETS = 3                              # tweets per run, Nigeria first
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
AUTO_POST = os.getenv("AUTO_POST", "false").lower() == "true"


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"stories": []}


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def find_image(e):
    """Image from the feed entry itself."""
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []):
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href", "")
    blob = (e.get("summary") or "") + " ".join(c.get("value", "") for c in e.get("content") or [])
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    return m.group(1) if m else ""


def og_image(url):
    """Fallback: read the article's own social preview image."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PolicyDossierBot/1.0"})
        page = urllib.request.urlopen(req, timeout=8).read(200000).decode("utf-8", "ignore")
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', page))
        return html.unescape(m.group(1)) if m else ""
    except Exception:
        return ""


def fetch_new(section, seen):
    items = []
    for source, url in FEEDS[section]:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"feed failed {url}: {e}")
            continue
        for e in feed.entries[:10]:
            link = e.get("link")
            if not link:
                continue
            sid = hashlib.sha1(link.encode()).hexdigest()[:16]
            if sid in seen:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            when = calendar.timegm(ts) if ts else 0
            items.append({"id": sid, "section": section, "source": source, "url": link,
                          "title": clean(e.get("title")), "snippet": clean(e.get("summary"))[:800],
                          "ts": when, "image": find_image(e)})
            seen.add(sid)
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:NEW_PER_RUN[section]]


def summarise(client, item):
    prompt = (
        "Write a neutral news summary of 1 to 2 sentences (max 45 words) in your own words. "
        "Use only facts stated in the text below. Do not copy phrases from it, do not add "
        "background, opinion or numbers that are not given. Reply with the summary only.\n\n"
        f"Headline: {item['title']}\nText: {item['snippet']}"
    )
    msg = client.messages.create(model=MODEL, max_tokens=150,
                                 messages=[{"role": "user", "content": prompt}])
    return msg.content[0].text.strip()


def post_to_x(item):
    import tweepy
    x = tweepy.Client(consumer_key=os.environ["X_API_KEY"], consumer_secret=os.environ["X_API_SECRET"],
                      access_token=os.environ["X_ACCESS_TOKEN"], access_token_secret=os.environ["X_ACCESS_SECRET"])
    title = item["title"]
    if len(title) > 240:
        title = title[:237].rstrip() + "..."
    # X counts any link as 23 characters.
    x.create_tweet(text=f"{title}\n\n{item['url']}")


CSS = """:root{--bg:#fff;--ink:#0a0a0a;--muted:#555;--line:#d9d9d9}
@media(prefers-color-scheme:dark){:root{--bg:#0a0a0a;--ink:#f5f5f5;--muted:#a8a8a8;--line:#2e2e2e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:400 1rem/1.55 Archivo,system-ui,sans-serif}
.top{background:#000;color:#fff}.wrap{max-width:980px;margin:0 auto;padding:0 20px}
.top .wrap{display:flex;align-items:center;gap:20px;padding-top:14px;padding-bottom:14px}
.top img{width:84px;height:84px;border-radius:6px}.top h1{margin:0;font-weight:800;font-size:2.2rem;line-height:1}
.top p{margin:6px 0 0;color:#bdbdbd}h2{font-weight:800;font-size:1.5rem;margin:40px 0 8px}
.story{border-top:2px solid var(--ink);padding:18px 0 22px}.story h3{font-weight:800;font-size:1.6rem;line-height:1.15;margin:4px 0 8px}
.story p{margin:0 0 12px;max-width:66ch}.meta{font-size:.88rem;color:var(--muted)}
figure{margin:10px 0 0}figure img{width:100%;max-height:420px;object-fit:cover;border-radius:6px;display:block;background:var(--line)}
figcaption{font-size:.8rem;color:var(--muted);margin-top:4px}a{color:var(--ink);font-weight:600}footer{border-top:1px solid var(--line);margin-top:40px;padding:18px 0 32px;color:var(--muted);font-size:.85rem}"""


def render(stories):
    e = html.escape
    def block(section):
        rows = [s for s in stories if s["section"] == section][:SHOW[section]]
        if not rows:
            return "<p class='meta'>No stories yet.</p>"
        return "".join(
            f"<article class='story'><span class='meta'>{e(s['date'])}</span>"
            + (f"<figure><img src='{e(s['image'])}' alt='' loading='lazy' referrerpolicy='no-referrer' "
               f"onerror=\"this.parentNode.remove()\"><figcaption>Image: {e(s['source'])}</figcaption></figure>"
               if s.get("image") else "")
            + f"<h3>{e(s['title'])}</h3><p>{e(s['summary'])}</p>"
            f"<p class='meta'>Source: <a href='{e(s['url'])}' target='_blank' rel='noopener'>{e(s['source'])}</a></p></article>"
            for s in rows)
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Policy Dossier</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&display=swap">
<style>{CSS}</style></head><body>
<div class="top"><div class="wrap"><img src="logo.jpg" alt="Policy Dossier logo">
<div><h1>Policy Dossier</h1><p>Facts. Context. Accountability.</p></div></div></div>
<div class="wrap"><h2>Nigeria</h2>{block("Nigeria")}<h2>Global</h2>{block("Global")}
<footer>Summaries are written automatically from public headlines and link to the original source. Last updated {datetime.datetime.utcnow():%d %B %Y, %H:%M} UTC.</footer></div></body></html>"""
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(page, encoding="utf-8")


def main():
    state = load_state()
    seen = {s["id"] for s in state["stories"]}
    client = Anthropic()
    fresh = []
    for section in ("Nigeria", "Global"):
        for item in fetch_new(section, seen):
            try:
                item["summary"] = summarise(client, item)
            except Exception as e:
                print(f"summary failed for {item['url']}: {e}")
                continue
            item["date"] = (datetime.datetime.utcfromtimestamp(item["ts"]) if item["ts"]
                            else datetime.datetime.utcnow()).strftime("%d %b %Y")
            img = item.get("image") or og_image(item["url"])
            item["image"] = img.replace("http://", "https://", 1) if img.startswith("http") else ""
            item["posted"] = False
            item.pop("snippet", None)
            fresh.append(item)
    state["stories"] = (fresh + state["stories"])[:300]

    if AUTO_POST:
        sent = 0
        for s in state["stories"]:          # Nigeria stories come first in `fresh`
            if s["posted"] or sent >= MAX_TWEETS:
                continue
            try:
                post_to_x(s)
                s["posted"] = True
                sent += 1
            except Exception as e:
                print(f"tweet failed for {s['url']}: {e}")
    else:
        print("AUTO_POST is off: site updated, nothing tweeted.")

    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    render(state["stories"])
    print(f"{len(fresh)} new stories.")


if __name__ == "__main__":
    main()
