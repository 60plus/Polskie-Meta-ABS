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
            if isinstance(item.get("@graph"), list):
                result.extend(x for x in item["@graph"] if isinstance(x, dict))
    return result


def first_name(value):
    if isinstance(value, dict):
        return clean(value.get("name"))
    if isinstance(value, list):
        values = [first_name(x) for x in value]
        return ", ".join(dict.fromkeys(x for x in values if x)) or None
    return clean(value)


def canonical(url):
    p = urlparse(url)
    return f"{BASE}{p.path.rstrip('/')}/"


def valid_product_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.startswith("/ksiazka/") or path.startswith("/audiobook/")


def path_type(url):
    return "audiobook" if urlparse(url).path.rstrip("/").startswith("/audiobook/") else "book"


def url_title(url):
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug)


def label_value(lines, labels):
    wanted = {norm(x) for x in labels}
    for i, line in enumerate(lines):
        if norm(line) in wanted:
            for nxt in lines[i + 1:i + 4]:
                if nxt and norm(nxt) not in wanted:
                    return nxt
    return None


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
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(wait)


async def search_page(page, query, section):
    url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    print(f"[Lubimyczytać] search: {url}")
    await open_page(page, url, 300)

    # Parse actual result cards only; never collect every /ksiazka/ or
    # /audiobook/ navigation link from the whole document.
    cards = page.locator(".book-card--l, .authorAllBooks__single")
    found, seen = [], set()
    for i in range(await cards.count()):
        card = cards.nth(i)
        try:
            link = card.locator("a[href*='/ksiazka/'], a[href*='/audiobook/']").first
            href = await link.get_attribute("href") if await link.count() else None
            if not href:
                continue
            title_loc = card.locator(".book-card__title, .authorAllBooks__singleTextTitle").first
            title = clean(await title_loc.text_content()) if await title_loc.count() else None
            if not title:
                continue
            href = href if href.startswith("http") else f"{BASE}{href}"
            href = canonical(href)
            if not valid_product_url(href) or href in seen:
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
    query = candidate["query"]
    await open_page(page, url, 150)
    body = await page.locator("body").inner_text()
    lines = [clean(x) for x in body.splitlines() if clean(x)]
    data = {
        "title": candidate.get("title"),
        "author": ", ".join(candidate.get("authors") or []) or None,
        "narrator": None, "publisher": None, "publishedYear": None,
        "description": None, "cover": None, "isbn": None, "duration": None,
        "genres": [], "series": None, "sequence": None, "language": "pol",
        "url": canonical(url), "type": path_type(url),
    }

    objects = []
    for raw in await page.locator("script[type='application/ld+json']").all_text_contents():
        objects.extend(jsonld_objects(raw))
    target, best = None, -1
    for item in objects:
        name = clean(item.get("name"))
        if name:
            s = similarity(name, query)
            if s > best:
                target, best = item, s
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
        data["cover"] = clean(image)
        pub = target.get("publisher")
        if isinstance(pub, dict):
            pub = pub.get("name")
        data["publisher"] = clean(pub)
        genre = target.get("genre")
        data["genres"] = [clean(x) for x in genre if clean(x)] if isinstance(genre, list) else ([clean(genre)] if genre else [])

    h1 = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
    if h1:
        data["title"] = h1

    try:
        desc = page.locator("#book-description").first
        if await desc.count():
            value = clean(await desc.text_content())
            if value:
                data["description"] = value
                print(f"[Lubimyczytać] description: chars={len(value)} type={data['type']} url={data['url']}")
    except Exception:
        pass

    for selector in ("a#js-lightboxCover[href]", ".book-cover__link[href]", "meta[property='og:image']", "meta[name='twitter:image']", "img.book-cover[src]"):
        try:
            loc = page.locator(selector).first
            if await loc.count():
                value = clean(await loc.get_attribute("href") or await loc.get_attribute("content") or await loc.get_attribute("src"))
                if value:
                    data["cover"] = value
                    break
        except Exception:
            pass

    if not data["author"]:
        names = [clean(x) for x in await page.locator("a[href*='/autor/']").all_text_contents() if clean(x)]
        if names:
            data["author"] = ", ".join(dict.fromkeys(names[:5]))

    data["publisher"] = data["publisher"] or label_value(lines, ["Wydawca", "Wydawnictwo"])
    data["publishedYear"] = data["publishedYear"] or parse_year(label_value(lines, ["Data pierwszego wydania", "Data wydania", "Data 1. wyd. pol.", "Data publikacji", "Data premiery", "Rok wydania"]))
    data["isbn"] = data["isbn"] or label_value(lines, ["ISBN"])
    data["duration"] = data["duration"] or parse_duration(label_value(lines, ["Czas czytania", "Długość", "Czas trwania"]))
    data["narrator"] = label_value(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    language = label_value(lines, ["Język"])
    data["language"] = "pol" if not language or norm(language) in {"polski", "polska", "pol"} else norm(language)
    category = label_value(lines, ["Kategoria", "Kategorie"])
    if category:
        data["genres"] = [clean(x) for x in re.split(r"[,;/]", category) if clean(x)]
    data["series"], data["sequence"] = series_from_lines(lines)
    if not data["isbn"]:
        m = re.search(r"\b(97[89]\d{10})\b", body)
        if m:
            data["isbn"] = m.group(1)
    return data


def candidate_score(candidate, query, author):
    title_s = similarity(candidate.get("title"), query)
    authors = candidate.get("authors") or []
    author_s = max([similarity(x, author) for x in authors], default=0.0) if author else 1.0
    # Do not eliminate a candidate just because the search card omitted author;
    # the detail page is authoritative. Give it a neutral author score.
    if author and not authors:
        author_s = 0.5
    return (title_s * 0.60 + author_s * 0.40) if author else title_s


def final_score(data, query, author):
    title_s = similarity(data.get("title"), query)
    author_s = similarity(data.get("author"), author) if author else 1.0
    if author and author_s < 0.25:
        return 0.0
    value = title_s * 0.60 + author_s * 0.40 if author else title_s
    if not data.get("isbn"):
        value *= 0.99
    if data.get("type") == "audiobook":
        value += 0.001
    return min(value, 1.0)


def to_match(data, value):
    return {
        "title": data.get("title"), "author": data.get("author"),
        "narrator": data.get("narrator"), "publisher": data.get("publisher"),
        "publishedYear": data.get("publishedYear"), "description": data.get("description"),
        "cover": data.get("cover"), "isbn": data.get("isbn"),
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
        queries = [(query, "ksiazki"), (query, "audiobooki")]
        if author:
            queries += [(f"{query} {author}", "ksiazki"), (f"{query} {author}", "audiobooki")]
        collected = []
        for q, section in queries:
            collected.extend(await search_page(search, q, section))
    finally:
        await search.close()

    unique = {}
    for item in collected:
        unique.setdefault(item["url"], item)
    candidates = list(unique.values())
    for item in candidates:
        item["query"] = query
        item["pre_score"] = candidate_score(item, query, author)
    candidates.sort(key=lambda x: (x["pre_score"], 1 if x["type"] == "audiobook" else 0), reverse=True)
    candidates = candidates[:30]
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    sem = asyncio.Semaphore(8)
    results = []
    async def one(candidate):
        async with sem:
            page = await context.new_page()
            try:
                data = await asyncio.wait_for(parse_detail(page, candidate), timeout=20)
                value = final_score(data, query, author)
                if value > 0:
                    results.append((value, data))
                    print(f"[Lubimyczytać] parsed: {data.get('title')} / {data.get('author')} type={data.get('type')} score={value:.3f} url={data.get('url')}")
            except Exception as exc:
                print(f"[Lubimyczytać] detail failed {candidate['url']}: {type(exc).__name__}: {exc}")
            finally:
                await page.close()
    await asyncio.gather(*(one(x) for x in candidates))

    results.sort(key=lambda x: (x[0], 1 if norm(x[1].get("title")) == norm(query) else 0, 1 if x[1].get("type") == "audiobook" else 0), reverse=True)
    matches = [to_match(data, value) for value, data in results[:MAX_RESULTS]]
    result = {"matches": matches}
    _cache[key] = (time.time(), result)
    print("[Lubimyczytać] final:", " | ".join(f"{x['title']}/{x.get('author')} [{x['type']}] ({x['similarity']:.3f})" for x in matches))
    return result


@app.get("/search")
async def search(query: str = Query(..., min_length=1), author: str = Query(""), authorization: str | None = Header(default=None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(await lubimyczytac_search(query, author))


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "lubimyczytac-pl"}


@app.on_event("shutdown")
async def shutdown():
    global _context, _browser, _pw
    if _context:
        await _context.close()
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
