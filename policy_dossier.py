"""
Policy Dossier

Workflow:
1. Fetch latest news from RSS feeds.
2. Prioritise Nigeria.
3. Select stories from different publishers.
4. Open each original article.
5. Extract the article text and actual story photograph.
6. Use GPT to create:
   - a short teaser for X/homepage
   - a neutral website article
7. Generate individual article pages.
8. Save stories.json.
9. Post short updates to X.

Designed to run with GitHub Actions.
"""

import calendar
import datetime as dt
import hashlib
import html
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from string import Template

import feedparser
import tweepy
from bs4 import BeautifulSoup
from openai import OpenAI


# ============================================================
# CONFIGURATION
# ============================================================

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


# Maximum new stories published per run.
MAX_NEW_PER_RUN = 4

# Maximum stories retained in stories.json.
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

MODEL = (
    os.environ.get("OPENAI_MODEL")
    or "gpt-6-luna"
)


# ============================================================
# GENERAL TEXT HELPERS
# ============================================================

def clean_text(value):
    """Convert HTML/feed content into clean plain text."""

    if not value:
        return ""

    value = html.unescape(str(value))

    value = re.sub(
        r"<[^>]+>",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def normalise_ai_text(value):
    """
    Convert GPT output safely into normal text.

    Handles:
    - strings
    - lists
    - dictionaries
    - other simple values
    """

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):

        pieces = []

        for item in value.values():

            text = normalise_ai_text(
                item
            )

            if text:
                pieces.append(text)

        return "\n\n".join(
            pieces
        ).strip()

    if isinstance(value, list):

        pieces = []

        for item in value:

            text = normalise_ai_text(
                item
            )

            if text:
                pieces.append(text)

        return "\n\n".join(
            pieces
        ).strip()

    return str(value).strip()


def parse_ai_json(text):
    """Safely parse JSON returned by GPT."""

    text = (
        text
        or ""
    ).strip()

    # Remove accidental markdown fences.
    if text.startswith("```"):

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"\s*```$",
            "",
            text,
        )

    try:

        return json.loads(text)

    except json.JSONDecodeError:

        match = re.search(
            r"\{.*\}",
            text,
            re.DOTALL,
        )

        if not match:

            raise ValueError(
                "GPT did not return valid JSON."
            )

        return json.loads(
            match.group(0)
        )


# ============================================================
# WEBPAGE FETCHING
# ============================================================

def fetch_page(url):
    """Download a source article page."""

    try:

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; PolicyDossierBot/1.0)"
                )
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=12,
        ) as response:

            return response.read(
                800000
            ).decode(
                "utf-8",
                "ignore",
            )

    except Exception as error:

        print(
            f"Could not fetch article "
            f"{url}: {error}"
        )

        return ""


# ============================================================
# ARTICLE IMAGE EXTRACTION
# ============================================================

def looks_like_generic_image(url, alt=""):
    """
    Reject obvious logos, branding, placeholders,
    favicons and other non-news images.
    """

    text = (
        f"{url} {alt}"
    ).lower()

    blocked_terms = (
        "logo",
        "site-logo",
        "site_logo",
        "brand-logo",
        "brand_logo",
        "favicon",
        "placeholder",
        "default-image",
        "default_image",
        "defaultimage",
        "avatar",
        "sprite",
        "icon",
        "loading.gif",
        "blank.gif",
        "ads/",
        "/advert",
        "advertisement",
        "newsletter",
        "social-icon",
        "social_icon",
    )

    return any(
        term in text
        for term in blocked_terms
    )


def make_absolute_url(
    page_url,
    image_url,
):
    """Convert relative image URL to absolute URL."""

    if not image_url:
        return ""

    image_url = html.unescape(
        str(image_url).strip()
    )

    if image_url.startswith(
        "data:"
    ):
        return ""

    image_url = urllib.parse.urljoin(
        page_url,
        image_url,
    )

    if not image_url.startswith(
        ("http://", "https://")
    ):
        return ""

    return image_url


