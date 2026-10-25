"""
Policy Dossier

Each scheduled run:

1. Post ONE previously-deployed story to X.
2. Fetch current news from RSS feeds.
3. Prioritise Nigeria.
4. Select up to 4 stories from different publishers.
5. Open each source article.
6. Extract useful article text.
7. Find the actual story photograph.
8. Download and convert the photograph to JPEG.
9. Use OpenAI to write:
   - a short teaser
   - a fuller Policy Dossier article
10. Generate individual Policy Dossier pages.
11. Queue ONE new story for the next X run.
12. Save everything.

The publisher name is displayed publicly ONLY at
the bottom of the individual article:

    Source: Publisher
"""

import calendar
import datetime as dt
import hashlib
import html
import io
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
from PIL import Image, ImageOps


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


# Maximum new website stories each run.
MAX_NEW_PER_RUN = 4

# Maximum X posts each run.
MAX_X_POSTS_PER_RUN = 1

# Maximum stories stored.
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
    or "gpt-6-luna"
)


# ============================================================
# GENERAL TEXT HELPERS
# ============================================================

def clean_text(value):

    if not value:
        return ""

    value = html.unescape(
        str(value)
    )

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

    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):

        parts = []

        for item in value.values():

            text = normalise_ai_text(
                item
            )

            if text:
                parts.append(text)

        return "\n\n".join(
            parts
        ).strip()

    if isinstance(value, list):

        parts = []

        for item in value:

            text = normalise_ai_text(
                item
            )

            if text:
                parts.append(text)

        return "\n\n".join(
            parts
        ).strip()

    return str(
        value
    ).strip()


def parse_ai_json(text):

    text = (
        text
        or ""
    ).strip()

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

        return json.loads(
            text
        )

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
# FETCH ARTICLE PAGE
# ============================================================

def fetch_page(url):

    try:

        request = urllib.request.Request(

            url,

            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; PolicyDossierBot/1.0)"
                ),

                "Accept": (
                    "text/html,"
                    "application/xhtml+xml,"
                    "application/xml;q=0.9,"
                    "*/*;q=0.8"
                ),
            },
        )


        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            return response.read(
                1_500_000
            ).decode(
                "utf-8",
                "ignore",
            )


    except Exception as error:

        print(
            "Could not fetch:",
            url,
            error,
        )

        return ""


# ============================================================
# ARTICLE TEXT
# ============================================================

def extract_paragraphs(container):

    if not container:
        return []

    output = []

    for paragraph in container.find_all(
        "p"
    ):

        text = clean_text(
            paragraph.get_text(
                " ",
                strip=True,
            )
        )

        if len(text) < 35:
            continue

        output.append(text)

    return output


