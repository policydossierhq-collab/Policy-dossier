"""Policy Dossier

Fetch news -> GPT write-up -> save stories.json ->
generate individual story pages -> post short updates to X.

Designed to run from GitHub Actions.
"""

import calendar
import datetime as dt
import hashlib
import html
import json
import os
import re
from string import Template

import feedparser
import tweepy
from openai import OpenAI


# ============================================================
# CONFIGURATION
# ============================================================

# Source name: (RSS feed URL, region)
FEEDS = {
    "Channels TV": (
        "https://www.channelstv.com/feed/",
        "Nigeria",
    ),
    "Nairametrics": (
        "https://nairametrics.com/feed/",
        "Nigeria",
    ),
    "Punch": (
        "https://punchng.com/feed/",
        "Nigeria",
    ),
    "Premium Times": (
        "https://www.premiumtimesng.com/feed",
        "Nigeria",
    ),
    "Vanguard": (
        "https://www.vanguardngr.com/feed/",
        "Nigeria",
    ),
    "BBC Politics": (
        "https://feeds.bbci.co.uk/news/politics/rss.xml",
        "World",
    ),
    "The Guardian": (
        "https://www.theguardian.com/politics/rss",
        "World",
    ),
    "NPR Politics": (
        "https://feeds.npr.org/1014/rss.xml",
        "World",
    ),
}

# Maximum number of new stories written / posted each run
MAX_NEW_PER_RUN = 4

# Maximum number of stories retained
KEEP = 200

FILE = "stories.json"

SITE_URL = (
    os.environ.get("SITE_URL")
    or "https://YOUR-SITE.com"
).rstrip("/")

X_URL = (
    os.environ.get("X_URL")
    or "https://x.com/YOUR_HANDLE"
)

# Cheap model suitable for repeated short news-writing tasks.
MODEL = (
    os.environ.get("OPENAI_MODEL")
    or "gpt-6-luna"
)


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    """Remove HTML and excessive whitespace from feed text."""
    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalise_text(value):
    """
    Safely convert GPT output into plain text.

    Handles strings, lists, dictionaries and other values.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        return "\n\n".join(
            normalise_text(v)
            for v in value.values()
            if normalise_text(v)
        ).strip()

    if isinstance(value, list):
        return "\n\n".join(
            normalise_text(v)
            for v in value
            if normalise_text(v)
        ).strip()

    return str(value).strip()


def parse_json_response(text):
    """
    Parse JSON returned by GPT.

    Also handles accidental markdown code fences.
    """
    text = (text or "").strip()

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)

    except json.JSONDecodeError:
        # Fallback: find the first JSON-looking object.
        match = re.search(r"\{.*\}", text, re.DOTALL)

        if not match:
            raise ValueError(
                "GPT did not return valid JSON."
            )

        return json.loads(match.group(0))


# ============================================================
# IMAGE EXTRACTION
# ============================================================

def find_image(entry):
    """
    Find the main image supplied in the RSS feed.

    Images are hotlinked. They are not copied to GitHub.
    """

    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = str(media.get("url", ""))

            if url.startswith("http"):
                return url

    for link in entry.get("links", []):
        if (
            link.get("rel") == "enclosure"
            and str(link.get("type", "")).startswith("image")
        ):
            url = str(link.get("href", ""))

            if url.startswith("http"):
                return url

    blob = (
        entry.get("summary", "")
        + "".join(
            content.get("value", "")
            for content in entry.get("content", [])
        )
    )

    match = re.search(
        r'<img[^>]+src=["\']([^"\']+)',
        blob,
        re.IGNORECASE,
    )

    if match:
        url = match.group(1)

        if url.startswith("http"):
            return url

    return ""


# ============================================================
# ARTICLE PAGE TEMPLATE
# ============================================================

PAGE = Template(
    """<!doctype html>
<html lang="en">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>

<title>$title · Policy Dossier</title>

<meta
    name="description"
    content="$summary"
>

<meta
    property="og:type"
    content="article"
>

<meta
    property="og:site_name"
    content="Policy Dossier"
>

<meta
    property="og:title"
    content="$title"
>

<meta
    property="og:description"
    content="$summary"
>

<meta
    property="og:url"
    content="$url"
>

$ogimg

<meta
    name="twitter:card"
    content="$card"
>