def extract_image_values(value):
    """
    Extract possible image URLs from JSON-LD image values.
    """

    results = []

    if isinstance(value, str):

        results.append(value)

    elif isinstance(value, list):

        for item in value:

            results.extend(
                extract_image_values(
                    item
                )
            )

    elif isinstance(value, dict):

        for key in (
            "url",
            "contentUrl",
            "@id",
        ):

            if value.get(key):

                results.extend(
                    extract_image_values(
                        value[key]
                    )
                )

    return results


def extract_jsonld_article_images(
    data,
):
    """
    Look specifically for images attached to Article /
    NewsArticle structured data.
    """

    images = []

    if isinstance(data, list):

        for item in data:

            images.extend(
                extract_jsonld_article_images(
                    item
                )
            )

        return images

    if not isinstance(
        data,
        dict,
    ):
        return images

    article_types = {
        "Article",
        "NewsArticle",
        "ReportageNewsArticle",
        "AnalysisNewsArticle",
    }

    types = data.get(
        "@type",
        [],
    )

    if isinstance(types, str):
        types = [types]

    if any(
        item in article_types
        for item in types
    ):

        images.extend(
            extract_image_values(
                data.get(
                    "image"
                )
            )
        )

        images.extend(
            extract_image_values(
                data.get(
                    "thumbnailUrl"
                )
            )
        )

    # JSON-LD commonly contains @graph.
    for value in data.values():

        if isinstance(
            value,
            (dict, list),
        ):

            images.extend(
                extract_jsonld_article_images(
                    value
                )
            )

    return images


def find_actual_article_image(
    page_url,
    soup,
):
    """
    Find the actual photograph associated with a story.

    Priority:
    1. NewsArticle JSON-LD image
    2. Open Graph article image
    3. Twitter article image
    4. Image within <article>
    5. Image within <main>

    It NEVER takes a screenshot of the webpage.
    """

    candidates = []


    # --------------------------------------------------------
    # 1. STRUCTURED ARTICLE DATA
    # --------------------------------------------------------

    for script in soup.find_all(
        "script",
        type=lambda value: (
            value
            and "ld+json" in value
        ),
    ):

        try:

            raw = (
                script.string
                or script.get_text()
                or ""
            )

            data = json.loads(raw)

            candidates.extend(
                (
                    image,
                    "",
                )
                for image
                in extract_jsonld_article_images(
                    data
                )
            )

        except Exception:
            continue


    # --------------------------------------------------------
    # 2. OPEN GRAPH IMAGE
    # --------------------------------------------------------

    og_image = soup.find(
        "meta",
        attrs={
            "property": "og:image"
        },
    )

    if (
        og_image
        and og_image.get(
            "content"
        )
    ):

        candidates.append(
            (
                og_image[
                    "content"
                ],
                "",
            )
        )


    # --------------------------------------------------------
    # 3. TWITTER IMAGE
    # --------------------------------------------------------

    twitter_image = soup.find(
        "meta",
        attrs={
            "name": "twitter:image"
        },
    )

    if (
        twitter_image
        and twitter_image.get(
            "content"
        )
    ):

        candidates.append(
            (
                twitter_image[
                    "content"
                ],
                "",
            )
        )


    # --------------------------------------------------------
    # 4. IMAGE INSIDE ARTICLE
    # --------------------------------------------------------

    article = soup.find(
        "article"
    )

    if article:

        for image in article.find_all(
            "img",
            limit=10,
        ):

            url = (
                image.get(
                    "src"
                )
                or image.get(
                    "data-src"
                )
                or image.get(
                    "data-lazy-src"
                )
                or ""
            )

            candidates.append(
                (
                    url,
                    image.get(
                        "alt",
                        "",
                    ),
                )
            )


    # --------------------------------------------------------
    # 5. IMAGE INSIDE MAIN
    # --------------------------------------------------------

    main = soup.find(
        "main"
    )

    if main:

        for image in main.find_all(
            "img",
            limit=10,
        ):

            url = (
                image.get(
                    "src"
                )
                or image.get(
                    "data-src"
                )
                or image.get(
                    "data-lazy-src"
                )
                or ""
            )

            candidates.append(
                (
                    url,
                    image.get(
                        "alt",
                        "",
                    ),
                )
            )


    # --------------------------------------------------------
    # PICK FIRST PLAUSIBLE NEWS PHOTO
    # --------------------------------------------------------

    for image_url, alt in candidates:

        image_url = make_absolute_url(
            page_url,
            image_url,
        )

        if not image_url:
            continue

        if looks_like_generic_image(
            image_url,
            alt,
        ):
            continue

        return image_url

    return ""


