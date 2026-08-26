import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
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
_http_lock = asyncio.Semaphore(1)


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
            for candidate in lines[i + 1:i + 7]:
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


async def open_page(page, url, wait=350):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(wait)


async def first_image_url(scope):
    for selector in ("img[data-src]", "img[data-original]", "img[data-lazy-src]", "img[src]", "source[srcset]", "img[srcset]"):
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
            pass
    return None


async def search_page(page, query, author, section, result_type):
    url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    if author:
        url += f"&author={quote(author)}"
    print(f"[Lubimyczytać] search: {url}")
    await open_page(page, url)

    found, seen = [], set()
    cards = page.locator(".book-card--l")
    for i in range(await cards.count()):
        card = cards.nth(i)
        try:
            title_loc = card.locator(".book-card__title").first
            title = clean(await title_loc.text_content()) if await title_loc.count() else None
            href = await title_loc.get_attribute("href") if await title_loc.count() else None
            if result_type == "audiobook":
                audio = card.locator("a[href*='/audiobook/']").first
                if await audio.count():
                    href = await audio.get_attribute("href") or href
            if not href:
                continue
            href = canonical(urljoin(BASE, href))
            if not is_product_url(href):
                continue
            actual_type = "audiobook" if "/audiobook/" in urlparse(href).path else result_type
            if href in seen:
                continue
            seen.add(href)
            authors = [clean(x) for x in await card.locator(".book-card__author a").all_text_contents() if clean(x)]
            found.append({
                "url": href,
                "title": title or url_title(href),
                "authors": list(dict.fromkeys(authors)),
                "type": actual_type,
                "search_cover": await first_image_url(card),
            })
        except Exception:
            pass

    links = page.locator("a[href*='/audiobook/']") if result_type == "audiobook" else page.locator("a[href*='/ksiazka/']")
    for i in range(min(await links.count(), 30)):
        try:
            link = links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue
            href = canonical(urljoin(BASE, href))
            if not is_product_url(href) or href in seen:
                continue
            seen.add(href)
            parent = link.locator("xpath=ancestor::*[contains(@class,'book-card')][1]").first
            found.append({
                "url": href,
                "title": clean(await link.text_content()) or url_title(href),
                "authors": [],
                "type": "audiobook" if "/audiobook/" in urlparse(href).path else result_type,
                "search_cover": await first_image_url(parent) if await parent.count() else None,
            })
        except Exception:
            pass

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


def static_fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": BASE + "/",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=25) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


async def static_detail(url, candidate):
    async with _http_lock:
        for attempt in range(3):
            try:
                html = await asyncio.to_thread(static_fetch, url)
                return BeautifulSoup(html, "html.parser")
            except Exception as exc:
                print(f"[Lubimyczytać] static detail attempt {attempt + 1}/3 failed: {type(exc).__name__}: {exc}")
                if attempt < 2:
                    await asyncio.sleep(2 + attempt * 3)
    return None


def soup_meta(soup, selector, attr="content"):
    if soup is None:
        return None
    node = soup.select_one(selector)
    return clean(node.get(attr)) if node and node.get(attr) else None


def soup_text(soup, selector):
    if soup is None:
        return None
    node = soup.select_one(selector)
    return clean(node.get_text(" ", strip=True)) if node else None


def soup_label_value(soup, labels):
    if soup is None:
        return None
    wanted = {norm(x) for x in labels}
    for node in soup.select("dt, span.book__txt, div, p"):
        text = clean(node.get_text(" ", strip=True))
        if not text:
            continue
        if norm(text.rstrip(":")) in wanted:
            nxt = node.find_next_sibling()
            if nxt:
                value = clean(nxt.get_text(" ", strip=True))
                if value:
                    return value
    return None


