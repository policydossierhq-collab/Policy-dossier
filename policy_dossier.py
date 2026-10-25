"""Policy Dossier: fetch news -> ChatGPT write-up -> save to stories.json -> post to X.
Run on a schedule (see .github/workflows/update.yml)."""
import json, os, re, html, hashlib, calendar, datetime as dt
from string import Template
import feedparser, tweepy
from openai import OpenAI

# source name: (feed url, region)
FEEDS = {
    "Channels TV": ("https://www.channelstv.com/feed/", "Nigeria"),
    "Nairametrics": ("https://nairametrics.com/feed/", "Nigeria"),
    "Punch": ("https://punchng.com/feed/", "Nigeria"),
    "Premium Times": ("https://www.premiumtimesng.com/feed", "Nigeria"),
    "Vanguard": ("https://www.vanguardngr.com/feed/", "Nigeria"),
    "BBC Politics": ("https://feeds.bbci.co.uk/news/politics/rss.xml", "World"),
    "The Guardian": ("https://www.theguardian.com/politics/rss", "World"),
    "NPR Politics": ("https://feeds.npr.org/1014/rss.xml", "World"),
}
MAX_NEW_PER_RUN = 4      # also caps how many tweets go out per run
KEEP = 200               # stories kept on the site
FILE = "stories.json"
SITE_URL = (os.environ.get("SITE_URL") or "https://YOUR-SITE.com").rstrip("/")
X_URL = os.environ.get("X_URL") or "https://x.com/YOUR_HANDLE"
MODEL = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"  # change if OpenAI retires it


def find_image(e):
    """Main image from the feed entry (hotlinked, never copied to our server)."""
    for k in ("media_content", "media_thumbnail"):
        for m in e.get(k) or []:
            if str(m.get("url", "")).startswith("http"):
                return m["url"]
    for l in e.get("links", []):
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l["href"]
    blob = e.get("summary", "") + "".join(c.get("value", "") for c in e.get("content", []))
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    return m.group(1) if m and m.group(1).startswith("http") else ""