<link
    href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800;900&family=Source+Serif+4:wght@400;600&display=swap"
    rel="stylesheet"
>

<style>

:root {
    --bg: #0a0a0a;
    --surface: #141414;
    --fg: #f5f5f5;
    --mute: #8f8f8f;
    --line: #262626;
    --ng: #2ecc71;
}

@media (prefers-color-scheme: light) {
    :root {
        --bg: #f6f5f2;
        --surface: #ffffff;
        --fg: #0a0a0a;
        --mute: #666666;
        --line: #dcdad4;
        --ng: #0b7a3b;
    }
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: var(--bg);
    color: var(--fg);
    font: 400 16px/1.55 Archivo, system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
}

a {
    color: inherit;
    text-decoration: none;
}

:focus-visible {
    outline: 3px solid var(--fg);
    outline-offset: 3px;
    border-radius: 4px;
}

.top {
    border-bottom: 1px solid var(--line);
}

.bar {
    max-width: 1120px;
    margin: 0 auto;
    padding: 0 20px;
    height: 64px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.brand b {
    font-weight: 900;
    font-size: 30px;
    letter-spacing: -.07em;
}

.brand span {
    font-weight: 800;
    font-size: 12px;
    letter-spacing: .22em;
    text-transform: uppercase;
    margin-left: 10px;
}

.back {
    font-weight: 600;
    font-size: 14px;
    color: var(--mute);
}

.back:hover {
    color: var(--fg);
}

article {
    max-width: 720px;
    margin: 0 auto;
    padding: 48px 20px 80px;
}

.tag {
    display: inline-flex;
    gap: 8px;
    font-weight: 800;
    font-size: 11px;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--mute);
}

.tag i {
    font-style: normal;
    color: var(--ng);
}

h1 {
    font-weight: 900;
    font-size: clamp(32px, 6vw, 54px);
    line-height: 1.03;
    letter-spacing: -.04em;
    margin: 16px 0 14px;
}

.meta {
    color: var(--mute);
    font-size: 14px;
    padding-bottom: 24px;
    border-bottom: 1px solid var(--line);
    margin-bottom: 28px;
}

.hero {
    display: block;
    width: 100%;
    aspect-ratio: 16/9;
    object-fit: cover;
    border-radius: 10px;
    background: var(--line);
}

.credit {
    color: var(--mute);
    font-size: 13px;
    margin: 8px 0 32px;
}

.body p {
    font: 400 19px/1.75 "Source Serif 4", Georgia, serif;
    margin: 0 0 1.2em;
}

.body p:first-child::first-line {
    font-weight: 600;
}

.src {
    display: inline-block;
    margin-top: 12px;
    background: var(--fg);
    color: var(--bg);
    border-radius: 999px;
    padding: 12px 24px;
    font-weight: 700;
}

.note {
    color: var(--mute);
    font-size: 13px;
    margin-top: 28px;
    border-top: 1px solid var(--line);
    padding-top: 18px;
}

</style>

</head>

<body>

<header class="top">

<div class="bar">

<a
    class="brand"
    href="../index.html"
>
<b>PD</b>
<span>Policy Dossier</span>
</a>

<a
    class="back"
    href="../index.html"
>
← All stories
</a>

</div>

</header>

<article>

<span class="tag">
$region
<span>$source</span>
</span>

<h1>$title</h1>

<div class="meta">
$date
</div>

$hero

<div class="body">
$body
</div>

<a
    class="src"
    href="$link"
    target="_blank"
    rel="noopener"
>
Read the original at $source →
</a>

<p class="note">

This explainer was written automatically from the
source headline and feed text.

Read the original publication for the full report.

<a
    href="$xurl"
    target="_blank"
    rel="noopener"
>
<u>Follow Policy Dossier on X</u>
</a>

</p>

</article>

</body>

