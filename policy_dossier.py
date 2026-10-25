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


MAX_NEW_PER_RUN = 4

KEEP = 200

STORIES_FILE = "stories.json"

IMAGES_DIR = Path("images")

STORY_PAGES_DIR = Path("stories")


SITE_URL = (
    os.environ.get("SITE_URL")
    or "https://policydossierhq-collab.github.io/Policy-dossier"
).rstrip("/")


X_URL = (
    os.environ.get("X_URL")
    or "https://x.com/Policydossier"
)


MODEL = (
    os.environ.get("OPENAI_MODEL")
    or "gpt-6-luna"
)


# ============================================================
# TEXT HELPERS
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


def parse_ai_json(text):

    text = (
        text
        or ""
    ).strip()

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
# FETCH ARTICLE
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
            "Could not fetch article:",
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


        if len(text) >= 35:

            output.append(
                text
            )


    return output


def find_article_text(soup):

    containers = [

        soup.find(
            "article"
        ),

        soup.find(
            "main"
        ),
    ]


    selectors = (

        "[class*='article-content']",

        "[class*='article-body']",

        "[class*='story-content']",

        "[class*='story-body']",

        "[class*='entry-content']",

        "[class*='post-content']",
    )


    for selector in selectors:

        containers.append(
            soup.select_one(
                selector
            )
        )


    for container in containers:

        paragraphs = (
            extract_paragraphs(
                container
            )
        )


        if len(paragraphs) >= 2:

            return "\n\n".join(
                paragraphs
            )[:12000]


    return ""


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


    if image_url.startswith(
        (
            "http://",
            "https://",
        )
    ):

        return image_url


    return ""


