import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI(title="Storytel PL Audiobookshelf Metadata Provider")
BASE = "https://www.storytel.com/pl"
SEARCH = BASE + "/search/all"
CACHE_TTL = 600
MAX_RESULTS = 10
_cache = {}
_pw = None
_browser = None
_context = None
_lock = asyncio.Lock()


def norm(v):
    s = str(v or "").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = re.sub(r"\s+", " ", v).strip()
        return v or None
    return v


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.96
    return SequenceMatcher(None, a, b).ratio()


def author_score(authors, wanted):
    if not wanted:
        return 1.0
    return max((sim(a, wanted) for a in authors), default=0.0)


def book_id(url):
    m = re.search(r"-(\d+)(?:/)?$", urlparse(url).path)
    return m.group(1) if m else ""


def title_from_url(url):
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return unquote(slug).replace("-", " ").strip()


def year(v):
    m = re.search(r"(?:19|20)\d{2}", str(v or ""))
    return m.group(0) if m else None


def duration(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return round(n / 60) if n > 300 else n
    s = str(v)
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s, re.I)
    if m:
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0) + round(int(m.group(3) or 0) / 60)
    m = re.search(r"(\d+)\s*(?:godz\.?|h)\s*(?:(\d+)\s*(?:min|m))?", s, re.I)
    return int(m.group(1)) * 60 + int(m.group(2) or 0) if m else None


def jsonld(raw):
    try:
        data = json.loads(raw)
    except Exception:
        return []
    vals = data if isinstance(data, list) else [data]
    out = []
    for x in vals:
        if isinstance(x, dict):
            out.append(x)
            if isinstance(x.get("@graph"), list):
                out.extend(y for y in x["@graph"] if isinstance(y, dict))
    return out


async def context():
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


async def goto(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)


async def search_urls(page, query):
    url = f"{SEARCH}?query={quote(query)}&formats=abook%2Cebook"
    print(f"[Storytel] browser search: {url}")
    await goto(page, url)
    for _ in range(5):
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(400)

    hrefs = await page.locator("a[href*='/pl/books/']").evaluate_all(
        "els => els.map(a => a.href).filter(Boolean)"
    )
    urls, seen = [], set()
    for href in hrefs:
        p = urlparse(href)
        if "/pl/books/" not in p.path:
            continue
        if not re.search(r"\d+/?$", p.path):
            continue
        u = f"https://www.storytel.com{p.path}"
        if u not in seen:
            seen.add(u)
            urls.append(u)
    print(f"[Storytel] browser search '{query}' -> {len(urls)} book URLs")
    return urls


async def detail(page, url):
    await goto(page, url)
    title = title_from_url(url)
    authors, narrators, genres = [], [], []
    publisher = description = cover = isbn = language = None
    published = series = sequence = dur = None

    scripts = await page.locator("script[type='application/ld+json']").all_text_contents()
    for raw in scripts:
        for item in jsonld(raw):
            if not isinstance(item, dict):
                continue
            if item.get("name"):
                title = clean(item.get("name")) or title
            description = clean(item.get("description")) or description
            image = item.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            cover = cover or image
            isbn = isbn or clean(item.get("isbn"))
            published = published or year(item.get("datePublished"))
            dur = dur or duration(item.get("duration"))
            pub = item.get("publisher")
            if isinstance(pub, dict):
                publisher = publisher or clean(pub.get("name"))
            elif isinstance(pub, str):
                publisher = publisher or clean(pub)
            ao = item.get("author")
            if isinstance(ao, list):
                authors += [clean(x.get("name") if isinstance(x, dict) else x) for x in ao]
            elif isinstance(ao, dict):
                authors.append(clean(ao.get("name")))
            elif isinstance(ao, str):
                authors.append(clean(ao))
            g = item.get("genre")
            if isinstance(g, list):
                genres += [clean(x) for x in g]
            elif g:
                genres.append(clean(g))

    body = await page.locator("body").inner_text(timeout=10000)
    if await page.locator("h1").count():
        title = clean(await page.locator("h1").first.text_content()) or title

    for selector, target in [("a[href*='/pl/authors/']", authors), ("a[href*='/pl/narrators/']", narrators)]:
        try:
            target += [clean(x) for x in await page.locator(selector).all_text_contents()]
        except Exception:
            pass

    try:
        cover = cover or await page.locator("meta[property='og:image']").get_attribute("content")
        description = description or await page.locator("meta[property='og:description']").get_attribute("content")
    except Exception:
        pass

    authors = list(dict.fromkeys(x for x in authors if x))
    narrators = list(dict.fromkeys(x for x in narrators if x))
    genres = list(dict.fromkeys(x for x in genres if x))

    if not isbn:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        isbn = m.group(1) if m else None
    published = published or year(body)
    if not dur:
        dur = duration(body)
    if not language:
        language = "pol" if re.search(r"\b(?:Język\s*)?Polski\b", body, re.I) else None
        language = language or ("eng" if re.search(r"\b(?:Język\s*)?English\b", body, re.I) else None)
    if not series:
        m = re.search(r"(?:Seria|Serie)\s+(.+?)\s+(\d+)\s+z\s+(\d+)", body, re.I)
        if m:
            series, sequence = clean(m.group(1)), m.group(2)

    return {
        "title": title,
        "authors": authors,
        "narrators": narrators,
        "publisher": publisher,
        "description": description,
        "cover": cover,
        "isbn": isbn,
        "publishedYear": published,
        "language": language,
        "duration": dur,
        "genres": genres,
        "series": series,
        "sequence": sequence,
        "url": url,
        "storytelId": book_id(url),
    }


