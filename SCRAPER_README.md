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

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /ping` | GET | Health check |
| `GET /scrape` | GET | Scrape a URL |
| `POST /scrape` | POST | Scrape a URL (preferred — avoids URL encoding issues) |
| `GET /docs` | GET | Interactive Swagger docs |

**Parameters (GET query / POST JSON body):**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | — | Target URL to scrape **(required)** |
| `format` | string | `"json"` | Response format: `json`, `html`, `markdown`, `cleaned` |
| `proxy` | bool | `false` | Route through Webshare rotating proxy pool |
| `browser` | bool | `false` | Force browser-based strategies (Playwright/nodriver) for JS-heavy sites |
| `refresh` | bool | `false` | Delete cached strategy registry for this domain — forces all 7 strategies to be tried fresh |

**Examples:**

```bash
# GET — returns JSON with html + markdown
curl "http://localhost:8000/scrape?url=https://example.com&proxy=true"

# POST — returns raw HTML
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","format":"html","proxy":true}'

# Force browser + refresh cache
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","format":"html","browser":true,"refresh":true}'
```

### Robustness

The scraper includes safeguards to prevent resource exhaustion across sequential requests:

| Feature | Description |
|---------|-------------|
| **Chrome cleanup** | Orphan `chrome` processes are force-killed after every browser strategy (6-7) and at the start of each `scrape_as_html` call, preventing memory leaks from accumulating |
| **Per-strategy timeout** | Each strategy runs with a timeout: 30s for HTTP strategies (1-5), 90s for browser strategies (6-7). A hung strategy doesn't block the remaining strategies |
| **Auto-retry with proxy** | If a browser strategy fails without proxy, it's retried once with proxy before moving to the next strategy |
| **Expandable content detection** | If HTTP content looks truncated ("read more" buttons), the scraper auto-upgrades to browser strategies |
| **Domain registry** | PostgreSQL-backed cache remembers the winning strategy per domain, reuses harvested cookies, and skips failed strategies on subsequent calls |

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
