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
PL = BASE + "/pl"
SEARCH = PL + "/szukaj/"
CACHE_TTL = 600
MAX_RESULTS = 10
_cache = {}
_pw = _browser = _context = None
_lock = asyncio.Lock()


def norm(value):
    s = str(value or "").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = re.sub(r"\s+", " ", value).strip()
        return value or None
    return value


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.97
    return SequenceMatcher(None, a, b).ratio()


def duration_minutes(value):
    text = str(value or "")
    m = re.search(r"(\d+)\s*(?:godz\.?|godziny|h)\s*(?:(\d+)\s*(?:min|m))?", text, re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*(?:min|m)\b", text, re.I)
    return int(m.group(1)) if m else None


def year(value):
    m = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return m.group(0) if m else None


def canonical(url):
    p = urlparse(url)
    return f"{BASE}{p.path.rstrip('/')}/"


def is_product(url):
    return urlparse(url).path.rstrip("/").startswith("/pl/audiobook/")


def is_series(url):
    return urlparse(url).path.rstrip("/").startswith("/pl/cykl/")


def slugify(value):
    return norm(value).replace(" ", "-")


def jsonld(raw):
    try:
        data = json.loads(raw)
    except Exception:
        return []
    items = data if isinstance(data, list) else [data]
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(item)
        if isinstance(item.get("@graph"), list):
            out.extend(x for x in item["@graph"] if isinstance(x, dict))
    return out


async def browser_context():
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
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"},
        )
        return _context


async def goto(page, url, wait=900):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except Exception:
        pass
    await page.wait_for_timeout(wait)
    for selector in ("button:has-text('Akceptuję')", "button:has-text('Zgadzam się')", "button:has-text('Zaakceptuj')"):
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=1200)
                await page.wait_for_timeout(300)
                break
        except Exception:
            pass


async def collect_links(page):
    hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    result, seen = [], set()
    for href in hrefs:
        p = urlparse(href)
        if p.netloc not in {"audioteka.com", "www.audioteka.com"} or not p.path.startswith("/pl/"):
            continue
        if not (is_product(href) or is_series(href)):
            continue
        u = canonical(href)
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


async def search_urls(page, query):
    url = f"{SEARCH}?phrase={quote(query)}"
    print(f"[Audioteka] search: {url}")
    await goto(page, url, 1400)
    for _ in range(5):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(300)
    urls = await collect_links(page)
    print(f"[Audioteka] search '{query}' -> {len(urls)} product/series URLs")
    return urls


async def direct_candidates(page, query):
    slug = slugify(query)
    candidates = [
        f"{PL}/audiobook/{slug}/",
        f"{PL}/cykl/{slug}/",
        f"{PL}/cykl/{slug}-audioserial/",
        f"{PL}/audiobook/{slug}-audioserial/",
    ]
    found = []
    for url in candidates:
        try:
            await goto(page, url, 500)
            final = canonical(page.url)
            title = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
            if page.url.startswith(BASE) and title and "404" not in (await page.title()).lower() and "nie znaleziono" not in title.lower():
                found.append(final)
                print(f"[Audioteka] direct candidate: {final}")
        except Exception as exc:
            print(f"[Audioteka] direct miss {url}: {type(exc).__name__}")
    return list(dict.fromkeys(found))


def extract_label(text, labels):
    label = "(?:" + "|".join(labels) + ")"
    m = re.search(rf"{label}\s*[:\-]?\s*(.+?)(?=\s+(?:Głosy|Lektor|Czyta|Autor|Wydawca|Długość|Typ|Format|Język|Opis|Kategoria|Kolekcje)\b|$)", text, re.I)
    return clean(m.group(1)) if m else None


def unique_names(value):
    if not value:
        return []
    parts = re.split(r"\s*,\s*|\s+oraz\s+", value)
    return list(dict.fromkeys(x for x in (clean(p) for p in parts) if x))


async def parse_page(page, url, series_hint=False, collect_page_links=False):
    await goto(page, url, 700)
    body = clean(await page.locator("body").inner_text()) or ""
    text = re.sub(r"\s+", " ", body)
    title = author = publisher = description = cover = isbn = published = duration = None
    narrators, genres = [], []

    try:
        title = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
    except Exception:
        pass
    try:
        title = title or clean(await page.locator("meta[property='og:title']").get_attribute("content"))
        description = clean(await page.locator("meta[property='og:description']").get_attribute("content"))
        cover = clean(await page.locator("meta[property='og:image']").get_attribute("content"))
    except Exception:
        pass

    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        for item in jsonld(raw):
            title = title or clean(item.get("name"))
            description = description or clean(item.get("description"))
            isbn = isbn or clean(item.get("isbn"))
            image = item.get("image")
            cover = cover or (image[0] if isinstance(image, list) and image else image)
            published = published or year(item.get("datePublished"))
            duration = duration or duration_minutes(item.get("duration"))
            pub = item.get("publisher")
            publisher = publisher or clean(pub.get("name") if isinstance(pub, dict) else pub)
            au = item.get("author")
            if isinstance(au, list):
                names = [clean(x.get("name") if isinstance(x, dict) else x) for x in au]
                author = author or ", ".join(x for x in names if x)
            elif isinstance(au, dict):
                author = author or clean(au.get("name"))
            elif isinstance(au, str):
                author = author or clean(au)
            genre = item.get("genre")
            if isinstance(genre, list):
                genres += [clean(x) for x in genre]
            elif genre:
                genres.append(clean(genre))

    author = author or extract_label(text, ["Autor", "Autorzy", "Scenariusz"])
    publisher = publisher or extract_label(text, ["Wydawca"])
    voice = extract_label(text, ["Głosy", "Lektor", "Czyta"])
    narrators = unique_names(voice)
    isbn_match = re.search(r"\b(97[89]\d{10})\b", text)
    isbn = isbn or (isbn_match.group(1) if isbn_match else None)
    published = published or year(text)
    duration = duration or duration_minutes(text)

    links = await collect_links(page) if collect_page_links else []
    return {
        "title": title,
        "author": author,
        "narrators": list(dict.fromkeys(narrators)),
        "publisher": publisher,
        "publishedYear": published,
        "description": description,
        "cover": cover,
        "isbn": isbn,
        "language": "pol",
        "duration": duration,
        "genres": list(dict.fromkeys(x for x in genres if x)),
        "url": canonical(url),
        "series": title if series_hint else None,
        "links": links,
    }


