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


def parse_year(value):
    m = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return m.group(0) if m else None


def parse_duration(value):
    text = str(value or "")
    iso = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text, re.I)
    if iso:
        return int(iso.group(1) or 0) * 60 + int(iso.group(2) or 0) + round(int(iso.group(3) or 0) / 60)
    h = re.search(r"(\d+)\s*(?:godz\.?|godziny|godzin|h)\b", text, re.I)
    m = re.search(r"(\d+)\s*(?:min\.?|minut|m)\b", text, re.I)
    if h:
        return int(h.group(1)) * 60 + int(m.group(1) if m else 0)
    return int(m.group(1)) if m else None


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


def path_type(url):
    return "audiobook" if urlparse(url).path.rstrip("/").startswith("/audiobook/") else "book"


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug).strip()


def lines_from_body(body):
    return [clean(x) for x in str(body or "").splitlines() if clean(x)]


def value_after_label(lines, labels):
    wanted = {norm(x) for x in labels}
    for i, line in enumerate(lines):
        if norm(line) not in wanted:
            continue
        for candidate in lines[i + 1:i + 5]:
            if candidate and norm(candidate) not in wanted:
                return candidate
    return None


def series_from_lines(lines):
    for i, line in enumerate(lines):
        if norm(line) in {"cykl", "seria"} and i + 1 < len(lines):
            value = lines[i + 1]
            m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)\)", value, re.I)
            if m:
                return clean(m.group(1)), m.group(2)
            return clean(value), None
        m = re.match(r"(?:Cykl|Seria):?\s*(.+?)(?:\s*\(tom\s+([0-9IVX]+)\))?$", line, re.I)
        if m:
            return clean(m.group(1)), m.group(2)
    return None, None


def series_match_name(series):
    if not series:
        return ""
    return norm(re.sub(r"\(tom\s+[^)]+\)", "", str(series), flags=re.I))


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


async def open_page(page, url, wait=250):
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(wait)


async def search_page(page, query, section):
    search_url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    print(f"[Lubimyczytać] search: {search_url}")
    await open_page(page, search_url, 300)
    hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    found, seen = [], set()
    for href in hrefs:
        parsed = urlparse(href)
        if parsed.netloc not in {"lubimyczytac.pl", "www.lubimyczytac.pl"} or not is_book_url(href):
            continue
        url = canonical(href)
        if url not in seen:
            seen.add(url)
            found.append(url)
    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} URL")
    return found