# ============================================================
# ARTICLE TEXT EXTRACTION
# ============================================================

def extract_paragraphs(
    container,
):
    """Extract meaningful paragraphs from an article container."""

    if not container:
        return []

    paragraphs = []

    for paragraph in container.find_all(
        "p"
    ):

        text = clean_text(
            paragraph.get_text(
                " ",
                strip=True,
            )
        )

        # Ignore tiny labels/navigation text.
        if len(text) < 35:
            continue

        paragraphs.append(text)

    return paragraphs


def find_article_text(
    soup,
):
    """
    Extract the actual article text where possible.

    It does not store or reproduce the original article.
    The text is only sent to GPT so it can write a
    summarised Policy Dossier version.
    """

    # First preference: semantic article element.
    article = soup.find(
        "article"
    )

    paragraphs = extract_paragraphs(
        article
    )

    # Fallback: main content area.
    if len(paragraphs) < 2:

        main = soup.find(
            "main"
        )

        paragraphs = extract_paragraphs(
            main
        )

    # Avoid excessive API input.
    text = "\n\n".join(
        paragraphs
    )

    return text[:10000]


def fetch_article_data(url):
    """
    Fetch original article and return:
    - article text
    - actual article image
    """

    page = fetch_page(url)

    if not page:
        return "", ""

    soup = BeautifulSoup(
        page,
        "html.parser",
    )

    article_text = find_article_text(
        soup
    )

    article_image = (
        find_actual_article_image(
            url,
            soup,
        )
    )

    return (
        article_text,
        article_image,
    )


# ============================================================
# REMOVE REPEATED / GENERIC IMAGES
# ============================================================

def normalised_image_url(url):
    """
    Normalise image URL to detect the same publisher image
    being reused across several stories.
    """

    if not url:
        return ""

    parsed = urllib.parse.urlsplit(
        url
    )

    return (
        f"{parsed.scheme}://"
        f"{parsed.netloc}"
        f"{parsed.path}"
    ).lower()


def remove_duplicate_images(
    stories,
):
    """
    If several different stories return the exact same image,
    assume it may be a generic publisher image.

    Remove it from ALL those stories rather than displaying
    the repeated publisher logo.
    """

    values = [
        normalised_image_url(
            story.get(
                "image",
                "",
            )
        )
        for story in stories
    ]

    counts = Counter(
        value
        for value in values
        if value
    )

    repeated = {
        value
        for value, count
        in counts.items()
        if count > 1
    }

    for story in stories:

        value = normalised_image_url(
            story.get(
                "image",
                "",
            )
        )

        if value in repeated:

            print(
                "Removed repeated image "
                f"for {story['title']}"
            )

            story["image"] = ""


# ============================================================
# SOURCE DIVERSITY
# ============================================================

