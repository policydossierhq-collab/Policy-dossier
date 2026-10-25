"""
Policy Dossier

Each scheduled run:

1. Fetch current news from RSS feeds.
2. Prioritise Nigerian news.
3. Select up to 4 stories from different publishers.
4. Fetch the original article.
5. Extract useful article text.
6. Find the actual story photograph.
7. Download that photograph into /images.
8. Use OpenAI to write:
   - a short homepage/X teaser
   - a fuller Policy Dossier article
9. Generate individual story pages.
10. Queue ONE new story for X.
11. Post ONE story that was created during an earlier run.
12. Save everything for GitHub Pages.

The publisher name is displayed ONLY at the bottom of
the individual article as:

    Source: Publisher Name
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

# Website articles created per run.
MAX_NEW_PER_RUN = 4

# X posts per run.
MAX_X_POSTS_PER_RUN = 1

# Maximum stories retained.
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

MODEL = (
    os.environ.get("OPENAI_MODEL")
    or "gpt-4o-mini"
)


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    """Convert HTML/source text into clean plain text."""

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
    """Safely convert AI output into normal text."""

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        parts = []

        for item in value.values():
            text = normalise_ai_text(item)

            if text:
                parts.append(text)

        return "\n\n".join(parts).strip()

    if isinstance(value, list):
        parts = []

        for item in value:
            text = normalise_ai_text(item)

            if text:
                parts.append(text)

        return "\n\n".join(parts).strip()

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
# FETCH WEBPAGE
# ============================================================

def fetch_page(url):
    """Download an article webpage."""

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; PolicyDossierBot/1.0)"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
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
# ARTICLE TEXT EXTRACTION
# ============================================================

def extract_paragraphs(container):

    if not container:
        return []

    paragraphs = []

    for paragraph in container.find_all("p"):

        text = clean_text(
            paragraph.get_text(
                " ",
                strip=True,
            )
        )

        if len(text) < 35:
            continue

        paragraphs.append(text)

    return paragraphs


def find_article_text(soup):
    """Find the main article text."""

    paragraphs = []

    article = soup.find("article")

    if article:
        paragraphs = extract_paragraphs(
            article
        )

    if len(paragraphs) < 2:

        main = soup.find("main")

        if main:
            paragraphs = extract_paragraphs(
                main
            )

    if len(paragraphs) < 2:

        selectors = (
            "[class*='article-content']",
            "[class*='article-body']",
            "[class*='story-content']",
            "[class*='story-body']",
            "[class*='entry-content']",
            "[class*='post-content']",
        )

        for selector in selectors:

            container = soup.select_one(
                selector
            )

            if not container:
                continue

            found = extract_paragraphs(
                container
            )

            if len(found) >= 2:
                paragraphs = found
                break

    text = "\n\n".join(
        paragraphs
    )

    return text[:12000]


# ============================================================
# IMAGE URL HELPERS
# ============================================================

def make_absolute_url(
    page_url,
    image_url,
):

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
    url,
    alt="",
):
    """
    Reject publisher logos, icons, avatars,
    placeholders and advertisements.
    """

    text = (
        f"{url} {alt}"
    ).lower()

    blocked = (
        "logo",
        "favicon",
        "placeholder",
        "default-image",
        "default_image",
        "defaultimage",
        "avatar",
        "author-image",
        "author_image",
        "profile-photo",
        "profile_image",
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

    if any(
        item in text
        for item in blocked
    ):
        return True

    size_match = re.search(
        r"(\d{2,4})x(\d{2,4})",
        text,
    )

    if size_match:

        width = int(
            size_match.group(1)
        )

        height = int(
            size_match.group(2)
        )

        if (
            width <= 250
            and height <= 250
        ):
            return True

    return False


def best_srcset_url(
    srcset,
    page_url,
):
    """Choose the largest image from srcset."""

    if not srcset:
        return ""

    choices = []

    for part in srcset.split(","):

        part = part.strip()

        if not part:
            continue

        bits = part.split()

        url = bits[0]

        score = 1

        if len(bits) > 1:

            descriptor = bits[1].lower()

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

        choices.append(
            (score, url)
        )

    if not choices:
        return ""

    choices.sort(
        reverse=True
    )

    return make_absolute_url(
        page_url,
        choices[0][1],
    )


def image_from_tag(
    image,
    page_url,
):

    alt = (
        image.get("alt")
        or image.get("title")
        or ""
    )

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


# ============================================================
# RSS IMAGE FALLBACK
# ============================================================

def find_feed_image(
    entry,
    article_url,
):
    """
    Use RSS image only as a fallback.

    Generic logos/placeholders are rejected.
    """

    for key in (
        "media_content",
        "media_thumbnail",
    ):

        for media in (
            entry.get(key)
            or []
        ):

            url = make_absolute_url(
                article_url,
                media.get(
                    "url",
                    "",
                ),
            )

            if (
                url
                and not looks_generic_image(
                    url
                )
            ):
                return url


    for link in entry.get(
        "links",
        [],
    ):

        if (
            link.get("rel")
            == "enclosure"
            and str(
                link.get(
                    "type",
                    "",
                )
            ).startswith("image")
        ):

            url = make_absolute_url(
                article_url,
                link.get(
                    "href",
                    "",
                ),
            )

            if (
                url
                and not looks_generic_image(
                    url
                )
            ):
                return url


    blob = (
        entry.get(
            "summary",
            "",
        )
        + "".join(
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

    match = re.search(
        r'<img[^>]+src=["\']([^"\']+)',
        blob,
        flags=re.IGNORECASE,
    )

    if match:

        url = make_absolute_url(
            article_url,
            match.group(1),
        )

        if (
            url
            and not looks_generic_image(
                url
            )
        ):
            return url

    return ""


# ============================================================
# JSON-LD IMAGE EXTRACTION
# ============================================================

def extract_image_values(value):

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


# ============================================================
# FIND ACTUAL STORY PHOTO
# ============================================================

def find_actual_article_image(
    article_url,
    soup,
):
    """
    Find the actual photograph used for the news story.

    Priority:
    1. figure/picture within article
    2. other images within article
    3. article-body images
    4. NewsArticle structured data
    5. Open Graph image
    6. Twitter image

    Publisher-logo fallback is not allowed.
    """

    candidates = []

    article = soup.find("article")


    # --------------------------------------------------------
    # ARTICLE FIGURES
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
                        article_url,
                    )

                    if (
                        url
                        and not looks_generic_image(
                            url
                        )
                    ):

                        candidates.append(
                            (120, url)
                        )


            image = figure.find(
                "img"
            )

            if image:

                url = image_from_tag(
                    image,
                    article_url,
                )

                if url:

                    candidates.append(
                        (115, url)
                    )


    # --------------------------------------------------------
    # OTHER ARTICLE IMAGES
    # --------------------------------------------------------

    if article:

        for image in article.find_all(
            "img",
            limit=20,
        ):

            url = image_from_tag(
                image,
                article_url,
            )

            if url:

                candidates.append(
                    (105, url)
                )


    # --------------------------------------------------------
    # ARTICLE BODY CONTAINERS
    # --------------------------------------------------------

    selectors = (
        "[class*='article-content']",
        "[class*='article-body']",
        "[class*='story-content']",
        "[class*='story-body']",
        "[class*='entry-content']",
        "[class*='post-content']",
    )

    for selector in selectors:

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
                article_url,
            )

            if url:

                candidates.append(
                    (100, url)
                )


    # --------------------------------------------------------
    # NEWSARTICLE JSON-LD
    # --------------------------------------------------------

    for script in soup.find_all(
        "script",
        type=lambda value: (
            value
            and "ld+json"
            in value
        ),
    ):

        try:

            raw = (
                script.string
                or script.get_text()
                or ""
            )

            data = json.loads(raw)

            urls = (
                extract_jsonld_article_images(
                    data
                )
            )

            for image_url in urls:

                url = make_absolute_url(
                    article_url,
                    image_url,
                )

                if (
                    url
                    and not looks_generic_image(
                        url
                    )
                ):

                    candidates.append(
                        (90, url)
                    )

        except Exception:
            continue


    # --------------------------------------------------------
    # OPEN GRAPH
    # --------------------------------------------------------

    og = soup.find(
        "meta",
        attrs={
            "property":
                "og:image"
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
        og
        and og.get(
            "content"
        )
    ):

        url = make_absolute_url(
            article_url,
            og["content"],
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
                (80, url)
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
            article_url,
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
                (70, url)
            )


    if not candidates:
        return ""


    candidates.sort(
        key=lambda item:
            item[0],
        reverse=True,
    )


    seen = set()

    for _, url in candidates:

        normalized = (
            url.split("?")[0]
            .lower()
        )

        if normalized in seen:
            continue

        seen.add(normalized)

        return url

    return ""


# ============================================================
# DOWNLOAD IMAGE INTO POLICY DOSSIER
# ============================================================

def download_story_image(
    image_url,
    story_id,
    referer="",
):
    """
    Save the story photograph under /images.

    This prevents hotlink-blocking when the site/X
    later requests the image.
    """

    if not image_url:
        return ""

    IMAGES_DIR.mkdir(
        exist_ok=True
    )

    try:

        headers = {
            "User-Agent":
                "Mozilla/5.0",
            "Accept":
                "image/avif,image/webp,image/apng,"
                "image/svg+xml,image/*,*/*;q=0.8",
        }

        if referer:
            headers["Referer"] = referer


        request = urllib.request.Request(
            image_url,
            headers=headers,
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
                5_000_001
            )


        # Tiny graphics are usually icons/logos.
        if len(data) < 15_000:

            print(
                "Rejected tiny image:",
                image_url,
            )

            return ""


        if len(data) > 5_000_000:

            print(
                "Image too large:",
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

            elif path.endswith(
                ".png"
            ):

                extension = ".png"

            elif path.endswith(
                ".webp"
            ):

                extension = ".webp"

            else:

                print(
                    "Unsupported image type:",
                    content_type,
                )

                return ""


        filename = (
            f"{story_id}"
            f"{extension}"
        )

        path = (
            IMAGES_DIR
            / filename
        )

        path.write_bytes(
            data
        )


        public_url = (
            f"{SITE_URL}/images/"
            f"{filename}"
        )

        print(
            "Saved image:",
            public_url,
        )

        return public_url


    except Exception as error:

        print(
            "Image download failed:",
            image_url,
            error,
        )

        return ""


# ============================================================
# FETCH ARTICLE + PHOTO
# ============================================================

def fetch_article_data(
    article_url,
    story_id,
    feed_image="",
):

    page = fetch_page(
        article_url
    )


    article_text = ""

    remote_image = ""


    if page:

        soup = BeautifulSoup(
            page,
            "html.parser",
        )

        article_text = (
            find_article_text(
                soup
            )
        )

        remote_image = (
            find_actual_article_image(
                article_url,
                soup,
            )
        )


    # If the article HTML itself did not expose a real image,
    # try the RSS image.
    if (
        not remote_image
        and feed_image
        and not looks_generic_image(
            feed_image
        )
    ):

        remote_image = (
            feed_image
        )


    local_image = ""


    if remote_image:

        local_image = (
            download_story_image(
                remote_image,
                story_id,
                referer=article_url,
            )
        )


    return (
        article_text,
        local_image,
    )


# ============================================================
# DIVERSE SOURCE SELECTION
# ============================================================

def choose_diverse_stories(
    candidates,
    limit,
):

    selected = []

    used_publishers = set()

    used_ids = set()


    # Nigerian publishers first.
    for story in candidates:

        if (
            story["region"]
            != "Nigeria"
        ):
            continue

        publisher = (
            story["_source"]
        )

        if publisher in used_publishers:
            continue

        selected.append(
            story
        )

        used_publishers.add(
            publisher
        )

        used_ids.add(
            story["id"]
        )

        if len(selected) >= limit:
            return selected


    # Then international publishers.
    for story in candidates:

        if (
            story["region"]
            != "World"
        ):
            continue

        publisher = (
            story["_source"]
        )

        if publisher in used_publishers:
            continue

        selected.append(
            story
        )

        used_publishers.add(
            publisher
        )

        used_ids.add(
            story["id"]
        )

        if len(selected) >= limit:
            return selected


    # Fill any remaining slots.
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
# OPENAI WRITING
# ============================================================

def write_up(
    client,
    title,
    article_text,
    rss_text,
):
    """
    Produce:
    - short teaser
    - Policy Dossier website article

    Publisher name is deliberately NOT passed to OpenAI,
    preventing it from unnecessarily mentioning the
    publisher in the article.
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
You write for Policy Dossier, an independent neutral
news and public-affairs publication.

