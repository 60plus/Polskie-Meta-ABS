import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI(title="Audioteka Polska Metadata Provider")

BASE = "https://audioteka.com"
PL = f"{BASE}/pl"
SEARCH = f"{PL}/szukaj/"
CACHE_TTL = 600
MAX_RESULTS = 10

_pw = None
_browser = None
_context = None
_browser_lock = asyncio.Lock()
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


def strip_audioserial(value):
    return clean(re.sub(r"\s*[.\-:]?\s*audioserial\b", "", value or "", flags=re.I))


def slugify(value):
    return norm(value).replace(" ", "-")


def canonical(url):
    parsed = urlparse(url)
    return f"{BASE}{parsed.path.rstrip('/')}/"


def is_audiobook(url):
    return urlparse(url).path.rstrip("/").startswith("/pl/audiobook/")


def is_series(url):
    return urlparse(url).path.rstrip("/").startswith("/pl/cykl/")


def parse_duration(value):
    text = str(value or "")
    h = re.search(r"(\d+)\s*(?:godz\.?|godziny|h)", text, re.I)
    m = re.search(r"(\d+)\s*(?:min\.?|m)\b", text, re.I)
    if not h and not m:
        return None
    return int(h.group(1)) * 60 + int(m.group(1) if m else 0) if h else int(m.group(1))


def parse_year(value):
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else None


def jsonld_objects(raw):
    try:
        value = json.loads(raw)
    except Exception:
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        result.append(item)
        graph = item.get("@graph")
        if isinstance(graph, list):
            result.extend(x for x in graph if isinstance(x, dict))
    return result


def first_person_name(value):
    if isinstance(value, dict):
        return clean(value.get("name"))
    if isinstance(value, list):
        names = [first_person_name(x) for x in value]
        names = [x for x in names if x]
        return ", ".join(dict.fromkeys(names)) or None
    return clean(value)


async def get_context():
    global _pw, _browser, _context
    async with _browser_lock:
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


async def open_page(page, url, wait=1000):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(wait)

    for text in ("Akceptuję", "Zgadzam się", "Zaakceptuj"):
        try:
            button = page.get_by_role("button", name=re.compile(text, re.I)).first
            if await button.count() and await button.is_visible():
                await button.click(timeout=1000)
                await page.wait_for_timeout(300)
                break
        except Exception:
            pass


async def page_links(page):
    hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    seen = set()
    result = []
    for href in hrefs:
        parsed = urlparse(href)
        if parsed.netloc not in ("audioteka.com", "www.audioteka.com"):
            continue
        if not parsed.path.startswith("/pl/"):
            continue
        if not (is_audiobook(href) or is_series(href)):
            continue
        url = canonical(href)
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


async def search_page(page, query):
    url = f"{SEARCH}?phrase={quote(query)}"
    print(f"[Audioteka] search: {url}")
    await open_page(page, url, 1200)

    # Audioteka search results can be lazy-loaded.
    for _ in range(5):
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(250)

    urls = await page_links(page)
    print(f"[Audioteka] search '{query}' -> {len(urls)} product/series URLs")
    return urls


async def direct_urls(page, query):
    slug = slugify(query)
    candidates = [
        f"{PL}/cykl/{slug}-audioserial/",
        f"{PL}/cykl/{slug}/",
        f"{PL}/audiobook/{slug}/",
        f"{PL}/audiobook/{slug}-audioserial/",
    ]
    found = []
    for candidate in candidates:
        try:
            await open_page(page, candidate, 500)
            current = canonical(page.url)
            title = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
            body = clean(await page.locator("body").inner_text()) or ""
            page_title = (await page.title()).lower()
            if page.url.startswith(BASE) and title and "404" not in page_title and "nie znaleziono" not in body.lower():
                if is_audiobook(current) or is_series(current):
                    found.append(current)
                    print(f"[Audioteka] direct candidate: {current}")
        except Exception as exc:
            print(f"[Audioteka] direct miss {candidate}: {type(exc).__name__}")
    return list(dict.fromkeys(found))


def extract_label(text, labels):
    label = "(?:" + "|".join(labels) + ")"
    match = re.search(
        rf"{label}\s*[:\-]?\s*(.+?)(?=\s+(?:Głosy|Lektor|Czyta|Autor|Autorzy|Wydawca|Długość|Język|Opis|Kategoria|Dostępne|Format)\b|$)",
        text,
        re.I,
    )
    return clean(match.group(1)) if match else None


