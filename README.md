# Storytel.pl-ADB

Custom metadata provider for [Audiobookshelf](https://www.audiobookshelf.org/) using the public Storytel Polska website.

The provider is intentionally small and self-contained: FastAPI exposes the Audiobookshelf-compatible search endpoint, while Playwright handles Storytel's client-rendered catalog pages.

## Features

- Search by title with optional author filtering.
- Polish Storytel catalog as the primary source.
- Metadata extraction for title, author, narrator, publisher, description, cover, ISBN, release year, language, duration, genres and series.
- Similarity-based ranking to select the most relevant matches.
- Short in-memory cache to reduce repeated Storytel requests.
- Docker and Docker Compose support.
- Authorization header required by the metadata endpoint.
- Health endpoint for container monitoring.

## API

### `GET /search`

Query parameters:

- `query` — required book title.
- `author` — optional author name.

Required header:

```http
Authorization: <any-value>
```

Example:

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3000/search?query=Legion&author=Dan%20Abnett'
```

Successful responses contain a `matches` array. A match includes Audiobookshelf-friendly fields such as `title`, `author`, `narrator`, `publisher`, `cover`, `isbn`, `genres`, `series`, `language`, `duration` and `similarity`.

### `GET /health`

Returns:

```json
{"status":"ok"}
```

## Docker

Build and run the provider with:

```bash
docker compose up -d --build
```

The container listens on port `3000` by default.

To stop it:

```bash
docker compose down
```

## Configuration

The application uses these defaults:

- Storytel locale: `pl-PL`
- Time zone: `Europe/Warsaw`
- HTTP port: `3000`
- Cache lifetime: 10 minutes
- Maximum returned matches: 10

The port can be changed with the `PORT` environment variable.

## Why Playwright

Storytel's public search is client-rendered. A plain HTTP scraper can therefore miss the catalog results that are visible in a browser. Playwright is used to load the rendered search page, collect book URLs, and then extract metadata from the individual book pages and their structured data.

## Project layout

```text
.
├── Dockerfile
├── compose.yml
├── requirements.txt
├── scraper.py
└── README.md
```

## Notes

This project consumes publicly accessible Storytel Polska pages. Storytel can change its page structure at any time, so selectors and metadata extraction may need maintenance when the site changes.
