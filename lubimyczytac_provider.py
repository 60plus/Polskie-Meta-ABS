import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse, urljoin

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

app = FastAPI(title="LubimyCzytać Metadata Provider")
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


def canonical(url):
    parsed = urlparse(url)
    return f"{BASE}{parsed.path.rstrip('/')}/"


def is_product_url(url):
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
        if norm(line) in wanted:
            for candidate in lines[i + 1:i + 5]:
                if candidate and norm(candidate) not in wanted:
                    return candidate
    return None


def label_value(lines, labels):
    return value_after_label(lines, labels)


def jsonld_objects(raw):
    try:
        value = json.loads(raw)
    except Exception:
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
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
        names = [first_name(x) for x in value]
        return ", ".join(dict.fromkeys(x for x in names if x)) or None
    return clean(value)


def series_from_lines(lines):
    for i, line in enumerate(lines):
        if norm(line) in {"cykl", "seria"} and i + 1 < len(lines):
            value = lines[i + 1]
            m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)\)", value, re.I)
            return clean(m.group(1) if m else value), (m.group(2) if m else None)
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
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"},
        )
        return _context


async def open_page(page, url, wait=300):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(wait)


def extract_author_candidates(card):
    return card.locator(".book-card__author a")


async def search_page(page, query, author, section, result_type):
    url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    if author:
        url += f"&author={quote(author)}"
    print(f"[Lubimyczytać] search: {url}")
    await open_page(page, url, 300)

    found, seen = [], set()
    cards = page.locator(".book-card--l")
    count = await cards.count()

    for i in range(count):
        card = cards.nth(i)
        try:
            title = clean(await card.locator(".book-card__title").first.text_content())
            link = card.locator(".book-card__title[href]").first
            href = await link.get_attribute("href") if await link.count() else None
            if not href:
                href_loc = card.locator("a[href*='/audiobook/']").first if result_type == "audiobook" else card.locator("a[href*='/ksiazka/']").first
                href = await href_loc.get_attribute("href") if await href_loc.count() else None
            if not href:
                continue
            href = href if href.startswith("http") else f"{BASE}{href}"
            href = canonical(href)
            if not is_product_url(href) or href in seen:
                continue
            authors = [clean(x) for x in await extract_author_candidates(card).all_text_contents() if clean(x)]
            seen.add(href)
            found.append({"url": href, "title": title or url_title(href), "authors": list(dict.fromkeys(authors)), "type": result_type})
        except Exception:
            continue

    if not found:
        links = page.locator(f"a[href*='/{'audiobook' if result_type == 'audiobook' else 'ksiazka'}/']")
        for i in range(min(await links.count(), 20)):
            link = links.nth(i)
            try:
                href = await link.get_attribute("href")
                if not href:
                    continue
                href = href if href.startswith("http") else f"{BASE}{href}"
                href = canonical(href)
                if href in seen or not is_product_url(href):
                    continue
                title = clean(await link.text_content()) or url_title(href)
                seen.add(href)
                found.append({"url": href, "title": title, "authors": [], "type": result_type})
            except Exception:
                continue

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


async def _text_after(scope, selectors):
    for selector in selectors:
        try:
            loc = scope.locator(selector).first
            if await loc.count():
                text = clean(await loc.text_content())
                if text:
                    return text
        except Exception:
            pass
    return None


async def _attr_first(scope, selectors, attrs=("href", "content", "src", "data-src")):
    for selector in selectors:
        try:
            loc = scope.locator(selector).first
            if not await loc.count():
                continue
            for attr in attrs:
                value = await loc.get_attribute(attr)
                if value:
                    return urljoin(BASE + "/", value)
        except Exception:
            pass
    return None