def extract_title_from_url(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-audioserial$", "", slug, flags=re.I)
    return slug.replace("-", " ").strip().title() if slug else None


async def parse_product(page, url, query, author=""):
    await open_page(page, url, 700)
    body = clean(await page.locator("body").inner_text()) or ""
    text = re.sub(r"\s+", " ", body)

    data = {
        "title": None,
        "author": None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "description": None,
        "cover": None,
        "isbn": None,
        "duration": None,
        "genres": [],
        "series": None,
        "url": canonical(url),
        "is_series": is_series(url),
    }

    # JSON-LD is more reliable than the visible H1 on Audioteka series pages.
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        for item in jsonld_objects(raw):
            item_type = str(item.get("@type", "")).lower()
            name = clean(item.get("name"))
            if name and (not data["title"] or item_type in ("book", "audiobook", "product")):
                data["title"] = name
            data["description"] = data["description"] or clean(item.get("description"))
            data["isbn"] = data["isbn"] or clean(item.get("isbn"))
            data["publishedYear"] = data["publishedYear"] or parse_year(item.get("datePublished"))
            data["duration"] = data["duration"] or parse_duration(item.get("duration"))
            image = item.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            data["cover"] = data["cover"] or clean(image)
            publisher = item.get("publisher")
            if isinstance(publisher, dict):
                publisher = publisher.get("name")
            data["publisher"] = data["publisher"] or clean(publisher)
            data["author"] = data["author"] or first_person_name(item.get("author"))
            data["narrator"] = data["narrator"] or first_person_name(item.get("readBy"))
            data["narrator"] = data["narrator"] or first_person_name(item.get("actor"))
            genre = item.get("genre")
            if isinstance(genre, list):
                data["genres"].extend(clean(x) for x in genre)
            elif genre:
                data["genres"].append(clean(genre))

    # og:* is a useful fallback for covers/descriptions.
    for selector, key in (("meta[property='og:title']", "title"), ("meta[property='og:description']", "description"), ("meta[property='og:image']", "cover")):
        try:
            value = clean(await page.locator(selector).get_attribute("content"))
            if value and not data[key]:
                data[key] = value
        except Exception:
            pass

    # Visible page fallbacks.
    if not data["title"] or (data["is_series"] and data["title"] in {"Cały sezon już dostępny!", "Cały sezon już dostępny"}):
        data["title"] = query
    if not data["author"]:
        data["author"] = extract_label(text, ["Autor", "Autorzy", "Scenariusz"])
    if not data["publisher"]:
        data["publisher"] = extract_label(text, ["Wydawca"])
    if not data["narrator"]:
        data["narrator"] = extract_label(text, ["Głosy", "Lektor", "Czyta"])
    data["publishedYear"] = data["publishedYear"] or parse_year(text)
    data["duration"] = data["duration"] or parse_duration(text)

    if author and not data["author"]:
        data["author"] = author

    # For a series, the search query is the canonical user-facing title.
    if data["is_series"]:
        data["series"] = query
        data["title"] = query

    if not data["cover"]:
        data["cover"] = None

    return data


def score(data, query, author):
    title_score = max(
        similarity(data.get("title"), query),
        similarity(strip_audioserial(data.get("title")), query),
    )
    author_score = similarity(data.get("author"), author) if author else 1.0
    if author and not data.get("author"):
        author_score = 0.55

    result = title_score * 0.78 + author_score * 0.22 if author else title_score
    if data.get("is_series") and title_score >= 0.90:
        result += 0.10
    return min(1.0, result)


async def audioteka_search(query, author=""):
    key = f"audioteka|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    context = await get_context()
    page = await context.new_page()
    try:
        urls = []
        urls.extend(await direct_urls(page, query))
        urls.extend(await search_page(page, query))
        if author:
            urls.extend(await search_page(page, f"{query} {author}"))
        urls = list(dict.fromkeys(urls))
    finally:
        await page.close()

    # Exact direct candidates and audioserial collections go first.
    qslug = slugify(query)
    def url_priority(url):
        path = urlparse(url).path.lower()
        exact = qslug in path
        return (
            0 if exact else 1,
            0 if "audioserial" in path else 1,
            0 if is_series(url) else 1,
        )

    urls = sorted(urls, key=url_priority)[:10]
    print(f"[Audioteka] candidates to parse: {len(urls)}")

    parsed = []
    for url in urls:
        page = await context.new_page()
        try:
            data = await asyncio.wait_for(parse_product(page, url, query, author), timeout=20)
            if data.get("title"):
                data["similarity"] = round(score(data, query, author), 4)
                parsed.append(data)
                print(
                    f"[Audioteka] parsed: {data['title']} / {data.get('author')} "
                    f"series={data.get('is_series')} score={data['similarity']:.3f}"
                )
        except Exception as exc:
            print(f"[Audioteka] parse failed {url}: {type(exc).__name__}: {exc}")
        finally:
            await page.close()

    # Author is a hard filter when Audiobookshelf supplies it and the page has an author.
    filtered = []
    for data in parsed:
        if data["similarity"] < 0.45:
            continue
        if author and data.get("author") and similarity(data["author"], author) < 0.40:
            continue
        filtered.append(data)

    # Prefer an exact series/product over generic homonyms.
    filtered.sort(key=lambda x: x["similarity"], reverse=True)

    matches = []
    for data in filtered[:MAX_RESULTS]:
        matches.append({
            "title": data.get("title"),
            "author": data.get("author"),
            "narrator": data.get("narrator"),
            "publisher": data.get("publisher") or "Audioteka",
            "publishedYear": data.get("publishedYear"),
            "description": data.get("description"),
            "cover": data.get("cover"),
            "isbn": data.get("isbn"),
            "genres": list(dict.fromkeys(x for x in data.get("genres", []) if x)) or None,
            "series": [{"series": data["series"], "sequence": None}] if data.get("series") else None,
            "language": "pol",
            "duration": data.get("duration"),
            "type": "audiobook",
            "similarity": data["similarity"],
        })

    result = {"matches": matches}
    print(
        "[Audioteka] final:",
        " | ".join(f"{x['title']}/{x.get('author')} ({x['similarity']:.3f})" for x in matches) or "<none>",
    )
    _cache[key] = (time.time(), result)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "audioteka-pl"}


@app.get("/search")
async def search(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(await audioteka_search(query, author))


@app.on_event("shutdown")
async def shutdown():
    global _context, _browser, _pw
    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None
    if _pw:
        await _pw.stop()
        _pw = None
