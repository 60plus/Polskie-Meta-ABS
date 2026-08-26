import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, urljoin, urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI(title="LubimyCzytać Metadata Provider")
BASE = "https://lubimyczytac.pl"
CACHE_TTL = 600
MAX_RESULTS = 20
MAX_DETAIL_CANDIDATES = 12
_pw = _browser = _context = None
_lock = asyncio.Lock()
_cache = {}


def clean(value):
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def norm(value):
    value = str(value or "").replace("ł", "l").replace("Ł", "L")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.97
    return SequenceMatcher(None, a, b).ratio()


def canonical(url):
    parsed = urlparse(url)
    return urljoin(BASE, parsed.path.rstrip("/") + "/")


def is_product_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.startswith("/ksiazka/") or path.startswith("/audiobook/")


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug).strip()


def parse_year(value):
    m = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return m.group(0) if m else None


def parse_duration(value):
    text = str(value or "")
    h = re.search(r"(\d+)\s*(?:godz\.?|godziny|godzin|h)\b", text, re.I)
    m = re.search(r"(\d+)\s*(?:min\.?|minut|m)\b", text, re.I)
    if h:
        return int(h.group(1)) * 60 + int(m.group(1) if m else 0)
    return int(m.group(1)) if m else None


def lines_from_body(body):
    return [clean(x) for x in str(body or "").splitlines() if clean(x)]


def value_after_label(lines, labels):
    wanted = {norm(x) for x in labels}
    for i, line in enumerate(lines):
        if norm(line) in wanted:
            for candidate in lines[i + 1:i + 6]:
                if candidate and norm(candidate) not in wanted:
                    return candidate
    return None


def jsonld_objects(raw):
    try:
        value = json.loads(raw)
    except Exception:
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        if isinstance(item, dict):
            result.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                result.extend(x for x in graph if isinstance(x, dict))
    return result


def first_name(value):
    if isinstance(value, dict):
        return clean(value.get("name"))
    if isinstance(value, list):
        names = [first_name(x) for x in value]
        return ", ".join(dict.fromkeys(x for x in names if x)) or None
    return clean(value)


def jsonld_value(obj, *keys):
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", []):
            return value
    return None


async def get_context():
    global _pw, _browser, _context
    async with _lock:
        if _context:
            return _context
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        _context = await _browser.new_context(
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1440, "height": 1000},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"},
        )
        return _context


async def open_page(page, url, wait=450):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(wait)


async def first_image_url(scope):
    """Extract an image URL from a card/page, including lazy-loaded variants."""
    selectors = (
        "img[src]",
        "img[data-src]",
        "img[data-original]",
        "img[data-lazy-src]",
        "source[srcset]",
        "img[srcset]",
    )
    for selector in selectors:
        try:
            loc = scope.locator(selector).first
            if not await loc.count():
                continue
            for attr in ("data-src", "data-original", "data-lazy-src", "src", "srcset"):
                value = clean(await loc.get_attribute(attr))
                if not value:
                    continue
                if attr == "srcset":
                    value = value.split(",")[0].strip().split(" ")[0]
                if value and not value.startswith("data:"):
                    return urljoin(BASE, value)
        except Exception:
            continue
    return None


