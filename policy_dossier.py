"""
Policy Dossier

Every scheduled run:

1. Fetch latest news from selected RSS feeds.
2. Prioritise Nigerian news.
3. Select up to 4 stories from different publishers.
4. Open the original article.
5. Extract the article text.
6. Find the actual photograph used for that news story.
7. Download that photograph into this GitHub project.
8. Use OpenAI to create:
   - a short homepage/X summary
   - a fuller neutral website article
9. Generate individual article pages.
10. Save stories.json.
11. Post ONE story to X per run.

Designed for GitHub Actions.
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
from pathlib import Path
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


# Website: up to 4 new articles per run.
MAX_NEW_PER_RUN = 4

# X: only 1 post per scheduled run.
MAX_X_POSTS_PER_RUN = 1

# Maximum number of stories retained.
KEEP = 200

STORIES_FILE = "stories.json"

IMAGES_DIR = Path("images")
STORY_PAGES_DIR = Path("stories")

SITE_URL = (
    os.environ.get("SITE_URL")
    or "https://YOUR-SITE.com"
).rstrip("/")

X_URL = (
    os.environ.get("X_URL")
    or "https://x.com/YOUR_HANDLE"
)

# You can override this with an OPENAI_MODEL GitHub variable.
MODEL = (
    os.environ.get("OPENAI_MODEL")
    or "gpt-4o-mini"
)


# ============================================================
# BASIC TEXT HELPERS
# ============================================================

def clean_text(value):
    """Convert HTML/source text to clean plain text."""

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
    Convert AI output to plain text safely.

    Handles strings, lists and dictionaries.
    """

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        pieces = []

        for item in value.values():
            text = normalise_ai_text(item)

            if text:
                pieces.append(text)

        return "\n\n".join(pieces).strip()

    if isinstance(value, list):
        pieces = []

        for item in value:
            text = normalise_ai_text(item)

            if text:
                pieces.append(text)

        return "\n\n".join(pieces).strip()

    return str(value).strip()


def parse_ai_json(text):
    """Parse JSON returned by OpenAI."""

    text = (text or "").strip()

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
                "OpenAI did not return valid JSON."
            )

        return json.loads(
            match.group(0)
        )


# ============================================================
# FETCH ARTICLE WEBPAGE
# ============================================================