def to_match(book, score):
    return {
        "title": book.get("title"),
        "author": ", ".join(book.get("authors") or []) or None,
        "narrator": ", ".join(book.get("narrators") or []) or None,
        "publisher": book.get("publisher"),
        "publishedYear": book.get("publishedYear"),
        "description": book.get("description"),
        "cover": book.get("cover"),
        "isbn": book.get("isbn"),
        "genres": book.get("genres") or None,
        "series": ([{"series": book["series"], "sequence": book.get("sequence")}] if book.get("series") else None),
        "language": book.get("language"),
        "duration": book.get("duration"),
        "type": "audiobook",
        "similarity": score,
    }


async def do_search(query, author):
    key = f"{norm(query)}|{norm(author)}"
    if key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]

    ctx = await context()
    page = await ctx.new_page()
    try:
        urls = await search_urls(page, query)
        if not urls and author:
            urls = await search_urls(page, f"{query} {author}")
    finally:
        await page.close()

    urls.sort(key=lambda u: sim(title_from_url(u), query), reverse=True)
    urls = urls[:40]
    books = []
    for i in range(0, len(urls), 4):
        pages = [await ctx.new_page() for _ in urls[i:i + 4]]
        try:
            books += await asyncio.gather(*(detail(p, u) for p, u in zip(pages, urls[i:i + 4])))
        finally:
            await asyncio.gather(*(p.close() for p in pages), return_exceptions=True)

    ranked = []
    for book in books:
        title_similarity = sim(book.get("title"), query)
        matched_author = author_score(book.get("authors", []), author)
        score = title_similarity * 0.75 + matched_author * 0.25 if author else title_similarity
        if book.get("language") == "pol":
            score += 0.05
        ranked.append((min(1.0, score), title_similarity, matched_author, book))
    ranked.sort(key=lambda item: item[0], reverse=True)

    final = []
    for score, title_similarity, matched_author, book in ranked:
        if title_similarity < 0.65:
            continue
        if author and matched_author < 0.50:
            continue
        final.append(to_match(book, score))
        if len(final) >= MAX_RESULTS:
            break

    print("[Storytel] final:", " | ".join(f"{item['title']}/{item['author']} ({item['similarity']:.3f})" for item in final))
    result = {"matches": final}
    _cache[key] = (time.time(), result)
    return result


@app.get("/search")
async def search_endpoint(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        return JSONResponse(await do_search(query, author))
    except Exception as exc:
        print(f"[Storytel] ERROR: {exc!r}")
        return JSONResponse({"matches": [], "error": str(exc)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.on_event("shutdown")
async def shutdown():
    global _context, _browser, _pw
    if _context:
        await _context.close()
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
