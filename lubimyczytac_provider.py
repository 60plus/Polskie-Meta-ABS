import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI(title="LubimyCzytać Metadata Provider")
BASE = "https://lubimyczytac.pl"
CACHE_TTL = 600
MAX_RESULTS = 20
MAX_DETAIL_CANDIDATES = 20
_pw = _browser = _context = None
_lock = asyncio.Lock()
_cache = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
}


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
    # Do not manufacture a trailing slash: audiobook pages are especially
    # sensitive to the exact canonical URL returned by the search page.
    path = parsed.path.rstrip("/")
    return urljoin(BASE, path + "/")


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
    wanted = {norm(x).rstrip(":") for x in labels}
    for i, line in enumerate(lines):
        current = norm(line).rstrip(":")
        if current in wanted:
            for candidate in lines[i + 1:i + 8]:
                if candidate and norm(candidate).rstrip(":") not in wanted:
                    return candidate
    return None


def text_after_heading(soup, labels):
    wanted = {norm(x).rstrip(":") for x in labels}
    for node in soup.find_all(["dt", "div", "span", "strong", "h2", "h3", "h4"]):
        text = clean(node.get_text(" ", strip=True))
        if not text:
            continue
        n = norm(text).rstrip(":")
        if n in wanted:
            nxt = node.find_next_sibling()
            if nxt:
                value = clean(nxt.get_text(" ", strip=True))
                if value and norm(value).rstrip(":") not in wanted:
                    return value
            parent = node.parent
            if parent:
                value = clean(parent.get_text(" ", strip=True))
                value = re.sub(r"^\s*[^:]{1,80}:\s*", "", value)
                if value and norm(value).rstrip(":") not in wanted:
                    return value
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
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        return _context


async def open_page(page, url, wait=150):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=2500)
    except Exception:
        pass
    await page.wait_for_timeout(wait)


async def first_image_url(scope):
    if not scope:
        return None
    for selector in ("img[src]", "img[data-src]", "img[data-original]", "img[data-lazy-src]", "img[srcset]"):
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
    await open_page(page, url, 150)

    found, seen = [], set()
    cards = page.locator(".book-card--l")
    count = await cards.count()
    for i in range(count):
        card = cards.nth(i)
        try:
            title_loc = card.locator(".book-card__title").first
            title = clean(await title_loc.text_content()) if await title_loc.count() else None
            href = await title_loc.get_attribute("href") if await title_loc.count() else None
            audio_link = card.locator("a[href*='/audiobook/']").first
            if result_type == "audiobook" and await audio_link.count():
                href = await audio_link.get_attribute("href") or href
            if not href:
                continue
            href = canonical(urljoin(BASE, href))
            if not is_product_url(href):
                continue
            actual_type = "audiobook" if "/audiobook/" in urlparse(href).path else result_type
            key = (href.rstrip("/"), actual_type)
            if key in seen:
                continue
            seen.add(key)
            authors = [clean(x) for x in await card.locator(".book-card__author a").all_text_contents() if clean(x)]
            found.append({
                "url": href,
                "title": title or url_title(href),
                "authors": list(dict.fromkeys(authors)),
                "type": actual_type,
                "search_cover": await first_image_url(card),
            })
        except Exception:
            continue

    # Search result pages can contain audiobook links nested in otherwise book-like cards.
    selector = "a[href*='/audiobook/']" if result_type == "audiobook" else "a[href*='/ksiazka/']"
    links = page.locator(selector)
    for i in range(min(await links.count(), 50)):
        try:
            link = links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue
            href = canonical(urljoin(BASE, href))
            if not is_product_url(href):
                continue
            actual_type = "audiobook" if "/audiobook/" in urlparse(href).path else result_type
            key = (href.rstrip("/"), actual_type)
            if key in seen:
                continue
            seen.add(key)
            parent = link.locator("xpath=ancestor::*[contains(@class,'book-card')][1]").first
            authors = []
            if await parent.count():
                authors = [clean(x) for x in await parent.locator(".book-card__author a").all_text_contents() if clean(x)]
            found.append({
                "url": href,
                "title": clean(await link.text_content()) or url_title(href),
                "authors": list(dict.fromkeys(authors)),
                "type": actual_type,
                "search_cover": await first_image_url(parent) if await parent.count() else None,
            })
        except Exception:
            continue

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


def static_field(soup, selector, attr=None):
    node = soup.select_one(selector)
    if not node:
        return None
    return clean(node.get(attr) if attr else node.get_text(" ", strip=True))


def static_meta(soup, selector):
    node = soup.select_one(selector)
    return clean(node.get("content")) if node else None


