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

app = FastAPI(title="LubimyCzytać Metadata Provider")
BASE = "https://lubimyczytac.pl"
CACHE_TTL = 600
MAX_RESULTS = 20
_http = None
_cache = {}
_lock = asyncio.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
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


def canonical(url):
    parsed = urlparse(url)
    return urljoin(BASE, parsed.path.rstrip("/") + "/")


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug).strip()


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
    if not value:
        return None
    soup = BeautifulSoup(str(value), "html.parser")
    return clean(soup.get_text(" ", strip=True))


def parse_jsonld_scripts(soup):
    result = []
    for node in soup.select("script[type='application/ld+json']"):
        raw = node.string or node.get_text()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
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
        vals = [first_name(x) for x in value]
        return ", ".join(dict.fromkeys(x for x in vals if x)) or None
    return clean(value)


def first_nonempty(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def text_after_label(soup, labels):
    wanted = {norm(x).rstrip(":") for x in labels}
    for dt in soup.select("dt"):
        label = norm(dt.get_text(" ", strip=True)).rstrip(":")
        if label in wanted:
            dd = dt.find_next_sibling("dd")
            if dd:
                value = clean(dd.get_text(" ", strip=True))
                if value:
                    return value
    # fallback for layouts that use generic divs
    for node in soup.find_all(["div", "span", "p", "strong"]):
        label = norm(node.get_text(" ", strip=True)).rstrip(":")
        if label in wanted:
            sibling = node.find_next_sibling()
            if sibling:
                value = clean(sibling.get_text(" ", strip=True))
                if value:
                    return value
    return None


def search_author_from_card(card):
    selectors = [
        ".book-card__author a",
        ".book-card__author",
        "a[href*='/autor/']",
    ]
    for selector in selectors:
        vals = [clean(x) for x in card.select(selector)[0:10]]
        texts = [clean(x.get_text(" ", strip=True)) for x in card.select(selector)]
        texts = [x for x in texts if x]
        if texts:
            return list(dict.fromkeys(texts))
    return []


def card_cover(card):
    for img in card.select("img"):
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            value = clean(img.get(attr))
            if value and not value.startswith("data:"):
                return urljoin(BASE, value)
        srcset = clean(img.get("srcset"))
        if srcset:
            return urljoin(BASE, srcset.split(",")[0].strip().split()[0])
    return None


async def get_http():
    global _http
    async with _lock:
        if _http is None:
            _http = httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0, connect=10.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return _http


async def fetch_html(url):
    client = await get_http()
    last = None
    for attempt in range(1, 4):
        try:
            response = await client.get(url, headers={"Referer": BASE + "/"})
            if response.status_code == 429:
                await asyncio.sleep(2 * attempt)
                continue
            if response.status_code in (403, 404):
                return None, response.status_code
            response.raise_for_status()
            return response.text, response.status_code
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.5 * attempt)
    print(f"[Lubimyczytać] HTTP failed: {url} {type(last).__name__}: {last}")
    return None, None


