import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI(title="BookBeat Polska Metadata Provider")
BASE = "https://www.bookbeat.com"
PL = f"{BASE}/pl"
SEARCH = f"{PL}/search"
CACHE_TTL = 600
MAX_RESULTS = 10
_http = None
_browser = None
_browser_context = None
_browser_page = None
_cache = {}
_lock = asyncio.Lock()
_browser_lock = asyncio.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
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


def parse_year(value):
    m = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return m.group(0) if m else None


def parse_duration(value):
    text = str(value or "")
    if not text:
        return None
    h = re.search(r"(\d+)\s*(?:godz\.?|godziny|godzin|h)\b", text, re.I)
    m = re.search(r"(\d+)\s*(?:min\.?|minut|m)\b", text, re.I)
    if h:
        return int(h.group(1)) * 60 + int(m.group(1) if m else 0)
    return int(m.group(1)) if m else None


def strip_html(value):
    return clean(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)) if value else None


def jsonld_objects(soup):
    result = []
    for node in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(node.string or node.get_text())
        except Exception:
            continue
        values = data if isinstance(data, list) else [data]
        for item in values:
            if not isinstance(item, dict):
                continue
            result.append(item)
            if isinstance(item.get("@graph"), list):
                result.extend(x for x in item["@graph"] if isinstance(x, dict))
    return result


def person_names(value):
    if isinstance(value, dict):
        name = clean(value.get("name"))
        return [name] if name else []
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(person_names(item))
        return list(dict.fromkeys(out))
    value = clean(value)
    return [value] if value else []


def first_person(value):
    names = person_names(value)
    return ", ".join(names) if names else None


def canonical(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path.startswith("/pl/book/"):
        return None
    if any(token in path for token in (":id", ":slug", "{id}", "{slug}")):
        return None
    return urljoin(BASE, path)


def is_book_url(url):
    if not url:
        return False
    path = urlparse(url).path
    return bool(re.match(r"^/pl/book/[A-Za-z0-9][A-Za-z0-9_-]*-\d+/?$", path))


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"-\d+$", "", slug).replace("-", " ").strip()


async def get_http():
    global _http
    async with _lock:
        if _http is None:
            _http = httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0, connect=10.0),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return _http


async def fetch_html(url):
    client = await get_http()
    for attempt in range(1, 3):
        try:
            response = await client.get(url, headers={"Referer": f"{PL}/", "Sec-Fetch-Site": "same-origin"})
            if response.status_code == 429:
                await asyncio.sleep(1.0 * attempt)
                continue
            response.raise_for_status()
            return response.text
        except Exception as exc:
            if attempt == 2:
                print(f"[BookBeat] HTTP failed: {url} {type(exc).__name__}: {exc}")
                return None
            await asyncio.sleep(0.25 * attempt)
    return None


async def browser_search_page(query):
    global _browser, _browser_context, _browser_page
    url = f"{SEARCH}?q={quote_plus(query)}&title={quote_plus(query)}"
    async with _browser_lock:
        try:
            if _browser_page is None:
                if _browser is None:
                    pw = await async_playwright().start()
                    _browser = pw.chromium
                    _browser_context = await _browser.launch_persistent_context(
                        user_data_dir="/tmp/bookbeat-playwright",
                        headless=True,
                        locale="pl-PL",
                        user_agent=HEADERS["User-Agent"],
                        viewport={"width": 1440, "height": 900},
                    )
                _browser_page = await _browser_context.new_page()
            page = _browser_page
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.locator("a[data-testid='book-card']").first.wait_for(timeout=7000)
            except Exception:
                pass
            found = parse_search_html(await page.content())
            print(f"[BookBeat] browser search '{query}' -> {len(found)} book URLs")
            return found
        except Exception as exc:
            print(f"[BookBeat] browser search failed: {type(exc).__name__}: {exc}")
            return []