def find_article_text(soup):

    paragraphs = []

    article = soup.find(
        "article"
    )


    if article:

        paragraphs = (
            extract_paragraphs(
                article
            )
        )


    if len(paragraphs) < 2:

        main = soup.find(
            "main"
        )

        if main:

            paragraphs = (
                extract_paragraphs(
                    main
                )
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


            found = (
                extract_paragraphs(
                    container
                )
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


def looks_generic_image(
    url,
    alt="",
):

    text = (
        f"{url} {alt}"
    ).lower()


    blocked = (

        "logo",

        "site-logo",

        "site_logo",

        "favicon",

        "placeholder",

        "default-image",

        "default_image",

        "defaultimage",

        "avatar",

        "profile-photo",

        "profile_image",

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


    if any(
        word in text
        for word in blocked
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

    if not srcset:
        return ""

    options = []


    for item in srcset.split(","):

        item = item.strip()

        if not item:
            continue


        pieces = item.split()

        url = pieces[0]

        score = 1


        if len(pieces) > 1:

            descriptor = (
                pieces[1]
                .lower()
            )


            try:

                if descriptor.endswith(
                    "w"
                ):

                    score = int(
                        descriptor[:-1]
                    )


                elif descriptor.endswith(
                    "x"
                ):

                    score = (
                        float(
                            descriptor[:-1]
                        )
                        * 1000
                    )


            except ValueError:

                pass


        options.append(
            (
                score,
                url,
            )
        )


    if not options:
        return ""


    options.sort(
        reverse=True
    )


    return make_absolute_url(
        page_url,
        options[0][1],
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

    for key in (
        "media_content",
        "media_thumbnail",
    ):

        media_items = (
            entry.get(key)
            or []
        )


        for media in media_items:

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

            link.get(
                "rel"
            ) == "enclosure"

            and

            str(
                link.get(
                    "type",
                    "",
                )
            ).startswith(
                "image"
            )
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

        +

        "".join(

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


    if isinstance(
        value,
        str,
    ):

        output.append(
            value
        )


    elif isinstance(
        value,
        list,
    ):

        for item in value:

            output.extend(

                extract_image_values(
                    item
                )
            )


    elif isinstance(
        value,
        dict,
    ):

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


def extract_jsonld_article_images(
    data,
):

    images = []


    if isinstance(
        data,
        list,
    ):

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


    types = data.get(
        "@type",
        [],
    )


    if isinstance(
        types,
        str,
    ):

        types = [
            types
        ]


    article_types = {

        "Article",

        "NewsArticle",

        "ReportageNewsArticle",

        "AnalysisNewsArticle",
    }


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
# FIND STORY IMAGE CANDIDATES
# ============================================================

def find_article_image_candidates(
    article_url,
    soup,
):

    candidates = []


    def add_candidate(
        score,
        url,
        alt="",
    ):

        if not url:
            return


        url = make_absolute_url(
            article_url,
            url,
        )


        if not url:
            return


        if looks_generic_image(
            url,
            alt,
        ):

            return


        candidates.append(
            (
                score,
                url,
            )
        )


    article = soup.find(
        "article"
    )


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

                        or

                        source.get(
                            "data-srcset",
                            "",
                        ),

                        article_url,
                    )


                    add_candidate(
                        130,
                        url,
                    )


            image = figure.find(
                "img"
            )


            if image:

                url = image_from_tag(
                    image,
                    article_url,
                )


                add_candidate(
                    125,
                    url,
                    image.get(
                        "alt",
                        "",
                    ),
                )


    # --------------------------------------------------------
    # OTHER ARTICLE IMAGES
    # --------------------------------------------------------

    if article:

        for image in article.find_all(
            "img",
            limit=25,
        ):

            url = image_from_tag(
                image,
                article_url,
            )


            add_candidate(
                115,
                url,
                image.get(
                    "alt",
                    "",
                ),
            )


    # --------------------------------------------------------
    # COMMON ARTICLE CONTENT CONTAINERS
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
            limit=15,
        ):

            url = image_from_tag(
                image,
                article_url,
            )


            add_candidate(

                110,

                url,

                image.get(
                    "alt",
                    "",
                ),
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


            data = json.loads(
                raw
            )


            for image_url in (
                extract_jsonld_article_images(
                    data
                )
            ):

                add_candidate(
                    100,
                    image_url,
                )


        except Exception:

            continue


    # --------------------------------------------------------
    # OPEN GRAPH IMAGE
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

        alt = (
            og_alt.get(
                "content",
                "",
            )
            if og_alt
            else ""
        )


        add_candidate(

            90,

            og.get(
                "content"
            ),

            alt,
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

        or

        soup.find(

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

        add_candidate(

            80,

            twitter_image.get(
                "content"
            ),
        )


    # Highest priority first.
    candidates.sort(

        key=lambda item:
            item[0],

        reverse=True,
    )


    unique = []

    seen = set()


    for _, url in candidates:

        normalised = (
            url
            .split("?")[0]
            .lower()
        )


        if normalised in seen:
            continue


        seen.add(
            normalised
        )


        unique.append(
            url
        )


    return unique


# ============================================================
# DOWNLOAD + CONVERT STORY IMAGE
# ============================================================

def download_story_image(
    remote_url,
    story_id,
    referer="",
):

    if not remote_url:

        return (
            "",
            "",
        )


    IMAGES_DIR.mkdir(
        exist_ok=True
    )


    try:

        headers = {

            "User-Agent":
                "Mozilla/5.0",

            "Accept":
                "image/avif,"
                "image/webp,"
                "image/apng,"
                "image/*,"
                "*/*;q=0.8",
        }


        if referer:

            headers[
                "Referer"
            ] = referer


        request = urllib.request.Request(
            remote_url,
            headers=headers,
        )


        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            data = response.read(
                8_000_000
            )


        if len(data) < 12_000:

            print(
                "Rejected tiny image:",
                remote_url,
            )

            return (
                "",
                "",
            )


        image = Image.open(
            io.BytesIO(
                data
            )
        )


        image.load()


        image = (
            ImageOps
            .exif_transpose(
                image
            )
        )


        width, height = (
            image.size
        )


        # Reject obvious icons / tiny graphics.
        if (
            width < 300
            or height < 160
        ):

            print(
                "Rejected small image:",
                width,
                height,
                remote_url,
            )

            return (
                "",
                "",
            )


        # Keep X/site files reasonably sized.
        image.thumbnail(
            (
                1600,
                1600,
            )
        )


        if image.mode != "RGB":

            image = image.convert(
                "RGB"
            )


        filename = (
            f"{story_id}.jpg"
        )


        local_path = (
            IMAGES_DIR
            / filename
        )


        image.save(

            local_path,

            format="JPEG",

            quality=88,

            optimize=True,
        )


        # Reject unexpected massive output.
        if (
            local_path.stat().st_size
            > 5_000_000
        ):

            image.save(

                local_path,

                format="JPEG",

                quality=75,

                optimize=True,
            )


        public_url = (
            f"{SITE_URL}/images/"
            f"{filename}"
        )


        print(
            "Saved story image:",
            public_url,
        )


        return (
            public_url,
            str(local_path),
        )


    except Exception as error:

        print(
            "Image rejected/download failed:",
            remote_url,
            error,
        )


        return (
            "",
            "",
        )


# ============================================================
# FETCH ARTICLE + CHOOSE REAL PHOTO
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

    candidates = []


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


        candidates.extend(

            find_article_image_candidates(
                article_url,
                soup,
            )
        )


    if (
        feed_image
        and not looks_generic_image(
            feed_image
        )
    ):

        candidates.append(
            feed_image
        )


    seen = set()


    for candidate in candidates:

        normalised = (
            candidate
            .split("?")[0]
            .lower()
        )


        if normalised in seen:
            continue


        seen.add(
            normalised
        )


        public_url, local_file = (
            download_story_image(

                candidate,

                story_id,

                referer=article_url,
            )
        )


        if (
            public_url
            and local_file
        ):

            return (
                article_text,
                public_url,
                local_file,
            )


    print(
        "No suitable story image found:",
        article_url,
    )


    return (
        article_text,
        "",
        "",
    )


# ============================================================
# DIVERSE STORY SELECTION
# ============================================================

def choose_diverse_stories(
    candidates,
    limit,
):

    selected = []

    used_publishers = set()

    used_ids = set()


    # --------------------------------------------------------
    # NIGERIA FIRST — UNIQUE PUBLISHERS
    # --------------------------------------------------------

    for story in candidates:

        if (
            story["region"]
            != "Nigeria"
        ):

            continue


        publisher = (
            story["_source"]
        )


        if (
            publisher
            in used_publishers
        ):

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


    # --------------------------------------------------------
    # WORLD — UNIQUE PUBLISHERS
    # --------------------------------------------------------

    for story in candidates:

        if (
            story["region"]
            != "World"
        ):

            continue


        publisher = (
            story["_source"]
        )


        if (
            publisher
            in used_publishers
        ):

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


    # --------------------------------------------------------
    # FILL ANY REMAINING SPACES
    # --------------------------------------------------------

    for story in candidates:

        if (
            story["id"]
            in used_ids
        ):

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

    source_material = (

        article_text

        if len(
            article_text
        ) >= 200

        else

        clean_text(
            rss_text
        )
    )


    if not source_material:

        raise ValueError(
            "No usable source material."
        )


    prompt = f"""
You are writing for Policy Dossier, a neutral news and
public-affairs publication.

Use ONLY the supplied source material.

Return ONLY valid JSON using exactly this structure:

{{
  "short": "Short summary",
  "body": "Website article"
}}

SHORT:

- Write 2 concise sentences.
- Aim for approximately 160 characters maximum.
- State the main development clearly.
- Neutral and factual.
- No emojis.
- No hashtags.
- No clickbait.
- Do not mention any publisher or source publication.

BODY:

- Write a polished Policy Dossier news article.
- Normally 250 to 450 words when the source contains
  enough information.
- Use 3 to 5 paragraphs.
- Explain what happened.
- Include the important facts and details.
- Explain why it matters only where supported.
- Include context only when supported by the supplied text.
- Rewrite completely in your own words.
- Do not reproduce the original article.
- Do not invent facts.
- Do not invent quotes.
- Do not invent numbers.
- Do not invent people, dates or organisations.
- Do not use outside knowledge.
- Do not identify the publisher.
- Do not write "according to [publisher]".
- Do not tell readers to visit another news website.
- Do not say "read the full story".
- Do not say "read more at".
- If there is insufficient source information,
  write a shorter accurate article.
- Do not mention AI.

Headline:
{title}

Source material:
{source_material[:12000]}
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
            "OpenAI returned no short summary."
        )


    if not body:

        raise ValueError(
            "OpenAI returned no article body."
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
    --bg:#090909;
    --surface:#121212;
    --fg:#f4f4f4;
    --mute:#8d8d8d;
    --line:#282828;
    --accent:#2ecc71;
}

@media (prefers-color-scheme:light) {

    :root {
        --bg:#f7f6f2;
        --surface:#fff;
        --fg:#0b0b0b;
        --mute:#696969;
        --line:#dedbd4;
        --accent:#087a3b;
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
    -webkit-font-smoothing:antialiased;
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
    height:68px;
    display:flex;
    align-items:center;
    justify-content:space-between;
}

.brand {
    display:flex;
    align-items:center;
    gap:10px;
}

.brand b {
    font-size:30px;
    font-weight:900;
    letter-spacing:-.08em;
}

.brand span {
    font-size:12px;
    font-weight:800;
    letter-spacing:.2em;
    text-transform:uppercase;
}

.back {
    font-size:14px;
    font-weight:600;
    color:var(--mute);
}

.back:hover {
    color:var(--fg);
}

article {
    max-width:760px;
    margin:0 auto;
    padding:52px 20px 90px;
}

.tag {
    color:var(--accent);
    font-size:11px;
    font-weight:800;
    letter-spacing:.17em;
    text-transform:uppercase;
}

h1 {
    margin:15px 0 16px;
    font-size:clamp(34px,6vw,58px);
    line-height:1.03;
    letter-spacing:-.045em;
    font-weight:900;
}

.meta {
    margin-bottom:30px;
    padding-bottom:24px;
    border-bottom:1px solid var(--line);
    color:var(--mute);
    font-size:14px;
}

.hero {
    display:block;
    width:100%;
    max-height:520px;
    object-fit:cover;
    border-radius:11px;
    margin:0 0 34px;
    background:var(--line);
}

.body p {
    margin:0 0 1.3em;
    font:400 19px/1.78 "Source Serif 4",Georgia,serif;
}

.source-line {
    margin:40px 0 0;
    padding-top:19px;
    border-top:1px solid var(--line);
    color:var(--mute);
    font-size:14px;
}

.source-line strong {
    color:var(--fg);
    font-weight:700;
}

.follow {
    margin-top:18px;
    color:var(--mute);
    font-size:13px;
}

.follow a:hover {
    color:var(--fg);
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

<div class="tag">
$region
</div>

<h1>
$title
</h1>

<div class="meta">
$date
</div>

$hero

<div class="body">
$body
</div>

<p class="source-line">
Source: <strong>$source</strong>
</p>

<p class="follow">

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


        article_text = (

            story.get(
                "body"
            )

            or

            story.get(
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

            escaped_image = (
                escape(
                    image
                )
            )


            og_image = (
                '<meta '
                'property="og:image" '
                f'content="{escaped_image}">'
                '\n'
                '<meta '
                'property="og:image:type" '
                'content="image/jpeg">'
            )


            twitter_image = (
                '<meta '
                'name="twitter:image" '
                f'content="{escaped_image}">'
            )


            card = (
                "summary_large_image"
            )


            hero = (
                f'<img '
                f'class="hero" '
                f'src="{escaped_image}" '
                f'alt="{escape(story["title"])}" '
                f'loading="eager">'
            )


        else:

            og_image = ""

            twitter_image = ""

            card = "summary"

            hero = ""


        source = story.get(
            "_source",
            "",
        )


        region = story.get(
            "region",
            "World",
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


            # Old code used "source".
            # Move it to internal "_source".
            if (

                "source"
                in story

                and

                "_source"
                not in story
            ):

                story[
                    "_source"
                ] = story.pop(
                    "source"
                )


            story.setdefault(
                "_source",
                "",
            )


            # Old stories should not suddenly repost.
            story.setdefault(
                "x_posted",
                True,
            )


            story.setdefault(
                "image",
                "",
            )


            story.setdefault(
                "image_file",
                "",
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
# X CLIENTS
# ============================================================

def create_x_clients():

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
            "X posting disabled. Missing:",
            ", ".join(
                missing
            ),
        )

        return (
            None,
            None,
        )


    # X API v2 — creates the post.
    x_client = tweepy.Client(

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


    # X API v1.1 authentication — media upload.
    auth = tweepy.OAuth1UserHandler(

        os.environ[
            "X_API_KEY"
        ],

        os.environ[
            "X_API_SECRET"
        ],

        os.environ[
            "X_ACCESS_TOKEN"
        ],

        os.environ[
            "X_ACCESS_SECRET"
        ],
    )


    media_api = tweepy.API(
        auth
    )


    return (
        x_client,
        media_api,
    )


# ============================================================
# X POST TEXT
# ============================================================

def build_x_post(story):

    article_url = (
        f"{SITE_URL}/stories/"
        f"{story['id']}.html"
    )


    title = clean_text(
        story.get(
            "title",
            "",
        )
    )


    summary = clean_text(
        story.get(
            "summary",
            "",
        )
    )


    if len(title) > 85:

        title = (
            title[:82].rstrip()
            + "..."
        )


    if len(summary) > 95:

        summary = (
            summary[:92].rstrip()
            + "..."
        )


    return (
        f"{title}\n\n"
        f"{summary}\n\n"
        f"{article_url}"
    )


# ============================================================
# DIRECT IMAGE UPLOAD TO X
# ============================================================

def upload_story_image_to_x(
    media_api,
    story,
):

    image_file = story.get(
        "image_file",
        "",
    )


    if not image_file:

        return None


    path = Path(
        image_file
    )


    if not path.exists():

        print(
            "X image file missing:",
            image_file,
        )

        return None


    try:

        media = media_api.media_upload(
            filename=str(
                path
            )
        )


        print(
            "Uploaded image to X:",
            image_file,
        )


        return (
            media.media_id_string
        )


    except Exception as error:

        print(
            "X media upload failed:",
            error,
        )

        raise


# ============================================================
# POST ONE PREVIOUS STORY TO X
# ============================================================

def post_previous_story_to_x(
    existing_stories,
    x_client,
    media_api,
):

    if (
        x_client is None
        or media_api is None
    ):

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


    # Oldest queued story first.
    pending.sort(

        key=lambda story:
            story.get(
                "published",
                "",
            )
    )


    story = pending[0]


    tweet_text = (
        build_x_post(
            story
        )
    )


    try:

        media_id = None


        if story.get(
            "image_file"
        ):

            media_id = (
                upload_story_image_to_x(
                    media_api,
                    story,
                )
            )


        if media_id:

            x_client.create_tweet(

                text=tweet_text,

                media_ids=[
                    media_id
                ],
            )


        else:

            # If a story genuinely has no usable photograph,
            # still allow the factual update to post.
            x_client.create_tweet(
                text=tweet_text
            )


        story[
            "x_posted"
        ] = True


        print(
            "Posted to X:",
            story[
                "title"
            ],
        )


    except Exception as error:

        # Leave x_posted=False.
        # It will retry next run.
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
    # POST ONE ALREADY-DEPLOYED STORY TO X
    # --------------------------------------------------------

    x_client, media_api = (
        create_x_clients()
    )


    post_previous_story_to_x(

        existing_stories,

        x_client,

        media_api,
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
    # FETCH RSS FEEDS
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
                "Feed failed:",
                publisher,
                error,
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
            # PUBLISHED DATE
            # -----------------------------------------------

            published_struct = (

                getattr(
                    entry,
                    "published_parsed",
                    None,
                )

                or

                getattr(
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

                or

                "".join(

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

                    # Internal only.
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
    # NEWEST FIRST
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
    # SELECT DIVERSE SOURCES
    # --------------------------------------------------------

    selected = (

        choose_diverse_stories(

            candidates,

            MAX_NEW_PER_RUN,
        )
    )


    print(
        "Selected",
        len(selected),
        "new stories."
    )


    # --------------------------------------------------------
    # FETCH ARTICLE + IMAGE
    # --------------------------------------------------------

    for story in selected:


        (
            article_text,
            image_url,
            image_file,
        ) = fetch_article_data(

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


        story[
            "article_text"
        ] = article_text


        story[
            "image"
        ] = image_url


        story[
            "image_file"
        ] = image_file


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
                "OpenAI failed for:",
                story[
                    "title"
                ],
                error,
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


        # Do not retain copied source text.
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
    # QUEUE ONE NEW STORY FOR NEXT X RUN
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

        +

        existing_stories

    )[:KEEP]


    # --------------------------------------------------------
    # REGENERATE ALL STORY PAGES
    # --------------------------------------------------------

    write_pages(
        stories
    )


    # --------------------------------------------------------
    # SAVE JSON
    # --------------------------------------------------------

    save_stories(
        stories
    )


    print(
        "Completed.",
        len(processed),
        "new website stories published."
    )


if __name__ == "__main__":

    main()
