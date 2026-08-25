# Storytel.pl-ADB

A lightweight custom metadata provider for [Audiobookshelf](https://www.audiobookshelf.org/) that searches the **Polish Storytel catalog** and returns audiobook metadata in an Audiobookshelf-compatible format.

> **Documentation language:** English.
> **Catalog and metadata:** Polish Storytel (`storytel.com/pl`).

## How it works

Storytel's search results are rendered by the browser, so a simple HTTP scraper is not reliable. This provider uses Playwright to load the real Storytel Poland search page, collect book URLs, open the individual book pages, and extract the metadata visible to users.

```text
Audiobookshelf
      |
      | GET /search?query=...&author=...
      v
Storytel.pl-ADB
      |
      | Playwright / Chromium
      v
Storytel Polska
      |
      | search results + book pages
      v
Audiobookshelf-compatible metadata
```

The provider does **not** depend on the older Lubimyczytac implementation or on Storytel's internal API. The Polish Storytel website is the source of truth.

## Features

- Searches the Polish Storytel catalog.
- Searches by title with optional author matching.
- Uses a real Chromium browser through Playwright.
- Extracts metadata from Storytel book pages and structured data.
- Returns Polish titles, descriptions, narrators and other Polish catalog metadata when available.
- Extracts title, author, narrator, publisher, description, cover, ISBN, release year, language, duration, genres and series information.
- Ranks results using title and author similarity.
- Prefers Polish-language results when several candidates are available.
- Uses a short in-memory cache to reduce repeated requests.
- Provides Docker and Docker Compose deployment.
- Includes a health endpoint for container monitoring.

## Requirements

- Docker
- Docker Compose

No local Python or Playwright installation is required when using Docker.

## Running with Docker Compose

```bash
git clone https://github.com/60plus/Storytel.pl-ADB.git
cd Storytel.pl-ADB
docker compose up -d --build
```

The provider listens on port `3000` by default.

Check the container:

```bash
docker compose ps
```

Check the health endpoint:

```bash
curl http://127.0.0.1:3000/health
```

Expected response:

```json
{"status":"ok"}
```

Stop the service:

```bash
docker compose down
```

## API

### `GET /search`

Search for a book in the Polish Storytel catalog.

Query parameters:

| Parameter | Required | Description |
|---|---|---|
| `query` | Yes | Book title or search phrase |
| `author` | No | Author name used to improve result ranking |

The endpoint requires an `Authorization` header. The provider only checks that the header is present; authentication is normally handled by the reverse proxy or Audiobookshelf integration.

Example:

```bash
curl -s \
  -H 'Authorization: test' \
  'http://127.0.0.1:3000/search?query=Wywy%C5%BCszenie%20Horusa&author=Dan%20Abnett'
```

Example response:

```json
{
  "matches": [
    {
      "title": "Wywyższenie Horusa",
      "author": "Dan Abnett",
      "narrator": "Filip Kosior",
      "publisher": "Storytel",
      "publishedYear": "2022",
      "description": "...",
      "cover": "https://covers.storytel.com/...",
      "isbn": "...",
      "genres": ["Science fiction"],
      "series": [
        {
          "series": "Herezja Horusa",
          "sequence": "1"
        }
      ],
      "language": "pol",
      "duration": 757,
      "type": "audiobook",
      "similarity": 1.0
    }
  ]
}
```

`duration` is returned in minutes, as expected by Audiobookshelf.

### `GET /health`

Simple health check:

```http
GET /health
```

Response:

```json
{"status":"ok"}
```

## Configuration

Default settings:

| Setting | Default |
|---|---|
| HTTP port | `3000` |
| Storytel locale | `pl-PL` |
| Time zone | `Europe/Warsaw` |
| Cache lifetime | 10 minutes |
| Maximum results | 10 |

The application also exposes the `PORT` environment variable for deployments where another internal port is required.

## Metadata source

The provider is specifically designed for **Storytel Poland**:

- Search: `https://www.storytel.com/pl/search/all`
- Book pages: `https://www.storytel.com/pl/books/...`
- Browser locale: `pl-PL`
- Time zone: `Europe/Warsaw`

The returned metadata therefore reflects the Polish Storytel catalog rather than a generic international Storytel catalog.

## Why Playwright?

Storytel's catalog is heavily client-rendered. The results visible in a normal browser are not necessarily present in the initial HTTP response.

Playwright solves this by running Chromium and allowing Storytel's frontend to render normally. The scraper then:

1. Opens the Polish Storytel search page.
2. Waits for the client-rendered catalog.
3. Collects links to book pages.
4. Opens the relevant book pages.
5. Reads structured data and page metadata.
6. Normalizes the data for Audiobookshelf.
7. Ranks the matches using title and author similarity.

## Project structure

```text
Storytel.pl-ADB/
├── Dockerfile
├── compose.yml
├── requirements.txt
├── scraper.py
└── README.md
```

## Development

The application is a small FastAPI service. The main implementation is contained in `scraper.py`.

For local development without Docker:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn scraper:app --host 0.0.0.0 --port 3000
```

## Maintenance

Storytel may change its frontend, URLs, structured data or page layout. If the public website changes, the Playwright selectors or metadata extraction logic may need to be updated.

The project intentionally keeps the implementation focused on the public Polish Storytel website instead of depending on undocumented internal APIs.

## License

This project is provided as-is. It is an unofficial community project and is not affiliated with or endorsed by Storytel or Audiobookshelf.