async def search_page(query, author, section, result_type):
    url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    if author:
        url += f"&author={quote(author)}"
    print(f"[Lubimyczytać] search: {url}")
    html, status = await fetch_html(url)
    if not html:
        print(f"[Lubimyczytać] {section} '{query}' -> 0 wyników (HTTP {status or '-'})")
        return []

    soup = BeautifulSoup(html, "html.parser")
    found = []
    seen = set()
    for card in soup.select(".book-card--l"):
        if card.find_parent(class_=re.compile(r"promoted-offers")):
            continue
        title_node = card.select_one(".book-card__title")
        if not title_node:
            continue
        title = clean(title_node.get_text(" ", strip=True))
        href = title_node.get("href")
        if not href:
            continue
        href = canonical(urljoin(BASE, href))
        audio_link = card.select_one("a[href*='/audiobook/']")
        if result_type == "audiobook" and audio_link and audio_link.get("href"):
            href = canonical(urljoin(BASE, audio_link.get("href")))
        if not ("/ksiazka/" in urlparse(href).path or "/audiobook/" in urlparse(href).path):
            continue
        actual_type = "audiobook" if "/audiobook/" in urlparse(href).path else result_type
        key = (href.rstrip("/"), actual_type)
        if key in seen:
            continue
        seen.add(key)
        authors = search_author_from_card(card)
        # For audiobook search, the page itself is the authoritative type even if LC links to /ksiazka/.
        if result_type == "audiobook" and "/audiobook/" not in urlparse(href).path:
            actual_type = "audiobook"
        found.append({
            "url": href,
            "title": title or url_title(href),
            "authors": authors,
            "type": actual_type,
            "search_cover": card_cover(card),
        })

    # The reference provider relies on the result cards. We only use direct product links as a very small fallback.
    if not found:
        selector = "a[href*='/audiobook/']" if result_type == "audiobook" else "a[href*='/ksiazka/']"
        for link in soup.select(selector)[:30]:
            href = link.get("href")
            if not href:
                continue
            href = canonical(urljoin(BASE, href))
            actual_type = result_type
            key = (href.rstrip("/"), actual_type)
            if key in seen:
                continue
            seen.add(key)
            parent = link.find_parent(class_=re.compile(r"book-card"))
            found.append({
                "url": href,
                "title": clean(link.get_text(" ", strip=True)) or url_title(href),
                "authors": search_author_from_card(parent) if parent else [],
                "type": actual_type,
                "search_cover": card_cover(parent) if parent else None,
            })

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


