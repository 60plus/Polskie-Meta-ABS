from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from scraper import context
from audioteka_scraper import AudiotekaScraper

app = FastAPI(title="Audioteka PL Audiobookshelf Metadata Provider")


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "audioteka-pl"}


@app.get("/search")
async def search_endpoint(
    query: str = Query(..., min_length=1),
    author: str = Query(""),
    authorization: str | None = Header(default=None),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        ctx = await context()
        return JSONResponse(await AudiotekaScraper(ctx).search(query, author))
    except Exception as exc:
        print(f"[Audioteka] ERROR: {exc!r}")
        return JSONResponse({"matches": [], "error": str(exc)}, status_code=500)
