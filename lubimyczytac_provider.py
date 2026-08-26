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
        if norm(line) not in wanted:
            continue
        for candidate in lines[i + 1:i + 5]:
            if candidate and norm(candidate) not in wanted:
                return candidate
    return None


# Backwards-compatible name used by lubimyczytac_patch.py.
# Keep one implementation so the provider and patch cannot drift apart.
def label_value(lines, labels):
    return value_after_label(lines, labels)


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


async def open_page(page, url, wait=250):
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(wait)


async def search_page(page, query, section):
    url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    print(f"[Lubimyczytać] search: {url}")
    await open_page(page, url, 300)

    # Exact card parsing: do not collect navigation links from the whole page.
    cards = page.locator(".book-card--l")
    found, seen = [], set()
    for i in range(await cards.count()):
        card = cards.nth(i)
        try:
            link = card.locator(".book-card__title[href]").first
            if not await link.count():
                link = card.locator("a[href*='/ksiazka/'], a[href*='/audiobook/']").first
            href = await link.get_attribute("href") if await link.count() else None
            title_loc = card.locator(".book-card__title").first
            title = clean(await title_loc.text_content()) if await title_loc.count() else None
            if not href or not title:
                continue
            href = href if href.startswith("http") else f"{BASE}{href}"
            href = canonical(href)
            if not is_product_url(href) or href in seen:
                continue
            authors = [clean(x) for x in await card.locator(".book-card__author a, a[href*='/autor/']").all_text_contents() if clean(x)]
            seen.add(href)
            found.append({"url": href, "title": title, "authors": list(dict.fromkeys(authors)), "type": path_type(href)})
        except Exception:
            continue

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


async def parse_detail(page, candidate):
    url = candidate["url"]
    await open_page(page, url, 450)
    body = await page.locator("body").inner_text()
    lines = lines_from_body(body)
    item_type = path_type(url)
    data = {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None,
        "publisher": None,
        "publishedYear": None,
        "description": None,
        "cover": None,
        "isbn": None,
        "duration": None,
        "genres": [],
        "series": None,
        "sequence": None,
        "language": "pol",
        "url": canonical(url),
        "type": item_type,
    }

    h1 = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
    if h1:
        data["title"] = h1

    objects = []
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        objects.extend(jsonld_objects(raw))

    target = None
    best = -1.0
    current_title = data.get("title") or candidate.get("title") or url_title(url)
    for item in objects:
        name = clean(item.get("name"))
        if not name:
            continue
        s = similarity(name, current_title)
        if s > best:
            target, best = item, s

    if target and best >= 0.85:
        data["title"] = clean(target.get("name")) or data["title"]
        json_author = first_name(target.get("author"))
        if json_author:
            data["author"] = json_author
        data["description"] = clean(target.get("description")) or data["description"]
        data["isbn"] = clean(target.get("isbn")) or data["isbn"]
        data["publishedYear"] = parse_year(target.get("datePublished")) or data["publishedYear"]
        data["duration"] = parse_duration(target.get("duration")) or data["duration"]
        image = target.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        data["cover"] = clean(image) or data["cover"]
        pub = target.get("publisher")
        if isinstance(pub, dict):
            pub = pub.get("name")
        data["publisher"] = clean(pub) or data["publisher"]
        genre = target.get("genre")
        if isinstance(genre, list):
            data["genres"] = [clean(x) for x in genre if clean(x)]
        elif genre:
            data["genres"] = [clean(genre)]

    try:
        description = page.locator("#book-description").first
        if await description.count():
            await description.wait_for(state="attached", timeout=2500)
            value = clean(await description.text_content())
            if value:
                data["description"] = value
                print(f"[Lubimyczytać] description: chars={len(value)} type={data['type']} url={data['url']}")
    except Exception:
        pass

    for selector in (
        "a#js-lightboxCover[href]",
        ".book-cover__link[href]",
        "meta[property='og:image']",
        "meta[name='twitter:image']",
        "meta[itemprop='image']",
        "img.book-cover[src]",
    ):
        try:
            loc = page.locator(selector).first
            if not await loc.count():
                continue
            value = clean(await loc.get_attribute("href") or await loc.get_attribute("content") or await loc.get_attribute("src"))
            if value:
                if value.startswith("/"):
                    value = BASE + value
                data["cover"] = value
                break
        except Exception:
            pass

    detail_authors = []
    for selector in ("a.book__author", ".book__authors a[href*='/autor/']"):
        try:
            detail_authors += [clean(x) for x in await page.locator(selector).all_text_contents() if clean(x)]
        except Exception:
            pass
    if detail_authors:
        data["author"] = ", ".join(dict.fromkeys(detail_authors[:5]))

    data["publisher"] = data["publisher"] or label_value(lines, ["Wydawca", "Wydawnictwo"])
    data["publishedYear"] = data["publishedYear"] or parse_year(label_value(lines, ["Data pierwszego wydania", "Data wydania", "Data 1. wyd. pol.", "Data publikacji", "Data premiery", "Rok wydania"]))
    data["isbn"] = data["isbn"] or label_value(lines, ["ISBN"])
    data["duration"] = data["duration"] or parse_duration(label_value(lines, ["Czas czytania", "Długość", "Czas trwania"]))
    data["narrator"] = label_value(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"]) or data["narrator"]
    language = label_value(lines, ["Język"])
    data["language"] = "pol" if not language or norm(language) in {"polski", "polska", "pol"} else norm(language)
    category = label_value(lines, ["Kategoria", "Kategorie"])
    if category:
        data["genres"] = [clean(x) for x in re.split(r"[,;/]", category) if clean(x)]
    data["series"], data["sequence"] = series_from_lines(lines)

    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        data["isbn"] = m.group(1) if m else None
    return data


def score(data, query, author):
    title_score = similarity(data.get("title"), query)
    author_score = similarity(data.get("author"), author) if author else 1.0
    if author and not data.get("author"):
        author_score = 0.5
    value = title_score * 0.60 + author_score * 0.40 if author else title_score
    if author and author_score < 0.25:
        return 0.0, title_score, author_score
    if not data.get("isbn"):
        value *= 0.99
    if data.get("type") == "audiobook":
        value += 0.001
    return min(value, 1.0), title_score, author_score


def to_match(data, value):
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
        "language": data.get("language", "pol"),
        "duration": data.get("duration"),
        "type": data.get("type", "book"),
        "similarity": round(value, 3),
    }