def fetch_page(url):
    """Download the original article webpage."""

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
            timeout=15,
        ) as response:
            return response.read(
                1_000_000
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
# ARTICLE TEXT
# ============================================================

def extract_paragraphs(container):
    """Extract useful article paragraphs."""

    if not container:
        return []

    output = []

    for paragraph in container.find_all("p"):
        text = clean_text(
            paragraph.get_text(
                " ",
                strip=True,
            )
        )

        # Ignore tiny captions/navigation labels.
        if len(text) < 35:
            continue

        output.append(text)

    return output


def find_article_text(soup):
    """
    Find the main news article text.

    The original text is not stored on Policy Dossier.
    It is only used as input for the AI summary/article.
    """

    paragraphs = []

    # First choice: semantic <article>.
    article = soup.find("article")

    if article:
        paragraphs = extract_paragraphs(
            article
        )

    # Fallback: <main>.
    if len(paragraphs) < 2:
        main = soup.find("main")

        if main:
            paragraphs = extract_paragraphs(
                main
            )

    # Fallback to common article-content containers.
    if len(paragraphs) < 2:
        selectors = [
            "[class*='article-content']",
            "[class*='article-body']",
            "[class*='story-content']",
            "[class*='story-body']",
            "[class*='entry-content']",
            "[class*='post-content']",
        ]

        for selector in selectors:
            container = soup.select_one(
                selector
            )

            if container:
                found = extract_paragraphs(
                    container
                )

                if len(found) >= 2:
                    paragraphs = found
                    break

    text = "\n\n".join(
        paragraphs
    )

    # Keep API input controlled.
    return text[:12000]


# ============================================================
# ACTUAL NEWS PHOTO EXTRACTION
# ============================================================

def make_absolute_url(
    page_url,
    image_url,
):
    """Convert a relative image URL into an absolute URL."""

    if not image_url:
        return ""

    image_url = html.unescape(
        str(image_url).strip()
    )

    if image_url.startswith("data:"):
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


def looks_generic_image(
    image_url,
    alt="",
):
    """
    Reject logos, avatars, adverts, placeholders,
    icons and obvious publisher branding.
    """

    text = (
        f"{image_url} {alt}"
    ).lower()

    blocked = (
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
        "profile",
        "author-image",
        "author_image",
        "headshot",
        "sprite",
        "social-icon",
        "social_icon",
        "newsletter",
        "advertisement",
        "/advert/",
        "/ads/",
        "loading.gif",
        "blank.gif",
    )

    return any(
        word in text
        for word in blocked
    )


def best_srcset_url(
    srcset,
    page_url,
):
    """Pick the highest-resolution image from srcset."""

    if not srcset:
        return ""

    candidates = []

    for part in srcset.split(","):
        part = part.strip()

        if not part:
            continue

        pieces = part.split()

        url = pieces[0]

        score = 1

        if len(pieces) > 1:
            descriptor = pieces[1].lower()

            try:
                if descriptor.endswith("w"):
                    score = int(
                        descriptor[:-1]
                    )

                elif descriptor.endswith("x"):
                    score = (
                        float(
                            descriptor[:-1]
                        )
                        * 1000
                    )

            except ValueError:
                pass

        candidates.append(
            (score, url)
        )

    if not candidates:
        return ""

    candidates.sort(
        reverse=True
    )

    return make_absolute_url(
        page_url,
        candidates[0][1],
    )


def image_from_tag(
    image,
    page_url,
):
    """Extract the best URL from an image element."""

    alt = (
        image.get("alt")
        or image.get("title")
        or ""
    )

    # High-resolution responsive images first.
    for attribute in (
        "srcset",
        "data-srcset",
        "data-lazy-srcset",
    ):
        url = best_srcset_url(
            image.get(
                attribute,
                "",
            ),
            page_url,
        )

        if (
            url
            and not looks_generic_image(
                url,
                alt,
            )
        ):
            return url

    # Normal and lazy-loading image attributes.
    for attribute in (
        "data-original",
        "data-lazy-src",
        "data-src",
        "data-lazy",
        "src",
    ):
        url = make_absolute_url(
            page_url,
            image.get(
                attribute,
                "",
            ),
        )

        if (
            url
            and not looks_generic_image(
                url,
                alt,
            )
        ):
            return url

    return ""


def extract_image_values(value):
    """Extract image URLs from JSON-LD."""

    output = []

    if isinstance(value, str):
        output.append(value)

    elif isinstance(value, list):
        for item in value:
            output.extend(
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
                output.extend(
                    extract_image_values(
                        value[key]
                    )
                )

    return output


def extract_jsonld_article_images(data):
    """Extract images specifically attached to NewsArticle data."""

    images = []

    if isinstance(data, list):
        for item in data:
            images.extend(
                extract_jsonld_article_images(
                    item
                )
            )

        return images

    if not isinstance(data, dict):
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
                data.get("image")
            )
        )

        images.extend(
            extract_image_values(
                data.get(
                    "thumbnailUrl"
                )
            )
        )

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
    Find the actual photograph belonging to the story.

    Priority:
    1. Figure/picture inside article
    2. Images inside article
    3. Article structured-data image
    4. Open Graph image
    5. Twitter image

    No webpage screenshots.
    No publisher-logo fallback.
    """

    candidates = []

    article = soup.find("article")


    # --------------------------------------------------------
    # FIGURES INSIDE ARTICLE
    # --------------------------------------------------------

    if article:
        for figure in article.find_all(
            "figure",
            limit=15,
        ):
            picture = figure.find(
                "picture"
            )

            if picture:
                for source in picture.find_all(
                    "source"
                ):
                    url = best_srcset_url(
                        source.get(
                            "srcset",
                            "",
                        )
                        or source.get(
                            "data-srcset",
                            "",
                        ),
                        page_url,
                    )

                    if (
                        url
                        and not looks_generic_image(
                            url
                        )
                    ):
                        candidates.append(
                            url
                        )

            image = figure.find("img")

            if image:
                url = image_from_tag(
                    image,
                    page_url,
                )

                if url:
                    candidates.append(
                        url
                    )


    # --------------------------------------------------------
    # OTHER IMAGES INSIDE ARTICLE
    # --------------------------------------------------------

    if article:
        for image in article.find_all(
            "img",
            limit=20,
        ):
            url = image_from_tag(
                image,
                page_url,
            )

            if url:
                candidates.append(
                    url
                )


    # --------------------------------------------------------
    # COMMON ARTICLE BODY CONTAINERS
    # --------------------------------------------------------

    for selector in (
        "[class*='article-content']",
        "[class*='article-body']",
        "[class*='story-content']",
        "[class*='story-body']",
        "[class*='entry-content']",
        "[class*='post-content']",
    ):
        container = soup.select_one(
            selector
        )

        if not container:
            continue

        for image in container.find_all(
            "img",
            limit=10,
        ):
            url = image_from_tag(
                image,
                page_url,
            )

            if url:
                candidates.append(
                    url
                )


    # --------------------------------------------------------
    # NEWSARTICLE JSON-LD
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

            for image_url in (
                extract_jsonld_article_images(
                    data
                )
            ):
                url = make_absolute_url(
                    page_url,
                    image_url,
                )

                if (
                    url
                    and not looks_generic_image(
                        url
                    )
                ):
                    candidates.append(
                        url
                    )

        except Exception:
            continue


    # --------------------------------------------------------
    # OPEN GRAPH IMAGE
    # --------------------------------------------------------

    og_image = soup.find(
        "meta",
        attrs={
            "property": "og:image"
        },
    )

    og_alt = soup.find(
        "meta",
        attrs={
            "property":
                "og:image:alt"
        },
    )

    if (
        og_image
        and og_image.get(
            "content"
        )
    ):
        url = make_absolute_url(
            page_url,
            og_image["content"],
        )

        alt = (
            og_alt.get(
                "content",
                "",
            )
            if og_alt
            else ""
        )

        if (
            url
            and not looks_generic_image(
                url,
                alt,
            )
        ):
            candidates.append(
                url
            )


    # --------------------------------------------------------
    # TWITTER IMAGE
    # --------------------------------------------------------

    twitter_image = (
        soup.find(
            "meta",
            attrs={
                "name":
                    "twitter:image"
            },
        )
        or soup.find(
            "meta",
            attrs={
                "property":
                    "twitter:image"
            },
        )
    )

    if (
        twitter_image
        and twitter_image.get(
            "content"
        )
    ):
        url = make_absolute_url(
            page_url,
            twitter_image[
                "content"
            ],
        )

        if (
            url
            and not looks_generic_image(
                url
            )
        ):
            candidates.append(
                url
            )


    # --------------------------------------------------------
    # RETURN FIRST UNIQUE PLAUSIBLE IMAGE
    # --------------------------------------------------------

    seen = set()

    for image_url in candidates:
        normalized = image_url.split(
            "?"
        )[0].lower()

        if normalized in seen:
            continue

        seen.add(normalized)

        return image_url

    return ""


# ============================================================
# DOWNLOAD STORY PHOTO INTO GITHUB
# ============================================================

def download_story_image(
    image_url,
    story_id,
):
    """
    Download the actual news photograph into /images.

    This avoids publisher hotlink-blocking and makes the
    image load from the Policy Dossier GitHub Pages site.
    """

    if not image_url:
        return ""

    IMAGES_DIR.mkdir(
        exist_ok=True
    )

    try:
        request = urllib.request.Request(
            image_url,
            headers={
                "User-Agent":
                    "Mozilla/5.0"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            content_type = (
                response.headers
                .get(
                    "Content-Type",
                    "",
                )
                .split(";")[0]
                .lower()
            )

            data = response.read(
                8_000_000
            )


        # Reject tiny files that are likely icons.
        if len(data) < 15_000:
            print(
                "Rejected tiny image:",
                image_url,
            )
            return ""


        extensions = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }

        extension = extensions.get(
            content_type
        )

        if not extension:
            path = urllib.parse.urlsplit(
                image_url
            ).path.lower()

            if path.endswith(
                (".jpg", ".jpeg")
            ):
                extension = ".jpg"

            elif path.endswith(".png"):
                extension = ".png"

            elif path.endswith(".webp"):
                extension = ".webp"

            else:
                print(
                    "Unsupported image type:",
                    content_type,
                    image_url,
                )
                return ""


        filename = (
            f"{story_id}"
            f"{extension}"
        )

        local_path = (
            IMAGES_DIR
            / filename
        )

        local_path.write_bytes(
            data
        )

        # Absolute site URL.
        return (
            f"{SITE_URL}/images/"
            f"{filename}"
        )

    except Exception as error:
        print(
            "Image download failed:",
            image_url,
            error,
        )

        return ""


# ============================================================
# FETCH ARTICLE DATA
# ============================================================

def fetch_article_data(
    url,
    story_id,
):
    """
    Return:
    - extracted article text
    - locally hosted actual story photograph
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

    remote_image = (
        find_actual_article_image(
            url,
            soup,
        )
    )

    local_image = (
        download_story_image(
            remote_image,
            story_id,
        )
        if remote_image
        else ""
    )

    return (
        article_text,
        local_image,
    )