def extract_book_urls(html):
    text = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    found, seen = [], set()
    patterns = (
        r"https?://(?:www\.)?bookbeat\.com/pl/book/[A-Za-z0-9][A-Za-z0-9_-]*-\d+",
        r"/pl/book/[A-Za-z0-9][A-Za-z0-9_-]*-\d+",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            url = canonical(match.group(0))
            if url and is_book_url(url) and url not in seen:
                seen.add(url)
                found.append(url)
    return found


def parse_search_html(html):
    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()
    for link in soup.select("a[data-testid='book-card'], a[href*='/pl/book/']"):
        href = canonical(urljoin(BASE, link.get("href") or ""))
        if not href or not is_book_url(href) or href in seen:
            continue
        seen.add(href)
        card = link
        for _ in range(6):
            parent = getattr(card, "parent", None)
            if not parent:
                break
            card = parent
            if len(clean(card.get_text(" ", strip=True)) or "") >= 20:
                break
        title = None
        for selector in ("[data-testid='book-card-title']", "h1", "h2", "h3", "h4", "[data-testid*='title']"):
            node = card.select_one(selector)
            if node and clean(node.get_text(" ", strip=True)):
                title = clean(node.get_text(" ", strip=True))
                break
        title = title or clean(link.get("aria-label")) or clean(link.get_text(" ", strip=True)) or url_title(href)
        authors = []
        author_node = card.select_one("[data-testid='book-card-author']")
        if author_node and clean(author_node.get_text(" ", strip=True)):
            authors.append(clean(author_node.get_text(" ", strip=True)))
        for selector in ("a[href*='/authors/']", "a[href*='/author/']"):
            authors.extend(clean(x.get_text(" ", strip=True)) for x in card.select(selector))
        authors = list(dict.fromkeys(x for x in authors if x))
        cover = None
        img = card.select_one("img")
        if img:
            for attr in ("src", "data-src", "data-lazy-src", "srcset"):
                value = clean(img.get(attr))
                if not value:
                    continue
                if attr == "srcset":
                    value = value.split(",")[0].strip().split()[0]
                if not value.startswith("data:"):
                    cover = urljoin(BASE, value)
                    break
        found.append({"url": href, "title": title, "authors": authors, "cover": cover})
    if not found:
        for href in extract_book_urls(html):
            if href not in seen:
                seen.add(href)
                found.append({"url": href, "title": url_title(href), "authors": [], "cover": None})
    return found


async def search_page(query):
    url = f"{SEARCH}?q={quote_plus(query)}&title={quote_plus(query)}"
    print(f"[BookBeat] search: {url}")
    html = await fetch_html(url)
    if html:
        found = parse_search_html(html)
        if found:
            print(f"[BookBeat] search '{query}' -> {len(found)} book URLs")
            return found
    print(f"[BookBeat] search '{query}' -> 0 book URLs, switching to browser fallback")
    found = await browser_search_page(query)
    if found:
        return found
    print(f"[BookBeat] search '{query}' -> 0 book URLs")
    return []


def labeled_metadata(soup):
    result = {}
    labels = (
        "Oryginalny rok publikacji",
        "Data publikacji audiobooka",
        "Data publikacji e-booka",
        "Godziny odliczone po przeczytaniu całego e-booka",
        "Wydawca audiobooka",
        "Wydawca e-booka",
        "Numer ISBN audiobooka",
        "Numer ISBN e-book",
    )
    for label in labels:
        nodes = soup.find_all(attrs={"aria-label": label})
        for node in nodes:
            value_node = node.find_next_sibling()
            if not value_node:
                parent = node.parent
                if parent:
                    siblings = [x for x in parent.find_all(recursive=False) if x is not node]
                    value_node = siblings[0] if siblings else None
            value = clean(value_node.get_text(" ", strip=True)) if value_node else None
            if value:
                result[label] = value
                break
    return result


def description_from_bookbeat(soup):
    node = soup.select_one("span[role='text'][aria-label]")
    if node and node.select_one("span[class*='bookInfo_summary']"):
        summary = node.select_one("span[class*='bookInfo_summary']")
        paragraphs = [clean(p.get_text(" ", strip=True)) for p in summary.select("p")]
        paragraphs = [p for p in paragraphs if p]
        if paragraphs:
            return "\n\n".join(paragraphs)
    node = soup.select_one("[class*='bookInfo_summary']")
    if not node:
        return None
    paragraphs = [clean(p.get_text(" ", strip=True)) for p in node.select("p")]
    paragraphs = [p for p in paragraphs if p]
    if paragraphs:
        return "\n\n".join(paragraphs)
    return clean(node.get_text(" ", strip=True))


def series_from_bookbeat(soup, flat):
    for link in soup.find_all("a", href=True):
        text = clean(link.get_text(" ", strip=True)) or ""
        m = re.search(r"Tom\s+(\d+)\s*[-–]\s*(.+)$", text, re.I)
        if m:
            return clean(m.group(2)), m.group(1)
    m = re.search(r"Tom\s+(\d+)\s*[-–]\s*([^\n]+)", flat, re.I)
    if m:
        return clean(m.group(2)), m.group(1)
    return None, None


def narrator_from_bookbeat(soup, description):
    m = re.search(r"(?:^|\n)\s*Lektor:\s*([^\n]+)", description or "", re.I)
    if m:
        return clean(m.group(1))
    for link in soup.find_all("a", href=True):
        if norm(link.get_text(" ", strip=True)) == "zespol lektorow":
            return "Zespół lektorów"
    return None


def genres_from_bookbeat(soup):
    values = []
    for node in soup.find_all(attrs={"aria-label": "Kategorie"}):
        parent = node.parent
        container = parent if parent else node
        for link in container.find_all("a", href=True):
            value = clean(link.get_text(" ", strip=True))
            if value:
                values.append(value)
        if not values and parent and parent.parent:
            for link in parent.parent.find_all("a", href=True):
                value = clean(link.get_text(" ", strip=True))
                if value:
                    values.append(value)
    if not values:
        for node in soup.find_all(string=lambda s: isinstance(s, str) and clean(s) == "Kategorie:"):
            parent = node.parent
            container = parent.parent if parent else None
            if container:
                for link in container.find_all("a", href=True):
                    value = clean(link.get_text(" ", strip=True))
                    if value:
                        values.append(value)
    return list(dict.fromkeys(values))


def parse_detail(html, candidate):
    soup = BeautifulSoup(html, "html.parser")
    flat = soup.get_text("\n", strip=True) or ""
    meta = labeled_metadata(soup)
    description = description_from_bookbeat(soup)

    data = {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "description": description,
        "cover": candidate.get("cover"),
        "isbn": None,
        "duration": None,
        "genres": [],
        "series": None,
        "sequence": None,
        "language": "pol",
        "type": "audiobook",
        "url": candidate["url"],
    }

    for item in jsonld_objects(soup):
        types = item.get("@type")
        types = {str(x).lower() for x in (types if isinstance(types, list) else [types])}
        if not types & {"book", "audiobook", "product", "creativework"}:
            continue
        data["title"] = clean(item.get("name")) or data["title"]
        data["description"] = data["description"] or strip_html(item.get("description"))
        data["author"] = first_person(item.get("author")) or data["author"]
        publisher = item.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        data["publisher"] = clean(publisher) or data["publisher"]
        image = item.get("image") or item.get("thumbnailUrl")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        if image and not data["cover"]:
            data["cover"] = urljoin(BASE, str(image))
        genre = item.get("genre")
        if isinstance(genre, list):
            data["genres"].extend(clean(x) for x in genre if clean(x))
        elif genre:
            data["genres"].append(clean(genre))

    h1 = soup.select_one("h1")
    if h1:
        data["title"] = clean(h1.get_text(" ", strip=True)) or data["title"]

    if not data["description"]:
        for selector in ("meta[property='og:description']", "meta[name='description']"):
            node = soup.select_one(selector)
            if node and node.get("content"):
                data["description"] = clean(node.get("content"))
                break

    og = soup.select_one("meta[property='og:image']")
    if og and og.get("content") and not data["cover"]:
        data["cover"] = urljoin(BASE, og["content"])

    if not data["author"]:
        authors = []
        for selector in ("a[href*='/authors/']", "a[href*='/author/']"):
            authors.extend(clean(x.get_text(" ", strip=True)) for x in soup.select(selector))
        data["author"] = ", ".join(dict.fromkeys(x for x in authors if x)) or None

    data["narrator"] = narrator_from_bookbeat(soup, description)

    # BookBeat can expose separate audiobook and e-book publishers. ABS has
    # one publisher field, so use the audiobook publisher first and the
    # e-book publisher as a fallback. If both are unavailable, keep JSON-LD.
    data["publisher"] = (
        meta.get("Wydawca audiobooka")
        or meta.get("Wydawca e-booka")
        or data["publisher"]
    )

    data["publishedYear"] = (
        parse_year(meta.get("Data publikacji audiobooka"))
        or parse_year(meta.get("Oryginalny rok publikacji"))
        or data["publishedYear"]
    )

    # Same principle as publisher: prefer the audiobook ISBN, then use the
    # e-book ISBN when the audiobook ISBN is absent.
    data["isbn"] = (
        meta.get("Numer ISBN audiobooka")
        or meta.get("Numer ISBN e-book")
        or data["isbn"]
    )

    duration_candidates = []
    for text in soup.stripped_strings:
        value = clean(text)
        if value and re.fullmatch(r"\d+\s*godz\.\s*\d+\s*min\.?(?:\s*)", value, re.I):
            duration_candidates.append(value)
    if duration_candidates:
        data["duration"] = parse_duration(duration_candidates[0])

    data["genres"] = genres_from_bookbeat(soup) or list(dict.fromkeys(x for x in data["genres"] if x))
    data["series"], data["sequence"] = series_from_bookbeat(soup, flat)

    language = None
    for node in soup.find_all(attrs={"aria-label": True}):
        label = clean(node.get("aria-label")) or ""
        if label in {"Język", "Języki"}:
            value_node = node.find_next_sibling()
            language = clean(value_node.get_text(" ", strip=True)) if value_node else None
            break
    if language and norm(language) not in {"polski", "polish", "pl"}:
        data["language"] = None

    data["genres"] = list(dict.fromkeys(x for x in data["genres"] if x))
    return data


def match_result(data, score):
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
        "language": data.get("language"),
        "duration": data.get("duration"),
        "type": "audiobook",
        "url": data.get("url"),
        "similarity": round(score, 3),
    }