PAGE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title · Policy Dossier</title>
<meta name="description" content="$summary">
<meta property="og:type" content="article"><meta property="og:site_name" content="Policy Dossier">
<meta property="og:title" content="$title"><meta property="og:description" content="$summary">
<meta property="og:url" content="$url">$ogimg
<meta name="twitter:card" content="$card">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800;900&family=Source+Serif+4:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0a;--surface:#141414;--fg:#f5f5f5;--mute:#8f8f8f;--line:#262626;--ng:#2ecc71}
@media (prefers-color-scheme:light){:root{--bg:#f6f5f2;--surface:#fff;--fg:#0a0a0a;--mute:#666;--line:#dcdad4;--ng:#0b7a3b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:400 16px/1.55 Archivo,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}:focus-visible{outline:3px solid var(--fg);outline-offset:3px;border-radius:4px}
.top{border-bottom:1px solid var(--line)}
.bar{max-width:1120px;margin:0 auto;padding:0 20px;height:64px;display:flex;align-items:baseline;justify-content:space-between;align-items:center}
.brand b{font-weight:900;font-size:30px;letter-spacing:-.07em}
.brand span{font-weight:800;font-size:12px;letter-spacing:.22em;text-transform:uppercase;margin-left:10px}
.back{font-weight:600;font-size:14px;color:var(--mute)}.back:hover{color:var(--fg)}
article{max-width:720px;margin:0 auto;padding:48px 20px 80px}
.tag{display:inline-flex;gap:8px;font-weight:800;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--mute)}
.tag i{font-style:normal;color:var(--ng)}
h1{font-weight:900;font-size:clamp(32px,6vw,54px);line-height:1.03;letter-spacing:-.04em;margin:16px 0 14px}
.meta{color:var(--mute);font-size:14px;padding-bottom:24px;border-bottom:1px solid var(--line);margin-bottom:28px}
.hero{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:var(--line)}
.credit{color:var(--mute);font-size:13px;margin:8px 0 32px}
.body p{font:400 19px/1.75 "Source Serif 4",Georgia,serif;margin:0 0 1.2em}
.body p:first-child::first-line{font-weight:600}
.src{display:inline-block;margin-top:12px;background:var(--fg);color:var(--bg);border-radius:999px;padding:12px 24px;font-weight:700}
.note{color:var(--mute);font-size:13px;margin-top:28px;border-top:1px solid var(--line);padding-top:18px}
</style></head>
<body>
<header class="top"><div class="bar"><a class="brand" href="../index.html"><b>PD</b><span>Policy Dossier</span></a><a class="back" href="../index.html">← All stories</a></div></header>
<article>
<span class="tag">$region<span>$source</span></span>
<h1>$title</h1><div class="meta">$date</div>
$hero<div class="body">$body</div>
<a class="src" href="$link" target="_blank" rel="noopener">Read the original at $source →</a>
<p class="note">This summary was written automatically from the source's headline and snippet. Read the original for the full report. <a href="$xurl" target="_blank" rel="noopener"><u>Follow Policy Dossier on X</u></a></p>
</article></body></html>""")

def write_pages(stories):
    os.makedirs("stories", exist_ok=True)
    for s in stories:
        q = lambda v: html.escape(str(v), quote=True)
        img = s.get("image", "")
        body = "".join(f"<p>{q(t)}</p>" for t in re.split(r"\n+", s.get("body") or s["summary"]) if t.strip())
        open(f"stories/{s['id']}.html", "w", encoding="utf-8").write(PAGE.substitute(
            title=q(s["title"]), summary=q(s["summary"]), url=q(f"{SITE_URL}/stories/{s['id']}.html"),
            ogimg=f'\n<meta property="og:image" content="{q(img)}">' if img else "",
            card="summary_large_image" if img else "summary",
            source=q(s["source"]), xurl=q(X_URL), region=("<i>Nigeria</i>" if s.get("region") == "Nigeria" else "World"), date=q(s["published"][:10]), link=q(s["link"]), body=body,
            hero=(f'<img class="hero" src="{q(img)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">'
                  f'<div class="credit">Image: {q(s["source"])}</div>') if img else ""))

def load():
    try:
        return json.load(open(FILE, encoding="utf-8"))
    except FileNotFoundError:
        return []

def write_up(client, title, text):
    """ChatGPT returns a tweet-length teaser and a short original write-up (not a copy of the article)."""
    r = client.chat.completions.create(
        model=MODEL, response_format={"type": "json_object"},
        messages=[{"role": "user", "content":
            "You write for Policy Dossier, a neutral policy news site. From the item below, return JSON with "
            '"short": 2 short plain sentences, under 150 characters total, and '
            '"body": a 3-paragraph neutral explainer in your own words (what happened, why it matters, context). '
            "No quotes from the source, no opinion, no invented facts.\n"
            f"Title: {title}\nText: {text[:2500]}"}])
    return json.loads(r.choices[0].message.content)

def main():
    stories = load()
    seen = {s["id"] for s in stories}
    ai = OpenAI()  # uses OPENAI_API_KEY
    x = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"], consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"], access_token_secret=os.environ["X_ACCESS_SECRET"])
    new = []
    for source, (url, region) in FEEDS.items():
        for e in feedparser.parse(url).entries[:10]:
            sid = hashlib.sha1(e.link.encode()).hexdigest()[:16]
            if sid in seen:
                continue
            seen.add(sid)
            pub = calendar.timegm(e.published_parsed) if getattr(e, "published_parsed", None) else dt.datetime.utcnow().timestamp()
            new.append({"id": sid, "title": e.title, "link": e.link, "source": source, "region": region, "image": find_image(e),
                        "raw": e.get("summary", ""), "published": dt.datetime.utcfromtimestamp(pub).isoformat() + "Z"})
    new.sort(key=lambda s: s["published"], reverse=True)
    new.sort(key=lambda s: s["region"] != "Nigeria")  # Nigerian news first (stable sort)
    for s in new[:MAX_NEW_PER_RUN]:
         w = write_up(ai, s["title"], s.pop("raw"))

        s["summary"] = w["short"].strip()

        body = w["body"]

        if isinstance(body, dict):
            body = "\n\n".join(str(v) for v in body.values())
        elif isinstance(body, list):
            body = "\n\n".join(str(v) for v in body)

        s["body"] = body.strip()

        stories.insert(0, s)
        tweet = f'{s["title"][:110]}\n\n{s["summary"][:150]}\n\nRead the full story: {SITE_URL}/stories/{s["id"]}.html'
        try:
            x.create_tweet(text=tweet)
        except Exception as err:
            print("X post failed:", err)
    stories = stories[:KEEP]
    write_pages(stories)
    json.dump(stories, open(FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

if __name__ == "__main__":
    main()
