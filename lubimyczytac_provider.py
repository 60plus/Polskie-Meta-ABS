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

app = FastAPI(title="Lubimyczytać Metadata Provider")

BASE = "https://lubimyczytac.pl"
CACHE_TTL = 600
MAX_RESULTS = 10
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


def parse_year(value):
    m = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return m.group(0) if m else None


def parse_duration(value):
    text = str(value or "")
    h = re.search(r"(\d+)\s*(?:godz\.?|godziny|godzin|h)\b", text, re.I)
    m = re.search(r"(\d+)\s*(?:min\.?|minut|m)\b", text, re.I)
    if not h and not m:
        iso = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text, re.I)
        if iso:
            return int(iso.group(1) or 0) * 60 + int(iso.group(2) or 0) + round(int(iso.group(3) or 0) / 60)
        return None
    return int(h.group(1)) * 60 + int(m.group(1) if m else 0) if h else int(m.group(1))


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


def first_name(value):
    if isinstance(value, dict):
        return clean(value.get("name"))
    if isinstance(value, list):
        names = [first_name(x) for x in value]
        return ", ".join(dict.fromkeys(x for x in names if x)) or None
    return clean(value)


def canonical(url):
    parsed = urlparse(url)
    return f"{BASE}{parsed.path.rstrip('/')}/"


def is_book_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.startswith("/ksiazka/") or path.startswith("/audiobook/")


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug).strip()


def lines_from_body(body):
    return [clean(x) for x in str(body or "").splitlines() if clean(x)]


def value_after_label(lines, labels):
    wanted = {norm(x) for x in labels}
    for i, line in enumerate(lines):
        if norm(line) in wanted:
            for candidate in lines[i + 1:i + 4]:
                if candidate and norm(candidate) not in wanted:
                    return candidate
    return None


def series_from_lines(lines):
    for i, line in enumerate(lines):
        if norm(line) in {"cykl", "seria"}:
            if i + 1 < len(lines):
                value = lines[i + 1]
                m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)\)", value, re.I)
                if m:
                    return clean(m.group(1)), m.group(2)
                return clean(value), None
        m = re.match(r"(?:Cykl|Seria):?\s*(.+?)(?:\s*\(tom\s+([0-9IVX]+)\))?$", line, re.I)
        if m:
            return clean(m.group(1)), m.group(2)
    return None, None


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
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"},
        )
        return _context


async def open_page(page, url, wait=500):
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(wait)


async def search_page(page, query):
    # LC currently exposes the global search as /szukaj?phrase=... . Keep the
    # trailing-slash variant as a fallback because the site has used both.
    urls = [
        f"{BASE}/szukaj?phrase={quote(query)}",
        f"{BASE}/szukaj/?phrase={quote(query)}",
    ]
    for search_url in urls:
        try:
            print(f"[Lubimyczytać] search: {search_url}")
            await open_page(page, search_url, 650)
            hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
            found, seen = [], set()
            for href in hrefs:
                parsed = urlparse(href)
                if parsed.netloc not in {"lubimyczytac.pl", "www.lubimyczytac.pl"}:
                    continue
                if not is_book_url(href):
                    continue
                u = canonical(href)
                if u not in seen:
                    seen.add(u)
                    found.append(u)
            print(f"[Lubimyczytać] '{query}' -> {len(found)} książka/audiobook URL")
            if found:
                return found
        except Exception as exc:
            print(f"[Lubimyczytać] search miss: {type(exc).__name__}: {exc}")
    return []


