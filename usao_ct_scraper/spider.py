from urllib.parse import quote, urljoin, urlparse, parse_qs
from uuid import uuid4

import scrapy


class RemoteAPIMixin:
    """Generic mixin for spiders routing through a remote scraping API.

    Handles:
      - Building properly encoded API URLs (quote the target)
      - Resolving relative links against the original target URL (not response.url)
    """

    API_BASE = "http://54.208.226.232:8000/scrape"
    USE_PROXY = True

    def api_url(self, target_url):
        url = f"{self.API_BASE}?url={quote(target_url)}&format=html"
        if self.USE_PROXY:
            url += "&proxy=1"
        return url

    def _original_url(self, response):
        """Extract the original target URL from the API response's query string."""
        return parse_qs(urlparse(response.url).query).get("url", [None])[0]

    def resolve_link(self, href, response):
        """Resolve a relative/absolute href against the original target URL."""
        if not href or href.startswith(("http://", "https://")):
            return href
        original = self._original_url(response)
        if not original:
            return href
        return urljoin(original, href)


class UsaoPressReleasesSpider(RemoteAPIMixin, DeltaMixin, PaginatedLinksExtractor):
    name = "usao-ct-press-releases"
    API_BASE = "http://54.208.226.232:8000/scrape"
    LISTING_URL = "https://www.justice.gov/usao-ct/pr"

    proxy_off = True
    extra_headers = {}

    USE_PROXY = True

    MAX_RETRIES = 3

    static_data = {
        "source": "justice.gov",
        "integration_id": "USAO_CT",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from uuid import uuid4
        self.static_data = {**self.static_data, "batch_id": str(uuid4())}

    custom_settings = {
        "ITEM_PIPELINES": "",
        "DOWNLOAD_TIMEOUT": 120,
        "RETRY_TIMES": 0,
        "CONCURRENT_REQUESTS": 1,
    }

    def start_requests(self):
        yield scrapy.Request(
            self.api_url(self.LISTING_URL), self.parse, meta={"page": 0}
        )

    def parse(self, response):
        page = response.meta["page"]
        retry_count = response.meta.get("retry_count", 0)

        self.logger.info(f"Parsing listing page {page}")
        articles = response.css("article.news-content-listing.node-press-release")
        self.logger.info(f"Found {len(articles)} articles")

        next_el = response.css('a[aria-label="Next page"]')

        if not articles:
            if retry_count < self.MAX_RETRIES:
                self.logger.warning(
                    f"Page {page} returned 0 articles (retry {retry_count + 1}/{self.MAX_RETRIES}), retrying..."
                )
                yield scrapy.Request(
                    response.url,
                    self.parse,
                    meta={"page": page, "retry_count": retry_count + 1},
                    dont_filter=True,
                )
                return

            if not next_el:
                self.logger.info("No more articles")
                return
            self.logger.warning(
                f"Page {page} returned 0 articles after {self.MAX_RETRIES} retries, continuing to next page"
            )

        for article in articles:
            title_el = article.css("h2 a")
            title = title_el.css("::text").get()
            href = title_el.attrib.get("href", "")
            full_url = self.resolve_link(href, response)
            pub_date_el = article.css("time")
            pub_date = pub_date_el.attrib.get("datetime", "")
            teaser_el = article.css("p")
            teaser = teaser_el.css("::text").get()
            yield {
                **self.static_data,
                "link": full_url,
                "title": title,
                "pubDate": pub_date,
                "article_description": teaser,
                "article_body": None,
                "html_src": None,
            }

        if next_el:
            next_href = next_el.attrib.get("href", "")
            if next_href:
                next_url = self.resolve_link(next_href, response)
                yield scrapy.Request(
                    self.api_url(next_url), self.parse, meta={"page": page + 1}
                )


class UsaoPressReleasesSpider_article(BaseSpider):
    proxy_off = True
    name = "usao-ct-press-releases-article"
    integration_id = 'USAO_CT'
    body_selectors = ["div.node-content.node-press-release"]
    html_selectors = ["html"]
    fetch_embedded_links = False

    custom_settings = {
        'ITEM_PIPELINES': {
            'bots.pipelines.PostgresNewsDetailsPipeline': 300,
        },
        'DOWNLOAD_TIMEOUT': 120,
        'RETRY_TIMES': 0,
        'CONCURRENT_REQUESTS': 1,
    }

    def start_requests(self):
        cursor = fetch_links_from_database(integration_id=self.integration_id)
        for index, link in enumerate(cursor):
            if index % 50 == 0 and index != 0:
                self.logger.info("sleeping...")
                time.sleep(10)
            url = link[0]
            data_id = link[3]
            self.logger.info(f"Fetching [{index}] url={url[:100]} data_id={data_id}")
            api_url = f"http://54.208.226.232:8000/scrape?url={quote(url)}&format=html"
            yield Request(
                api_url,
                headers={},
                meta={
                    'dont_redirect': True,
                    'link': url,
                    'data_id': data_id,
                },
                errback=self.errback,
            )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