def parse_detail_html(html, candidate):
    soup = BeautifulSoup(html, "html.parser")
    media_type = candidate.get("type", "book")
    data = {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "description": None,
        "cover": candidate.get("search_cover"),
        "isbn": None,
        "duration": None,
        "series": None,
        "sequence": None,
        "language": "pol",
        "type": media_type,
        "url": candidate["url"],
    }

    data["title"] = clean((soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else None) or data["title"])
    desc = soup.select_one("#book-description")
    data["description"] = strip_html(desc.decode_contents()) if desc else None
    if not data["description"]:
        og_desc = soup.select_one("meta[property='og:description']")
        data["description"] = clean(og_desc.get("content")) if og_desc else None

    cover = first_nonempty(
        soup.select_one("a#js-lightboxCover"),
        soup.select_one(".book-cover__link"),
    )
    if cover:
        cover = cover.get("href")
    else:
        og = soup.select_one("meta[property='og:image']")
        cover = og.get("content") if og else None
    if cover:
        data["cover"] = urljoin(BASE, clean(cover))

    pub = soup.select_one("[data-ga-book-publishers]")
    if pub:
        data["publisher"] = clean(pub.get("data-ga-book-publishers"))
    if not data["publisher"]:
        for node in soup.select("span.book__txt"):
            txt = clean(node.get_text(" ", strip=True))
            if txt and "Wydawnictwo" in txt:
                link = node.find("a")
                data["publisher"] = clean(link.get_text(" ", strip=True) if link else txt.split(":", 1)[-1])
                break
    data["publisher"] = data["publisher"] or text_after_label(soup, ["Wydawnictwo", "Wydawca"])

    isbn = soup.select_one("meta[property='books:isbn']")
    data["isbn"] = clean(isbn.get("content")) if isbn else None
    if not data["isbn"]:
        m = re.search(r"\b97[89]\d{10}\b", soup.get_text(" ", strip=True))
        data["isbn"] = m.group(0) if m else None

    data["language"] = text_after_label(soup, ["Język", "Języki"]) or "pol"
    data["publishedYear"] = parse_year(text_after_label(soup, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery"]))
    data["narrator"] = text_after_label(soup, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    data["duration"] = parse_duration(text_after_label(soup, ["Długość", "Czas trwania", "Czas trwania audiobooka", "Czas czytania", "Czas"]))

    series_text = text_after_label(soup, ["Cykl", "Seria"])
    if series_text:
        match = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)\)", series_text, re.I)
        data["series"] = clean(match.group(1) if match else series_text)
        data["sequence"] = match.group(2) if match else None

    for obj in parse_jsonld_scripts(soup):
        typ = obj.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if not any(x in {"Book", "Audiobook", "Product"} for x in types):
            continue
        data["title"] = clean(obj.get("name")) or data["title"]
        data["author"] = first_name(obj.get("author")) or data["author"]
        data["publisher"] = first_name(obj.get("publisher")) or data["publisher"]
        data["description"] = data["description"] or strip_html(obj.get("description"))
        data["isbn"] = data["isbn"] or clean(obj.get("isbn") or obj.get("productID"))
        image = obj.get("image") or obj.get("thumbnailUrl")
        if isinstance(image, list): image = image[0] if image else None
        if isinstance(image, dict): image = image.get("url")
        data["cover"] = data["cover"] or (urljoin(BASE, str(image)) if image else None)
        if media_type == "audiobook":
            data["narrator"] = data["narrator"] or first_name(obj.get("readBy") or obj.get("reader") or obj.get("narrator"))
            data["duration"] = data["duration"] or parse_duration(obj.get("duration") or obj.get("timeRequired"))
        break

    if not data["author"]:
        data["author"] = ", ".join(candidate.get("authors") or []) or None
    return data


async def detail_one(candidate):
    html, status = await fetch_html(candidate["url"])
    if html:
        data = parse_detail_html(html, candidate)
        print(
            f"[Lubimyczytać] detail: type={data['type']} cover={'yes' if data.get('cover') else 'no'} "
            f"description={len(data.get('description') or '')}chars publisher={data.get('publisher') or '-'} "
            f"narrator={data.get('narrator') or '-'} duration={data.get('duration') or '-'} "
            f"year={data.get('publishedYear') or '-'} url={candidate['url']}"
        )
        return data

    print(f"[Lubimyczytać] detail HTTP {status or '-'}: {candidate['url']}")
    return {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None, "publisher": None, "publishedYear": None,
        "description": None, "cover": candidate.get("search_cover"), "isbn": None,
        "duration": None, "series": None, "sequence": None, "language": "pol",
        "type": candidate.get("type", "book"), "url": candidate["url"],
    }


def score(data, query, author, candidate_authors):
    title_s = similarity(data.get("title"), query)
    values = ([data["author"]] if data.get("author") else []) + (candidate_authors or [])
    author_s = max((similarity(x, author) for x in values if x), default=0.0) if author else 1.0
    return title_s * 0.60 + author_s * 0.40 if author else title_s


def to_match(data, value):
    description = data.get("description") or None
    return {
        "title": data.get("title"),
        "author": data.get("author"),
        "narrator": data.get("narrator"),
        "publisher": data.get("publisher"),
        "publishedYear": data.get("publishedYear"),
        "description": description,
        "cover": data.get("cover"),
        "isbn": data.get("isbn"),
        "genres": None,
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

    # Same architecture as lakafior provider: two fast HTTP searches, no Playwright for search or detail.
    books_task = search_page(query, author, "ksiazki", "book")
    audio_task = search_page(query, author, "audiobooki", "audiobook")
    books, audiobooks = await asyncio.gather(books_task, audio_task)

    matches = books + audiobooks
    # preserve same URL when it appears in both sections, but keep the type from the section
    unique = {}
    for item in matches:
        unique[(item["url"].rstrip("/"), item["type"])] = item
    matches = list(unique.values())

    for item in matches:
        item["similarity"] = score(item, query, author, item.get("authors"))
    matches.sort(key=lambda x: (x["similarity"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    matches = matches[:20]

    # Match reference provider: fetch full metadata for the 20 ranked items in parallel.
    full = await asyncio.gather(*(detail_one(item) for item in matches))
    ranked = []
    for candidate, data in zip(matches, full):
        value = score(data, query, author, candidate.get("authors"))
        ranked.append((value, data))
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
    try:
        return JSONResponse(await lubimyczytac_search(query, author))
    except Exception as exc:
        print(f"[Lubimyczytać] search failed: {type(exc).__name__}: {exc}")
        return JSONResponse({"matches": [], "error": str(exc)}, status_code=200)