async def parse_detail(page, url, query, author=""):
    await open_page(page, url, 350)
    body = await page.locator("body").inner_text()
    lines = lines_from_body(body)
    data = {
        "title": None, "author": None, "narrator": None, "publisher": None,
        "publishedYear": None, "description": None, "cover": None,
        "isbn": None, "duration": None, "genres": [], "series": None,
        "sequence": None, "url": canonical(url), "format": None,
    }

    objects = []
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        objects.extend(jsonld_objects(raw))

    target = None
    best = 0.0
    for item in objects:
        name = clean(item.get("name"))
        if not name:
            continue
        score = similarity(name, query)
        if score > best:
            best = score
            target = item

    if target:
        data["title"] = clean(target.get("name"))
        data["author"] = first_name(target.get("author"))
        data["description"] = clean(target.get("description"))
        data["isbn"] = clean(target.get("isbn"))
        data["publishedYear"] = parse_year(target.get("datePublished"))
        data["duration"] = parse_duration(target.get("duration"))
        image = target.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        data["cover"] = clean(image)
        publisher = target.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        data["publisher"] = clean(publisher)
        genre = target.get("genre")
        if isinstance(genre, list):
            data["genres"] = [clean(x) for x in genre if clean(x)]
        elif genre:
            data["genres"] = [clean(genre)]

    h1 = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
    data["title"] = h1 or data["title"] or url_title(url)

    # The description is deliberately taken from the dedicated LC block.
    # It is the same element for ordinary books and audiobook pages.
    try:
        description = page.locator("#book-description").first
        if await description.count():
            value = clean(await description.text_content())
            if value:
                data["description"] = value
                print(f"[Lubimyczytać] description: chars={len(value)}")
    except Exception as exc:
        print(f"[Lubimyczytać] description failed: {type(exc).__name__}")

    for selector, key in (
        ("meta[property='og:image']", "cover"),
        ("meta[property='og:description']", "description"),
    ):
        try:
            value = clean(await page.locator(selector).get_attribute("content"))
            if value and not data[key]:
                data[key] = value
        except Exception:
            pass

    # Authors: prefer JSON-LD, then explicit author links.
    if not data["author"]:
        try:
            names = await page.locator("a[href*='/autor/']").all_text_contents()
            names = [clean(x) for x in names if clean(x)]
            if names:
                data["author"] = ", ".join(dict.fromkeys(names[:5]))
        except Exception:
            pass

    data["publisher"] = data["publisher"] or value_after_label(lines, ["Wydawca", "Wydawnictwo"])
    data["format"] = value_after_label(lines, ["Format"])
    data["publishedYear"] = data["publishedYear"] or parse_year(value_after_label(lines, [
        "Data wydania", "Data 1. wyd. pol.", "Data publikacji", "Data premiery", "Rok wydania"
    ]))
    data["isbn"] = data["isbn"] or value_after_label(lines, ["ISBN"])
    data["duration"] = data["duration"] or parse_duration(value_after_label(lines, ["Czas czytania", "Długość", "Czas trwania"]))

    language = value_after_label(lines, ["Język"])
    if language and norm(language) in {"polski", "polska", "pol"}:
        data["language"] = "pol"
    else:
        data["language"] = "pol"

    category = value_after_label(lines, ["Kategoria", "Kategorie"])
    if category:
        data["genres"] = [clean(x) for x in re.split(r"[,;/]", category) if clean(x)]

    series, sequence = series_from_lines(lines)
    data["series"], data["sequence"] = series, sequence

    # Audiobook pages can expose the narrator/performers in labels such as
    # Lektor, Czyta or Lektorzy. Keep this separate from the author.
    narrator = value_after_label(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    if narrator:
        data["narrator"] = narrator

    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        data["isbn"] = m.group(1) if m else None

    if author and not data["author"]:
        data["author"] = author

    return data


def score(data, query, author):
    ts = similarity(data.get("title"), query)
    aa = similarity(data.get("author"), author) if author else 1.0
    if author and not data.get("author"):
        aa = 0.55
    return ts * 0.78 + aa * 0.22 if author else ts


def to_match(data, score_value):
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
        "type": "audiobook" if "/audiobook/" in data.get("url", "") else "book",
        "similarity": score_value,
    }


async def lubimyczytac_search(query, author=""):
    key = f"lubimyczytac|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    context = await get_context()
    search = await context.new_page()
    try:
        urls = await search_page(search, query)
        if not urls and author:
            urls = await search_page(search, f"{query} {author}")
    finally:
        await search.close()

    urls = list(dict.fromkeys(urls))[:30]
    books = []
    for i in range(0, len(urls), 4):
        pages = [await context.new_page() for _ in urls[i:i + 4]]
        try:
            books.extend(await asyncio.gather(*(
                parse_detail(p, u, query, author)
                for p, u in zip(pages, urls[i:i + 4])
            )))
        finally:
            await asyncio.gather(*(p.close() for p in pages), return_exceptions=True)

    ranked = []
    for book in books:
        ts = similarity(book.get("title"), query)
        aa = similarity(book.get("author"), author) if author else 1.0
        s = score(book, query, author)
        ranked.append((s, ts, aa, book))
    ranked.sort(key=lambda x: x[0], reverse=True)

    final = [
        to_match(book, round(min(1.0, s), 3))
        for s, ts, aa, book in ranked
        if ts >= 0.55 and (not author or aa >= 0.45)
    ][:MAX_RESULTS]

    result = {"matches": final}
    print("[Lubimyczytać] final:", " | ".join(
        f"{x['title']}/{x['author']} ({x['similarity']:.3f})" for x in final
    ))
    _cache[key] = (time.time(), result)
    return result


async def authenticate(authorization):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/search")
async def search_endpoint(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
    await authenticate(authorization)
    return JSONResponse(await lubimyczytac_search(query, author))


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "lubimyczytac"}


@app.get("/providers")
async def providers(authorization: str | None = Header(default=None)):
    await authenticate(authorization)
    return {"providers": [{"id": "lubimyczytac-pl", "name": "Lubimyczytać Polska", "port": 3002}]}


@app.on_event("shutdown")
async def shutdown():
    global _context, _browser, _pw
    if _context:
        await _context.close()
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
