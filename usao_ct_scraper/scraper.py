import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

API = "http://54.208.226.232:8000/scrape?url={}&format=html"
BASE_URL = "https://www.justice.gov"
LISTING_URL = f"{BASE_URL}/usao-ct/pr"
OUTPUT_FILE = "press_releases.json"
RATE_LIMIT_DELAY = 2.0


def log(*args, **kwargs):
    print(*args, **kwargs, flush=True)


def fetch(url):
    try:
        r = requests.get(API.format(url), timeout=60)
        if r.status_code == 200 and r.text:
            return r.text
        log(f"  [!] API returned {r.status_code}")
    except Exception as e:
        log(f"  [!] Request failed: {e}")
    return None


def extract_links(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for article in soup.select("article.news-content-listing.node-press-release"):
        title_el = article.select_one("h2 a")
        if not title_el:
            continue
        href = title_el.get("href")
        if not href:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "url": href if href.startswith("http") else urljoin(BASE_URL, href),
            "date": article.select_one("time").get("datetime") if article.select_one("time") else None,
        })
    return results


def next_page_url(html):
    el = BeautifulSoup(html, "html.parser").select_one("a[aria-label='Next page']")
    return urljoin(LISTING_URL, el.get("href")) if el and el.get("href") else None


def extract_release(html, url, listing_date):
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1.page-title")
    time_el = soup.select_one("article time[datetime]")
    body_el = soup.select_one("article.grid-row")
    return {
        "title": h1.get_text(strip=True) if h1 else "",
        "url": url,
        "date": time_el.get("datetime") if time_el else listing_date,
        "date_published": time_el.get_text(strip=True) if time_el else None,
        "body_text": re.sub(r"\n{3,}", "\n\n", body_el.get_text(strip=True)) if body_el else "",
    }


def load_existing(path):
    if not Path(path).exists():
        return [], set()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data, {item["url"] for item in data}


def save(path, results):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="Scrape DOJ CT press releases via remote API")
    parser.add_argument("--pages", type=int, default=1, help="Number of listing pages to scrape (default: 1, use -1 for all)")
    parser.add_argument("--output", default=OUTPUT_FILE, help=f"Output file (default: {OUTPUT_FILE})")
    parser.add_argument("--delay", type=float, default=RATE_LIMIT_DELAY, help=f"Delay between requests in seconds (default: {RATE_LIMIT_DELAY})")
    args = parser.parse_args()

    max_pages = args.pages if args.pages != -1 else None
    output = args.output
    delay = args.delay

    existing, seen_urls = load_existing(output)
    log(f"Loaded {len(existing)} existing ({len(seen_urls)} unique URLs)")

    new_results = []
    page_num = 0
    url = LISTING_URL

    while url:
        page_num += 1
        log(f"\n--- Page {page_num}: {url}")

        html = fetch(url)
        if not html:
            break

        links = extract_links(html)
        new_links = [l for l in links if l["url"] not in seen_urls]
        log(f"  {len(links)} items, {len(new_links)} new")

        if not new_links:
            log("  [*] All scraped, stopping")
            break

        for i, item in enumerate(new_links, 1):
            log(f"  [{i}/{len(new_links)}] {item['title'][:80]}")
            pr = extract_release(fetch(item["url"]), item["url"], item["date"])
            if pr:
                new_results.append(pr)
                seen_urls.add(item["url"])
            time.sleep(delay)

        url = next_page_url(html)
        if url and max_pages and page_num >= max_pages:
            log(f"  [*] Reached max pages ({max_pages})")
            break

    all_results = existing + new_results
    save(output, all_results)
    log(f"\nDone! Total: {len(all_results)} | New this run: {len(new_results)}")
    log(f"Saved to {output}")


if __name__ == "__main__":
    main()
