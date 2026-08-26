import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="BookBeat Polska Metadata Provider")

BASE = "https://www.bookbeat.com"
PL = f"{BASE}/pl"
SEARCH = f"{PL}/search"
CACHE_TTL = 600
MAX_RESULTS = 10
_http = None
_cache = {}
_lock = asyncio.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
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
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else None


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
    if not value:
        return None
    return clean(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True))


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
        return [clean(value.get("name"))] if clean(value.get("name")) else []
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(person_names(item))
        return list(dict.fromkeys(x for x in out if x))
    return [clean(value)] if clean(value) else []


def first_person(value):
    names = person_names(value)
    return ", ".join(names) if names else None


def canonical(url):
    parsed = urlparse(url)
    return urljoin(BASE, parsed.path.rstrip("/"))


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return slug.replace("-", " ").strip()


def is_book_url(url):
    return urlparse(url).path.startswith("/pl/book/")


async def get_http():
    global _http
    async with _lock:
        if _http is None:
            _http = httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0, connect=10.0),
                limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
            )
        return _http


async def fetch_html(url):
    client = await get_http()
    for attempt in range(1, 4):
        try:
            response = await client.get(url, headers={"Referer": f"{PL}/"})
            if response.status_code == 429:
                await asyncio.sleep(1.5 * attempt)
                continue
            response.raise_for_status()
            return response.text
        except Exception as exc:
            if attempt == 3:
                print(f"[BookBeat] HTTP failed: {url} {type(exc).__name__}: {exc}")
            await asyncio.sleep(0.4 * attempt)
    return None


async def search_page(query, author=None, series=False):
    params = {"q": query}
    if series:
        params["series"] = query
    else:
        params["title"] = query
    url = f"{SEARCH}?q={quote(query)}"
    if series:
        url += f"&series={quote(query)}"
    else:
        url += f"&title={quote(query)}"
    print(f"[BookBeat] search: {url}")
    html = await fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()

    for link in soup.select("a[href*='/pl/book/']"):
        href = link.get("href")
        if not href:
            continue
        href = canonical(urljoin(BASE, href))
        if not is_book_url(href) or href in seen:
            continue
        seen.add(href)

        card = link
        for _ in range(5):
            parent = card.parent
            if not parent:
                break
            text = clean(parent.get_text(" ", strip=True)) or ""
            if len(text) > 30:
                card = parent
                break
            card = parent

        title = None
        for selector in ["h2", "h3", "h4", "[data-testid*='title']"]:
            node = card.select_one(selector)
            if node and clean(node.get_text(" ", strip=True)):
                title = clean(node.get_text(" ", strip=True))
                break
        title = title or clean(link.get_text(" ", strip=True)) or url_title(href)

        authors = []
        for selector in ["a[href*='/authors/']", "a[href*='/author/']"]:
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

    print(f"[BookBeat] search '{query}' -> {len(found)} book URLs")
    return found


def label_value(lines, labels):
    wanted = {norm(x).rstrip(":") for x in labels}
    for i, line in enumerate(lines):
        if norm(line).rstrip(":") != wanted:
            continue
        for candidate in lines[i + 1:i + 5]:
            candidate = clean(candidate)
            if candidate and norm(candidate).rstrip(":") not in wanted:
                return candidate
    return None


def find_label_text(text, labels):
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([^\n|]+)", text, re.I)
        if match:
            value = clean(match.group(1))
            if value:
                return value
    return None


