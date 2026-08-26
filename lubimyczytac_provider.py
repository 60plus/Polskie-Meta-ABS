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
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
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
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(wait)


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

            # On the audiobook search page LC can expose the real /audiobook/
            # link elsewhere in the card. Prefer it whenever present.
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

            # Media type comes from the search section, but if LC gives us an
            # explicit /audiobook/ URL it is the strongest possible signal.
            actual_type = "audiobook" if "/audiobook/" in urlparse(href).path else result_type
            key = (href, actual_type)
            if key in seen:
                continue
            seen.add(key)

            authors = [
                clean(x)
                for x in await card.locator(".book-card__author a").all_text_contents()
                if clean(x)
            ]
            found.append({
                "url": href,
                "title": title or url_title(href),
                "authors": list(dict.fromkeys(authors)),
                "type": actual_type,
            })
        except Exception:
            continue

    # Last-resort direct link scan. This is important for cards whose title
    # anchor points at /ksiazka/ while the audiobook anchor is hidden nearby.
    if result_type == "audiobook":
        links = page.locator("a[href*='/audiobook/']")
    else:
        links = page.locator("a[href*='/ksiazka/']")

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
            title = clean(await link.text_content()) or url_title(href)
            found.append({"url": href, "title": title, "authors": [], "type": actual_type})
        except Exception:
            continue

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