</html>
"""
)


# ============================================================
# WEBSITE GENERATION
# ============================================================

def write_pages(stories):
    """Generate an HTML page for every story."""

    os.makedirs("stories", exist_ok=True)

    for story in stories:

        escape = lambda value: html.escape(
            str(value),
            quote=True,
        )

        image = story.get("image", "")

        article_text = (
            story.get("body")
            or story.get("summary")
            or ""
        )

        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(
                r"\n+",
                article_text,
            )
            if paragraph.strip()
        ]

        body_html = "".join(
            f"<p>{escape(paragraph)}</p>"
            for paragraph in paragraphs
        )

        article_url = (
            f"{SITE_URL}/stories/"
            f"{story['id']}.html"
        )

        if image:
            og_image = (
                '\n<meta property="og:image" '
                f'content="{escape(image)}">'
            )

            card = "summary_large_image"

            hero = (
                f'<img class="hero" '
                f'src="{escape(image)}" '
                f'alt="" '
                f'referrerpolicy="no-referrer" '
                f'onerror="this.remove()">'
                f'<div class="credit">'
                f'Image: {escape(story["source"])}'
                f'</div>'
            )

        else:
            og_image = ""
            card = "summary"
            hero = ""

        region_html = (
            "<i>Nigeria</i>"
            if story.get("region") == "Nigeria"
            else "World"
        )

        page = PAGE.substitute(
            title=escape(story["title"]),
            summary=escape(story["summary"]),
            url=escape(article_url),
            ogimg=og_image,
            card=card,
            source=escape(story["source"]),
            xurl=escape(X_URL),
            region=region_html,
            date=escape(
                story.get(
                    "published",
                    "",
                )[:10]
            ),
            link=escape(story["link"]),
            body=body_html,
            hero=hero,
        )

        filename = (
            f"stories/{story['id']}.html"
        )

        with open(
            filename,
            "w",
            encoding="utf-8",
        ) as file:
            file.write(page)


# ============================================================
# STATE
# ============================================================

def load():
    """Load existing stories."""

    try:
        with open(
            FILE,
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, list):
            return data

        return []

    except (
        FileNotFoundError,
        json.JSONDecodeError,
    ):
        return []


# ============================================================
# GPT WRITING
# ============================================================

def write_up(client, title, text):
    """
    Generate:
    - short teaser for X/homepage
    - three-paragraph website explainer

    GPT is instructed not to invent information.
    """

    source_text = clean_text(text)

    prompt = f"""
You write for Policy Dossier, a neutral policy and
public-affairs news publication.

Use ONLY the information supplied below.

Return valid JSON only, with exactly these fields:

{{
  "short": "A concise two-sentence teaser.",
  "body": "A three-paragraph explainer."
}}

Requirements for "short":
- Maximum 150 characters total.
- Two short sentences.
- Neutral and factual.
- Suitable for an X post.
- Do not use hashtags.
- Do not use emojis.

Requirements for "body":
- Three clear paragraphs.
- Aim for approximately 250 to 400 words ONLY if
  the source material contains enough information.
- If there is not enough information, write a shorter
  article rather than inventing details.
- Paragraph 1: what happened.
- Paragraph 2: why it matters.
- Paragraph 3: relevant context that is explicitly
  supported by the supplied text.
- Do not introduce facts, statistics, dates,
  quotations, people, organisations or background
  that are not present in the supplied material.
- Do not copy long phrases from the source.
- Do not add opinions.
- Do not speculate.
- Do not mention that you are an AI.

Headline:
{title}

