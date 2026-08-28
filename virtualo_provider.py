import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="Virtualo Polska Metadata Provider")
BASE = "https://virtualo.pl"
CACHE_TTL = 600
MAX_RESULTS = 10
_http = None
_lock = asyncio.Lock()
_cache = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
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


def clean_product_title(value):
    """Remove Virtualo's format suffix while keeping the real book title."""
    value = clean(value)
    if not value:
        return None
    # Examples: "Operacja Mir - audiobook", "Operacja Mir - Audiobook (Książka audio MP3)",
    # "Operacja Mir - ebook" and similar Virtualo product labels.
    value = re.sub(
        r"\s*[-–—|]\s*(?:audiobook|ebook|e-book)\b.*$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s*\((?:audiobook|ebook|e-book)\)\s*$", "", value, flags=re.I)
    return clean(value)


def parse_year(value):
    m = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return m.group(0) if m else None


def parse_duration(value):
    text = str(value or "")
    h = re.search(r"(\d+)\s*(?:godz\.?|godziny|godzin|h)\b", text, re.I)
    m = re.search(r"(\d+)\s*(?:min\.?|minut|m)\b", text, re.I)
    if h:
        return int(h.group(1)) * 60 + int(m.group(1) if m else 0)
    return int(m.group(1)) if m else None


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
        value = value.get("name")
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(person_names(item))
        return list(dict.fromkeys(out))
    value = clean(value)
    return [value] if value else []


def canonical(url):
    parsed = urlparse(url)
    if parsed.netloc not in {"virtualo.pl", "www.virtualo.pl", ""}:
        return None
    path = parsed.path.rstrip("/")
    if not (path.startswith("/audiobook/") or path.startswith("/ebook/")):
        return None
    return urljoin(BASE, path + "/")


def product_type(url):
    return "audiobook" if "/audiobook/" in urlparse(url).path else "book"


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-i\d+$", "", slug, flags=re.I)
    return clean_product_title(slug.replace("-", " ")) or ""


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
            response = await client.get(url, headers={"Referer": BASE + "/"})
            if response.status_code == 429:
                await asyncio.sleep(attempt)
                continue
            response.raise_for_status()
            return response.text
        except Exception as exc:
            if attempt == 2:
                print(f"[Virtualo] HTTP failed: {url} {type(exc).__name__}: {exc}")
            else:
                await asyncio.sleep(0.25 * attempt)
    return None


async def url_exists(url):
    """Check a CDN cover URL without downloading the whole image."""
    client = await get_http()
    try:
        response = await client.head(url, headers={"Referer": BASE + "/"}, follow_redirects=True)
        if response.status_code in {200, 204, 206}:
            return True
        if response.status_code == 405:
            response = await client.get(
                url,
                headers={"Referer": BASE + "/", "Range": "bytes=0-0"},
                follow_redirects=True,
            )
            return response.status_code in {200, 206}
    except Exception:
        pass
    return False


async def resolve_cover(candidates):
    """Prefer high > medium > small and use the first reachable Virtualo CDN image."""
    for url in candidates:
        if await url_exists(url):
            return url
    return candidates[-1] if candidates else None


def cover_candidates_from_page(soup):
    """Read Virtualo's data-interchange image list and add a high-res candidate."""
    candidates = []

    for image in soup.select("img[data-interchange]"):
        raw = image.get("data-interchange") or ""
        urls = re.findall(r"https?://[^\s,\]]+", raw)
        for url in urls:
            url = url.rstrip("]),")
            if url not in candidates:
                candidates.append(url)

        # Virtualo currently publishes medium/small in data-interchange.
        # Try the same file in /high/ first, then keep the advertised sizes.
        for url in list(candidates):
            if "/covers/" in url and re.search(r"/covers/(?:medium|small)/", url):
                high = re.sub(r"/covers/(?:medium|small)/", "/covers/high/", url, count=1)
                if high not in candidates:
                    candidates.insert(0, high)

        if candidates:
            break

    # Also support plain src/srcset/og:image if data-interchange is unavailable.
    if not candidates:
        for image in soup.select("img[src], img[srcset]"):
            for attr in ("src", "srcset"):
                value = image.get(attr)
                if not value:
                    continue
                if attr == "srcset":
                    value = value.split(",")[0].strip().split()[0]
                if value.startswith("http") and "/covers/" in value:
                    candidates.append(value)
                    break
            if candidates:
                break

    # Ensure order is explicitly high -> medium -> small.
    def rank(url):
        if "/covers/high/" in url:
            return 0
        if "/covers/medium/" in url:
            return 1
        if "/covers/small/" in url:
            return 2
        return 3

    return list(dict.fromkeys(sorted(candidates, key=rank)))


def card_container(link):
    node = link
    for _ in range(8):
        parent = getattr(node, "parent", None)
        if not parent:
            break
        node = parent
        text = clean(node.get_text(" ", strip=True)) or ""
        if len(text) >= 60 and ("Wydawnictwo:" in text or "EBOOK:" in text or "AUDIOBOOK:" in text):
            return node
    return link.parent or link


