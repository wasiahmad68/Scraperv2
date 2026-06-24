import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright, Page
from usao_ct_scraper.models import PressRelease
from usao_ct_scraper import config


async def extract_listing_links(page: Page) -> list[dict]:
    articles = await page.query_selector_all("article.news-content-listing.node-press-release")
    results = []

    for article in articles:
        title_el = await article.query_selector("h2 a")
        if not title_el:
            continue

        title = (await title_el.inner_text()).strip()
        href = await title_el.get_attribute("href")
        if not href:
            continue

        full_url = href if href.startswith("http") else config.BASE_URL + href

        date_el = await article.query_selector("time")
        date = await date_el.get_attribute("datetime") if date_el else None

        results.append({"title": title, "url": full_url, "date": date})

    return results


async def get_next_page_url(page: Page) -> str | None:
    next_el = await page.query_selector("a[aria-label='Next page']")
    if next_el:
        href = await next_el.get_attribute("href")
        if href:
            return f"{config.LISTING_URL}{href}"
    return None


async def extract_press_release(page: Page, url: str, listing_date: str | None) -> PressRelease | None:
    try:
        await page.goto(url, wait_until="networkidle", timeout=config.TIMEOUT)
    except Exception as e:
        print(f"    [!] Failed to load: {e}")
        return None

    h1 = await page.query_selector("h1.page-title")
    title = (await h1.inner_text()).strip() if h1 else ""

    time_el = await page.query_selector("article time[datetime]")
    date = await time_el.get_attribute("datetime") if time_el else listing_date

    date_text = None
    if time_el:
        date_text = (await time_el.inner_text()).strip()

    topic_el = await page.query_selector(".node-content:has(.node-type:has-text('Press Release'))")
    body_text = ""
    if topic_el:
        body_text = (await topic_el.inner_text()).strip()

    if not body_text:
        article = await page.query_selector("article.grid-row")
        if article:
            body_text = (await article.inner_text()).strip()

    body_text = re.sub(r"\n{3,}", "\n\n", body_text)

    return PressRelease(
        title=title or listing_date or "",
        url=url,
        date=date or listing_date,
        date_published=date_text,
        body_text=body_text,
    )


def load_existing(path: str) -> tuple[list[dict], set[str]]:
    if not Path(path).exists():
        return [], set()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    urls = {item["url"] for item in data}
    return data, urls


def save_results(path: str, results: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved {len(results)} results to {path}")


async def scrape_all():
    existing_data, scraped_urls = load_existing(config.OUTPUT_FILE)
    print(f"Loaded {len(existing_data)} existing records ({len(scraped_urls)} unique URLs)")

    new_results = []

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=config.HEADLESS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        page_num = 0
        url = config.LISTING_URL

        try:
            while url:
                page_num += 1
                print(f"\n--- Listing page {page_num}: {url}")

                try:
                    await page.goto(url, wait_until="networkidle", timeout=config.TIMEOUT)
                except Exception as e:
                    print(f"  [!] Failed to load listing: {e}")
                    break

                links = await extract_listing_links(page)
                new_links = [l for l in links if l["url"] not in scraped_urls]
                print(f"  Found {len(links)} items, {len(new_links)} new")

                if not new_links:
                    print("  [*] All items on this page already scraped, stopping")
                    break

                for item in new_links:
                    print(f"  >> {item['title'][:80]}")
                    pr = await extract_press_release(page, item["url"], item["date"])
                    if pr:
                        new_results.append(pr)
                        scraped_urls.add(item["url"])
                    await asyncio.sleep(config.RATE_LIMIT_DELAY)

                url = await get_next_page_url(page)
                if url and config.MAX_PAGES and page_num >= config.MAX_PAGES:
                    print(f"  [*] Reached max pages ({config.MAX_PAGES})")
                    break

        finally:
            await browser.close()

    all_results = list(existing_data)
    all_results.extend(r.to_dict() for r in new_results)
    save_results(config.OUTPUT_FILE, all_results)

    print(f"\n{'='*60}")
    print(f"Done! Total: {len(all_results)} | New this run: {len(new_results)}")
    print(f"Saved to: {config.OUTPUT_FILE}")


def main():
    asyncio.run(scrape_all())


if __name__ == "__main__":
    main()
