import asyncio
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
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(wait)


def extract_author_candidates(card):
    # Kept separate so search parsing stays deterministic and fast.
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
                # Current LC sometimes has a separate audiobook URL in the card.
                href_loc = card.locator("a[href*='/audiobook/']").first if result_type == "audiobook" else card.locator("a[href*='/ksiazka/']").first
                href = await href_loc.get_attribute("href") if await href_loc.count() else None
            if not href:
                continue

            href = href if href.startswith("http") else f"{BASE}{href}"
            href = canonical(href)
            if not is_product_url(href) or href in seen:
                continue

            authors = [
                clean(x)
                for x in await extract_author_candidates(card).all_text_contents()
                if clean(x)
            ]
            seen.add(href)
            found.append({
                "url": href,
                "title": title or url_title(href),
                "authors": list(dict.fromkeys(authors)),
                "type": result_type,
            })
        except Exception:
            continue

    # Do not use all product links as candidates. Only use them when the
    # dedicated card parser found nothing, otherwise navigation/recommendation
    # links can pollute the result set and bury the true match.
    if not found:
        links = page.locator(f"a[href*='/{result_type if result_type == 'audiobook' else 'ksiazka'}/']")
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


async def parse_detail(page, candidate):
    url = candidate["url"]
    await open_page(page, url, 450)
    body = await page.locator("body").inner_text()
    lines = lines_from_body(body)
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
        "type": candidate.get("type") or path_type(url),
    }

    h1 = clean(await page.locator("h1").first.text_content()) if await page.locator("h1").count() else None
    if h1:
        data["title"] = h1

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

    # Direct product-page selectors first. This prevents recommendation JSON-LD
    # from replacing the metadata of the requested audiobook.
    cover_selectors = (
        "a#js-lightboxCover[href]",
        ".book-cover__link[href]",
        "meta[property='og:image']",
        "meta[name='twitter:image']",
        "meta[itemprop='image']",
    )
    for selector in cover_selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count():
                value = clean(await loc.get_attribute("href") or await loc.get_attribute("content"))
                if value:
                    data["cover"] = value if value.startswith("http") else BASE + value
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

    data["publisher"] = value_after_label(lines, ["Wydawca", "Wydawnictwo"])
    data["publishedYear"] = parse_year(value_after_label(lines, ["Data pierwszego wydania", "Data wydania", "Data publikacji", "Data premiery", "Rok wydania"]))
    data["isbn"] = value_after_label(lines, ["ISBN"])
    data["duration"] = parse_duration(value_after_label(lines, ["Czas czytania", "Długość", "Czas trwania"]))
    data["narrator"] = value_after_label(lines, ["Lektor", "Lektorzy", "Czyta", "Czytają", "Narrator"])
    language = value_after_label(lines, ["Język"])
    data["language"] = "pol" if not language or norm(language) in {"polski", "polska", "pol"} else norm(language)

    m = re.search(r"\b(97[89]\d{10})\b", body)
    data["isbn"] = data["isbn"] or (m.group(1) if m else None)

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
        # Match the proven LC request model: phrase + author as a separate
        # query parameter, for both books and audiobooks.
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
    # Keep the same 20-result strategy as the proven reference implementation.
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
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.close()
    if _pw:
        await _pw.stop()