def apply_static_metadata(data, soup, candidate):
    if soup is None:
        return data

    h1 = soup.select_one("h1")
    if h1:
        data["title"] = clean(h1.get_text(" ", strip=True)) or data["title"]

    data["cover"] = data["cover"] or soup_meta(soup, "meta[property='og:image']")
    data["cover"] = data["cover"] or soup_meta(soup, "meta[name='twitter:image']")
    if not data["cover"]:
        node = soup.select_one("a#js-lightboxCover, .book-cover__link")
        if node and node.get("href"):
            data["cover"] = urljoin(BASE, node["href"])

    data["description"] = data["description"] or soup_text(soup, "#book-description")
    data["description"] = data["description"] or soup_meta(soup, "meta[property='og:description']")

    publisher = soup_text(soup, "span.book__txt a")
    data["publisher"] = data["publisher"] or publisher
    data["publisher"] = data["publisher"] or soup_meta(soup, "meta[name='publisher']")
    data["publisher"] = data["publisher"] or soup_label_value(soup, ["Wydawca", "Wydawnictwo"])

    data["isbn"] = data["isbn"] or soup_meta(soup, "meta[property='books:isbn']")
    data["publishedDate"] = data["publishedDate"] or soup_label_value(soup, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery"])
    data["publishedYear"] = data["publishedYear"] or parse_year(data["publishedDate"])
    data["narrator"] = data["narrator"] or soup_label_value(soup, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator", "Narracja"])
    data["duration"] = data["duration"] or parse_duration(soup_label_value(soup, ["Długość", "Czas czytania", "Czas trwania", "Czas trwania audiobooka"]))
    data["translator"] = data["translator"] or soup_label_value(soup, ["Tłumacz", "Tłumacz:"])
    data["pages"] = data["pages"] or soup_label_value(soup, ["Liczba stron", "Strony"])
    language = soup_label_value(soup, ["Język", "Język:"])
    if language:
        data["language"] = "pol" if norm(language.split(",")[0]) in {"polski", "polska", "pol"} else norm(language.split(",")[0])

    series = soup_label_value(soup, ["Cykl", "Seria"])
    if series:
        m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+).*?\)", series, re.I)
        data["series"] = clean(m.group(1) if m else series)
        data["sequence"] = m.group(2) if m else None

    genre = soup.select_one(".book__category.d-sm-block.d-none")
    if genre:
        data["genres"] = [clean(x) for x in genre.get_text(" ", strip=True).split(",") if clean(x)]
    data["tags"] = [clean(x.get_text(" ", strip=True)) for x in soup.select("a[href*='/ksiazki/t/']") if clean(x.get_text(" ", strip=True))]

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
            if image:
                data["cover"] = data["cover"] or urljoin(BASE, str(image))
            data["duration"] = data["duration"] or parse_duration(jsonld_value(obj, "duration", "timeRequired"))
            data["narrator"] = data["narrator"] or first_name(jsonld_value(obj, "readBy", "reader", "narrator"))
            break

    if not data["isbn"]:
        text = soup.get_text(" ", strip=True)
        m = re.search(r"\b(97[89]\d{10})\b", text)
        data["isbn"] = m.group(1) if m else None
    if not data["author"]:
        data["author"] = ", ".join(candidate.get("authors") or []) or None
    return data