async def search_page(page, query, author, section, result_type):
    url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    if author:
        url += f"&author={quote(author)}"
    print(f"[Lubimyczytać] search: {url}")
    await open_page(page, url, 350)

    found, seen = [], set()
    cards = page.locator(".book-card--l")
    count = await cards.count()

    for i in range(count):
        card = cards.nth(i)
        try:
            title_loc = card.locator(".book-card__title").first
            title = clean(await title_loc.text_content()) if await title_loc.count() else None
            href = await title_loc.get_attribute("href") if await title_loc.count() else None

            # Audiobook search cards sometimes expose the title as /ksiazka/ while
            # the actual audio product link is elsewhere in the same card.
            if result_type == "audiobook":
                audio_link = card.locator("a[href*='/audiobook/']").first
                if await audio_link.count():
                    audio_href = await audio_link.get_attribute("href")
                    if audio_href:
                        href = audio_href
            elif not href:
                book_link = card.locator("a[href*='/ksiazka/']").first
                if await book_link.count():
                    href = await book_link.get_attribute("href")

            if not href:
                continue
            href = canonical(urljoin(BASE, href))
            if not is_product_url(href):
                continue

            actual_type = "audiobook" if "/audiobook/" in urlparse(href).path else result_type
            key = (href, actual_type)
            if key in seen:
                continue
            seen.add(key)

            authors = [clean(x) for x in await card.locator(".book-card__author a").all_text_contents() if clean(x)]
            # IMPORTANT: audiobook detail pages can be protected differently from
            # book pages. Keep the cover from the search card as a reliable fallback.
            card_cover = await first_image_url(card)
            found.append({
                "url": href,
                "title": title or url_title(href),
                "authors": list(dict.fromkeys(authors)),
                "type": actual_type,
                "search_cover": card_cover,
            })
        except Exception:
            continue

    # Direct link scan catches audiobook links hidden outside the title anchor.
    links = page.locator("a[href*='/audiobook/']") if result_type == "audiobook" else page.locator("a[href*='/ksiazka/']")
    for i in range(min(await links.count(), 30)):
        try:
            link = links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue
            href = canonical(urljoin(BASE, href))
            if not is_product_url(href):
                continue
            actual_type = "audiobook" if "/audiobook/" in urlparse(href).path else result_type
            key = (href, actual_type)
            if key in seen:
                continue
            seen.add(key)
            parent = link.locator("xpath=ancestor::*[contains(@class,'book-card')][1]").first
            cover = await first_image_url(parent) if await parent.count() else None
            found.append({"url": href, "title": clean(await link.text_content()) or url_title(href), "authors": [], "type": actual_type, "search_cover": cover})
        except Exception:
            continue

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