def parse_search_html(html):
    soup = BeautifulSoup(html, "html.parser")
    found, seen = [], set()
    for link in soup.select("a[href*='/audiobook/'], a[href*='/ebook/']"):
        href = canonical(urljoin(BASE, link.get("href") or ""))
        if not href or href in seen:
            continue
        card = card_container(link)
        text = clean(card.get_text(" ", strip=True)) or ""
        if "AUDIOBOOK:" not in text and "EBOOK:" not in text:
            continue
        seen.add(href)
        title = None
        for selector in ("h1", "h2", "h3", "h4", "[class*='title']"):
            node = card.select_one(selector)
            value = clean(node.get_text(" ", strip=True)) if node else None
            if value and len(value) < 180:
                title = value
                break
        title = clean_product_title(title or clean(link.get_text(" ", strip=True)) or url_title(href))
        authors = []
        for node in card.select("a[href*='/autor/']"):
            value = clean(node.get_text(" ", strip=True))
            if value:
                authors.append(value)
        found.append({"url": href, "title": title, "authors": list(dict.fromkeys(authors)), "type": product_type(href)})
    print(f"[Virtualo] search -> {len(found)} product URLs")
    return found


def label_node(soup, label):
    wanted = norm(label).rstrip(":")
    for node in soup.find_all(["p", "div", "span", "strong", "b", "dt", "th"]):
        if norm(clean(node.get_text(" ", strip=True))).rstrip(":") == wanted:
            return node
    return None


def label_value(soup, *labels):
    for label in labels:
        node = label_node(soup, label)
        if not node:
            continue
        sibling = node.find_next_sibling()
        if sibling:
            value = clean(sibling.get_text(" ", strip=True))
            if value and norm(value) != norm(label):
                return value
        parent = node.parent
        if parent:
            children = [x for x in parent.find_all(recursive=False) if getattr(x, "name", None)]
            if node in children:
                index = children.index(node)
                if index + 1 < len(children):
                    value = clean(children[index + 1].get_text(" ", strip=True))
                    if value:
                        return value
    return None


def links_after_label(soup, *labels):
    for label in labels:
        node = label_node(soup, label)
        if not node:
            continue
        parent = node.parent or node
        values = [clean(a.get_text(" ", strip=True)) for a in parent.select("a[href]")]
        values = [x for x in values if x and norm(x) != norm(label)]
        if values:
            return list(dict.fromkeys(values))
    return []


def description_from_page(soup):
    """Extract the complete Virtualo description tab instead of the short meta description."""
    patterns = (
        r"^opis(?:\s+(?:audiobooka|e-booka|ebooka))?$",
        r"^opis$",
    )

    for text_node in soup.find_all(string=True):
        text = clean(text_node)
        if not text or not any(re.match(pattern, text, re.I) for pattern in patterns):
            continue

        start = text_node.parent
        chunks = []
        started = False
        seen_title = False

        # Walk the document after the description tab label. Stop at the next
        # metadata section so unrelated page content is never appended.
        for element in start.next_elements:
            if isinstance(element, NavigableString):
                value = clean(str(element))
                if not value:
                    continue
                normalized = norm(value)
                if normalized in {"szczegoly", "opis audiobooka", "opis ebooka", "opis e booka"}:
                    if normalized == "szczegoly" and not chunks:
                        continue
                    continue
                if normalized.startswith("cena virtualo") or normalized == "bestsellery":
                    break
                if normalized.startswith("kategoria:") or normalized.startswith("zabezpieczenie:"):
                    break
                if normalized.startswith("isbn:") or normalized.startswith("rozmiar pliku:"):
                    break
                if not started:
                    started = True
                if not seen_title and len(value) < 180:
                    # The product title is repeated at the beginning of the tab.
                    if re.search(r"\s[-–—]\s*(?:audiobook|ebook|e-book)\b", value, re.I):
                        seen_title = True
                        continue
                chunks.append(value)

        # Avoid returning a huge accidental page fragment.
        if chunks:
            result = clean(" ".join(dict.fromkeys(chunks)))
            if len(result or "") >= 80:
                return result

    # Fallbacks only when the dedicated description tab could not be found.
    for selector in ("[itemprop='description']", "meta[property='og:description']", "meta[name='description']"):
        node = soup.select_one(selector)
        if node:
            value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
            if clean(value):
                return clean(value)
    return None