async def parse_detail(page, candidate):
    url = canonical(candidate["url"])
    media_type = "audiobook" if "/audiobook/" in urlparse(url).path else candidate.get("type", "book")
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

    # First try the browser page. This is still best for normal book pages.
    try:
        await open_page(page, url, 500)
        body = await page.locator("body").inner_text()
        lines = lines_from_body(body)
        h1 = page.locator("h1").first
        if await h1.count():
            data["title"] = clean(await h1.text_content()) or data["title"]
        for selector, attr in (("a#js-lightboxCover", "href"), (".book-cover__link", "href"), ("meta[property='og:image']", "content")):
            loc = page.locator(selector).first
            if await loc.count():
                value = clean(await loc.get_attribute(attr))
                if value:
                    data["cover"] = urljoin(BASE, value)
                    break
        data["description"] = clean(await page.locator("#book-description").first.text_content()) if await page.locator("#book-description").count() else None
        if not data["description"]:
            meta = page.locator("meta[property='og:description']").first
            if await meta.count():
                data["description"] = clean(await meta.get_attribute("content"))
        pub = page.locator("span.book__txt").filter(has_text="Wydawnictwo:").locator("a").first
        if await pub.count():
            data["publisher"] = clean(await pub.text_content())
        if not data["publisher"]:
            node = page.locator("[data-ga-book-publishers]").first
            if await node.count():
                data["publisher"] = clean(await node.get_attribute("data-ga-book-publishers"))
        isbn = page.locator("meta[property='books:isbn']").first
        if await isbn.count():
            data["isbn"] = clean(await isbn.get_attribute("content"))
        data["publisher"] = data["publisher"] or value_after_label(lines, ["Wydawca", "Wydawnictwo"])
        data["publishedDate"] = value_after_label(lines, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery"])
        data["publishedYear"] = parse_year(data["publishedDate"])
        data["narrator"] = value_after_label(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator", "Narracja"])
        data["duration"] = parse_duration(value_after_label(lines, ["Długość", "Czas czytania", "Czas trwania", "Czas trwania audiobooka"]))
        data["series"] = value_after_label(lines, ["Cykl", "Seria"])
        if data["series"]:
            m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+).*?\)", data["series"], re.I)
            data["sequence"] = m.group(2) if m else None
            data["series"] = clean(m.group(1) if m else data["series"])
        lang = value_after_label(lines, ["Język", "Język:"])
        if lang:
            data["language"] = "pol" if norm(lang.split(",")[0]) in {"polski", "polska", "pol"} else norm(lang.split(",")[0])
    except Exception as exc:
        print(f"[Lubimyczytać] browser detail failed: {url} {type(exc).__name__}: {exc}")

    # Critical fallback: the original provider uses a plain HTTP HTML fetch.
    # LC currently serves richer audiobook metadata to that request than to
    # some automated browser sessions. This is why books worked while audio
    # returned cover=yes but description/publisher/narrator were empty.
    if media_type == "audiobook" and (not data["description"] or not data["publisher"] or not data["narrator"]):
        soup = await static_detail(url, candidate)
        data = apply_static_metadata(data, soup, candidate)

    if not data.get("author"):
        data["author"] = ", ".join(candidate.get("authors") or []) or None
    if not data.get("isbn"):
        m = re.search(r"\b(97[89]\d{10})\b", str(data.get("description") or ""))
        if m:
            data["isbn"] = m.group(1)

    print(
        f"[Lubimyczytać] detail: type={data['type']} cover={'yes' if data.get('cover') else 'no'} "
        f"description={len(data.get('description') or '')}chars publisher={data.get('publisher') or '-'} "
        f"narrator={data.get('narrator') or '-'} duration={data.get('duration') or '-'} year={data.get('publishedYear') or '-'} url={url}"
    )
    return data


def score(data, query, author, candidate_authors=None):
    title_s = similarity(data.get("title"), query)
    values = [data.get("author")] + list(candidate_authors or [])
    author_s = max((similarity(x, author) for x in values if x), default=0.5) if author else 1.0
    if author and author_s < 0.15:
        return 0.0
    value = title_s * 0.60 + author_s * 0.40 if author else title_s
    if not data.get("isbn"):
        value *= 0.99
    return min(value, 1.0)


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

    by_url = {}
    for item in books + audiobooks:
        existing = by_url.get(item["url"])
        if not existing or (not existing.get("authors") and item.get("authors")) or (not existing.get("search_cover") and item.get("search_cover")):
            by_url[item["url"]] = item
    candidates = list(by_url.values())

    for item in candidates:
        title_s = similarity(item.get("title"), query)
        author_s = max((similarity(x, author) for x in item.get("authors") or []), default=0.5) if author else 1.0
        item["pre_score"] = title_s * 0.60 + author_s * 0.40 if author else title_s
    candidates.sort(key=lambda x: (x["pre_score"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    candidates = candidates[:MAX_DETAIL_CANDIDATES]
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    sem = asyncio.Semaphore(2)
    async def parse_one(candidate):
        async with sem:
            page = await context.new_page()
            try:
                data = await parse_detail(page, candidate)
                return score(data, query, author, candidate.get("authors")), data
            except Exception as exc:
                print(f"[Lubimyczytać] detail failed: {candidate['url']} {type(exc).__name__}: {exc}")
                return None
            finally:
                await page.close()

    parsed = await asyncio.gather(*(parse_one(c) for c in candidates))
    ranked = [(value, data) for result in parsed if result for value, data in [result] if value > 0]
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
