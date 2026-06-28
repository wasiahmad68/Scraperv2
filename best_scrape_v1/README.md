# Best Scraper API

A multi-strategy web scraper that bypasses Cloudflare and other anti-bot protections, exposed as a FastAPI service.

## How it works

The scraper tries up to 7 strategies — from simple HTTP requests all the way to a headed real Google Chrome instance (via `nodriver`) with canvas noise injection and cookie warming. Cloudflare Turnstile challenges are handled automatically.

### Adaptive strategy selection

The scraper learns from every request via a **PostgreSQL-backed domain registry**:

- **Per-domain strategy ordering** — the strategy that last succeeded for a domain is tried first on every subsequent request. Known-failed strategies are skipped entirely.
- **Cookie reuse** — browser cookies harvested from Playwright / nodriver are stored and re-injected on the next visit.
- **Forgetting** — stale knowledge is automatically cleared:
  - Cookies expire after 1 hour (or earlier if the cookie's own `expires` field says so).
  - A working strategy is forgotten after 7 days of no use.
  - If the known working strategy fails 3 times in a row the entire domain entry is wiped.

---

## Quick start

```bash
docker compose up -d
```

The API is at `http://localhost:8000`.

---

## API Endpoints

### `GET /` — Web form

Opens a browser form where you can paste any URL (including those containing `?` and `&`) and click Scrape. JavaScript encodes the URL client-side before sending to the GET endpoint.

### `GET /ping` — Health check

```bash
curl http://localhost:8000/ping
# {"status":"ok"}
```

### `GET /scrape` — Scrape a URL

```bash
# Simple URL (no ? or & in target)
curl "http://localhost:8000/scrape?url=https://example.com&format=html"

# URL with query params — must be URL-encoded
curl "http://localhost:8000/scrape?url=https%3A%2F%2Fwww.justice.gov%2Fusao%2Fpressreleases%3Fsort_by%3Dfield_date&format=html&proxy=1"
```

#### Query Parameters

| Parameter | Type    | Default  | Description |
|-----------|---------|----------|-------------|
| `url`     | string  | required | The URL to scrape |
| `format`  | string  | `json`   | Response format: `json`, `html`, `markdown`, `cleaned` |
| `proxy`   | boolean | `false`  | Route through the proxy pool |
| `browser` | boolean | `false`  | Force browser-based strategies for JS-heavy sites |

#### Response formats

- **`format=json`** (default) — Returns JSON with `html`, `markdown`, `cleaned_markdown`, and metadata
- **`format=html`** — Returns raw HTML directly (useful for Scrapy selectors)
- **`format=markdown`** — Returns plain text markdown
- **`format=cleaned`** — Returns markdown with boilerplate (nav, footer, banners) removed

### `POST /scrape` — Scrape a URL (JSON body)

For URLs containing `?` or `&` — no encoding needed, the URL goes in the JSON body.

```bash
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.justice.gov/usao/pressreleases?sort_by=field_date","format":"html","proxy":true}'
```

From JavaScript:
```js
fetch("http://localhost:8000/scrape", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({url: "https://www.justice.gov/usao/pressreleases?sort_by=field_date", format: "html", proxy: true})
}).then(r => r.text()).then(console.log)
```

### `GET /docs` — Interactive Swagger docs

FastAPI's auto-generated documentation with "Try it out" button for every endpoint.

---

## Docker Compose

The `docker-compose.yml` at the project root starts both the scraper API and a PostgreSQL database:

```bash
# Start
docker compose up -d

# Restart scraper only (after code changes)
docker compose restart scraper

# View logs
docker compose logs -f scraper

# Stop
docker compose down
```

### Volume mount

The `docker-compose.yml` mounts `./best_scrape_v1` to `/app` inside the container, so changes to `api.py` and `scraper.py` take effect immediately on restart — no image rebuild needed.

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `PGHOST` | PostgreSQL server hostname |
| `PGPORT` | PostgreSQL server port (default: `5432`) |
| `PGDATABASE` | Database name |
| `PGUSER` | Database user |
| `PGPASSWORD` | Database password |
| `PROXY_TOKEN` | Webshare API token for proxy pool |
| `PROXY_USERNAME` | Proxy authentication username |
| `PROXY_PASSWORD` | Proxy authentication password |

The `scraper_domains` table is created automatically on first run.

---

## Proxy pool

When `proxy=1` is set, the scraper fetches 500 proxies from Webshare, caches them for 1 hour, and rotates through them per strategy attempt.

---

## Notes

- **HTTP status codes:** 502 is returned when all scraping strategies fail
- **Scrapy integration:** Use `format=html` so Scrapy's `response.css()` selectors work on the raw HTML
- **Rate limiting:** Set `DOWNLOAD_TIMEOUT: 120` in Scrapy settings; each scrape can take 30-60s