def choose_diverse_stories(
    candidates,
    limit,
):
    """
    Choose one story from each publisher first.

    Nigeria is prioritised.

    Example:
    Punch
    Channels TV
    Premium Times
    Nairametrics

    rather than:
    Punch
    Punch
    Punch
    Punch
    """

    selected = []

    selected_ids = set()

    used_sources = set()


    # --------------------------------------------------------
    # FIRST: UNIQUE NIGERIAN SOURCES
    # --------------------------------------------------------

    for story in candidates:

        if story[
            "region"
        ] != "Nigeria":
            continue

        if story[
            "source"
        ] in used_sources:
            continue

        selected.append(
            story
        )

        selected_ids.add(
            story["id"]
        )

        used_sources.add(
            story["source"]
        )

        if len(
            selected
        ) >= limit:

            return selected


    # --------------------------------------------------------
    # SECOND: UNIQUE WORLD SOURCES
    # --------------------------------------------------------

    for story in candidates:

        if story[
            "region"
        ] != "World":
            continue

        if story[
            "source"
        ] in used_sources:
            continue

        selected.append(
            story
        )

        selected_ids.add(
            story["id"]
        )

        used_sources.add(
            story["source"]
        )

        if len(
            selected
        ) >= limit:

            return selected


    # --------------------------------------------------------
    # THIRD: FILL ANY EMPTY SPACES
    # --------------------------------------------------------

    for story in candidates:

        if story[
            "id"
        ] in selected_ids:
            continue

        selected.append(
            story
        )

        selected_ids.add(
            story["id"]
        )

        if len(
            selected
        ) >= limit:
            break

    return selected


# ============================================================
# GPT ARTICLE WRITING
# ============================================================

def write_up(
    client,
    title,
    source,
    article_text,
    rss_text,
):
    """
    GPT creates TWO outputs:

    short:
        short homepage/X teaser

    body:
        proper Policy Dossier website article
    """

    source_material = (
        article_text
        if len(
            article_text
        ) >= 200
        else clean_text(
            rss_text
        )
    )

    if not source_material:

        raise ValueError(
            "No usable source text."
        )

    prompt = f"""
You are writing for Policy Dossier, a neutral news and
public-affairs publication.

Create TWO different outputs from the source material.

Return ONLY valid JSON using exactly this structure:

{{
  "short": "Short teaser here",
  "body": "Website article here"
}}

SHORT:

- Write 2 concise sentences.
- Maximum approximately 160 characters.
- This is for the homepage and X.
- State the main development clearly.
- No hashtags.
- No emojis.
- No clickbait.

BODY:

- Write a neutral Policy Dossier article.
- Usually 250 to 450 words when enough information exists.
- Use 3 to 5 paragraphs.
- Explain:
  1. what happened;
  2. the important details;
  3. why the development matters;
  4. relevant context supported by the source.
- Write completely in your own words.
- Do not reproduce the source article.
- Do not invent facts.
- Do not invent quotations.
- Do not invent numbers, dates, people or organisations.
- Do not add outside information.
- If the source does not provide enough information for
  250 words, write a shorter accurate article instead.
- Do not mention AI.
- Do not mention these instructions.

Original publication:
{source}

Original headline:
{title}

Source material:
{source_material[:10000]}
""".strip()

    response = client.responses.create(
        model=MODEL,
        input=prompt,
    )

    result = parse_ai_json(
        response.output_text
    )

    short = normalise_ai_text(
        result.get(
            "short",
            "",
        )
    )

    body = normalise_ai_text(
        result.get(
            "body",
            "",
        )
    )

    if not short:

        raise ValueError(
            "GPT returned no short teaser."
        )

    if not body:

        raise ValueError(
            "GPT returned no article body."
        )

    return {
        "short": short,
        "body": body,
    }


# ============================================================
# STORY PAGE TEMPLATE
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

Policy Dossier automatically produces a neutral explainer
from source material supplied by the original publication.

Read the linked original report for the complete source.

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
# GENERATE WEBSITE STORY PAGES
# ============================================================