async def parse_detail(page, candidate):
    url = candidate["url"]
    item_type = path_type(url)
    await open_page(page, url, 700)
    try:
        await page.locator("h1, #book-description, a#js-lightboxCover, meta[property='og:image']").first.wait_for(state="attached", timeout=4000)
    except Exception:
        pass

    body = await page.locator("body").inner_text(timeout=10000)
    lines = lines_from_body(body)
    data = {
        "title": candidate.get("title"), "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None, "publisher": None, "publishedYear": None, "description": None,
        "cover": None, "isbn": None, "duration": None, "genres": [], "series": None,
        "sequence": None, "language": "pol", "url": canonical(url), "type": item_type,
    }

    h1 = await _text_after(page, ["h1"])
    if h1:
        data["title"] = h1

    objects = []
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        objects.extend(jsonld_objects(raw))

    target = None
    best = -1.0
    current_path = urlparse(url).path.rstrip("/")
    for item in objects:
        name = clean(item.get("name"))
        if not name:
            continue
        item_url = str(item.get("url") or "")
        s = similarity(name, data["title"] or url_title(url))
        if item_url and urlparse(item_url).path.rstrip("/") == current_path:
            s += 2.0
        types = item.get("@type")
        type_text = " ".join(types) if isinstance(types, list) else str(types or "")
        if item_type == "audiobook" and re.search(r"audiobook|audio", type_text, re.I):
            s += 0.25
        if item_type == "book" and re.search(r"book", type_text, re.I):
            s += 0.10
        if s > best:
            best, target = s, item

    if target:
        data["title"] = clean(target.get("name")) or data["title"]
        data["author"] = first_name(target.get("author")) or data["author"]
        data["description"] = clean(target.get("description"))
        data["isbn"] = clean(target.get("isbn"))
        data["publishedYear"] = parse_year(target.get("datePublished"))
        data["duration"] = parse_duration(target.get("duration"))
        image = target.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        data["cover"] = urljoin(BASE + "/", image) if image else None
        pub = target.get("publisher")
        if isinstance(pub, dict):
            pub = pub.get("name")
        data["publisher"] = clean(pub)
        genre = target.get("genre")
        if isinstance(genre, list):
            data["genres"] = [clean(x) for x in genre if clean(x)]
        elif genre:
            data["genres"] = [clean(genre)]

    try:
        desc = page.locator("#book-description").first
        if await desc.count():
            value = clean(await desc.text_content())
            if value:
                data["description"] = value
                print(f"[Lubimyczytać] description: chars={len(value)} type={data['type']} url={data['url']}")
    except Exception:
        pass

    data["cover"] = await _attr_first(page, [
        "a#js-lightboxCover[href]", "a#js-lightboxCover img[src]",
        ".book-cover__link[href]", ".book-cover__link img[src]",
        "meta[property='og:image']", "meta[name='twitter:image']",
        "link[rel='image_src'][href]", "img.book-cover[src]", "img[src*='lubimyczytac']",
    ]) or data["cover"]

    names = []
    for selector in ("a.book__author", ".book__authors a[href*='/autor/']", "a[href*='/autor/']"):
        try:
            names += [clean(x) for x in await page.locator(selector).all_text_contents() if clean(x)]
        except Exception:
            pass
    if names:
        data["author"] = ", ".join(dict.fromkeys(names[:8]))

    data["publisher"] = data["publisher"] or await _text_after(page, [
        "span.book__txt:has-text('Wydawnictwo:') a", "[data-ga-book-publishers]"
    ]) or label_value(lines, ["Wydawca", "Wydawnictwo"])

    date_text = await _text_after(page, [
        "dt[title*='Data pierwszego wydania'] + dd", "dt[title*='Data wydania'] + dd",
        "dt:has-text('Data pierwszego wydania') + dd", "dt:has-text('Data wydania') + dd",
        "dt:has-text('Data publikacji') + dd", "dt:has-text('Data premiery') + dd",
    ])
    data["publishedYear"] = parse_year(date_text) or data["publishedYear"] or parse_year(label_value(lines, [
        "Data pierwszego wydania", "Data wydania", "Data 1. wyd. pol.", "Data publikacji", "Data premiery", "Rok wydania"
    ]))

    try:
        isbn = await page.locator("meta[property='books:isbn']").get_attribute("content")
        data["isbn"] = clean(isbn) or data["isbn"]
    except Exception:
        pass
    data["isbn"] = data["isbn"] or label_value(lines, ["ISBN"])
    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        data["isbn"] = m.group(1) if m else None

    data["duration"] = data["duration"] or parse_duration(label_value(lines, ["Czas czytania", "Długość", "Czas trwania"]))
    data["narrator"] = label_value(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    language = label_value(lines, ["Język"])
    data["language"] = "pol" if not language or norm(language) in {"polski", "polska", "pol"} else norm(language)

    category = label_value(lines, ["Kategoria", "Kategorie"])
    if category:
        data["genres"] = [clean(x) for x in re.split(r"[,;/]", category) if clean(x)]

    series_text = await _text_after(page, ["span.d-none.d-sm-block.mt-1:has-text('Cykl:') a", "a[href*='/cykl/']", "a[href*='/seria/']"])
    if series_text:
        m = re.match(r"(.+?)\s*\(tom\s+([0-9IVX]+)", series_text, re.I)
        if m:
            data["series"], data["sequence"] = clean(m.group(1)), m.group(2)
        else:
            data["series"] = clean(series_text)
    if not data["series"]:
        data["series"], data["sequence"] = series_from_lines(lines)

    return data


def score(data, query, author):
    title_s = similarity(data.get("title"), query)
    author_s = similarity(data.get("author"), author) if author else 1.0
    if author and not data.get("author"):
        author_s = 0.5
    combined = title_s * 0.60 + author_s * 0.40 if author else title_s
    if author and author_s < 0.25:
        return 0.0, title_s, author_s
    if not data.get("isbn"):
        combined *= 0.99
    return min(combined, 1.0), title_s, author_s


def to_match(data, value):
    return {
        "title": data.get("title"), "author": data.get("author"), "narrator": data.get("narrator"),
        "publisher": data.get("publisher"), "publishedYear": data.get("publishedYear"),
        "description": data.get("description"), "cover": data.get("cover"), "isbn": data.get("isbn"),
        "genres": data.get("genres") or None,
        "series": ([{"series": data["series"], "sequence": data.get("sequence")}] if data.get("series") else None),
        "language": data.get("language", "pol"), "duration": data.get("duration"),
        "type": data.get("type", "book"), "similarity": round(value, 3),
    }


async def lubimyczytac_search(query, author=""):
    key = f"lubimyczytac|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    context = await get_context()
    search = await context.new_page()
    try:
        books = await search_page(search, query, author, "ksiazki", "book")
        audiobooks = await search_page(search, query, author, "audiobooki", "audiobook")
    finally:
        await search.close()

    candidates = books + audiobooks
    unique = {}
    for item in candidates:
        unique.setdefault(item["url"], item)
    candidates = list(unique.values())

    for item in candidates:
        title_s = similarity(item.get("title"), query)
        author_s = max((similarity(x, author) for x in item.get("authors") or []), default=0.5) if author else 1.0
        item["pre_score"] = title_s * 0.60 + author_s * 0.40 if author else title_s

    candidates.sort(key=lambda x: (x["pre_score"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    candidates = candidates[:20]
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    sem = asyncio.Semaphore(8)
    async def parse_one(candidate):
        async with sem:
            page = await context.new_page()
            try:
                data = await parse_detail(page, candidate)
                value, title_s, author_s = score(data, query, author)
                return value, title_s, author_s, data
            except Exception as exc:
                print(f"[Lubimyczytać] detail failed: {candidate['url']} {type(exc).__name__}: {exc}")
                return None
            finally:
                await page.close()

    parsed = await asyncio.gather(*(parse_one(c) for c in candidates))
    ranked = []
    for result in parsed:
        if not result:
            continue
        value, title_s, author_s, data = result
        if value <= 0:
            continue
        ranked.append((value, title_s, author_s, data))
        print(f"[Lubimyczytać] parsed: {data.get('title')} / {data.get('author')} type={data.get('type')} score={value:.3f} url={data.get('url')}")

    ranked.sort(key=lambda x: (x[0], 1 if x[3].get("type") == "audiobook" else 0), reverse=True)
    final = [to_match(data, value) for value, _, _, data in ranked[:MAX_RESULTS]]
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
    return JSONResponse(await lubimyczytac_search(query, author))


@app.on_event("shutdown")
async def shutdown():
    global _context, _browser, _pw
    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None
    if _pw:
        await _pw.stop()
        _pw = None
