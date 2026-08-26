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

app = FastAPI(title="Polskie Meta dla Audiobookshelf")
CACHE_TTL = 600
MAX_RESULTS = 10
_cache = {}
_pw = _browser = _context = None
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
    for item in vals:
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
            locale="pl-PL", timezone_id="Europe/Warsaw", viewport={"width": 1440, "height": 1000},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"},
        )
        return _context


async def goto(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(1800)


async def first_text(scope, selectors):
    for selector in selectors:
        try:
            locator = scope.locator(selector).first
            if await locator.count():
                text = clean(await locator.text_content())
                if text:
                    return text
        except Exception:
            pass
    return None


# Storytel Polska
STORYTEL_BASE = "https://www.storytel.com/pl"
STORYTEL_SEARCH = STORYTEL_BASE + "/search/all"


def storytel_book_id(url):
    match = re.search(r"-(\d+)(?:/)?$", urlparse(url).path)
    return match.group(1) if match else ""


def storytel_title_from_url(url):
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return unquote(slug).replace("-", " ").strip()


async def storytel_search_urls(page, query):
    url = f"{STORYTEL_SEARCH}?query={quote(query)}&formats=abook%2Cebook"
    print(f"[Storytel] search: {url}")
    await goto(page, url)
    for _ in range(5):
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(350)
    hrefs = await page.locator("a[href*='/pl/books/']").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    urls, seen = [], set()
    for href in hrefs:
        p = urlparse(href)
        if "/pl/books/" not in p.path or not re.search(r"\d+/?$", p.path):
            continue
        u = f"https://www.storytel.com{p.path}"
        if u not in seen:
            seen.add(u)
            urls.append(u)
    print(f"[Storytel] '{query}' -> {len(urls)} book URLs")
    return urls


async def storytel_detail(page, url):
    await goto(page, url)
    title = storytel_title_from_url(url)
    authors, narrators, genres = [], [], []
    publisher = description = cover = isbn = language = None
    published = series = sequence = dur = None
    scripts = await page.locator("script[type='application/ld+json']").all_text_contents()
    for raw in scripts:
        for item in jsonld(raw):
            title = clean(item.get("name")) or title
            description = description or clean(item.get("description"))
            image = item.get("image")
            if isinstance(image, list): image = image[0] if image else None
            cover = cover or image
            isbn = isbn or clean(item.get("isbn"))
            published = published or year(item.get("datePublished"))
            dur = dur or duration(item.get("duration"))
            pub = item.get("publisher")
            publisher = publisher or clean(pub.get("name") if isinstance(pub, dict) else pub)
            author = item.get("author")
            if isinstance(author, list): authors += [clean(x.get("name") if isinstance(x, dict) else x) for x in author]
            elif isinstance(author, dict): authors.append(clean(author.get("name")))
            elif isinstance(author, str): authors.append(clean(author))
            genre = item.get("genre")
            genres += [clean(x) for x in genre] if isinstance(genre, list) else ([clean(genre)] if genre else [])
    if await page.locator("h1").count():
        title = clean(await page.locator("h1").first.text_content()) or title
    try:
        authors += [clean(x) for x in await page.locator("a[href*='/pl/authors/']").all_text_contents()]
        narrators += [clean(x) for x in await page.locator("a[href*='/pl/narrators/']").all_text_contents()]
        cover = cover or await page.locator("meta[property='og:image']").get_attribute("content")
        description = description or await page.locator("meta[property='og:description']").get_attribute("content")
    except Exception:
        pass
    body = await page.locator("body").inner_text(timeout=10000)
    authors = list(dict.fromkeys(x for x in authors if x))
    narrators = list(dict.fromkeys(x for x in narrators if x))
    genres = list(dict.fromkeys(x for x in genres if x))
    if not isbn:
        m = re.search(r"\b(97[89]\d{10})\b", body); isbn = m.group(1) if m else None
    published = published or year(body); dur = dur or duration(body)
    language = language or ("pol" if re.search(r"\b(?:Język\s*)?Polski\b", body, re.I) else None)
    if not series:
        m = re.search(r"(?:Seria|Serie)\s+(.+?)\s+(\d+)\s+z\s+(\d+)", body, re.I)
        if m: series, sequence = clean(m.group(1)), m.group(2)
    return {"title": title, "authors": authors, "narrators": narrators, "publisher": publisher, "description": description,
            "cover": cover, "isbn": isbn, "publishedYear": published, "language": language, "duration": dur,
            "genres": genres, "series": series, "sequence": sequence, "url": url, "storytelId": storytel_book_id(url)}


def storytel_match(book, score_value):
    return {"title": book.get("title"), "author": ", ".join(book.get("authors") or []) or None,
            "narrator": ", ".join(book.get("narrators") or []) or None, "publisher": book.get("publisher"),
            "publishedYear": book.get("publishedYear"), "description": book.get("description"), "cover": book.get("cover"),
            "isbn": book.get("isbn"), "genres": book.get("genres") or None,
            "series": ([{"series": book["series"], "sequence": book.get("sequence")}] if book.get("series") else None),
            "language": book.get("language"), "duration": book.get("duration"), "type": "audiobook", "similarity": score_value}


async def storytel_search(query, author):
    key = f"storytel|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL: return cached[1]
    ctx = await browser_context(); page = await ctx.new_page()
    try:
        urls = await storytel_search_urls(page, query)
        if not urls and author: urls = await storytel_search_urls(page, f"{query} {author}")
    finally: await page.close()
    urls.sort(key=lambda u: sim(storytel_title_from_url(u), query), reverse=True); urls = urls[:40]
    books = []
    for i in range(0, len(urls), 4):
        pages = [await ctx.new_page() for _ in urls[i:i+4]]
        try: books += await asyncio.gather(*(storytel_detail(p, u) for p, u in zip(pages, urls[i:i+4])))
        finally: await asyncio.gather(*(p.close() for p in pages), return_exceptions=True)
    ranked = []
    for book in books:
        ts = sim(book.get("title"), query); aa = author_score(book.get("authors", []), author)
        score_value = ts * .75 + aa * .25 if author else ts
        if book.get("language") == "pol": score_value += .05
        ranked.append((min(1, score_value), ts, aa, book))
    ranked.sort(key=lambda x: x[0], reverse=True)
    final = [storytel_match(b, s) for s, ts, aa, b in ranked if ts >= .65 and (not author or aa >= .50)][:MAX_RESULTS]
    result = {"matches": final}; print("[Storytel] final:", " | ".join(f"{x['title']}/{x['author']} ({x['similarity']:.3f})" for x in final)); _cache[key] = (time.time(), result); return result


# Audioteka Polska
AUDIOTEKA_BASE = "https://audioteka.com/pl"
AUDIOTEKA_SEARCH = AUDIOTEKA_BASE + "/szukaj/"


def audioteka_title_from_url(url):
    parts = [x for x in urlparse(url).path.split("/") if x]
    return unquote(parts[-1]).replace("-", " ").strip() if parts else ""


def audioteka_id(url):
    parts = [x for x in urlparse(url).path.split("/") if x]
    return parts[-1] if parts else url


async def audioteka_search_urls(page, query):
    url = f"{AUDIOTEKA_SEARCH}?phrase={quote(query)}"; print(f"[Audioteka] search: {url}"); await goto(page, url)
    for _ in range(4): await page.mouse.wheel(0, 1600); await page.wait_for_timeout(300)
    hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    urls, seen = [], set()
    for href in hrefs:
        p = urlparse(href)
        if p.netloc not in {"audioteka.com", "www.audioteka.com"} or not p.path.startswith("/pl/"): continue
        if any(x in p.path for x in ("/szukaj/", "/cykl/", "/kategoria/", "/autor/", "/wydawca/", "/tag/")): continue
        parts = [x for x in p.path.split("/") if x]
        if len(parts) < 2: continue
        u = f"https://audioteka.com{p.path.rstrip('/')}"
        if u not in seen: seen.add(u); urls.append(u)
    print(f"[Audioteka] '{query}' -> {len(urls)} candidate URLs"); return urls


def audioteka_series_from_body(body):
    for pattern in (r"(?:Seria|Cykl)\s+(.+?)\s+(?:tom|część|nr)\s+(\d+)", r"(?:Seria|Cykl)\s+(.+?)\s+(\d+)\s+z\s+\d+"):
        m = re.search(pattern, body, re.I)
        if m: return clean(m.group(1)), m.group(2)
    return None, None


async def audioteka_detail(page, url):
    await goto(page, url); title = None; authors, narrators, genres = [], [], []
    publisher = description = cover = isbn = None; published = dur = None; series = sequence = None
    scripts = await page.locator("script[type='application/ld+json']").all_text_contents()
    for raw in scripts:
        for item in jsonld(raw):
            title = title or clean(item.get("name")); description = description or clean(item.get("description")); image = item.get("image")
            if isinstance(image, list): image = image[0] if image else None
            cover = cover or image; isbn = isbn or clean(item.get("isbn")); published = published or year(item.get("datePublished")); dur = dur or duration(item.get("duration"))
            pub = item.get("publisher"); publisher = publisher or clean(pub.get("name") if isinstance(pub, dict) else pub)
            ad = item.get("author")
            if isinstance(ad, list): authors += [clean(x.get("name") if isinstance(x, dict) else x) for x in ad]
            elif isinstance(ad, dict): authors.append(clean(ad.get("name")))
            elif isinstance(ad, str): authors.append(clean(ad))
            genre = item.get("genre"); genres += [clean(x) for x in genre] if isinstance(genre, list) else ([clean(genre)] if genre else [])
    if await page.locator("h1").count(): title = clean(await page.locator("h1").first.text_content()) or title
    a = await first_text(page, ["a[href*='/pl/autor/']", "a[href*='/pl/author/']", "[class*='author']"]); authors.append(a) if a else None
    n = await first_text(page, ["dt:has-text('Głosy') + dd", "dt:has-text('Lektor') + dd", "tr:has-text('Głosy') td:last-child", "tr:has-text('Lektor') td:last-child", "[class*='narrator']"]); narrators.append(n) if n else None
    publisher = publisher or await first_text(page, ["dt:has-text('Wydawca') + dd", "tr:has-text('Wydawca') td:last-child", "[class*='publisher']"])
    try:
        description = description or await page.locator("meta[property='og:description']").get_attribute("content")
        cover = cover or await page.locator("meta[property='og:image']").get_attribute("content")
    except Exception: pass
    body = await page.locator("body").inner_text(timeout=10000); authors = list(dict.fromkeys(x for x in authors if x)); narrators = list(dict.fromkeys(x for x in narrators if x)); genres = list(dict.fromkeys(x for x in genres if x))
    if not isbn:
        m = re.search(r"\b(97[89]\d{10})\b", body); isbn = m.group(1) if m else None
    published = published or year(body); dur = dur or duration(body); series, sequence = audioteka_series_from_body(body)
    return {"title": title or audioteka_title_from_url(url), "author": ", ".join(authors) if authors else None, "narrator": ", ".join(narrators) if narrators else None, "publisher": publisher, "description": description, "cover": cover, "isbn": isbn, "publishedYear": published, "language": "pol", "duration": dur, "genres": genres, "series": series, "sequence": sequence, "url": url, "audiotekaId": audioteka_id(url)}


def audioteka_match(book, score_value):
    return {"title": book.get("title"), "author": book.get("author"), "narrator": book.get("narrator"), "publisher": book.get("publisher"), "publishedYear": book.get("publishedYear"), "description": book.get("description"), "cover": book.get("cover"), "isbn": book.get("isbn"), "genres": book.get("genres") or None, "series": ([{"series": book["series"], "sequence": book.get("sequence")}] if book.get("series") else None), "language": book.get("language"), "duration": book.get("duration"), "type": "audiobook", "similarity": score_value}


async def audioteka_search(query, author):
    key = f"audioteka|{norm(query)}|{norm(author)}"; cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL: return cached[1]
    ctx = await browser_context(); page = await ctx.new_page()
    try:
        urls = await audioteka_search_urls(page, query)
        if not urls and author: urls = await audioteka_search_urls(page, f"{query} {author}")
    finally: await page.close()
    urls.sort(key=lambda u: sim(audioteka_title_from_url(u), query), reverse=True); urls = urls[:40]; books = []
    for i in range(0, len(urls), 4):
        pages = [await ctx.new_page() for _ in urls[i:i+4]]
        try: books += await asyncio.gather(*(audioteka_detail(p, u) for p, u in zip(pages, urls[i:i+4])))
        finally: await asyncio.gather(*(p.close() for p in pages), return_exceptions=True)
    ranked = []
    for book in books:
        ts = sim(book.get("title"), query); aa = sim(book.get("author"), author) if author else 1.0; ranked.append((ts * .75 + aa * .25 if author else ts, ts, aa, book))
    ranked.sort(key=lambda x: x[0], reverse=True); final = [audioteka_match(b, min(1, s)) for s, ts, aa, b in ranked if ts >= .55 and (not author or aa >= .45)][:MAX_RESULTS]
    result = {"matches": final}; print("[Audioteka] final:", " | ".join(f"{x['title']}/{x['author']} ({x['similarity']:.3f})" for x in final)); _cache[key] = (time.time(), result); return result


async def authenticate(authorization):
    if not authorization: raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/search")
async def search_endpoint(query: str = Query(..., min_length=1), author: str = Query(""), provider: str | None = Query(default=None), x_provider: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    await authenticate(authorization)
    selected = (x_provider or provider or "storytel").lower()
    if selected in {"storytel", "storytel-pl"}: return JSONResponse(await storytel_search(query, author))
    if selected in {"audioteka", "audioteka-pl"}: return JSONResponse(await audioteka_search(query, author))
    raise HTTPException(status_code=404, detail="Unknown provider")


@app.get("/health")
async def health(x_provider: str | None = Header(default=None)):
    return {"status": "ok", "provider": x_provider or "polish-metadata"}


@app.get("/providers")
async def providers(authorization: str | None = Header(default=None)):
    await authenticate(authorization)
    return {"providers": [{"id": "storytel-pl", "name": "Storytel Polska", "port": 3000}, {"id": "audioteka-pl", "name": "Audioteka Polska", "port": 3001}]}


@app.on_event("shutdown")
async def shutdown():
    global _context, _browser, _pw
    if _context: await _context.close()
    if _browser: await _browser.close()
    if _pw: await _pw.stop()