def write_pages(stories):

    os.makedirs(
        "stories",
        exist_ok=True,
    )

    for story in stories:

        def escape(value):

            return html.escape(
                str(value),
                quote=True,
            )

        image = story.get(
            "image",
            "",
        )

        article_text = (
            story.get(
                "body"
            )
            or story.get(
                "summary"
            )
            or ""
        )

        paragraphs = [
            paragraph.strip()
            for paragraph
            in re.split(
                r"\n+",
                article_text,
            )
            if paragraph.strip()
        ]

        body_html = "".join(
            f"<p>{escape(paragraph)}</p>"
            for paragraph
            in paragraphs
        )

        article_url = (
            f"{SITE_URL}/stories/"
            f"{story['id']}.html"
        )

        if image:

            og_image = (
                '<meta property="og:image" '
                f'content="{escape(image)}">'
            )

            card = (
                "summary_large_image"
            )

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

        region = (
            "<i>Nigeria</i>"
            if story.get(
                "region"
            ) == "Nigeria"
            else "World"
        )

        page = PAGE.substitute(
            title=escape(
                story["title"]
            ),
            summary=escape(
                story["summary"]
            ),
            url=escape(
                article_url
            ),
            ogimg=og_image,
            card=card,
            source=escape(
                story["source"]
            ),
            xurl=escape(
                X_URL
            ),
            region=region,
            date=escape(
                story.get(
                    "published",
                    "",
                )[:10]
            ),
            link=escape(
                story["link"]
            ),
            body=body_html,
            hero=hero,
        )

        filename = (
            f"stories/"
            f"{story['id']}.html"
        )

        with open(
            filename,
            "w",
            encoding="utf-8",
        ) as file:

            file.write(page)


# ============================================================
# LOAD / SAVE
# ============================================================

def load_stories():

    try:

        with open(
            FILE,
            encoding="utf-8",
        ) as file:

            stories = json.load(
                file
            )

        if not isinstance(
            stories,
            list,
        ):

            return []

        # Existing old stories should NOT suddenly repost to X.
        for story in stories:

            story.setdefault(
                "x_posted",
                True,
            )

        return stories

    except (
        FileNotFoundError,
        json.JSONDecodeError,
    ):

        return []


def save_stories(stories):

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


# ============================================================
# X
# ============================================================