def parse_detail(html, candidate):
    soup = BeautifulSoup(html, "html.parser")
    data = {
        "title": clean_product_title(candidate.get("title")),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "description": description_from_page(soup),
        "cover": None,
        "coverCandidates": cover_candidates_from_page(soup),
        "isbn": None,
        "duration": None,
        "genres": [],
        "series": None,
        "sequence": None,
        "language": "pol",
        "type": candidate.get("type", "book"),
        "url": candidate["url"],
    }

    h1 = soup.select_one("h1")
    if h1:
        data["title"] = clean_product_title(h1.get_text(" ", strip=True)) or data["title"]

    authors = links_after_label(soup, "Autor", "Autorka")
    if authors:
        data["author"] = ", ".join(authors)
    elif not data["author"]:
        authors = [clean(a.get_text(" ", strip=True)) for a in soup.select("a[href*='/autor/']")]
        data["author"] = ", ".join(dict.fromkeys(x for x in authors if x)) or None

    data["publisher"] = label_value(soup, "Wydawnictwo", "Wydawca")
    narrators = links_after_label(soup, "Czyta", "Lektor")
    data["narrator"] = ", ".join(narrators) if narrators else label_value(soup, "Czyta", "Lektor")
    data["publishedYear"] = parse_year(label_value(soup, "Data wydania", "Data publikacji"))
    data["duration"] = parse_duration(label_value(soup, "Czas"))

    category = label_value(soup, "Kategoria", "Kategorie")
    if category:
        data["genres"] = [x.strip() for x in re.split(r"[,;|]", category) if x.strip()]

    language = label_value(soup, "Język")
    if language and norm(language) not in {"polski", "pl"}:
        data["language"] = language

    full_text = soup.get_text(" ", strip=True)
    isbn = re.search(r"(?:ISBN\s*:?[\s-]*)?(97[89][\d -]{10,17})\b", full_text, re.I)
    if isbn:
        data["isbn"] = re.sub(r"[^0-9]", "", isbn.group(1))

    for obj in jsonld_objects(soup):
        types = obj.get("@type")
        types = {str(x).lower() for x in (types if isinstance(types, list) else [types])}
        if not types & {"book", "audiobook", "product", "creativework"}:
            continue
        data["title"] = clean_product_title(obj.get("name")) or data["title"]
        data["description"] = data["description"] or clean(obj.get("description"))
        data["author"] = data["author"] or ", ".join(person_names(obj.get("author"))) or None
        publisher = obj.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        data["publisher"] = data["publisher"] or clean(publisher)
        data["isbn"] = data["isbn"] or clean(obj.get("isbn") or obj.get("productID"))
        data["publishedYear"] = data["publishedYear"] or parse_year(obj.get("datePublished"))
        genre = obj.get("genre")
        if isinstance(genre, list):
            data["genres"].extend(clean(x) for x in genre if clean(x))
        elif genre:
            data["genres"].append(clean(genre))

    # Only use JSON-LD/og:image as a fallback. Virtualo's data-interchange
    # contains the actual cover variants and is preferred.
    if not data["coverCandidates"]:
        og = soup.select_one("meta[property='og:image']")
        if og and og.get("content"):
            data["coverCandidates"] = [urljoin(BASE, og["content"])]

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
        "type": data.get("type"),
        "url": data.get("url"),
        "similarity": round(score, 3),
    }


async def virtualo_search(query, author=""):
    key = f"virtualo|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        print(f"[Virtualo] cache hit: {key}")
        return cached[1]

    url = f"{BASE}/?q={quote_plus(query)}"
    print(f"[Virtualo] search: {url}")
    html = await fetch_html(url)
    if not html:
        return {"matches": []}

    candidates = parse_search_html(html)
    candidates.sort(
        key=lambda x: (
            similarity(x.get("title"), query) * 0.75
            + similarity(", ".join(x.get("authors") or []), author) * 0.25
            if author
            else similarity(x.get("title"), query)
        ),
        reverse=True,
    )
    candidates = candidates[:20]
    print(f"[Virtualo] candidates to parse: {len(candidates)}")

    async def enrich(item):
        detail_html = await fetch_html(item["url"])
        if not detail_html:
            return None
        try:
            data = parse_detail(detail_html, item)
            data["cover"] = await resolve_cover(data.get("coverCandidates") or [])

            title_score = similarity(data.get("title"), query)
            author_score = similarity(data.get("author"), author) if author else 1.0
            score = title_score * 0.75 + author_score * 0.25 if author else title_score
            if data.get("language") == "pol":
                score = min(1.0, score + 0.02)
            print(
                f"[Virtualo] detail: {data.get('title')} / {data.get('author')} score={score:.3f} "
                f"type={data.get('type')} narrator={data.get('narrator')} publisher={data.get('publisher')} "
                f"year={data.get('publishedYear')} isbn={data.get('isbn')} duration={data.get('duration')} "
                f"genres={data.get('genres')} cover={data.get('cover')} url={data.get('url')}"
            )
            return score, data
        except Exception as exc:
            print(f"[Virtualo] detail failed: {item['url']} {type(exc).__name__}: {exc}")
            return None

    enriched = []
    for i in range(0, len(candidates), 8):
        enriched.extend(x for x in await asyncio.gather(*(enrich(x) for x in candidates[i:i + 8])) if x)

    enriched.sort(key=lambda x: x[0], reverse=True)
    final = [match_result(data, score) for score, data in enriched if score >= 0.55][:MAX_RESULTS]
    result = {"matches": final}
    print("[Virtualo] final:", " | ".join(f"{x['title']}/{x['author']} [{x['similarity']:.3f}]" for x in final))
    if final:
        _cache[key] = (time.time(), result)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "virtualo"}


@app.get("/search")
async def search(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
    if authorization is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    return JSONResponse(await virtualo_search(query, author))
