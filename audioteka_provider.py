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
PL = f"{BASE}/pl"
SEARCH = f"{PL}/szukaj/"
CACHE_TTL = 600
MAX_RESULTS = 10

_pw = None
_browser = None
_context = None
_browser_lock = asyncio.Lock()
_cache = {}

BLOCKED_SERIES_SLUGS = {
    "nowosci", "oferty-specjalne", "bestsellery", "zapowiedzi",
    "cykle-audiobookow", "kategorie", "fantastyka", "kryminał",
    "kryminal", "romans", "literatura-piekna",
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


def strip_audioserial(value):
    return clean(re.sub(r"\s*[.\-:]?\s*audioserial\b", "", value or "", flags=re.I))


def slugify(value):
    return norm(value).replace(" ", "-")


def canonical(url):
    parsed = urlparse(url)
    return f"{BASE}{parsed.path.rstrip('/')}/"


def is_audiobook(url):
    return urlparse(url).path.rstrip("/").startswith("/pl/audiobook/")


def is_series(url):
    return urlparse(url).path.rstrip("/").startswith("/pl/cykl/")


def is_blocked_url(url):
    if not is_series(url):
        return False
    slug = urlparse(url).path.rstrip("/").split("/")[-1].lower()
    return slug in BLOCKED_SERIES_SLUGS


def parse_duration(value):
    text = str(value or "")
    h = re.search(r"(\d+)\s*(?:godz\.?|godziny|h)", text, re.I)
    m = re.search(r"(\d+)\s*(?:min\.?|m)\b", text, re.I)
    if not h and not m:
        return None
    return int(h.group(1)) * 60 + int(m.group(1) if m else 0) if h else int(m.group(1))


def parse_year(value):
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else None


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


def first_person_name(value):
    if isinstance(value, dict):
        return clean(value.get("name"))
    if isinstance(value, list):
        names = [first_person_name(x) for x in value]
        names = [x for x in names if x]
        return ", ".join(dict.fromkeys(names)) or None
    return clean(value)


def jsonld_type(item):
    value = item.get("@type")
    if isinstance(value, list):
        return {str(x).lower() for x in value}
    return {str(value).lower()} if value else set()


def extract_lines(text):
    return [clean(x) for x in str(text or "").splitlines() if clean(x)]


def extract_line_value(lines, labels):
    wanted = {norm(x) for x in labels}
    for i, line in enumerate(lines):
        if norm(line) not in wanted:
            continue
        for candidate in lines[i + 1:i + 5]:
            if candidate and norm(candidate) not in wanted and len(candidate) <= 180:
                return candidate
    return None


def extract_names_after_label(lines, labels, stop_labels, max_names=8):
    wanted = {norm(x) for x in labels}
    stops = {norm(x) for x in stop_labels}
    for i, line in enumerate(lines):
        if norm(line) not in wanted:
            continue
        names = []
        for candidate in lines[i + 1:]:
            n = norm(candidate)
            if n in stops:
                break
            if len(candidate) <= 100 and candidate not in names:
                names.append(candidate)
            if len(names) >= max_names:
                break
        return ", ".join(names) if names else None
    return None


def extract_isbn(text):
    match = re.search(r"\b(97[89]\d{10})\b", text or "")
    return match.group(1) if match else None


def extract_title_from_url(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-audioserial$", "", slug, flags=re.I)
    return slug.replace("-", " ").strip().title() if slug else None


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-audioserial$", "", slug, flags=re.I)
    return slug.replace("-", " ") if slug else ""


async def get_context():
    global _pw, _browser, _context
    async with _browser_lock:
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


async def open_page(page, url, wait=400):
    # Audioteka keeps background requests open; networkidle caused long hangs.
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(wait)
    for text in ("Akceptuję", "Zgadzam się", "Zaakceptuj"):
        try:
            button = page.get_by_role("button", name=re.compile(text, re.I)).first
            if await button.count() and await button.is_visible():
                await button.click(timeout=700)
                await page.wait_for_timeout(150)
                break
        except Exception:
            pass


async def page_links(page):
    hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    seen = set()
    result = []
    for href in hrefs:
        parsed = urlparse(href)
        if parsed.netloc not in ("audioteka.com", "www.audioteka.com"):
            continue
        if not parsed.path.startswith("/pl/"):
            continue
        if not (is_audiobook(href) or is_series(href)):
            continue
        url = canonical(href)
        if is_blocked_url(url):
            continue
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


async def search_page(page, query):
    url = f"{SEARCH}?phrase={quote(query)}"
    print(f"[Audioteka] search: {url}")
    await open_page(page, url, 450)
    for _ in range(2):
        await page.mouse.wheel(0, 1400)
        await page.wait_for_timeout(150)
    urls = await page_links(page)
    print(f"[Audioteka] search '{query}' -> {len(urls)} product/series URLs")
    return urls


async def direct_urls(page, query):
    slug = slugify(query)
    candidates = [
        f"{PL}/cykl/{slug}-audioserial/",
        f"{PL}/audiobook/{slug}/",
        f"{PL}/cykl/{slug}/",
        f"{PL}/audiobook/{slug}-audioserial/",
    ]
    for candidate in candidates:
        try:
            await open_page(page, candidate, 350)
            current = canonical(page.url)
            title = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
            page_title = (await page.title()).lower()
            valid = (
                page.url.startswith(BASE)
                and title
                and "404" not in page_title
                and "nie znaleziono" not in page_title
                and (is_audiobook(current) or is_series(current))
                and not is_blocked_url(current)
            )
            if valid:
                print(f"[Audioteka] direct candidate: {current}")
                return [current]
        except Exception as exc:
            print(f"[Audioteka] direct miss {candidate}: {type(exc).__name__}")
    return []


def rank_urls(urls, query):
    unique = list(dict.fromkeys(urls))
    q = norm(query)

    def rank(url):
        title_sim = similarity(url_title(url), query)
        exact = norm(url_title(url)) == q
        serial = "audioserial" in url.lower()
        return (0 if exact else 1, 0 if title_sim >= 0.90 else 1, -title_sim, 0 if serial else 1, 0 if is_series(url) else 1)

    return sorted([u for u in unique if not is_blocked_url(u)], key=rank)


async def first_selector_text(page, selectors, min_length=1):
    """Return the first useful visible text matching one of the supplied selectors."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                value = clean(await locator.inner_text())
                if value and len(value) >= min_length:
                    return value
        except Exception:
            pass
    return None


async def extract_description(page, series_page):
    # Audioteka uses different dedicated description containers for products
    # and series. Do not use generic body text: it can pick up reviews, footer,
    # recommendations, or other unrelated copy.
    if series_page:
        selectors = [
            "[class*='paragraph_description_']",
            "[class^='paragraph_description_']",
        ]
    else:
        selectors = [
            ".audiobook-description",
            "[class*='audiobook-description']",
        ]
    return await first_selector_text(page, selectors, min_length=20)


async def extract_published_year(page, lines, jsonld_year=None):
    if jsonld_year:
        return jsonld_year

    # Prefer explicit metadata attributes/meta tags before looking at rendered labels.
    meta_selectors = [
        "meta[itemprop='datePublished']",
        "meta[property='article:published_time']",
        "meta[name='datePublished']",
        "meta[name='release_date']",
        "meta[name='publish_date']",
        "time[datetime]",
        "[itemprop='datePublished']",
    ]
    for selector in meta_selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                value = clean(await locator.get_attribute("content"))
                if not value:
                    value = clean(await locator.get_attribute("datetime"))
                if not value:
                    value = clean(await locator.inner_text())
                year = parse_year(value)
                if year:
                    return year
        except Exception:
            pass

    # Audioteka has used several visible labels over time.
    value = extract_line_value(
        lines,
        [
            "Data publikacji",
            "Data wydania",
            "Data premiery",
            "Rok publikacji",
            "Rok wydania",
            "Rok premiery",
            "Premiera",
            "Wydano",
        ],
    )
    return parse_year(value)


async def parse_product(page, url, query, author="", enrich_series=False):
    await open_page(page, url, 450)
    body = await page.locator("body").inner_text()
    lines = extract_lines(body)
    text = re.sub(r"\s+", " ", clean(body) or "")
    series_page = is_series(url)

    data = {
        "title": None, "author": None, "narrator": None, "publisher": None,
        "publishedYear": None, "description": None, "cover": None,
        "isbn": None, "duration": None, "genres": [], "series": None,
        "url": canonical(url), "is_series": series_page,
    }

    candidates = []
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        candidates.extend(jsonld_objects(raw))

    target = None
    best = -1.0
    for item in candidates:
        types = jsonld_type(item)
        name = clean(item.get("name"))
        if not name:
            continue
        if not (types & {"product", "book", "audiobook", "creativework", "audiobookseries"}):
            continue
        s = similarity(strip_audioserial(name), query)
        if s > best:
            best = s
            target = item

    if target and best >= 0.60:
        data["title"] = clean(target.get("name"))
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
        data["author"] = first_person_name(target.get("author"))
        data["narrator"] = first_person_name(target.get("readBy")) or first_person_name(target.get("actor"))
        genre = target.get("genre")
        if isinstance(genre, list):
            data["genres"] = [clean(x) for x in genre if clean(x)]
        elif genre:
            data["genres"] = [clean(genre)]

    h1 = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
    og_title = None
    og_description = None
    og_image = None
    for selector, key in (("meta[property='og:title']", "title"), ("meta[property='og:description']", "description"), ("meta[property='og:image']", "image")):
        try:
            value = clean(await page.locator(selector).get_attribute("content"))
            if key == "title": og_title = value
            elif key == "description": og_description = value
            else: og_image = value
        except Exception:
            pass

    if series_page:
        title = None
        for candidate_title in (og_title, data["title"], h1):
            if candidate_title and similarity(strip_audioserial(candidate_title), query) >= 0.70:
                title = strip_audioserial(candidate_title)
                break
        data["title"] = title or query or extract_title_from_url(url)
    else:
        data["title"] = h1 or og_title or data["title"] or extract_title_from_url(url) or query

    if data["title"] in {"Cały sezon już dostępny!", "Cały sezon już dostępny"}:
        data["title"] = query or extract_title_from_url(url)

    # Use Audioteka's dedicated description blocks first, then JSON-LD/OG fallback.
    dedicated_description = await extract_description(page, series_page)
    data["description"] = dedicated_description or data["description"] or og_description
    data["cover"] = data["cover"] or og_image
    data["publishedYear"] = await extract_published_year(page, lines, data["publishedYear"])
    data["author"] = data["author"] or extract_line_value(lines, ["Autor", "Autorzy", "Scenariusz"])
    data["publisher"] = data["publisher"] or extract_line_value(lines, ["Wydawca"])
    data["narrator"] = data["narrator"] or extract_names_after_label(lines, ["Głosy", "Lektor", "Czyta"], ["Długość", "Wydawca", "Typ", "Format", "Język", "Kategoria", "Opis"])

    if not data["duration"]:
        data["duration"] = parse_duration(extract_line_value(lines, ["Długość"]))
    if not data["isbn"] and not series_page:
        data["isbn"] = extract_isbn(text)

    if author and not data.get("author"):
        data["author"] = author

    if series_page:
        data["series"] = data["title"]
        data["title"] = query or data["title"]

        if enrich_series:
            episode_urls = [u for u in await page_links(page) if is_audiobook(u) and similarity(url_title(u), query) >= 0.35]
            if episode_urls:
                episode_page = await page.context.new_page()
                try:
                    episode = await asyncio.wait_for(parse_product(episode_page, episode_urls[0], query, author, False), timeout=12)
                    for key in ("author", "narrator", "publisher", "publishedYear", "description", "cover", "isbn", "duration", "genres"):
                        if not data.get(key) and episode.get(key):
                            data[key] = episode[key]
                except Exception as exc:
                    print(f"[Audioteka] series episode enrichment failed: {type(exc).__name__}: {exc}")
                finally:
                    await episode_page.close()

    return data


def score(data, query, author):
    title_score = max(similarity(data.get("title"), query), similarity(strip_audioserial(data.get("title")), query))
    author_score = similarity(data.get("author"), author) if author else 1.0
    if author and not data.get("author"):
        author_score = 0.55
    result = title_score * 0.78 + author_score * 0.22 if author else title_score
    if data.get("is_series") and title_score >= 0.90:
        result += 0.03
    return min(1.0, result)


async def audioteka_search(query, author=""):
    key = f"audioteka|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    context = await get_context()
    page = await context.new_page()
    try:
        urls = await direct_urls(page, query)
    finally:
        await page.close()

    if urls:
        urls = rank_urls(urls, query)
    else:
        page = await context.new_page()
        try:
            urls = await search_page(page, query)
        finally:
            await page.close()
        urls = rank_urls(urls, query)

        if author and (not urls or similarity(url_title(urls[0]), query) < 0.75):
            page = await context.new_page()
            try:
                urls = rank_urls(urls + await search_page(page, f"{query} {author}"), query)
            finally:
                await page.close()

    urls = urls[:5]
    print(f"[Audioteka] candidates to parse: {len(urls)}")

    parsed = []
    for url in urls:
        page = await context.new_page()
        try:
            data = await asyncio.wait_for(parse_product(page, url, query, author, is_series(url)), timeout=18)
            if data.get("title"):
                data["similarity"] = round(score(data, query, author), 4)
                parsed.append(data)
                print(f"[Audioteka] parsed: {data['title']} / {data.get('author')} series={data.get('is_series')} score={data['similarity']:.3f} url={url}")
        except Exception as exc:
            print(f"[Audioteka] parse failed {url}: {type(exc).__name__}: {exc}")
        finally:
            await page.close()

    filtered = []
    for data in parsed:
        if similarity(data.get("title"), query) < 0.45:
            continue
        if author and data.get("author") and similarity(data["author"], author) < 0.40:
            continue
        filtered.append(data)

    filtered.sort(key=lambda x: -x["similarity"])
    unique = []
    seen = set()
    for data in filtered:
        if data.get("url") in seen:
            continue
        seen.add(data.get("url"))
        unique.append(data)

    matches = []
    for data in unique[:MAX_RESULTS]:
        matches.append({
            "title": data.get("title"),
            "author": data.get("author"),
            "narrator": data.get("narrator"),
            "publisher": data.get("publisher") or "Audioteka",
            "publishedYear": data.get("publishedYear"),
            "description": data.get("description"),
            "cover": data.get("cover"),
            "isbn": data.get("isbn"),
            "genres": list(dict.fromkeys(x for x in data.get("genres", []) if x)) or None,
            "series": [{"series": data["series"], "sequence": None}] if data.get("series") else None,
            "language": "pol",
            "duration": data.get("duration"),
            "type": "audiobook",
            "similarity": data["similarity"],
        })

    result = {"matches": matches}
    print("[Audioteka] final:", " | ".join(f"{x['title']}/{x.get('author')} ({x['similarity']:.3f})" for x in matches) or "<none>")
    _cache[key] = (time.time(), result)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "audioteka-pl"}


@app.get("/search")
async def search(query: str = Query(..., min_length=1), author: str = Query(""), authorization: str | None = Header(default=None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(await audioteka_search(query, author))


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
