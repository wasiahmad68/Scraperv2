# Best Scraper

A multi-strategy web scraper that bypasses Cloudflare and other anti-bot protections, exposed as a FastAPI service.

## How it works

The scraper tries up to 7 strategies — from simple HTTP requests all the way to a headed real Google Chrome instance (via `nodriver`) with canvas noise injection and cookie warming. Cloudflare Turnstile challenges are handled automatically.

### Adaptive strategy selection

The scraper learns from every request via a **PostgreSQL-backed domain registry**:

- **Per-domain strategy ordering** — the strategy that last succeeded for a domain is tried first on every subsequent request. Known-failed strategies are skipped entirely.
- **Cookie reuse** — browser cookies harvested from strategies 6 and 7 (Playwright / nodriver) are stored and re-injected on the next visit, which can bypass Cloudflare Turnstile entirely when `cf_clearance` is still valid.
- **Forgetting** — stale knowledge is automatically cleared so the scraper adapts when sites change their protection:
  - Cookies expire after 1 hour (or earlier if the cookie's own `expires` field says so).
  - A working strategy is forgotten after 7 days of no use, triggering a fresh full sweep.
  - If the known working strategy fails 3 times in a row the entire domain entry is wiped.

## PostgreSQL setup

The registry uses the standard PostgreSQL environment variables. No extra configuration keys are required.

| Variable      | Default (psql) | Description               |
|---------------|----------------|---------------------------|
| `PGHOST`      | `localhost`    | PostgreSQL server hostname |
| `PGPORT`      | `5432`         | PostgreSQL server port     |
| `PGDATABASE`  | current user   | Database name              |
| `PGUSER`      | current user   | Database user              |
| `PGPASSWORD`  | _(none)_       | Database password          |

The `scraper_domains` table and its index are created automatically on first run.

## Run with Docker

### 1. Build the image

```bash
docker build -t best-scraper .
```

### 2. Start the API server

```bash
docker run -p 8000:8000 --env-file .env --name scraper best-scraper
```

The API is now available at `http://localhost:8000`.

### 3. Use the API

#### Health check

```bash
curl http://localhost:8000/ping
# {"status":"ok"}
```

#### Scrape a URL

```bash
curl "http://localhost:8000/scrape?url=https://example.com"
```

The response is a JSON object:

```json
{
  "url": "https://example.com",
  "strategy": 1,
  "html": "<html>...</html>",
  "markdown": "# Full markdown ...",
  "cleaned_markdown": "# Cleaned markdown (nav/footer/banners removed) ..."
}
```

### Query parameters

| Parameter | Type   | Default  | Description       |
|-----------|--------|----------|-------------------|
| `url`     | string | required | The URL to scrape |

### Interactive docs

FastAPI's auto-generated docs are available at `http://localhost:8000/docs`.

### Stop the container

```bash
docker stop scraper && docker rm scraper
```

## Run the standalone scraper (without the API)

Override the default command to run `scraper.py` directly:

```bash
docker run --rm \
  -e PGHOST=your-db-host \
  -e PGDATABASE=scraper \
  -e PGUSER=scraper \
  -e PGPASSWORD=secret \
  best-scraper bash -c \
  "Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &>/tmp/xvfb.log & sleep 1 && python scraper.py"
```

## Run tests

Test cases live in `scraper_text.json`. Mount the local directory so the container picks up the file (and any edits) without a rebuild:

```bash
# Build once
docker build -t best-scraper .

# Run tests (mount local dir so scraper_text.json and scraper.py are live)
docker run --rm \
  -v "$(pwd):/app" \
  -e DISPLAY=:99 \
  -e PGHOST=your-db-host \
  -e PGDATABASE=scraper \
  -e PGUSER=scraper \
  -e PGPASSWORD=secret \
  best-scraper \
  bash -c "Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &>/tmp/xvfb.log & sleep 1 && python scraper.py"
```

Each test case is evaluated against expected text snippets. Results are printed per URL and a summary is shown at the end:

```
PASS: https://example.com
FAIL: https://other.com
      missing: 'expected snippet'

============================================================
Results: 1 passed, 1 failed out of 2 total
```

To run with boilerplate stripping (`clean=True`):

```bash
docker run --rm \
  -v "$(pwd):/app" \
  -e DISPLAY=:99 \
  -e PGHOST=your-db-host \
  -e PGDATABASE=scraper \
  -e PGUSER=scraper \
  -e PGPASSWORD=secret \
  best-scraper \
  bash -c "Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &>/tmp/xvfb.log & sleep 1 && python -c \"from scraper import run_tests; run_tests('scraper_text.json', clean=True)\""
```