async def parse_detail(page, candidate):
    url = candidate["url"]
    await open_page(page, url, 600)
    body = await page.locator("body").inner_text()
    lines = lines_from_body(body)
    media_type = "audiobook" if "/audiobook/" in urlparse(url).path else candidate.get("type", "book")

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
        "url": canonical(url),
        "type": media_type,
    }

    h1 = page.locator("h1").first
    if await h1.count():
        value = clean(await h1.text_content())
        if value:
            data["title"] = value

    # Read structured metadata first. Audiobook pages use a different markup
    # from /ksiazka/ pages, and JSON-LD/meta tags are much more stable than
    # book-only CSS selectors.
    try:
        scripts = await page.locator("script[type='application/ld+json']").all_text_contents()
        for script in scripts:
            for obj in jsonld_objects(script):
                typ = obj.get("@type")
                types = typ if isinstance(typ, list) else [typ]
                if not any(x in {"Book", "Audiobook", "Product"} for x in types):
                    continue
                data["title"] = clean(obj.get("name")) or data["title"]
                data["author"] = first_name(obj.get("author")) or data["author"]
                data["publisher"] = first_name(obj.get("publisher")) or data["publisher"]
                data["description"] = clean(obj.get("description")) or data["description"]
                data["isbn"] = clean(obj.get("isbn")) or data["isbn"]
                image = jsonld_value(obj, "image", "thumbnailUrl")
                if isinstance(image, list):
                    image = image[0] if image else None
                if isinstance(image, dict):
                    image = image.get("url")
                if image:
                    data["cover"] = urljoin(BASE, str(image))
                duration = jsonld_value(obj, "duration", "timeRequired")
                data["duration"] = parse_duration(duration) or data["duration"]
                narrator = jsonld_value(obj, "readBy", "narrator", "actor")
                data["narrator"] = first_name(narrator) or data["narrator"]
                break
    except Exception:
        pass

    # Meta tags work for both page families and are the main fallback for the
    # audiobook cover/description.
    async def meta_content(selector):
        loc = page.locator(selector).first
        if await loc.count():
            return clean(await loc.get_attribute("content"))
        return None

    data["description"] = data["description"] or await meta_content("meta[property='og:description']")
    data["cover"] = data["cover"] or await meta_content("meta[property='og:image']")
    data["cover"] = data["cover"] or await meta_content("meta[name='twitter:image']")
    data["cover"] = data["cover"] or await meta_content("meta[itemprop='image']")
    if data["cover"]:
        data["cover"] = urljoin(BASE, data["cover"])

    # Book-page selectors retained for the normal /ksiazka/ layout.
    description_selectors = (
        "#book-description",
        ".book__description",
        ".book-description",
        "[class*='book-description']",
        "[class*='description']",
    )
    if not data["description"]:
        for selector in description_selectors:
            try:
                loc = page.locator(selector).first
                if await loc.count():
                    value = clean(await loc.text_content())
                    if value and len(value) > 40:
                        data["description"] = value
                        break
            except Exception:
                pass

    # Cover selectors for both book and audiobook pages. For audiobook LC
    # commonly uses an image/anchor outside the old #js-lightboxCover node.
    cover_selectors = (
        "a#js-lightboxCover[href]",
        ".book-cover__link[href]",
        ".book-cover a[href]",
        "a[href*='/images/'] img",
        "main img[alt*='Okładka']",
        "main img[alt*='Siostry']",
        "main img[src]",
    )
    if not data["cover"]:
        for selector in cover_selectors:
            try:
                loc = page.locator(selector).first
                if not await loc.count():
                    continue
                value = await loc.get_attribute("href")
                if not value:
                    value = await loc.get_attribute("src")
                if not value:
                    value = await loc.get_attribute("data-src")
                if value:
                    data["cover"] = urljoin(BASE, value)
                    break
            except Exception:
                pass

    # Detail author/narrator selectors plus visible labels.
    detail_authors = []
    for selector in (
        "a.book__author",
        ".book__authors a[href*='/autor/']",
        "a[href*='/autor/']",
        "a[href*='/autorzy/']",
    ):
        try:
            detail_authors += [clean(x) for x in await page.locator(selector).all_text_contents() if clean(x)]
        except Exception:
            pass
    # Do not overwrite a good search-card author with recommendation authors.
    if detail_authors:
        candidates = list(dict.fromkeys(detail_authors))
        requested = candidate.get("authors") or []
        matching = [x for x in candidates if not requested or max(similarity(x, y) for y in requested) >= 0.70]
        if matching:
            data["author"] = ", ".join(matching[:5])

    data["publisher"] = data["publisher"] or value_after_label(lines, ["Wydawca", "Wydawnictwo"])
    data["publishedYear"] = parse_year(value_after_label(lines, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery", "Rok wydania"]))
    data["isbn"] = data["isbn"] or value_after_label(lines, ["ISBN"])
    data["duration"] = data["duration"] or parse_duration(value_after_label(lines, ["Czas czytania", "Długość", "Czas trwania", "Czas trwania audiobooka"]))
    data["narrator"] = data["narrator"] or value_after_label(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator", "Narracja"])

    language = value_after_label(lines, ["Język"])
    data["language"] = "pol" if not language or norm(language) in {"polski", "polska", "pol"} else norm(language)

    series_value = value_after_label(lines, ["Cykl", "Seria"])
    if series_value:
        m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)\)", series_value, re.I)
        data["series"] = clean(m.group(1) if m else series_value)
        data["sequence"] = m.group(2) if m else None

    # Search-card author remains the final fallback. This is especially
    # important for audiobook pages whose visible author markup differs.
    if not data.get("author"):
        data["author"] = ", ".join(candidate.get("authors") or []) or None

    m = re.search(r"\b(97[89]\d{10})\b", body)
    data["isbn"] = data["isbn"] or (m.group(1) if m else None)

    print(
        f"[Lubimyczytać] description: chars={len(data.get('description') or '')} "
        f"type={data['type']} cover={'yes' if data.get('cover') else 'no'} "
        f"url={data['url']}"
    )
    return data


def score(data, query, author, candidate_authors=None):
    title_s = similarity(data.get("title"), query)
    author_values = []
    if data.get("author"):
        author_values.append(data["author"])
    author_values.extend(candidate_authors or [])
    author_s = max((similarity(x, author) for x in author_values if x), default=0.5) if author else 1.0
    combined = title_s * 0.60 + author_s * 0.40 if author else title_s
    if author and author_s < 0.15:
        return 0.0, title_s, author_s
    if not data.get("isbn"):
        combined *= 0.99
    return min(combined, 1.0), title_s, author_s


def to_match(data, value):
    return {
        "title": data.get("title"),
        "author": data.get("author"),
        "narrator": data.get("narrator"),
        "publisher": data.get("publisher"),
        "publishedYear": data.get("publishedYear"),
        "description": data.get("description"),
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

    candidates.sort(key=lambda x: (x["pre_score"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    candidates = candidates[:MAX_RESULTS]
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    sem = asyncio.Semaphore(6)

    async def parse_one(candidate):
        async with sem:
            page = await context.new_page()
            try:
                data = await parse_detail(page, candidate)
                value, title_s, author_s = score(data, query, author, candidate.get("authors"))
                return value, title_s, author_s, data
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
        value, title_s, author_s, data = result
        if value <= 0:
            continue
        ranked.append((value, title_s, author_s, data))
        print(
            f"[Lubimyczytać] parsed: {data.get('title')} / {data.get('author')} "
            f"type={data.get('type')} score={value:.3f} url={data.get('url')}"
        )

    ranked.sort(key=lambda x: (x[0], 1 if x[3].get("type") == "audiobook" else 0), reverse=True)
    final = [to_match(data, value) for value, _, _, data in ranked[:MAX_RESULTS]]
    print(
        "[Lubimyczytać] final:",
        " | ".join(f"{x['title']}/{x.get('author')} [{x['type']}] ({x['similarity']:.3f})" for x in final),
    )

    result = {"matches": final}
    _cache[key] = (time.time(), result)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "lubimyczytac"}


@app.get("/search")
async def search(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(await lubimyczytac_search(query, author))