def create_x_client():

    required = [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_SECRET",
    ]

    missing = [
        name
        for name
        in required
        if not os.environ.get(
            name
        )
    ]

    if missing:

        print(
            "X posting disabled. "
            "Missing: "
            + ", ".join(
                missing
            )
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
    Create a short X update.

    Website gets the full article.
    X gets only a few lines.
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

    if len(title) > 80:

        title = (
            title[:77].rstrip()
            + "..."
        )

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

    # Final emergency trim.
    if len(tweet) > 275:

        excess = (
            len(tweet)
            - 275
        )

        new_length = max(
            40,
            len(summary)
            - excess
            - 3,
        )

        summary = (
            summary[
                :new_length
            ].rstrip()
            + "..."
        )

        tweet = (
            f"{title}\n\n"
            f"{summary}\n\n"
            f"Read more: {article_url}"
        )

    return tweet


def post_pending_to_x(
    stories,
    x_client,
    max_posts=4,
):
    """
    Post stories that have not yet successfully reached X.
    """

    if x_client is None:
        return

    sent = 0

    for story in stories:

        if sent >= max_posts:
            break

        if story.get(
            "x_posted",
            True,
        ):
            continue

        try:

            x_client.create_tweet(
                text=build_x_post(
                    story
                )
            )

            story[
                "x_posted"
            ] = True

            sent += 1

            print(
                "Posted to X:",
                story["title"],
            )

        except Exception as error:

            print(
                "X post failed for "
                f"{story['title']}: "
                f"{error}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    existing_stories = (
        load_stories()
    )

    existing_ids = {
        story["id"]
        for story
        in existing_stories
        if story.get(
            "id"
        )
    }

    candidate_ids = set()

    candidates = []

    now = dt.datetime.now(
        dt.timezone.utc
    )


    # --------------------------------------------------------
    # FETCH RSS FEEDS
    # --------------------------------------------------------

    for source, (
        feed_url,
        region,
    ) in FEEDS.items():

        try:

            feed = feedparser.parse(
                feed_url
            )

        except Exception as error:

            print(
                f"Feed failed: "
                f"{source}: "
                f"{error}"
            )

            continue


        for entry in (
            feed.entries[:10]
        ):

            link = entry.get(
                "link"
            )

            title = clean_text(
                entry.get(
                    "title",
                    "",
                )
            )

            if (
                not link
                or not title
            ):

                continue

            story_id = hashlib.sha1(
                link.encode(
                    "utf-8"
                )
            ).hexdigest()[:16]

            if (
                story_id
                in existing_ids
            ):

                continue

            if (
                story_id
                in candidate_ids
            ):

                continue

            candidate_ids.add(
                story_id
            )


            # -----------------------------------------------
            # PUBLICATION DATE
            # -----------------------------------------------

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

                timestamp = (
                    calendar.timegm(
                        published_struct
                    )
                )

                published = (
                    dt.datetime.fromtimestamp(
                        timestamp,
                        tz=dt.timezone.utc,
                    )
                )

            else:

                published = now


            # -----------------------------------------------
            # RSS FALLBACK TEXT
            # -----------------------------------------------

            rss_text = (
                entry.get(
                    "summary",
                    "",
                )
                or "".join(
                    item.get(
                        "value",
                        "",
                    )
                    for item
                    in entry.get(
                        "content",
                        [],
                    )
                )
            )


            candidates.append(
                {
                    "id": story_id,
                    "title": title,
                    "link": link,
                    "source": source,
                    "region": region,
                    "rss_text": rss_text,
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


    # --------------------------------------------------------
    # SORT NEWEST FIRST
    # --------------------------------------------------------

    candidates.sort(
        key=lambda story: (
            story[
                "published"
            ]
        ),
        reverse=True,
    )


    # --------------------------------------------------------
    # NIGERIA FIRST
    # --------------------------------------------------------

    candidates.sort(
        key=lambda story: (
            story[
                "region"
            ] != "Nigeria"
        )
    )


    # --------------------------------------------------------
    # CHOOSE DIFFERENT PUBLISHERS
    # --------------------------------------------------------

    selected = (
        choose_diverse_stories(
            candidates,
            MAX_NEW_PER_RUN,
        )
    )

    print(
        "Selected stories:"
    )

    for story in selected:

        print(
            f"- {story['source']}: "
            f"{story['title']}"
        )


    # --------------------------------------------------------
    # GET ACTUAL ARTICLE TEXT + PHOTO
    # --------------------------------------------------------

    for story in selected:

        article_text, image = (
            fetch_article_data(
                story["link"]
            )
        )

        story[
            "article_text"
        ] = article_text

        story[
            "image"
        ] = image


    # If the same image appears on more than one story,
    # remove it from all of them.
    remove_duplicate_images(
        selected
    )


    # --------------------------------------------------------
    # OPENAI
    # --------------------------------------------------------

    ai = OpenAI()

    processed = []

    for story in selected:

        try:

            result = write_up(
                ai,
                story["title"],
                story["source"],
                story.get(
                    "article_text",
                    "",
                ),
                story.get(
                    "rss_text",
                    "",
                ),
            )

        except Exception as error:

            print(
                "GPT failed for "
                f"{story['title']}: "
                f"{error}"
            )

            continue


        story[
            "summary"
        ] = result[
            "short"
        ].strip()

        story[
            "body"
        ] = result[
            "body"
        ].strip()

        story[
            "x_posted"
        ] = False


        # Never store source article text in stories.json.
        story.pop(
            "article_text",
            None,
        )

        story.pop(
            "rss_text",
            None,
        )

        processed.append(
            story
        )


    # --------------------------------------------------------
    # ADD NEW STORIES
    # --------------------------------------------------------

    stories = (
        processed
        + existing_stories
    )[:KEEP]


    # --------------------------------------------------------
    # GENERATE WEBSITE ARTICLES
    # --------------------------------------------------------

    write_pages(
        stories
    )


    # --------------------------------------------------------
    # POST SHORT VERSION TO X
    # --------------------------------------------------------

    x_client = (
        create_x_client()
    )

    post_pending_to_x(
        stories,
        x_client,
        max_posts=MAX_NEW_PER_RUN,
    )


    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    save_stories(
        stories
    )

    print(
        f"Completed. "
        f"{len(processed)} new "
        f"stories published."
    )


if __name__ == "__main__":
    main()