async def enrich_series(ctx, data):
    episode_urls = [u for u in data.get("links", []) if is_product(u)]
    if not episode_urls:
        return data
    page = await ctx.new_page()
    try:
        episode = await parse_page(page, episode_urls[0], collect_page_links=False)
        for key in ("author", "publisher", "publishedYear", "description", "cover", "isbn"):
            if not data.get(key) and episode.get(key):
                data[key] = episode[key]
        data["narrators"] = list(dict.fromkeys(data.get("narrators", []) + episode.get("narrators", [])))
        return data
    finally:
        await page.close()


async def parse_candidate(ctx, url):
    page = await ctx.new_page()
    try:
        if is_series(url):
            data = await parse_page(page, url, series_hint=True, collect_page_links=True)
        else:
            data = await parse_page(page, url, collect_page_links=False)
    finally:
        await page.close()
    if is_series(url):
        data = await enrich_series(ctx, data)
    return data


def score_book(book, query, author):
    title_score = sim(book.get("title"), query)
    author_score = sim(book.get("author"), author) if author else 1.0
    if author and not book.get("author"):
        author_score = 0.60
    score = title_score * 0.78 + author_score * 0.22 if author else title_score
    # Exact audioserial slug/title match gets priority over generic results.
    if book.get("series") and title_score >= 0.90:
        score += 0.03
    return min(score, 1.0)


async def audioteka_search(query, author=""):
    key = f"audioteka|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    ctx = await browser_context()
    page = await ctx.new_page()
    try:
        # For short/common queries (e.g. "Siostry"), search first and then
        # explicitly try the audioserial slug. This catches /cykl/*-audioserial/
        # even when the search UI does not expose the series card.
        urls = await search_urls(page, query)
        direct = await direct_candidates(page, query)
        urls = list(dict.fromkeys(direct + urls))
        if author:
            author_urls = await search_urls(page, f"{query} {author}")
            urls = list(dict.fromkeys(urls + author_urls))
    finally:
        await page.close()

    query_n = norm(query)
    def priority(u):
        path_n = norm(urlparse(u).path)
        exact_slug = query_n and query_n.replace(" ", "-") in path_n
        return (0 if exact_slug else 1, 0 if is_series(u) else 1)
    urls = sorted(urls, key=priority)
    urls = list(dict.fromkeys(urls))[:12]

    print(f"[Audioteka] candidates to parse: {len(urls)}")
    books = []
    for url in urls:
        try:
            book = await asyncio.wait_for(parse_candidate(ctx, url), timeout=30)
            if book.get("title"):
                books.append(book)
                print(f"[Audioteka] parsed: {book.get('title')} / {book.get('author')} ({url})")
        except Exception as exc:
            print(f"[Audioteka] detail failed {url}: {type(exc).__name__}: {exc}")

    ranked = []
    for book in books:
        title_score = sim(book.get("title"), query)
        author_score = sim(book.get("author"), author) if author else 1.0
        if title_score < 0.45:
            continue
        if author and book.get("author") and author_score < 0.40:
            continue
        book["similarity"] = round(score_book(book, query, author), 4)
        ranked.append(book)

    ranked.sort(key=lambda x: x["similarity"], reverse=True)
    matches = []
    for book in ranked[:MAX_RESULTS]:
        matches.append({
            "title": book.get("title"),
            "author": book.get("author"),
            "narrator": ", ".join(book.get("narrators", [])) or None,
            "publisher": book.get("publisher"),
            "publishedYear": book.get("publishedYear"),
            "description": book.get("description"),
            "cover": book.get("cover"),
            "isbn": book.get("isbn"),
            "genres": book.get("genres") or None,
            "series": ([{"series": book["series"], "sequence": None}] if book.get("series") else None),
            "language": "pol",
            "duration": book.get("duration"),
            "type": "audiobook",
            "similarity": book["similarity"],
        })

    print("[Audioteka] final:", " | ".join(f"{x['title']}/{x.get('author')} ({x['similarity']:.3f})" for x in matches))
    result = {"matches": matches}
    _cache[key] = (time.time(), result)
    return result


@app.get("/search")
async def search(query: str = Query(..., min_length=1), author: str = Query(""), authorization: str | None = Header(default=None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(await audioteka_search(query, author))


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "audioteka-pl"}


@app.on_event("shutdown")
async def shutdown():
    global _context, _browser, _pw
    if _context:
        await _context.close()
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