async def bookbeat_search(query, author=""):
    key = f"bookbeat|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        print(f"[BookBeat] cache hit: {key}")
        return cached[1]

    candidates = await search_page(query)
    if author:
        def candidate_rank(item):
            title_score = similarity(item.get("title"), query)
            author_score = similarity(item.get("authors", [""])[0] if item.get("authors") else "", author)
            return title_score * 0.75 + author_score * 0.25
        candidates.sort(key=candidate_rank, reverse=True)
    else:
        candidates.sort(key=lambda item: similarity(item.get("title"), query), reverse=True)

    candidates = candidates[:20]
    print(f"[BookBeat] candidates to parse: {len(candidates)}")

    async def enrich(item):
        html = await fetch_html(item["url"])
        if not html:
            return None
        try:
            data = parse_detail(html, item)
            ts = similarity(data.get("title"), query)
            aa = similarity(data.get("author"), author) if author else 1.0
            score = ts * 0.75 + aa * 0.25 if author else ts
            if data.get("language") == "pol":
                score = min(1.0, score + 0.02)
            print(
                f"[BookBeat] detail: {data.get('title')} / {data.get('author')} "
                f"score={score:.3f} narrator={data.get('narrator')} publisher={data.get('publisher')} "
                f"year={data.get('publishedYear')} isbn={data.get('isbn')} duration={data.get('duration')} "
                f"genres={data.get('genres')} series={data.get('series')}#{data.get('sequence')} url={data.get('url')}"
            )
            return score, data
        except Exception as exc:
            print(f"[BookBeat] detail failed: {item['url']} {type(exc).__name__}: {exc}")
            return None

    enriched = []
    for i in range(0, len(candidates), 8):
        batch = await asyncio.gather(*(enrich(x) for x in candidates[i:i + 8]))
        enriched.extend(x for x in batch if x)

    enriched.sort(key=lambda x: x[0], reverse=True)
    final = []
    for score, data in enriched:
        if score < 0.55:
            continue
        final.append(match_result(data, score))
        if len(final) >= MAX_RESULTS:
            break

    result = {"matches": final}
    print("[BookBeat] final:", " | ".join(f"{x['title']}/{x['author']} [{x['similarity']:.3f}]" for x in final))
    if final:
        _cache[key] = (time.time(), result)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "bookbeat"}


@app.get("/search")
async def search(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
    if authorization is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    return JSONResponse(await bookbeat_search(query, author))