async def parse_detail(page, url, query, author=""):
    await open_page(page, url, 200)
    body = await page.locator("body").inner_text()
    lines = lines_from_body(body)
    item_type = path_type(url)
    data = {
        "title": None, "author": None, "narrator": None, "publisher": None,
        "publishedYear": None, "description": None, "cover": None, "isbn": None,
        "duration": None, "genres": [], "series": None, "sequence": None,
        "url": canonical(url), "type": item_type, "rating": None,
    }

    objects = []
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        objects.extend(jsonld_objects(raw))
    target = None
    best = -1.0
    for item in objects:
        name = clean(item.get("name"))
        if not name:
            continue
        score = similarity(name, query)
        if score > best:
            target, best = item, score

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

    try:
        description = page.locator("#book-description").first
        if await description.count():
            await description.wait_for(state="attached", timeout=1500)
            value = clean(await description.text_content())
            if value:
                data["description"] = value
                print(f"[Lubimyczytać] description: chars={len(value)} type={data['type']} url={data['url']}")
    except Exception as exc:
        print(f"[Lubimyczytać] description failed: {type(exc).__name__}")

    # Dedicated cover sources, including LC's audiobook cover.
    for selector in (
        "a#js-lightboxCover[href]",
        ".book-cover__link[href]",
        "meta[property='og:image']",
        "meta[name='twitter:image']",
        "img.book-cover[src]",
    ):
        try:
            loc = page.locator(selector).first
            if not await loc.count():
                continue
            value = clean(await loc.get_attribute("href") or await loc.get_attribute("content") or await loc.get_attribute("src"))
            if value:
                data["cover"] = value
                break
        except Exception:
            pass

    if not data["author"]:
        try:
            names = [clean(x) for x in await page.locator("a[href*='/autor/']").all_text_contents() if clean(x)]
            if names:
                data["author"] = ", ".join(dict.fromkeys(names[:5]))
        except Exception:
            pass

    data["publisher"] = data["publisher"] or value_after_label(lines, ["Wydawca", "Wydawnictwo"])
    data["publishedYear"] = data["publishedYear"] or parse_year(value_after_label(lines, [
        "Data pierwszego wydania", "Data wydania", "Data 1. wyd. pol.", "Data publikacji", "Data premiery", "Rok wydania"
    ]))
    data["isbn"] = data["isbn"] or value_after_label(lines, ["ISBN"])
    data["duration"] = data["duration"] or parse_duration(value_after_label(lines, ["Czas czytania", "Długość", "Czas trwania"]))

    narrator = value_after_label(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    if narrator:
        data["narrator"] = narrator

    language = value_after_label(lines, ["Język"])
    data["language"] = "pol" if not language or norm(language) in {"polski", "polska", "pol"} else norm(language)

    category = value_after_label(lines, ["Kategoria", "Kategorie"])
    if category:
        data["genres"] = [clean(x) for x in re.split(r"[,;/]", category) if clean(x)]

    series, sequence = series_from_lines(lines)
    data["series"], data["sequence"] = series, sequence

    try:
        rating = await page.locator("meta[property='books:rating:value']").get_attribute("content")
        if rating:
            data["rating"] = float(rating) / 2
    except Exception:
        pass

    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        data["isbn"] = m.group(1) if m else None

    if author and not data["author"]:
        data["author"] = author
    return data


def score(data, query, author):
    title_score = similarity(data.get("title"), query)
    author_score = similarity(data.get("author"), author) if author else 1.0
    if author and not data.get("author"):
        author_score = 0.0
    value = title_score * 0.60 + author_score * 0.40 if author else title_score

    # Author is a strong filter for LC. When supplied, unrelated authors should
    # not survive merely because the title is popular/common.
    if author and author_score < 0.40:
        return 0.0, title_score, author_score

    # Slight penalty for missing ISBN, matching the reference provider.
    if not data.get("isbn"):
        value *= 0.99
    return min(1.0, value), title_score, author_score


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
        "type": data.get("type", "book"),
        "similarity": round(score_value, 3),
    }


async def lubimyczytac_search(query, author=""):
    key = f"lubimyczytac|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    context = await get_context()
    search = await context.new_page()
    try:
        # IMPORTANT: perform exactly the same two searches as the reference
        # provider: books + audiobooks. Do not collapse them into global search.
        book_urls = await search_page(search, query, "ksiazki")
        audiobook_urls = await search_page(search, query, "audiobooki")
        urls = list(dict.fromkeys(book_urls + audiobook_urls))

        if author:
            book_author_urls = await search_page(search, f"{query} {author}", "ksiazki")
            audiobook_author_urls = await search_page(search, f"{query} {author}", "audiobooki")
            urls = list(dict.fromkeys(urls + book_author_urls + audiobook_author_urls))
    finally:
        await search.close()

    # Parse a broader candidate pool so a rare audiobook is not lost among
    # similarly named books. Then rank locally and return max 20.
    urls = urls[:40]
    books = []
    for i in range(0, len(urls), 6):
        batch_urls = urls[i:i + 6]
        pages = [await context.new_page() for _ in batch_urls]
        try:
            books.extend(await asyncio.gather(*(
                parse_detail(page, url, query, author)
                for page, url in zip(pages, batch_urls)
            )))
        finally:
            await asyncio.gather(*(page.close() for page in pages), return_exceptions=True)

    ranked = []
    for book in books:
        s, ts, aa = score(book, query, author)
        if not s:
            continue
        ranked.append((s, ts, aa, book))

    def sort_key(item):
        s, ts, aa, book = item
        exact = 1 if norm(book.get("title")) == norm(query) else 0
        audio = 1 if book.get("type") == "audiobook" else 0
        return (s, exact, audio, ts, aa)

    ranked.sort(key=sort_key, reverse=True)
    final = [to_match(book, s) for s, ts, aa, book in ranked[:MAX_RESULTS]]

    result = {"matches": final}
    print("[Lubimyczytać] candidates=", len(ranked))
    print("[Lubimyczytać] final:", " | ".join(
        f"{x['title']}/{x['author']} [{x['type']}] ({x['similarity']:.3f})"
        for x in final
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