async def parse_detail(page, candidate):
    url = canonical(candidate["url"])
    media_type = "audiobook" if "/audiobook/" in urlparse(url).path else candidate.get("type", "book")
    await open_page(page, url, 650)
    body = await page.locator("body").inner_text()
    lines = lines_from_body(body)

    data = {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "publishedDate": None,
        "description": None,
        "cover": candidate.get("search_cover"),
        "isbn": None,
        "duration": None,
        "pages": None,
        "translator": None,
        "genres": [],
        "tags": [],
        "series": None,
        "sequence": None,
        "language": "pol",
        "url": url,
        "type": media_type,
    }

    # If the detail request is a bot/challenge page, the search-card cover is
    # still valid. We continue parsing every available fallback instead of
    # returning an empty audiobook record.
    try:
        h1 = page.locator("h1").first
        if await h1.count():
            value = clean(await h1.text_content())
            if value:
                data["title"] = value
    except Exception:
        pass

    # Reference provider selectors + generic image fallbacks. The latter are
    # especially important for /audiobook/ pages.
    for selector, attr in (
        ("a#js-lightboxCover", "href"),
        (".book-cover__link", "href"),
        ("meta[property='og:image']", "content"),
        ("meta[name='twitter:image']", "content"),
        ("meta[itemprop='image']", "content"),
    ):
        try:
            loc = page.locator(selector).first
            if await loc.count():
                value = clean(await loc.get_attribute(attr))
                if value:
                    data["cover"] = urljoin(BASE, value)
                    break
        except Exception:
            pass
    if not data["cover"]:
        data["cover"] = await first_image_url(page.locator("main").first if await page.locator("main").count() else page.locator("body"))

    # Description: keep the same #book-description first, then OpenGraph/body fallbacks.
    for selector in ("#book-description", ".book__description", ".book-description", "[class*='book-description']"):
        if data["description"]:
            break
        try:
            loc = page.locator(selector).first
            if await loc.count():
                value = clean(await loc.text_content())
                if value:
                    data["description"] = value
        except Exception:
            pass
    if not data["description"]:
        try:
            loc = page.locator("meta[property='og:description']").first
            if await loc.count():
                data["description"] = clean(await loc.get_attribute("content"))
        except Exception:
            pass

    # Publisher and language follow the original provider's selectors.
    try:
        loc = page.locator("span.book__txt").filter(has_text="Wydawnictwo:").locator("a").first
        if await loc.count():
            data["publisher"] = clean(await loc.text_content())
        if not data["publisher"]:
            loc = page.locator("[data-ga-book-publishers]").first
            if await loc.count():
                data["publisher"] = clean(await loc.get_attribute("data-ga-book-publishers"))
    except Exception:
        pass

    try:
        loc = page.locator("dt").filter(has_text="Język:").locator("xpath=following-sibling::dd[1]").first
        if await loc.count():
            language = clean(await loc.text_content())
            if language:
                data["language"] = "pol" if norm(language.split(",")[0]) in {"polski", "polska", "pol"} else norm(language.split(",")[0])
    except Exception:
        pass

    try:
        loc = page.locator("meta[property='books:isbn']").first
        if await loc.count():
            data["isbn"] = clean(await loc.get_attribute("content"))
    except Exception:
        pass

    # JSON-LD is useful when present, but never required for an audiobook.
    try:
        for script in await page.locator("script[type='application/ld+json']").all_text_contents():
            for obj in jsonld_objects(script):
                typ = obj.get("@type")
                types = typ if isinstance(typ, list) else [typ]
                if not any(x in {"Book", "Audiobook", "Product"} for x in types):
                    continue
                data["title"] = clean(obj.get("name")) or data["title"]
                data["author"] = first_name(obj.get("author")) or data["author"]
                data["publisher"] = first_name(obj.get("publisher")) or data["publisher"]
                data["description"] = data["description"] or clean(obj.get("description"))
                data["isbn"] = data["isbn"] or clean(jsonld_value(obj, "isbn", "productID"))
                image = jsonld_value(obj, "image", "thumbnailUrl")
                if isinstance(image, list):
                    image = image[0] if image else None
                if isinstance(image, dict):
                    image = image.get("url")
                if image:
                    data["cover"] = urljoin(BASE, str(image))
                data["duration"] = data["duration"] or parse_duration(jsonld_value(obj, "duration", "timeRequired"))
                data["narrator"] = data["narrator"] or first_name(jsonld_value(obj, "readBy", "reader", "narrator"))
                break
    except Exception:
        pass

    # Visible labels are particularly important on the current audiobook layout.
    data["publisher"] = data["publisher"] or value_after_label(lines, ["Wydawca", "Wydawnictwo"])
    data["publishedYear"] = parse_year(value_after_label(lines, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery", "Rok wydania"]))
    data["publishedDate"] = value_after_label(lines, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery"])
    data["isbn"] = data["isbn"] or value_after_label(lines, ["ISBN"])
    data["narrator"] = data["narrator"] or value_after_label(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    data["duration"] = data["duration"] or parse_duration(value_after_label(lines, ["Długość", "Czas czytania", "Czas trwania", "Czas trwania audiobooka"]))
    data["translator"] = value_after_label(lines, ["Tłumacz", "Tłumacz:"])
    data["pages"] = value_after_label(lines, ["Liczba stron", "Strony"])

    # Detail author selectors; only replace the search author with a matching one.
    try:
        detail_authors = []
        for selector in ("a.book__author", ".book__authors a[href*='/autor/']"):
            detail_authors += [clean(x) for x in await page.locator(selector).all_text_contents() if clean(x)]
        if detail_authors:
            unique = list(dict.fromkeys(detail_authors))
            requested = candidate.get("authors") or []
            matching = [x for x in unique if not requested or max(similarity(x, y) for y in requested) >= 0.7]
            if matching:
                data["author"] = ", ".join(matching[:5])
    except Exception:
        pass

    series_value = value_after_label(lines, ["Cykl", "Seria"])
    if series_value:
        m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)\)", series_value, re.I)
        data["series"] = clean(m.group(1) if m else series_value)
        data["sequence"] = m.group(2) if m else None

    try:
        genre = page.locator(".book__category.d-sm-block.d-none").first
        if await genre.count():
            text = clean(await genre.text_content())
            data["genres"] = [x.strip() for x in text.split(",") if x.strip()] if text else []
    except Exception:
        pass
    try:
        data["tags"] = [clean(x) for x in await page.locator("a[href*='/ksiazki/t/']").all_text_contents() if clean(x)]
    except Exception:
        pass

    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        data["isbn"] = m.group(1) if m else None
    if not data.get("author"):
        data["author"] = ", ".join(candidate.get("authors") or []) or None

    print(
        f"[Lubimyczytać] detail: type={data['type']} cover={'yes' if data.get('cover') else 'no'} "
        f"description={len(data.get('description') or '')}chars publisher={data.get('publisher') or '-'} "
        f"narrator={data.get('narrator') or '-'} year={data.get('publishedYear') or '-'} url={url}"
    )
    return data


def score(data, query, author, candidate_authors=None):
    title_s = similarity(data.get("title"), query)
    values = []
    if data.get("author"):
        values.append(data["author"])
    values.extend(candidate_authors or [])
    author_s = max((similarity(x, author) for x in values if x), default=0.5) if author else 1.0
    combined = title_s * 0.60 + author_s * 0.40 if author else title_s
    if author and author_s < 0.15:
        return 0.0
    if not data.get("isbn"):
        combined *= 0.99
    return min(combined, 1.0)


def to_match(data, value):
    description = data.get("description")
    if description == "Ta książka nie posiada jeszcze opisu.":
        description = "Brak opisu."
    return {
        "title": data.get("title"),
        "author": data.get("author"),
        "narrator": data.get("narrator"),
        "publisher": data.get("publisher"),
        "publishedYear": data.get("publishedYear"),
        "description": description,
        "cover": data.get("cover"),
        "isbn": data.get("isbn"),
        "genres": data.get("genres") or None,
        "series": ([{"series": data["series"], "sequence": data.get("sequence")}] if data.get("series") else None),
        "language": data.get("language", "pol"),
        "duration": data.get("duration"),
        "type": data.get("type", "book"),
        "similarity": round(value, 3),
    }


async def lubimyczytac_search(query, author=""):
    key = f"lubimyczytac|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        print(f"[Lubimyczytać] cache hit: {key}")
        return cached[1]

    context = await get_context()
    search = await context.new_page()
    try:
        books = await search_page(search, query, author, "ksiazki", "book")
        audiobooks = await search_page(search, query, author, "audiobooki", "audiobook")
    finally:
        await search.close()

    candidates = books + audiobooks
    for item in candidates:
        title_s = similarity(item.get("title"), query)
        author_s = max((similarity(x, author) for x in item.get("authors") or []), default=0.5) if author else 1.0
        item["pre_score"] = title_s * 0.60 + author_s * 0.40 if author else title_s

    # Exact title+author matches first. This avoids parsing 20 pages and makes
    # the UI responsive enough that one click normally finishes the request.
    candidates.sort(key=lambda x: (x["pre_score"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    candidates = candidates[:MAX_DETAIL_CANDIDATES]
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    # Do not hit LC with six simultaneous detail requests. That was the main
    # reason audiobook pages frequently returned empty/challenge documents.
    sem = asyncio.Semaphore(2)

    async def parse_one(candidate):
        async with sem:
            page = await context.new_page()
            try:
                data = await parse_detail(page, candidate)
                value = score(data, query, author, candidate.get("authors"))
                return value, data
            except Exception as exc:
                print(f"[Lubimyczytać] detail failed: {candidate['url']} {type(exc).__name__}: {exc}")
                return None
            finally:
                await page.close()

    parsed = await asyncio.gather(*(parse_one(c) for c in candidates))
    ranked = []
    for result in parsed:
        if not result:
            continue
        value, data = result
        if value <= 0:
            continue
        ranked.append((value, data))
        print(f"[Lubimyczytać] parsed: {data.get('title')} / {data.get('author')} type={data.get('type')} score={value:.3f} url={data.get('url')}")

    ranked.sort(key=lambda x: (x[0], 1 if x[1].get("type") == "audiobook" else 0), reverse=True)
    final = [to_match(data, value) for value, data in ranked[:MAX_RESULTS]]
    print("[Lubimyczytać] final:", " | ".join(f"{x['title']}/{x.get('author')} [{x['type']}] ({x['similarity']:.3f})" for x in final))

    result = {"matches": final}
    _cache[key] = (time.time(), result)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "lubimyczytac"}


@app.get("/search")
async def search(query: str = Query(..., min_length=1), author: str = Query(""), authorization: str | None = Header(default=None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(await lubimyczytac_search(query, author))