def looks_generic_image(
    url,
    alt="",
):

    text = (
        f"{url} {alt}"
    ).lower()


    blocked = (

        "logo",

        "favicon",

        "placeholder",

        "default-image",

        "default_image",

        "avatar",

        "author-image",

        "profile-photo",

        "headshot",

        "sprite",

        "social-icon",

        "newsletter",

        "advertisement",

        "/advert/",

        "/ads/",

        "loading.gif",

        "blank.gif",
    )


    if any(
        term in text
        for term in blocked
    ):

        return True


    match = re.search(
        r"(\d{2,4})x(\d{2,4})",
        text,
    )


    if match:

        width = int(
            match.group(1)
        )

        height = int(
            match.group(2)
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


    choices = []


    for part in srcset.split(","):

        bits = (
            part.strip()
            .split()
        )


        if not bits:
            continue


        score = 1


        if len(bits) > 1:

            descriptor = (
                bits[1]
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


        choices.append(
            (
                score,
                bits[0],
            )
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
        image.get(
            "alt"
        )

        or

        image.get(
            "title"
        )

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
# JSON-LD IMAGES
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
            (
                dict,
                list,
            ),
        ):

            images.extend(

                extract_jsonld_article_images(
                    value
                )
            )


    return images


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

        or ""
    )


    blob += "".join(

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


    match = re.search(

        r'<img[^>]+src=["\']([^"\']+)',

        blob,

        re.IGNORECASE,
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
# FIND ARTICLE IMAGE
# ============================================================

def find_article_image_candidates(
    article_url,
    soup,
):

    candidates = []


    def add(
        score,
        url,
        alt="",
    ):

        url = make_absolute_url(
            article_url,
            url,
        )


        if (
            url
            and not looks_generic_image(
                url,
                alt,
            )
        ):

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


                    add(
                        140,
                        url,
                    )


            image = figure.find(
                "img"
            )


            if image:

                add(

                    135,

                    image_from_tag(
                        image,
                        article_url,
                    ),

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
            limit=30,
        ):

            add(

                125,

                image_from_tag(
                    image,
                    article_url,
                ),

                image.get(
                    "alt",
                    "",
                ),
            )


    # --------------------------------------------------------
    # JSON-LD
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

            data = json.loads(

                script.string

                or

                script.get_text()

                or ""
            )


            for image_url in (
                extract_jsonld_article_images(
                    data
                )
            ):

                add(
                    110,
                    image_url,
                )


        except Exception:

            pass


    # --------------------------------------------------------
    # OPEN GRAPH
    # --------------------------------------------------------

    og_image = soup.find(

        "meta",

        attrs={
            "property":
                "og:image"
        },
    )


    if (
        og_image
        and og_image.get(
            "content"
        )
    ):

        add(

            100,

            og_image.get(
                "content"
            ),
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

        add(

            90,

            twitter_image.get(
                "content"
            ),
        )


    candidates.sort(

        key=lambda item:
            item[0],

        reverse=True,
    )


    unique = []

    seen = set()


    for _, url in candidates:

        key = (
            url.split("?")[0]
            .lower()
        )


        if key in seen:
            continue


        seen.add(
            key
        )


        unique.append(
            url
        )


    return unique


# ============================================================
# DOWNLOAD IMAGE TO GITHUB
# ============================================================

def download_story_image(
    remote_url,
    story_id,
    referer="",
):

    if not remote_url:

        return ""


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
            timeout=20,
        ) as response:

            data = response.read(
                8_000_000
            )


        if len(data) < 12_000:

            return ""


        image = Image.open(
            io.BytesIO(
                data
            )
        )


        image.load()


        image = ImageOps.exif_transpose(
            image
        )


        width, height = (
            image.size
        )


        if (
            width < 400
            or height < 200
        ):

            return ""


        ratio = (
            width
            / height
        )


        if (
            ratio < 0.55
            or ratio > 4.5
        ):

            return ""


        image.thumbnail(
            (
                1600,
                1200,
            )
        )


        if image.mode != "RGB":

            image = image.convert(
                "RGB"
            )


        # IMPORTANT:
        # Store a RELATIVE path in stories.json.
        relative_path = (
            f"images/"
            f"{story_id}.jpg"
        )


        local_path = Path(
            relative_path
        )


        image.save(

            local_path,

            format="JPEG",

            quality=88,

            optimize=True,
        )


        print(
            "Saved story image:",
            relative_path,
        )


        return relative_path


    except Exception as error:

        print(
            "Image download failed:",
            remote_url,
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

    article_text = ""

    candidates = []


    page = fetch_page(
        article_url
    )


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

        key = (
            candidate
            .split("?")[0]
            .lower()
        )


        if key in seen:

            continue


        seen.add(
            key
        )


        relative_path = (
            download_story_image(

                candidate,

                story_id,

                referer=article_url,
            )
        )


        if relative_path:

            return (
                article_text,
                relative_path,
            )


    print(
        "No suitable story image found:",
        article_url,
    )


    return (
        article_text,
        "",
    )


# ============================================================
# STORY SELECTION
# ============================================================

def choose_diverse_stories(
    candidates,
    limit,
):

    selected = []

    used_publishers = set()

    used_ids = set()


    for wanted_region in (
        "Nigeria",
        "World",
    ):

        for story in candidates:

            if (
                story[
                    "region"
                ] != wanted_region
            ):

                continue


            if (
                story[
                    "_source"
                ] in used_publishers
            ):

                continue


            selected.append(
                story
            )


            used_publishers.add(
                story[
                    "_source"
                ]
            )


            used_ids.add(
                story[
                    "id"
                ]
            )


            if len(
                selected
            ) >= limit:

                return selected


    for story in candidates:

        if (
            story[
                "id"
            ] in used_ids
        ):

            continue


        selected.append(
            story
        )


        if len(
            selected
        ) >= limit:

            break


    return selected


# ============================================================
# OPENAI
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
You write for Policy Dossier, a neutral news and
public-affairs publication.

Use ONLY the supplied source material.

Return ONLY valid JSON:

{{
  "short": "Short summary",
  "body": "Website article"
}}

SHORT:

- Write two concise sentences.
- Aim for about 160 characters total.
- Neutral and factual.
- No hashtags.
- No emojis.
- No clickbait.
- Do not mention the source publisher.

BODY:

- Write a polished Policy Dossier article.
- Normally 250 to 450 words when enough information exists.
- Use 3 to 5 paragraphs.
- Explain what happened.
- Include important details.
- Include context only when supported.
- Rewrite completely in your own words.
- Do not reproduce the original article.
- Do not invent facts.
- Do not invent quotations.
- Do not invent numbers.
- Do not invent dates, people or organisations.
- Do not use outside knowledge.
- Do not identify the publisher.
- Do not tell readers to visit another news website.
- Do not say "read the full story".
- Do not say "read more at".
- If information is limited, write a shorter accurate article.

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


    short = clean_text(

        result.get(
            "short",
            "",
        )
    )


    body = str(
        result.get(
            "body",
            "",
        )
    ).strip()


    if (
        not short
        or not body
    ):

        raise ValueError(
            "OpenAI returned incomplete output."
        )


    return {

        "short":
            short,

        "body":
            body,
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
    content="width=device-width,initial-scale=1"
>

<title>
$title · Policy Dossier
</title>

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
    --fg:#f4f4f4;
    --mute:#777;
    --line:#ddd;
    --accent:#087a3b;
}

@media (prefers-color-scheme:light) {

    :root {
        --bg:#f7f6f2;
        --fg:#0b0b0b;
        --mute:#707070;
        --line:#d9d6cf;
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
    margin:auto;
    padding:0 20px;
    height:68px;
    display:flex;
    align-items:center;
    justify-content:space-between;
}

.brand {
    display:flex;
    gap:10px;
    align-items:center;
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
    color:var(--mute);
}

article {
    max-width:760px;
    margin:auto;
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
}

.follow {
    margin-top:18px;
    color:var(--mute);
    font-size:13px;
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

<span>
Policy Dossier
</span>

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

Source:
<strong>
$source
</strong>

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
# WEBSITE IMAGE PATHS
# ============================================================

def absolute_image_url(
    relative_path,
):

    if not relative_path:
        return ""


    return (
        f"{SITE_URL}/"
        f"{relative_path.lstrip('/')}"
    )


def article_image_src(
    relative_path,
):

    if not relative_path:
        return ""


    # Article pages are /stories/xxx.html,
    # therefore the local image is one folder up.
    return (
        f"../"
        f"{relative_path.lstrip('/')}"
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


        article_url = (
            f"{SITE_URL}/stories/"
            f"{story['id']}.html"
        )


        image = story.get(
            "image",
            "",
        )


        paragraphs = [

            paragraph.strip()

            for paragraph

            in re.split(

                r"\n+",

                story.get(
                    "body"
                )

                or

                story.get(
                    "summary"
                )

                or "",
            )

            if paragraph.strip()
        ]


        body_html = "".join(

            f"<p>{escape(paragraph)}</p>"

            for paragraph
            in paragraphs
        )


        if image:

            # X receives the ABSOLUTE URL.
            image_absolute = escape(
                absolute_image_url(
                    image
                )
            )


            # The website itself uses the RELATIVE URL.
            image_source = escape(
                article_image_src(
                    image
                )
            )


            og_image = (

                '<meta '
                'property="og:image" '
                f'content="{image_absolute}">\n'

                '<meta '
                'property="og:image:secure_url" '
                f'content="{image_absolute}">\n'

                '<meta '
                'property="og:image:type" '
                'content="image/jpeg">'
            )


            twitter_image = (

                '<meta '
                'name="twitter:image" '
                f'content="{image_absolute}">'
            )


            card = (
                "summary_large_image"
            )


            hero = (

                '<img '
                'class="hero" '

                f'src="{image_source}" '

                f'alt="{escape(story["title"])}" '

                'loading="eager">'
            )


        else:

            og_image = ""

            twitter_image = ""

            card = "summary"

            hero = ""


        page = PAGE.substitute(

            title=escape(
                story[
                    "title"
                ]
            ),

            summary=escape(
                story.get(
                    "summary",
                    "",
                )
            ),

            url=escape(
                article_url
            ),

            ogimg=og_image,

            card=card,

            twitterimg=twitter_image,

            region=escape(
                story.get(
                    "region",
                    "World",
                )
            ),

            date=escape(
                story.get(
                    "published",
                    "",
                )[:10]
            ),

            hero=hero,

            body=body_html,

            source=escape(
                story.get(
                    "_source",
                    "",
                )
            ),

            xurl=escape(
                X_URL
            ),
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
# CLEAN OLD GENERATED FILES
# ============================================================

def clean_generated_files(
    stories,
):

    valid_story_files = {

        f"{story['id']}.html"

        for story
        in stories
    }


    valid_image_files = {

        Path(
            story[
                "image"
            ]
        ).name

        for story
        in stories

        if story.get(
            "image"
        )
    }


    STORY_PAGES_DIR.mkdir(
        exist_ok=True
    )


    IMAGES_DIR.mkdir(
        exist_ok=True
    )


    for path in STORY_PAGES_DIR.glob(
        "*.html"
    ):

        if (
            path.name
            not in valid_story_files
        ):

            path.unlink(
                missing_ok=True
            )


    for path in IMAGES_DIR.iterdir():

        if (
            path.is_file()
            and path.name
            not in valid_image_files
        ):

            path.unlink(
                missing_ok=True
            )


# ============================================================
# LOAD / SAVE STORIES
# ============================================================

def load_stories():

    try:

        stories = json.loads(

            Path(
                STORIES_FILE
            ).read_text(
                encoding="utf-8"
            )
        )


        if not isinstance(
            stories,
            list,
        ):

            return []


        for story in stories:


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


            story.setdefault(
                "image",
                "",
            )


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

    Path(
        STORIES_FILE
    ).write_text(

        json.dumps(
            stories,
            indent=2,
            ensure_ascii=False,
        ),

        encoding="utf-8",
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
            "X posting disabled. Missing:",
            ", ".join(
                missing
            ),
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


    if len(title) > 82:

        title = (
            title[:79]
            .rstrip()
            + "..."
        )


    if len(summary) > 92:

        summary = (
            summary[:89]
            .rstrip()
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
            "No deployed story waiting for X."
        )

        return


    pending.sort(

        key=lambda story:
            story.get(
                "published",
                "",
            )
    )


    story = pending[0]


    try:

        # Only the Policy Dossier URL is posted.
        # X should read og:image/twitter:image from that page.
        x_client.create_tweet(

            text=build_x_post(
                story
            )
        )


        story[
            "x_posted"
        ] = True


        print(
            "Posted link to X:",
            story[
                "title"
            ],
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
    # POST ONE STORY CREATED ON THE PREVIOUS RUN
    # --------------------------------------------------------

    x_client = (
        create_x_client()
    )


    post_previous_story_to_x(

        existing_stories,

        x_client,
    )


    # --------------------------------------------------------
    # EXISTING IDS
    # --------------------------------------------------------

    existing_ids = {

        story[
            "id"
        ]

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


        feed = feedparser.parse(
            feed_url
        )


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

                published = (

                    dt.datetime.fromtimestamp(

                        calendar.timegm(
                            published_struct
                        ),

                        tz=dt.timezone.utc,
                    )
                )


            else:

                published = now


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


            candidates.append(
                {

                    "id":
                        story_id,

                    "title":
                        title,

                    "link":
                        link,

                    "_source":
                        publisher,

                    "region":
                        region,

                    "rss_text":
                        rss_text,

                    "feed_image":
                        find_feed_image(
                            entry,
                            link,
                        ),

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


    # Newest first.
    candidates.sort(

        key=lambda story:
            story[
                "published"
            ],

        reverse=True,
    )


    # Nigeria first.
    candidates.sort(

        key=lambda story:
            story[
                "region"
            ] != "Nigeria"
    )


    selected = (
        choose_diverse_stories(

            candidates,

            MAX_NEW_PER_RUN,
        )
    )


    # --------------------------------------------------------
    # FETCH ARTICLE + IMAGE
    # --------------------------------------------------------

    for story in selected:


        (
            article_text,
            image_path,
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
        ] = image_path


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
                "OpenAI failed:",
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
        ]


        story[
            "body"
        ] = result[
            "body"
        ]


        # Website only by default.
        story[
            "x_posted"
        ] = True


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


    # Exactly one fresh story is queued for X.
    # It will be posted on the NEXT run after Pages deploys.
    if processed:

        processed[0][
            "x_posted"
        ] = False


    stories = (

        processed

        +

        existing_stories

    )[:KEEP]


    # Generate HTML.
    write_pages(
        stories
    )


    # Automatically remove old HTML/images that are no longer used.
    clean_generated_files(
        stories
    )


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
