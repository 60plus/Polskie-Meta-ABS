import re
from urllib.parse import urljoin, urlparse

import lubimyczytac_provider as provider
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


BASE = provider.BASE


def absolute_url(value):
    value = provider.clean(value)
    if not value:
        return None
    return urljoin(BASE + "/", value)


def first_year(value):
    return provider.parse_year(value)


async def text_after(scope, selectors):
    for selector in selectors:
        try:
            loc = scope.locator(selector).first
            if await loc.count():
                text = provider.clean(await loc.text_content())
                if text:
                    return text
        except Exception:
            pass
    return None


async def attr_first(scope, selectors, attrs=("href", "content", "src", "data-src")):
    for selector in selectors:
        try:
            loc = scope.locator(selector).first
            if not await loc.count():
                continue
            for attr in attrs:
                value = await loc.get_attribute(attr)
                if value:
                    return absolute_url(value)
        except Exception:
            pass
    return None


async def robust_parse_detail(page, candidate):
    url = candidate["url"]
    query = candidate.get("query") or candidate.get("title") or provider.url_title(url)
    item_type = provider.path_type(url)

    # The audiobook page renders additional metadata after DOMContentLoaded.
    # The old 150 ms wait was too short and could leave an almost empty card.
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        await page.locator("h1, #book-description, a#js-lightboxCover, meta[property='og:image']").first.wait_for(
            state="attached", timeout=4000
        )
    except Exception:
        pass
    await page.wait_for_timeout(700)

    body = await page.locator("body").inner_text(timeout=10000)
    lines = [provider.clean(x) for x in body.splitlines() if provider.clean(x)]

    data = {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "description": None,
        "cover": None,
        "isbn": None,
        "duration": None,
        "genres": [],
        "series": None,
        "sequence": None,
        "language": "pol",
        "url": provider.canonical(url),
        "type": item_type,
    }

    # Prefer the actual product heading. This prevents JSON-LD from a
    # recommendation with the same title from replacing the selected result.
    h1 = await text_after(page, ["h1"])
    if h1:
        data["title"] = h1

    # JSON-LD is used only as a supplementary source. Prefer an object whose
    # URL points at the current /ksiazka/ or /audiobook/ page.
    objects = []
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        objects.extend(provider.jsonld_objects(raw))

    target = None
    best = -1.0
    current_path = urlparse(url).path.rstrip("/")
    for item in objects:
        name = provider.clean(item.get("name"))
        if not name:
            continue
        item_url = str(item.get("url") or "")
        score = provider.similarity(name, data["title"] or query)
        if item_url and urlparse(item_url).path.rstrip("/") == current_path:
            score += 2.0
        types = item.get("@type")
        type_text = " ".join(types) if isinstance(types, list) else str(types or "")
        if item_type == "audiobook" and re.search(r"audiobook|audio", type_text, re.I):
            score += 0.25
        if item_type == "book" and re.search(r"book", type_text, re.I):
            score += 0.10
        if score > best:
            best = score
            target = item

    if target:
        data["title"] = data["title"] or provider.clean(target.get("name"))
        data["author"] = data["author"] or provider.first_name(target.get("author"))
        data["description"] = provider.clean(target.get("description"))
        data["isbn"] = provider.clean(target.get("isbn"))
        data["publishedYear"] = first_year(target.get("datePublished"))
        data["duration"] = provider.parse_duration(target.get("duration"))
        image = target.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        data["cover"] = absolute_url(image)
        pub = target.get("publisher")
        if isinstance(pub, dict):
            pub = pub.get("name")
        data["publisher"] = provider.clean(pub)
        genre = target.get("genre")
        if isinstance(genre, list):
            data["genres"] = [provider.clean(x) for x in genre if provider.clean(x)]
        elif genre:
            data["genres"] = [provider.clean(genre)]

    # Dedicated description works for both normal books and audiobooks.
    try:
        desc = page.locator("#book-description").first
        if await desc.count():
            value = provider.clean(await desc.text_content())
            if value:
                data["description"] = value
                print(
                    f"[Lubimyczytać] description: chars={len(value)} "
                    f"type={data['type']} url={data['url']}"
                )
    except Exception:
        pass

    # Lubimyczytać has several cover variants. Audiobooks commonly use the
    # same lightbox link but a rectangular image, so do not restrict by image
    # dimensions or by the normal book-cover class.
    data["cover"] = await attr_first(
        page,
        [
            "a#js-lightboxCover[href]",
            "a#js-lightboxCover img[src]",
            ".book-cover__link[href]",
            ".book-cover__link img[src]",
            "meta[property='og:image']",
            "meta[name='twitter:image']",
            "link[rel='image_src'][href]",
            "img.book-cover[src]",
            "img[src*='lubimyczytac']",
        ],
    ) or data["cover"]

    # Author links on detail pages are more trustworthy than a generic JSON-LD
    # object, especially on audiobook pages with related products.
    try:
        names = [
            provider.clean(x)
            for x in await page.locator("a[href*='/autor/']").all_text_contents()
            if provider.clean(x)
        ]
        if names:
            data["author"] = ", ".join(dict.fromkeys(names[:8]))
    except Exception:
        pass

    data["publisher"] = data["publisher"] or await text_after(
        page,
        [
            "span.book__txt:has-text('Wydawnictwo:') a",
            "[data-ga-book-publishers]",
        ],
    ) or provider.label_value(lines, ["Wydawca", "Wydawnictwo"])

    # Use the explicit date definition first. The audiobook page can have a
    # publication/premiere date in a different place than a printed edition.
    date_text = await text_after(
        page,
        [
            "dt[title*='Data pierwszego wydania'] + dd",
            "dt[title*='Data wydania'] + dd",
            "dt:has-text('Data pierwszego wydania') + dd",
            "dt:has-text('Data wydania') + dd",
            "dt:has-text('Data publikacji') + dd",
            "dt:has-text('Data premiery') + dd",
        ],
    )
    data["publishedYear"] = (
        first_year(date_text)
        or data["publishedYear"]
        or first_year(provider.label_value(lines, [
            "Data pierwszego wydania",
            "Data wydania",
            "Data 1. wyd. pol.",
            "Data publikacji",
            "Data premiery",
            "Rok wydania",
        ]))
    )

    # Prefer the page's explicit ISBN meta property.
    try:
        isbn = await page.locator("meta[property='books:isbn']").get_attribute("content")
        data["isbn"] = provider.clean(isbn) or data["isbn"]
    except Exception:
        pass
    data["isbn"] = data["isbn"] or provider.label_value(lines, ["ISBN"])
    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        if m:
            data["isbn"] = m.group(1)

    data["duration"] = data["duration"] or provider.parse_duration(
        provider.label_value(lines, ["Czas czytania", "Długość", "Czas trwania"])
    )
    data["narrator"] = provider.label_value(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])

    language = provider.label_value(lines, ["Język"])
    data["language"] = "pol" if not language or provider.norm(language) in {"polski", "polska", "pol"} else provider.norm(language)

    category = provider.label_value(lines, ["Kategoria", "Kategorie"])
    if category:
        data["genres"] = [provider.clean(x) for x in re.split(r"[,;/]", category) if provider.clean(x)]

    # DOM series marker is preferable because it carries the volume number.
    series_text = await text_after(
        page,
        [
            "span.d-none.d-sm-block.mt-1:has-text('Cykl:') a",
            "a[href*='/cykl/']",
            "a[href*='/seria/']",
        ],
    )
    if series_text:
        m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)", series_text, re.I)
        if m:
            data["series"], data["sequence"] = provider.clean(m.group(1)), m.group(2)
        else:
            data["series"] = provider.clean(series_text)
    if not data["series"]:
        data["series"], data["sequence"] = provider.series_from_lines(lines)

    return data


# Replace only the detail parser. Search/ranking/API remain the existing
# provider implementation.
provider.parse_detail = robust_parse_detail


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(provider.app, host="127.0.0.1", port=8002)