# ============================================================
# SELECT DIFFERENT PUBLISHERS
# ============================================================

def choose_diverse_stories(
    candidates,
    limit,
):
    """
    Prefer one story from each publisher.

    Nigeria comes first.
    """

    selected = []

    used_sources = set()

    used_ids = set()


    # --------------------------------------------------------
    # UNIQUE NIGERIAN PUBLISHERS
    # --------------------------------------------------------

    for story in candidates:
        if (
            story["region"]
            != "Nigeria"
        ):
            continue

        if (
            story["source"]
            in used_sources
        ):
            continue

        selected.append(
            story
        )

        used_sources.add(
            story["source"]
        )

        used_ids.add(
            story["id"]
        )

        if len(selected) >= limit:
            return selected


    # --------------------------------------------------------
    # UNIQUE WORLD PUBLISHERS
    # --------------------------------------------------------

    for story in candidates:
        if (
            story["region"]
            != "World"
        ):
            continue

        if (
            story["source"]
            in used_sources
        ):
            continue

        selected.append(
            story
        )

        used_sources.add(
            story["source"]
        )

        used_ids.add(
            story["id"]
        )

        if len(selected) >= limit:
            return selected


    # --------------------------------------------------------
    # FILL EMPTY SPACES
    # --------------------------------------------------------

    for story in candidates:
        if story["id"] in used_ids:
            continue

        selected.append(
            story
        )

        used_ids.add(
            story["id"]
        )

        if len(selected) >= limit:
            break

    return selected