async def lubimyczytac_search(query, author=""):
    key = f"lubimyczytac|{norm(query)}|{norm(author)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    context = await get_context()
    search = await context.new_page()
    try:
        book_cards = await search_page(search, query, "ksiazki")
        audiobook_cards = await search_page(search, query, "audiobooki")
        collected = book_cards + audiobook_cards
        if author:
            collected += await search_page(search, f"{query} {author}", "ksiazki")
            collected += await search_page(search, f"{query} {author}", "audiobooki")
    finally:
        await search.close()

    unique = {}
    for item in collected:
        unique.setdefault(item["url"], item)
    candidates = list(unique.values())

    for item in candidates:
        title_s = similarity(item.get("title"), query)
        authors = item.get("authors") or []
        author_s = max((similarity(x, author) for x in authors), default=0.5) if author else 1.0
        item["query"] = query
        item["pre_score"] = title_s * 0.60 + author_s * 0.40 if author else title_s

    candidates.sort(key=lambda x: (x["pre_score"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    candidates = candidates[:30]
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    sem = asyncio.Semaphore(8)

    async def parse_one(candidate):
        async with sem:
            page = await context.new_page()
            try:
                return await parse_detail(page, candidate)
            except Exception as exc:
                print(f"[Lubimyczytać] detail failed: {candidate['url']} {type(exc).__name__}: {exc}")
                return None
            finally:
                await page.close()

    parsed = await asyncio.gather(*(parse_one(c) for c in candidates))
    ranked = []
    for data in parsed:
        if not data:
            continue
        value, title_s, author_s = score(data, query, author)
        if value <= 0:
            continue
        ranked.append((value, title_s, author_s, data))
        print(f"[Lubimyczytać] parsed: {data.get('title')} / {data.get('author')} type={data.get('type')} score={value:.3f} url={data.get('url')}")

    ranked.sort(key=lambda x: (x[0], 1 if x[3].get("type") == "audiobook" else 0, x[1], x[2]), reverse=True)
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
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