def series_info(text):
    patterns = [
        r"(?:Tom|Część)\s+(\d+)\s*[-–]\s*([^\n]+)",
        r"([^\n]+?)\s+Tom\s+(\d+)",
        r"([^\n]+?)\s+(\d+)\s+z\s+\d+",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        groups = match.groups()
        if groups[0].isdigit():
            return clean(groups[1]), groups[0]
        return clean(groups[0]), groups[1]
    return None, None


def parse_detail(html, candidate):
    soup = BeautifulSoup(html, "html.parser")
    body = soup.get_text("\n", strip=True)
    flat = clean(body) or ""
    lines = [clean(x) for x in body.splitlines() if clean(x)]

    data = {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "description": None,
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
        typ = item.get("@type")
        types = {str(x).lower() for x in (typ if isinstance(typ, list) else [typ])}
        if not types & {"book", "audiobook", "product", "creativework"}:
            continue
        data["title"] = clean(item.get("name")) or data["title"]
        data["description"] = data["description"] or strip_html(item.get("description"))
        data["author"] = first_person(item.get("author")) or data["author"]
        data["narrator"] = first_person(item.get("readBy")) or first_person(item.get("actor")) or data["narrator"]
        publisher = item.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        data["publisher"] = clean(publisher) or data["publisher"]
        data["isbn"] = clean(item.get("isbn") or item.get("productID")) or data["isbn"]
        data["publishedYear"] = parse_year(item.get("datePublished")) or data["publishedYear"]
        data["duration"] = parse_duration(item.get("duration")) or data["duration"]
        image = item.get("image") or item.get("thumbnailUrl")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        data["cover"] = data["cover"] or (urljoin(BASE, str(image)) if image else None)
        genre = item.get("genre")
        if isinstance(genre, list):
            data["genres"].extend(clean(x) for x in genre if clean(x))
        elif genre:
            data["genres"].append(clean(genre))

    h1 = soup.select_one("h1")
    if h1:
        data["title"] = clean(h1.get_text(" ", strip=True)) or data["title"]

    for selector in [
        "meta[property='og:description']",
        "meta[name='description']",
        "[data-testid*='description']",
    ]:
        node = soup.select_one(selector)
        if node:
            value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
            data["description"] = data["description"] or clean(value)
            if data["description"]:
                break

    og = soup.select_one("meta[property='og:image']")
    if og and og.get("content"):
        data["cover"] = data["cover"] or urljoin(BASE, og["content"])

    if not data["author"]:
        authors = []
        for selector in ["a[href*='/authors/']", "a[href*='/author/']"]:
            authors.extend(clean(x.get_text(" ", strip=True)) for x in soup.select(selector))
        data["author"] = ", ".join(dict.fromkeys(x for x in authors if x)) or None

    data["narrator"] = data["narrator"] or find_label_text(flat, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    data["publisher"] = data["publisher"] or find_label_text(flat, ["Wydawnictwo", "Wydawca"])
    data["publishedYear"] = data["publishedYear"] or parse_year(find_label_text(flat, ["Data wydania", "Data publikacji", "Data premiery"]))
    data["duration"] = data["duration"] or parse_duration(find_label_text(flat, ["Czas trwania", "Długość", "Czas"])) or parse_duration(flat)

    if not data["isbn"]:
        match = re.search(r"\b(97[89]\d{10})\b", flat)
        data["isbn"] = match.group(1) if match else None

    language = find_label_text(flat, ["Język", "Języki"])
    if language and norm(language) in {"polski", "polish", "pl"}:
        data["language"] = "pol"

    series, sequence = series_info(flat)
    data["series"] = series
    data["sequence"] = sequence
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

    # BookBeat exposes title and series search separately. Query both so a
    # title that is mainly surfaced through a series still gets found.
    pages = await asyncio.gather(
        search_page(query, author, series=False),
        search_page(query, author, series=True),
    )
    candidates = []
    seen = set()
    for group in pages:
        for item in group:
            if item["url"] not in seen:
                seen.add(item["url"])
                candidates.append(item)

    if author:
        author_query = f"{query} {author}"
        extra = await search_page(author_query, author, series=False)
        for item in extra:
            if item["url"] not in seen:
                seen.add(item["url"])
                candidates.append(item)

    def candidate_rank(item):
        title_score = similarity(item.get("title"), query)
        author_score = similarity(item.get("authors", [""])[0] if item.get("authors") else "", author) if author else 1.0
        return title_score * 0.75 + author_score * 0.25 if author else title_score

    candidates.sort(key=candidate_rank, reverse=True)
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
            print(f"[BookBeat] detail: {data.get('title')} / {data.get('author')} score={score:.3f} url={data.get('url')}")
            return score, data
        except Exception as exc:
            print(f"[BookBeat] detail failed: {item['url']} {type(exc).__name__}: {exc}")
            return None

    enriched = []
    for i in range(0, len(candidates), 6):
        batch = await asyncio.gather(*(enrich(x) for x in candidates[i:i + 6]))
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