# ============================================================
# OPENAI ARTICLE WRITING
# ============================================================

def write_up(
    client,
    title,
    source,
    article_text,
    rss_text,
):
    """
    Create:
    - short homepage/X summary
    - fuller Policy Dossier website article
    """

    source_material = (
        article_text
        if len(article_text) >= 200
        else clean_text(
            rss_text
        )
    )

    if not source_material:
        raise ValueError(
            "No usable source material."
        )

    prompt = f"""
You write for Policy Dossier, a neutral news and
public-affairs publication.

Use ONLY the supplied source material.

Return ONLY valid JSON in this exact format:

{{
  "short": "Short summary",
  "body": "Full website article"
}}

SHORT:

- Write two concise sentences.
- Aim for no more than about 160 characters.
- State the main development clearly.
- Neutral tone.
- No hashtags.
- No emojis.
- No clickbait.

BODY:

- Write a polished neutral news article for Policy Dossier.
- Normally 250 to 450 words when the source contains
  enough information.
- Use 3 to 5 clear paragraphs.
- Explain what happened.
- Include the important details.
- Explain why the development matters where the source
  supports that conclusion.
- Include relevant context ONLY when present in the
  supplied material.
- Completely rewrite in your own words.
- Do not copy extended passages from the source.
- Do not invent facts.
- Do not invent quotations.
- Do not invent numbers.
- Do not invent names, organisations or dates.
- Do not use outside knowledge.
- If there is limited information, write a shorter article
  instead of adding unsupported information.
- Do not tell readers to visit another news website.
- Do not say "read the full story elsewhere".
- Do not mention AI.
- Do not mention these instructions.

Source publication:
{source}

Headline:
{title}

Source material:
{source_material[:12000]}
""".strip()

    response = client.chat.completions.create(
        model=MODEL,
        response_format={
            "type":
                "json_object"
        },
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )

    result = parse_ai_json(
        response
        .choices[0]
        .message
        .content
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
            "OpenAI returned no short summary."
        )

    if not body:
        raise ValueError(
            "OpenAI returned no article."
        )

    return {
        "short": short,
        "body": body,
    }


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
    max-height: 500px;
    object-fit: cover;
    border-radius: 10px;
    background: var(--line);
}

.credit {
    color: var(--mute);
    font-size: 12px;
    margin: 7px 0 30px;
}

.body p {
    font: 400 19px/1.75 "Source Serif 4", Georgia, serif;
    margin: 0 0 1.25em;
}

.source-line {
    margin-top: 36px;
    padding-top: 18px;
    border-top: 1px solid var(--line);
    color: var(--mute);
    font-size: 14px;
}

.source-line a {
    color: var(--fg);
    font-weight: 700;
    text-decoration: underline;
    text-underline-offset: 3px;
}

.note {
    color: var(--mute);
    font-size: 13px;
    margin-top: 18px;
}