Use ONLY the supplied source material.

Return valid JSON using exactly:

{{
  "short": "Short summary",
  "body": "Website article"
}}

SHORT:

- Write 2 concise sentences.
- Approximately 160 characters maximum.
- State the main development clearly.
- Neutral and factual.
- No hashtags.
- No emojis.
- No clickbait.
- Do not mention the publisher or source publication.

BODY:

- Write a polished Policy Dossier news article.
- Normally 250 to 450 words when enough information exists.
- Use 3 to 5 clear paragraphs.
- Explain what happened.
- Include the important details.
- Explain why the development matters only if supported.
- Include relevant context only if supported by the supplied text.
- Rewrite everything in your own words.
- Do not reproduce the original article.
- Do not invent facts.
- Do not invent quotations.
- Do not invent numbers.
- Do not invent dates, people or organisations.
- Do not use outside knowledge.
- Do not identify or mention the source publication.
- Do not tell readers to visit another website.
- Do not say "read the full story".
- Do not say "according to [publisher]".
- If the available information is limited, write a shorter
  accurate article rather than adding unsupported information.
- Do not mention AI.
- Do not mention these instructions.

Headline:
{title}

Source material:
{source_material[:12000]}
""".strip()


    response = (
        client
        .chat
        .completions
        .create(
            model=MODEL,

            response_format={
                "type":
                    "json_object"
            },

            messages=[
                {
                    "role":
                        "user",

                    "content":
                        prompt,
                }
            ],
        )
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
            "OpenAI returned no summary."
        )


    if not body:

        raise ValueError(
            "OpenAI returned no article."
        )


    return {
        "short":
            short,

        "body":
            body,
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

<meta
    name="twitter:title"
    content="$title"
>

<meta
    name="twitter:description"
    content="$summary"
>

$twitterimg

<link
    href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800;900&family=Source+Serif+4:wght@400;600&display=swap"
    rel="stylesheet"
>

<style>

:root {
    --bg:#0a0a0a;
    --fg:#f5f5f5;
    --mute:#8f8f8f;
    --line:#262626;
    --ng:#2ecc71;
}

@media (prefers-color-scheme:light) {

    :root {
        --bg:#f6f5f2;
        --fg:#0a0a0a;
        --mute:#666;
        --line:#dcdad4;
        --ng:#0b7a3b;
    }
}

* {
    box-sizing:border-box;
}

body {
    margin:0;
    background:var(--bg);
    color:var(--fg);
    font:400 16px/1.55 Archivo,system-ui,sans-serif;
}

a {
    color:inherit;
    text-decoration:none;
}

.top {
    border-bottom:1px solid var(--line);
}

.bar {
    max-width:1120px;
    margin:0 auto;
    padding:0 20px;
    height:64px;
    display:flex;
    justify-content:space-between;
    align-items:center;
}

.brand b {
    font-weight:900;
    font-size:30px;
    letter-spacing:-.07em;
}

.brand span {
    font-weight:800;
    font-size:12px;
    letter-spacing:.22em;
    text-transform:uppercase;
    margin-left:10px;
}

.back {
    font-weight:600;
    font-size:14px;
    color:var(--mute);
}

article {
    max-width:720px;
    margin:0 auto;
    padding:48px 20px 80px;
}

.tag {
    display:inline-flex;
    font-weight:800;
    font-size:11px;
    letter-spacing:.16em;
    text-transform:uppercase;
    color:var(--ng);
}

h1 {
    font-weight:900;
    font-size:clamp(32px,6vw,54px);
    line-height:1.03;
    letter-spacing:-.04em;
    margin:16px 0 14px;
}

.meta {
    color:var(--mute);
    font-size:14px;
    padding-bottom:24px;
    border-bottom:1px solid var(--line);
    margin-bottom:28px;
}

.hero {
    display:block;
    width:100%;
    max-height:500px;
    object-fit:cover;
    border-radius:10px;
    margin-bottom:30px;
}

.body p {
    font:400 19px/1.75 "Source Serif 4",Georgia,serif;
    margin:0 0 1.25em;
}

.source-line {
    margin-top:36px;
    padding-top:18px;
    border-top:1px solid var(--line);
    color:var(--mute);
    font-size:14px;
}

.source-line strong {
    color:var(--fg);
}

.note {
    color:var(--mute);
    font-size:13px;
    margin-top:18px;
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
<strong>$source</strong>
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
# GENERATE ARTICLE PAGES
# ============================================================

def write_pages(stories):

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
                text,
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

            twitter_image = (
                '<meta name="twitter:image" '
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
            )

        else:

            og_image = ""
            twitter_image = ""
            card = "summary"
            hero = ""


        region = (
            "Nigeria"
            if story.get(
                "region"
            ) == "Nigeria"
            else "World"
        )


        source = story.get(
            "_source",
            "",
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

            twitterimg=twitter_image,

            card=card,

            source=escape(
                source
            ),

            xurl=escape(
                X_URL
            ),

            region=escape(
                region
            ),

            date=escape(
                story.get(
                    "published",
                    "",
                )[:10]
            ),

            body=body_html,

            hero=hero,
        )


        path = (
            STORY_PAGES_DIR
            / f"{story['id']}.html"
        )


        path.write_text(
            page,
            encoding="utf-8",
        )


# ============================================================
# LOAD / MIGRATE STORIES
# ============================================================

def load_stories():
    """
    Load existing stories.

    IMPORTANT:
    Old versions stored the publisher as "source".

    We migrate it to "_source" so the existing homepage's
    story.source code can no longer display the publisher.
    """

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


        for story in stories:

            # Hide publisher from homepage.
            if (
                "source" in story
                and "_source"
                not in story
            ):

                story["_source"] = (
                    story.pop(
                        "source"
                    )
                )


            story.setdefault(
                "_source",
                ""
            )


            # Existing legacy stories should not suddenly
            # repost to X.
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

    required = (
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_SECRET",
    )


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
            "X disabled. Missing: "
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
    """Create the short Policy Dossier X post."""

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


    return (
        f"{title}\n\n"
        f"{summary}\n\n"
        f"{article_url}"
    )


def post_previous_story_to_x(
    existing_stories,
    x_client,
):
    """
    Post ONE story from a previous workflow run.

    That story's webpage and photograph should already
    have been deployed by GitHub Pages.
    """

    if x_client is None:
        return


    pending = [
        story
        for story
        in existing_stories
        if not story.get(
            "x_posted",
            True,
        )
    ]


    if not pending:

        print(
            "No previously deployed "
            "story waiting for X."
        )

        return


    # Oldest pending story first.
    pending.sort(
        key=lambda story:
            story.get(
                "published",
                "",
            )
    )


    story = pending[0]


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


    # --------------------------------------------------------
    # POST ONE OLD/DEPLOYED STORY TO X FIRST
    # --------------------------------------------------------

    x_client = (
        create_x_client()
    )


    post_previous_story_to_x(
        existing_stories,
        x_client,
    )


    # --------------------------------------------------------
    # EXISTING STORY IDS
    # --------------------------------------------------------

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
    # FETCH RSS
    # --------------------------------------------------------

    for publisher, (
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
                f"{publisher}: "
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


            if story_id in existing_ids:
                continue


            if story_id in candidate_ids:
                continue


            candidate_ids.add(
                story_id
            )


            # -----------------------------------------------
            # DATE
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
            # RSS TEXT
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


            # -----------------------------------------------
            # RSS IMAGE FALLBACK
            # -----------------------------------------------

            feed_image = (
                find_feed_image(
                    entry,
                    link,
                )
            )


            candidates.append(
                {
                    "id":
                        story_id,

                    "title":
                        title,

                    "link":
                        link,

                    # Internal publisher field.
                    # Homepage's story.source will be empty.
                    "_source":
                        publisher,

                    "region":
                        region,

                    "rss_text":
                        rss_text,

                    "feed_image":
                        feed_image,

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
        key=lambda story:
            story[
                "published"
            ],
        reverse=True,
    )


    # --------------------------------------------------------
    # NIGERIA FIRST
    # --------------------------------------------------------

    candidates.sort(
        key=lambda story:
            story[
                "region"
            ] != "Nigeria"
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
        "Selected:",
        len(selected),
        "stories",
    )


    # --------------------------------------------------------
    # FETCH ARTICLE TEXT + ACTUAL PHOTO
    # --------------------------------------------------------

    for story in selected:


        article_text, image = (
            fetch_article_data(

                story[
                    "link"
                ],

                story[
                    "id"
                ],

                story.get(
                    "feed_image",
                    "",
                ),
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

                story[
                    "title"
                ],

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


        # Website-only by default.
        story[
            "x_posted"
        ] = True


        # Do not save copied source content.
        story.pop(
            "article_text",
            None,
        )

        story.pop(
            "rss_text",
            None,
        )

        story.pop(
            "feed_image",
            None,
        )


        processed.append(
            story
        )


    # --------------------------------------------------------
    # QUEUE EXACTLY ONE NEW STORY FOR NEXT X RUN
    # --------------------------------------------------------

    if processed:

        processed[0][
            "x_posted"
        ] = False


    # --------------------------------------------------------
    # MERGE
    # --------------------------------------------------------

    stories = (
        processed
        + existing_stories
    )[:KEEP]


    # --------------------------------------------------------
    # GENERATE ARTICLE HTML
    # --------------------------------------------------------

    write_pages(
        stories
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
