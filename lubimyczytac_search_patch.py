import asyncio
from urllib.parse import quote

import lubimyczytac_patch as patch

provider = patch.provider
BASE = provider.BASE


async def search_page(page, query, section):
    url = f"{BASE}/szukaj/{section}?phrase={quote(query)}"
    print(f"[Lubimyczytać] search: {url}")
    await provider.open_page(page, url, 300)

    found, seen = [], set()
    cards = page.locator(".book-card--l")

    # A single LC card can contain both /ksiazka/ and /audiobook/ links.
    # Treat every product URL as a separate candidate instead of taking only
    # .book-card__title[href].first (which usually points to the printed book).
    for i in range(await cards.count()):
        card = cards.nth(i)
        try:
            title = provider.clean(await card.locator(".book-card__title").first.text_content())
            authors = [
                provider.clean(x)
                for x in await card.locator(
                    ".book-card__author a, a[href*='/autor/']"
                ).all_text_contents()
                if provider.clean(x)
            ]
            links = card.locator("a[href*='/ksiazka/'], a[href*='/audiobook/']")
            for j in range(await links.count()):
                href = await links.nth(j).get_attribute("href")
                if not href:
                    continue
                href = href if href.startswith("http") else f"{BASE}{href}"
                href = provider.canonical(href)
                if not provider.is_product_url(href) or href in seen:
                    continue
                seen.add(href)
                found.append({
                    "url": href,
                    "title": title or provider.url_title(href),
                    "authors": list(dict.fromkeys(authors)),
                    "type": provider.path_type(href),
                })
        except Exception:
            continue

    # Also collect product links outside the standard card wrapper. This is
    # needed for the current LC audiobook result markup.
    links = page.locator("a[href*='/audiobook/'], a[href*='/ksiazka/']")
    for i in range(await links.count()):
        link = links.nth(i)
        try:
            href = await link.get_attribute("href")
            if not href:
                continue
            href = href if href.startswith("http") else f"{BASE}{href}"
            href = provider.canonical(href)
            if not provider.is_product_url(href) or href in seen:
                continue

            title = provider.clean(await link.text_content()) or provider.url_title(href)
            authors = []
            ancestor = link.locator(
                "xpath=ancestor::*[self::article or self::li or contains(@class,'book-card') or contains(@class,'search-result')][1]"
            ).first
            if await ancestor.count():
                ancestor_title = provider.clean(
                    await ancestor.locator(".book-card__title").first.text_content()
                ) if await ancestor.locator(".book-card__title").count() else None
                title = ancestor_title or title
                authors = [
                    provider.clean(x)
                    for x in await ancestor.locator(
                        ".book-card__author a, a[href*='/autor/']"
                    ).all_text_contents()
                    if provider.clean(x)
                ]

            seen.add(href)
            found.append({
                "url": href,
                "title": title if title and len(title) <= 180 else provider.url_title(href),
                "authors": list(dict.fromkeys(authors)),
                "type": provider.path_type(href),
            })
        except Exception:
            continue

    print(f"[Lubimyczytać] {section} '{query}' -> {len(found)} wyników")
    return found


provider.search_page = search_page


async def lubimyczytac_search(query, author=""):
    key = f"lubimyczytac|{provider.norm(query)}|{provider.norm(author)}"
    cached = provider._cache.get(key)
    if cached and provider.time.time() - cached[0] < provider.CACHE_TTL:
        return cached[1]

    context = await provider.get_context()
    search = await context.new_page()
    try:
        book_cards = await search_page(search, query, "ksiazki")
        audiobook_cards = await search_page(search, query, "audiobooki")
        if author:
            book_cards += await search_page(search, f"{query} {author}", "ksiazki")
            audiobook_cards += await search_page(search, f"{query} {author}", "audiobooki")
    finally:
        await search.close()

    collected = book_cards + audiobook_cards
    unique = {}
    for item in collected:
        unique.setdefault(item["url"], item)
    candidates = list(unique.values())

    for item in candidates:
        title_s = provider.similarity(item.get("title"), query)
        authors = item.get("authors") or []
        author_s = max(
            (provider.similarity(x, author) for x in authors),
            default=0.5,
        ) if author else 1.0
        item["query"] = query
        item["pre_score"] = title_s * 0.60 + author_s * 0.40 if author else title_s

    candidates.sort(
        key=lambda x: (
            x["pre_score"],
            1 if x["type"] == "audiobook" else 0,
        ),
        reverse=True,
    )

    # Never let a large list of printed-book results evict audiobook matches.
    # Keep the strongest candidates overall plus a dedicated audiobook slice.
    top = candidates[:30]
    seen_urls = {x["url"] for x in top}
    for item in candidates:
        if item["type"] != "audiobook" or item["url"] in seen_urls:
            continue
        if len(top) >= 50:
            break
        top.append(item)
        seen_urls.add(item["url"])

    candidates = top
    print(f"[Lubimyczytać] candidates to parse: {len(candidates)}")

    sem = asyncio.Semaphore(8)

    async def parse_one(candidate):
        async with sem:
            page = await context.new_page()
            try:
                return await provider.parse_detail(page, candidate)
            except Exception as exc:
                print(
                    f"[Lubimyczytać] detail failed: {candidate['url']} "
                    f"{type(exc).__name__}: {exc}"
                )
                return None
            finally:
                await page.close()

    parsed = await asyncio.gather(*(parse_one(c) for c in candidates))
    ranked = []
    for data in parsed:
        if not data:
            continue
        value, title_s, author_s = provider.score(data, query, author)
        if value <= 0:
            continue
        ranked.append((value, title_s, author_s, data))
        print(
            f"[Lubimyczytać] parsed: {data.get('title')} / {data.get('author')} "
            f"type={data.get('type')} score={value:.3f} url={data.get('url')}"
        )

    ranked.sort(
        key=lambda x: (
            x[0],
            1 if x[3].get("type") == "audiobook" else 0,
            x[1],
            x[2],
        ),
        reverse=True,
    )
    final = [provider.to_match(data, value) for value, _, _, data in ranked[:provider.MAX_RESULTS]]
    print(
        "[Lubimyczytać] final:",
        " | ".join(
            f"{x['title']}/{x.get('author')} [{x['type']}] ({x['similarity']:.3f})"
            for x in final
        ),
    )

    result = {"matches": final}
    provider._cache[key] = (provider.time.time(), result)
    return result


provider.lubimyczytac_search = lubimyczytac_search
app = provider.app
