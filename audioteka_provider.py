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

app = FastAPI(title="Audioteka Polska Metadata Provider")
CACHE_TTL = 600
MAX_RESULTS = 10
_cache = {}
_pw = _browser = _context = None
_lock = asyncio.Lock()

BASE = "https://audioteka.com"
PL = BASE + "/pl"
SEARCH = PL + "/szukaj/"


def norm(value):
    value = str(value or "").replace("ł", "l").replace("Ł", "L")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = re.sub(r"\s+", " ", value).strip()
        return value or None
    return value


def similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.96
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


def jsonld(raw):
    try:
        data = json.loads(raw)
    except Exception:
        return []
    items = data if isinstance(data, list) else [data]
    out = []
    for item in items:
        if isinstance(item, dict):
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
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
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
    # Audioteka can show a consent layer which otherwise hides links.
    for selector in (
        "button:has-text('Akceptuję')",
        "button:has-text('Zgadzam się')",
        "button:has-text('Zaakceptuj')",
        "[id*=cookie] button",
    ):
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=1500)
                await page.wait_for_timeout(500)
                break
        except Exception:
            pass


def slugify(value):
    value = norm(value)
    return value.replace(" ", "-")


def is_product_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.startswith("/pl/audiobook/")


def is_series_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.startswith("/pl/cykl/")


def canonical(url):
    p = urlparse(url)
    return f"{BASE}{p.path.rstrip('/')}/"


async def collect_links(page):
    hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    result = []
    seen = set()
    for href in hrefs:
        p = urlparse(href)
        if p.netloc not in {"audioteka.com", "www.audioteka.com"}:
            continue
        if not p.path.startswith("/pl/"):
            continue
        if not (is_product_url(href) or is_series_url(href)):
            continue
        url = canonical(href)
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


async def search_page_urls(page, query):
    url = f"{SEARCH}?phrase={quote(query)}"
    print(f"[Audioteka] search: {url}")
    await goto(page, url)
    for _ in range(6):
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(350)
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
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if page.url.startswith(BASE) and await page.locator("h1").count():
                text = clean(await page.locator("body").inner_text()) or ""
                if "Nie znaleziono" not in text and "404" not in (await page.title()):
                    found.append(canonical(page.url))
                    print(f"[Audioteka] direct candidate: {page.url}")
        except Exception:
            pass
    return list(dict.fromkeys(found))


async def parse_page(page, url, series_hint=False):
    await goto(page, url)
    body = clean(await page.locator("body").inner_text()) or ""
    title = None
    author = None
    narrators = []
    publisher = None
    description = None
    cover = None
    isbn = None
    published = None
    duration = None
    genres = []

    scripts = await page.locator("script[type='application/ld+json']").all_text_contents()
    for raw in scripts:
        for item in jsonld(raw):
            title = title or clean(item.get("name"))
            description = description or clean(item.get("description"))
            isbn = isbn or clean(item.get("isbn"))
            cover = cover or item.get("image")
            if isinstance(cover, list):
                cover = cover[0] if cover else None
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

    if await page.locator("h1").count():
        title = clean(await page.locator("h1").first.text_content()) or title

    # Audioteka exposes author/narrator information as ordinary page text too.
    text = re.sub(r"\s+", " ", body)
    if not author:
        m = re.search(r"(?:Autor|Autorzy|Scenariusz)\s*[:\-]\s*(.+?)(?=\s+(?:Głosy|Lektor|Wydawca|Długość|Opis|Informacje)\b|$)", text, re.I)
        author = clean(m.group(1)) if m else None
    m = re.search(r"(?:Głosy|Lektor|Czyta)\s*[:\-]\s*(.+?)(?=\s+(?:Długość|Wydawca|Typ|Format|Język|Opis)\b|$)", text, re.I)
    if m:
        narrators = [clean(x) for x in re.split(r",|\s+oraz\s+", m.group(1)) if clean(x)]
    if not publisher:
        m = re.search(r"Wydawca\s*[:\-]?\s*(.+?)(?=\s+(?:Typ|Format|Język|Opis|Informacje)\b|$)", text, re.I)
        publisher = clean(m.group(1)) if m else None
    if not isbn:
        m = re.search(r"\b(97[89]\d{10})\b", text)
        isbn = m.group(1) if m else None
    published = published or year(text)
    duration = duration or duration_minutes(text)
    if not description:
        try:
            description = await page.locator("meta[property='og:description']").get_attribute("content")
        except Exception:
            pass
    if not cover:
        try:
            cover = await page.locator("meta[property='og:image']").get_attribute("content")
        except Exception:
            pass

    links = await collect_links(page)
    return {
        "title": title,
        "author": author,
        "narrators": list(dict.fromkeys(x for x in narrators if x)),
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


async def expand_series(ctx, series_url):
    page = await ctx.new_page()
    try:
        data = await parse_page(page, series_url, series_hint=True)
        episode_urls = [u for u in data["links"] if is_product_url(u)]
        if not episode_urls:
            return [data]
        # Prefer the series itself as the result, but enrich duration from episodes.
        durations = []
        for url in episode_urls[:20]:
            try:
                episode = await parse_page(page, url)
                if episode.get("duration"):
                    durations.append(episode["duration"])
                if not data.get("author") and episode.get("author"):
                    data["author"] = episode["author"]
                data["narrators"] = list(dict.fromkeys(data["narrators"] + episode.get("narrators", [])))
            except Exception:
                pass
        if durations:
            data["duration"] = sum(durations)
        data["title"] = data.get("title") or ""
        data["series"] = data.get("title")
        return [data]
    finally:
        await page.close()


async def audioteka_search(query, author=""):
    key = f"audioteka|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    ctx = await browser_context()
    page = await ctx.new_page()
    try:
        urls = await search_page_urls(page, query)
        if author and not urls:
            urls = await search_page_urls(page, f"{query} {author}")
        # Search pages can occasionally hide the exact result; direct slug candidates
        # are especially useful for Audioteka's /cykl/... audioserial pages.
        direct = await direct_candidates(page, query)
        urls = list(dict.fromkeys(urls + direct))
    finally:
        await page.close()

    products = [u for u in urls if is_product_url(u)]
    series = [u for u in urls if is_series_url(u)]
    books = []

    for url in products[:30]:
        p = await ctx.new_page()
        try:
            books.append(await parse_page(p, url))
        except Exception as exc:
            print(f"[Audioteka] detail failed {url}: {exc}")
        finally:
            await p.close()

    for url in series[:10]:
        try:
            books.extend(await expand_series(ctx, url))
        except Exception as exc:
            print(f"[Audioteka] series failed {url}: {exc}")

    ranked = []
    for book in books:
        title_score = similarity(book.get("title"), query)
        author_score = similarity(book.get("author"), author) if author else 1.0
        # Allow "Mazurski przekręt 2" to match the series title
        # "Mazurski przekręt 2. Audioserial".
        score = title_score * 0.75 + author_score * 0.25 if author else title_score
        if author and author_score < 0.45:
            continue
        if title_score < 0.55:
            continue
        book["similarity"] = round(min(score, 1.0), 4)
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
async def search(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
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
