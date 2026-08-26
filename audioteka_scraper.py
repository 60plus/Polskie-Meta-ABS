import asyncio
import json
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, urljoin, urlparse

AUDIO_BASE = "https://audioteka.com"
AUDIO_SEARCH = "https://audioteka.com/pl/szukaj/"


def norm(v):
    s = str(v or "").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.96
    return SequenceMatcher(None, a, b).ratio()


def duration_minutes(text):
    s = str(text or "")
    m = re.search(r"(\d+)\s*(?:godz\.?|godziny|h)\s*(?:(\d+)\s*(?:min|m))?", s, re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*(?:min|m)\b", s, re.I)
    return int(m.group(1)) if m else None


class AudiotekaScraper:
    def __init__(self, context):
        self.context = context

    async def goto(self, page, url):
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

    async def search_urls(self, query):
        page = await self.context.new_page()
        try:
            url = f"{AUDIO_SEARCH}?phrase={quote(query)}"
            print(f"[Audioteka] browser search: {url}")
            await self.goto(page, url)
            for _ in range(5):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(350)
            hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
            urls, seen = [], set()
            for href in hrefs:
                p = urlparse(href)
                if p.netloc not in ("audioteka.com", "www.audioteka.com"):
                    continue
                path = p.path
                if not path.startswith("/pl/"):
                    continue
                if any(x in path for x in ("/szukaj", "/cykl/", "/autor/", "/wydawca/", "/gatunek/")):
                    continue
                u = urljoin(AUDIO_BASE, path)
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
            print(f"[Audioteka] '{query}' -> {len(urls)} URLs")
            return urls
        finally:
            await page.close()

    async def detail(self, url):
        page = await self.context.new_page()
        try:
            await self.goto(page, url)
            body = await page.locator("body").inner_text(timeout=10000)
            title = (await page.locator("h1").first.text_content() or "").strip() if await page.locator("h1").count() else None
            cover = await page.locator("meta[property='og:image']").get_attribute("content")
            description = await page.locator("meta[property='og:description']").get_attribute("content")
            author = narrator = publisher = isbn = published = None
            scripts = await page.locator("script[type='application/ld+json']").all_text_contents()
            for raw in scripts:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if not title:
                        title = item.get("name")
                    description = description or item.get("description")
                    isbn = isbn or item.get("isbn")
                    image = item.get("image")
                    if not cover:
                        cover = image[0] if isinstance(image, list) and image else image
                    au = item.get("author")
                    if isinstance(au, dict):
                        author = author or au.get("name")
                    elif isinstance(au, str):
                        author = author or au
                    pub = item.get("publisher")
                    if isinstance(pub, dict):
                        publisher = publisher or pub.get("name")
                    elif isinstance(pub, str):
                        publisher = publisher or pub
                    m = re.search(r"(?:19|20)\d{2}", str(item.get("datePublished") or ""))
                    published = published or (m.group(0) if m else None)
            text = re.sub(r"\s+", " ", body)
            if not author:
                m = re.search(r"(?:Autor|Autorzy)\s*[:\-]\s*(.+?)(?=\s+(?:Lektor|Czyta|Wydawca|ISBN|Czas|Język)\b|$)", text, re.I)
                author = m.group(1).strip() if m else None
            if not narrator:
                m = re.search(r"(?:Lektor|Czyta|Głosy)\s*[:\-]\s*(.+?)(?=\s+(?:Wydawca|ISBN|Czas|Język)\b|$)", text, re.I)
                narrator = m.group(1).strip() if m else None
            if not publisher:
                m = re.search(r"Wydawca\s*[:\-]\s*(.+?)(?=\s+(?:ISBN|Czas|Język)\b|$)", text, re.I)
                publisher = m.group(1).strip() if m else None
            if not isbn:
                m = re.search(r"\b(97[89]\d{10})\b", text)
                isbn = m.group(1) if m else None
            if not published:
                m = re.search(r"(?:Rok wydania|Data wydania)\s*[:\-]?\s*((?:19|20)\d{2})", text, re.I)
                published = m.group(1) if m else None
            if not title:
                return None
            return {"title": title, "author": author, "narrator": narrator, "publisher": publisher,
                    "publishedYear": published, "description": description, "cover": cover, "isbn": isbn,
                    "language": "pol", "duration": duration_minutes(text), "type": "audiobook", "url": url}
        finally:
            await page.close()

    async def search(self, query, author=""):
        urls = await self.search_urls(query)
        if not urls and author:
            urls = await self.search_urls(f"{query} {author}")
        books = await asyncio.gather(*(self.detail(u) for u in urls[:30]), return_exceptions=True)
        ranked = []
        for book in books:
            if not isinstance(book, dict):
                continue
            ts = sim(book.get("title"), query)
            a = sim(book.get("author"), author) if author else 1.0
            score = ts * 0.75 + a * 0.25 if author else ts
            if ts >= 0.60 and (not author or a >= 0.45):
                book["similarity"] = round(min(score, 1.0), 4)
                ranked.append(book)
        ranked.sort(key=lambda x: x["similarity"], reverse=True)
        print("[Audioteka] final:", " | ".join(f"{b['title']}/{b.get('author')} ({b['similarity']:.3f})" for b in ranked[:10]))
        return {"matches": ranked[:10]}