Source text:
{source_text[:3500]}
""".strip()

    response = client.responses.create(
        model=MODEL,
        input=prompt,
    )

    result = parse_json_response(
        response.output_text
    )

    short = normalise_text(
        result.get("short", "")
    )

    body = normalise_text(
        result.get("body", "")
    )

    if not short:
        raise ValueError(
            "GPT returned an empty short summary."
        )

    if not body:
        raise ValueError(
            "GPT returned an empty article body."
        )

    return {
        "short": short,
        "body": body,
    }


# ============================================================
# X
# ============================================================

def create_x_client():
    """Create X API client using GitHub secrets."""

    required = [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_SECRET",
    ]

    missing = [
        name
        for name in required
        if not os.environ.get(name)
    ]

    if missing:
        print(
            "X posting disabled. Missing: "
            + ", ".join(missing)
        )
        return None

    return tweepy.Client(
        consumer_key=os.environ[
            "X_API_KEY"
        ],
        consumer_secret=os.environ[
            "X_API_SECRET"
        ],
        access_token=os.environ[
            "X_ACCESS_TOKEN"
        ],
        access_token_secret=os.environ[
            "X_ACCESS_SECRET"
        ],
    )


def build_x_post(story):
    """
    Produce a deliberately short X post.

    URLs are shortened by X, but keeping this under
    ~250 characters provides a comfortable margin.
    """

    article_url = (
        f"{SITE_URL}/stories/"
        f"{story['id']}.html"
    )

    title = clean_text(
        story["title"]
    )

    summary = clean_text(
        story["summary"]
    )

    if len(title) > 85:
        title = title[:82].rstrip() + "..."

    if len(summary) > 105:
        summary = (
            summary[:102].rstrip()
            + "..."
        )

    tweet = (
        f"{title}\n\n"
        f"{summary}\n\n"
        f"Read more: {article_url}"
    )

    return tweet


# ============================================================
# MAIN
# ============================================================

def main():

    stories = load()

    seen = {
        story["id"]
        for story in stories
        if story.get("id")
    }

    ai = OpenAI()

    x_client = create_x_client()

    new = []

    now = dt.datetime.now(
        dt.timezone.utc
    )

    # --------------------------------------------------------
    # FETCH RSS STORIES
    # --------------------------------------------------------

    for source, (url, region) in FEEDS.items():

        try:
            feed = feedparser.parse(url)

        except Exception as error:
            print(
                f"Feed failed: {source}: "
                f"{error}"
            )
            continue

        for entry in feed.entries[:10]:

            link = entry.get("link")

            title = clean_text(
                entry.get("title", "")
            )

            if not link or not title:
                continue

            sid = hashlib.sha1(
                link.encode("utf-8")
            ).hexdigest()[:16]

            if sid in seen:
                continue

            seen.add(sid)

            published_struct = (
                getattr(
                    entry,
                    "published_parsed",
                    None,
                )
                or getattr(
                    entry,
                    "updated_parsed",
                    None,
                )
            )

            if published_struct:
                timestamp = calendar.timegm(
                    published_struct
                )

                published = (
                    dt.datetime.fromtimestamp(
                        timestamp,
                        tz=dt.timezone.utc,
                    )
                )

            else:
                published = now

            raw = (
                entry.get("summary", "")
                or "".join(
                    content.get(
                        "value",
                        "",
                    )
                    for content in entry.get(
                        "content",
                        [],
                    )
                )
            )

            new.append(
                {
                    "id": sid,
                    "title": title,
                    "link": link,
                    "source": source,
                    "region": region,
                    "image": find_image(
                        entry
                    ),
                    "raw": raw,
                    "published": (
                        published
                        .isoformat()
                        .replace(
                            "+00:00",
                            "Z",
                        )
                    ),
                }
            )

    # Most recent first.
    new.sort(
        key=lambda story: story[
            "published"
        ],
        reverse=True,
    )

    # Nigeria first while preserving date order.
    new.sort(
        key=lambda story:
            story["region"] != "Nigeria"
    )

    # --------------------------------------------------------
    # WRITE NEW STORIES
    # --------------------------------------------------------

    processed = []

    for story in new[
        :MAX_NEW_PER_RUN
    ]:

        raw = story.pop(
            "raw",
            "",
        )

        try:
            writeup = write_up(
                ai,
                story["title"],
                raw,
            )

        except Exception as error:
            print(
                "GPT write-up failed for "
                f"{story['link']}: "
                f"{error}"
            )
            continue

        story["summary"] = (
            writeup["short"].strip()
        )

        story["body"] = (
            writeup["body"].strip()
        )

        processed.append(story)

        # ----------------------------------------------------
        # POST SHORT VERSION TO X
        # ----------------------------------------------------

        if x_client is not None:

            tweet = build_x_post(
                story
            )

            try:
                x_client.create_tweet(
                    text=tweet
                )

                print(
                    "Posted to X: "
                    + story["title"]
                )

            except Exception as error:
                # X failure does NOT prevent website update.
                print(
                    "X post failed for "
                    f"{story['link']}: "
                    f"{error}"
                )

    # Keep newest processed stories first.
    stories = (
        processed
        + stories
    )[:KEEP]

    # --------------------------------------------------------
    # BUILD WEBSITE
    # --------------------------------------------------------

    write_pages(stories)

    # --------------------------------------------------------
    # SAVE JSON
    # --------------------------------------------------------

    with open(
        FILE,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            stories,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print(
        f"Completed. "
        f"{len(processed)} new "
        f"stories published."
    )


if __name__ == "__main__":
    main()
