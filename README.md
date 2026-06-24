# Scraper

Two web scraping projects for extracting press release content.

---

## [`best_scrape_v1/`](./best_scrape_v1) — Multi-strategy web scraper API

Bypasses Cloudflare and anti-bot protections via 7 strategies (simple HTTP → real Chrome with canvas noise injection). Exposed as a FastAPI service with a PostgreSQL-backed domain registry that learns which strategy works per domain and reuses cookies across runs.

### Quick start

```bash
docker compose up -d
```

The API is at `http://localhost:8000`.

### API

| Endpoint | Description |
|----------|-------------|
| `GET /ping` | Health check |
| `GET /scrape?url=<URL>&browser=false` | Scrape a URL, returns JSON with `html`, `markdown`, `cleaned_markdown` |
| `GET /docs` | Interactive Swagger docs |

**Parameters:**
- `url` (required) — the page to scrape
- `browser` (optional, default `false`) — force browser-based strategies (Playwright/nodriver) for JS-heavy sites

```bash
curl "http://localhost:8000/scrape?url=https://example.com&browser=true"
```

### Tests

```bash
cd best_scrape_v1
python -c "from scraper import run_tests; run_tests('scraper_text.json')"
```

---

## [`usao_ct_scraper/`](./usao_ct_scraper) — DOJ District of Connecticut scraper

Playwright-based scraper that extracts press releases from `https://www.justice.gov/usao-ct/pr`. Supports incremental scraping (skips previously seen URLs).

### Usage

```bash
cd usao_ct_scraper
pip install -r requirements.txt
playwright install firefox
python -m usao_ct_scraper.scraper
```

Output is written to `press_releases.json`.

### Configuration

Edit `usao_ct_scraper/config.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `BASE_URL` | `https://www.justice.gov` | Base domain |
| `LISTING_URL` | `/usao-ct/pr` | Press release listing page |
| `OUTPUT_FILE` | `press_releases.json` | Output file path |
| `HEADLESS` | `True` | Run browser headlessly |
| `TIMEOUT` | `30000` | Page load timeout (ms) |
| `MAX_PAGES` | `None` | Max listing pages to crawl |
| `RATE_LIMIT_DELAY` | `2.0` | Seconds between requests |