def parse_static_metadata(soup, candidate):
    media_type = "audiobook" if "/audiobook/" in candidate["url"] else candidate.get("type", "book")
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
        "url": candidate["url"],
        "type": media_type,
    }

    data["title"] = static_field(soup, "h1") or data["title"]
    data["description"] = (
        static_field(soup, "#book-description")
        or static_field(soup, "[id*='description']")
        or static_field(soup, "[class*='description']")
        or static_meta(soup, "meta[property='og:description']")
    )
    data["cover"] = (
        static_field(soup, "a#js-lightboxCover", "href")
        or static_field(soup, ".book-cover__link", "href")
        or static_meta(soup, "meta[property='og:image']")
        or data["cover"]
    )
    data["cover"] = urljoin(BASE, data["cover"]) if data["cover"] else None
    data["publisher"] = (
        static_field(soup, "[data-ga-book-publishers]", "data-ga-book-publishers")
        or text_after_heading(soup, ["Wydawnictwo", "Wydawca"])
    )
    data["isbn"] = static_meta(soup, "meta[property='books:isbn']")
    data["language"] = text_after_heading(soup, ["Język"]) or "pol"
    data["publishedDate"] = text_after_heading(soup, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery"])
    data["publishedYear"] = parse_year(data["publishedDate"])
    data["translator"] = text_after_heading(soup, ["Tłumacz"])
    data["pages"] = text_after_heading(soup, ["Liczba stron", "Strony"])
    data["narrator"] = text_after_heading(soup, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    data["duration"] = parse_duration(text_after_heading(soup, ["Długość", "Czas", "Czas trwania", "Czas trwania audiobooka"]))

    for script in soup.select("script[type='application/ld+json']"):
        for obj in jsonld_objects(script.string or script.get_text()):
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
            data["cover"] = data["cover"] or (urljoin(BASE, str(image)) if image else None)
            if media_type == "audiobook":
                data["narrator"] = data["narrator"] or first_name(jsonld_value(obj, "readBy", "reader", "narrator"))
                data["duration"] = data["duration"] or parse_duration(jsonld_value(obj, "duration", "timeRequired"))
            break

    body_text = soup.get_text("\n", strip=True)
    lines = lines_from_body(body_text)
    data["publisher"] = data["publisher"] or value_after_label(lines, ["Wydawca", "Wydawnictwo"])
    data["publishedDate"] = data["publishedDate"] or value_after_label(lines, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery"])
    data["publishedYear"] = data["publishedYear"] or parse_year(data["publishedDate"])
    data["isbn"] = data["isbn"] or value_after_label(lines, ["ISBN"])
    data["narrator"] = data["narrator"] or value_after_label(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    data["duration"] = data["duration"] or parse_duration(value_after_label(lines, ["Długość", "Czas", "Czas czytania", "Czas trwania", "Czas trwania audiobooka"]))
    data["translator"] = data["translator"] or value_after_label(lines, ["Tłumacz"])
    data["pages"] = data["pages"] or value_after_label(lines, ["Liczba stron", "Strony"])
    series_value = value_after_label(lines, ["Cykl", "Seria"])
    if series_value:
        m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)\)", series_value, re.I)
        data["series"] = clean(m.group(1) if m else series_value)
        data["sequence"] = m.group(2) if m else None
    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body_text)
        data["isbn"] = m.group(1) if m else None
    if not data["author"]:
        data["author"] = ", ".join(candidate.get("authors") or []) or None
    return data


async def parse_detail(page, candidate):
    # One browser request only. urllib was returning HTTP 404 for current LC
    # audiobook URLs, while the normal browser request succeeds.
    url = candidate["url"]
    await open_page(page, url, 120)
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    data = parse_static_metadata(soup, candidate)
    data["url"] = url
    # Search-card cover is a reliable fallback when the detail DOM changes.
    if not data.get("cover"):
        data["cover"] = candidate.get("search_cover")
    print(f"[Lubimyczytać] detail: type={data['type']} cover={'yes' if data.get('cover') else 'no'} description={len(data.get('description') or '')}chars publisher={data.get('publisher') or '-'} narrator={data.get('narrator') or '-'} duration={data.get('duration') or '-'} year={data.get('publishedYear') or '-'} url={url}")
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
        books, audiobooks = await asyncio.gather(
            search_page(search, query, author, "ksiazki", "book"),
            search_page(search, query, author, "audiobooki", "audiobook"),
        )
    finally:
        await search.close()

    candidates = books + audiobooks
    unique = {}
    for item in candidates:
        key_url = (item["url"].rstrip("/"), item["type"])
        if key_url not in unique:
            unique[key_url] = item
    candidates = list(unique.values())

    for item in candidates:
        title_s = similarity(item.get("title"), query)
        author_s = max((similarity(x, author) for x in item.get("authors") or []), default=0.5) if author else 1.0
        item["pre_score"] = title_s * 0.60 + author_s * 0.40 if author else title_s

    candidates.sort(key=lambda x: (x["pre_score"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    candidates = candidates[:MAX_DETAIL_CANDIDATES]
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    sem = asyncio.Semaphore(4)
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
    seen = set()
    for result in parsed:
        if not result:
            continue
        value, data = result
        if value <= 0:
            continue
        dedupe = (data.get("url", "").rstrip("/"), data.get("type"))
        if dedupe in seen:
            continue
        seen.add(dedupe)
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