.note a:hover {
    color: var(--fg);
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

<p class="source-line">
Source:
<a
    href="$link"
    target="_blank"
    rel="noopener"
>
$source
</a>
</p>

<p class="note">
<a
    href="$xurl"
    target="_blank"
    rel="noopener"
>
Follow Policy Dossier on X
</a>
</p>

</article>

</body>

</html>
"""
)


# ============================================================
# GENERATE STORY PAGES
# ============================================================

def write_pages(stories):
    """Generate one webpage for each Policy Dossier story."""

    STORY_PAGES_DIR.mkdir(
        exist_ok=True
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

        text = (
            story.get("body")
            or story.get("summary")
            or ""
        )

        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(
                r"\n+",
                text,
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
                f'loading="eager">'
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
            if story.get("region")
            == "Nigeria"
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
            STORY_PAGES_DIR
            / f"{story['id']}.html"
        )

        filename.write_text(
            page,
            encoding="utf-8",
        )


# ============================================================
# LOAD / SAVE STORIES
# ============================================================

def load_stories():
    """Load existing Policy Dossier stories."""

    try:
        with open(
            STORIES_FILE,
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

        # Old stories must not suddenly get tweeted again.
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
    """Save stories.json."""

    with open(
        STORIES_FILE,
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
    """Create authenticated X client."""

    required = [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_SECRET",
    ]

    missing = [
        name
        for name in required
        if not os.environ.get(
            name
        )
    ]

    if missing:
        print(
            "X posting disabled. Missing: "
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
    """Build one concise X post."""

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

    if len(summary) > 100:
        summary = (
            summary[:97].rstrip()
            + "..."
        )

    tweet = (
        f"{title}\n\n"
        f"{summary}\n\n"
        f"{article_url}"
    )

    # Conservative final size limit.
    if len(tweet) > 270:
        allowed = max(
            40,
            100
            - (
                len(tweet)
                - 270
            ),
        )

        summary = (
            summary[:allowed]
            .rstrip()
            + "..."
        )

        tweet = (
            f"{title}\n\n"
            f"{summary}\n\n"
            f"{article_url}"
        )

    return tweet


def post_one_to_x(
    stories,
    x_client,
):
    """
    Publish at most ONE X post per workflow run.

    Intentionally non-X stories are marked posted so
    a permanent backlog does not build up.
    """

    if x_client is None:
        return

    pending = [
        story
        for story in stories
        if not story.get(
            "x_posted",
            True,
        )
    ]

    if not pending:
        print(
            "No X post waiting."
        )
        return

    # Oldest pending first, so a previously failed post
    # gets another chance before a newer one.
    story = pending[-1]

    try:
        x_client.create_tweet(
            text=build_x_post(
                story
            )
        )

        story[
            "x_posted"
        ] = True

        print(
            "Posted to X:",
            story["title"],
        )

    except Exception as error:
        print(
            "X post failed:",
            error,
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
        if story.get("id")
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


        for entry in feed.entries[:10]:

            link = entry.get(
                "link"
            )

            title = clean_text(
                entry.get(
                    "title",
                    "",
                )
            )

            if not link or not title:
                continue

            story_id = hashlib.sha1(
                link.encode(
                    "utf-8"
                )
            ).hexdigest()[:16]

            if story_id in existing_ids:
                continue

            if story_id in candidate_ids:
                continue

            candidate_ids.add(
                story_id
            )


            # -----------------------------------------------
            # PUBLISHED DATE
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
    # NEWEST STORIES FIRST
    # --------------------------------------------------------

    candidates.sort(
        key=lambda story:
            story["published"],
        reverse=True,
    )


    # --------------------------------------------------------
    # NIGERIAN STORIES FIRST
    # --------------------------------------------------------

    candidates.sort(
        key=lambda story:
            story["region"]
            != "Nigeria"
    )


    # --------------------------------------------------------
    # SELECT DIFFERENT PUBLISHERS
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
    # FETCH FULL ARTICLE + ACTUAL STORY PHOTO
    # --------------------------------------------------------

    for story in selected:
        article_text, image = (
            fetch_article_data(
                story["link"],
                story["id"],
            )
        )

        story[
            "article_text"
        ] = article_text

        story[
            "image"
        ] = image


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
                "OpenAI failed for "
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


        # By default these are website-only stories.
        story[
            "x_posted"
        ] = True


        # Do not store copied source article text.
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
    # CHOOSE ONLY ONE NEW STORY FOR X
    # --------------------------------------------------------

    if processed:
        processed[0][
            "x_posted"
        ] = False


    # --------------------------------------------------------
    # MERGE WITH EXISTING STORIES
    # --------------------------------------------------------

    stories = (
        processed
        + existing_stories
    )[:KEEP]


    # --------------------------------------------------------
    # GENERATE WEBSITE
    # --------------------------------------------------------

    write_pages(
        stories
    )


    # --------------------------------------------------------
    # POST ONE STORY TO X
    # --------------------------------------------------------

    x_client = (
        create_x_client()
    )

    post_one_to_x(
        stories,
        x_client,
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
        f"website stories published."
    )


if __name__ == "__main__":
    main()
