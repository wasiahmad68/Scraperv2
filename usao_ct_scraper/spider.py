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






class UsaoPressReleasesSpider(DeltaMixin, PaginatedLinksExtractor):
    name = "usao-ct-press-releases"
    API_BASE = "http://54.208.226.232:8000/scrape"
    LISTING_URL = "https://www.justice.gov/usao-ct/pr"
    BASE_URL = "https://www.justice.gov"

    proxy_off = True
    extra_headers = {}

    USE_PROXY = True

    MAX_RETRIES = 3
    handle_httpstatus_list = [502, 503, 504]

    static_data = {
        "country": "Canada",
        "is_federal_level": False,
        "database": "Ontario Superior Court",
        "category": "Decisions by Superior Court of Justice",
        "source_id": "REGENS000405"
    }

    custom_settings = {
        "DOWNLOAD_TIMEOUT": 120,
        "RETRY_TIMES": 0,
        "CONCURRENT_REQUESTS": 1,
        "ITEM_PIPELINES": {
            "bots.pipelines.GUIDPipeline": 100,
            "bots.pipelines.PostgresLinkPipeline": 500,
        },
        "DUPEFILTER_CLASS": "scrapy.dupefilters.BaseDupeFilter",
    }

    def start_requests(self):
        api_url = f"{self.API_BASE}?url={self.LISTING_URL}&format=html"
        if self.USE_PROXY:
            api_url += "&proxy=1"
        yield scrapy.Request(api_url, self.parse, meta={"page": 0})

    def parse(self, response):
        page = response.meta["page"]
        retry_count = response.meta.get("retry_count", 0)

        self.logger.info(f"Parsing listing page {page} (status {response.status})")

        if response.status != 200:
            self.logger.warning(f"Non-200 status {response.status}, will retry")

        try:
            articles = response.css("article.news-content-listing.node-press-release")
        except ValueError:
            articles = []
        self.logger.info(f"Found {len(articles)} articles")

        try:
            next_el = response.css('a[aria-label="Next page"]')
        except ValueError:
            next_el = []

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

            self.logger.warning(
                f"Page {page} exhausted retries, status={response.status}"
            )

        for article in articles:
            title_el = article.css("h2 a")
            title = title_el.css("::text").get()
            href = title_el.attrib.get("href", "")
            full_url = (
                f"{self.BASE_URL}{href}" if href.startswith("/") else href
            )
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
                "created_at": datetime.now(),
                "link_type": "HTML",
                "json_dump": None,
                "links": None,
            }

        if next_el:
            next_href = next_el.attrib.get("href", "")
            if next_href:
                next_url = f"{self.LISTING_URL}{next_href}"
            else:
                self.logger.warning("Next element has no href")
                return
        elif response.status == 200:
            self.logger.info("No more articles")
            return
        else:
            next_url = f"{self.LISTING_URL}?page={page + 1}"

        api_url = f"{self.API_BASE}?url={next_url}&format=html"
        if self.USE_PROXY:
            api_url += "&proxy=1"
        yield scrapy.Request(api_url, self.parse, meta={"page": page + 1})




# class CanliiSpider(DeltaMixin, PaginatedLinksExtractor):
#     name = "canlii_onsc"
#     proxy_off = True

#     # Years from 1823 to current year
#     start_year = 1823
#     end_year = datetime.now().year

#     # start_urls = [
#     #     f"https://www.canlii.org/en/on/onsc/nav/date/{year}/items"
#     #     for year in range(start_year, end_year + 1)
#     # ]
#     start_urls = ['https://www.canlii.org/on/onsc/nav/date/2026/items']
#     pubdate_format = "%Y-%m-%d"

#     static_data = {
#         "country": "Canada",
#         "is_federal_level": False,
#         "database": "Ontario Superior Court",
#         "category": "Decisions by Superior Court of Justice",
#         "source_id": "REGENS000405"
#     }

#     def parse_pubdate(self, pubdate_str: str | None) -> datetime | None:

#         if pubdate_str is None or self.pubdate_format is None:
#             return

#         if type(self.pubdate_format) is str:
#             self.pubdate_format = [self.pubdate_format]

#         for pubdate_format in self.pubdate_format:
#             try:
#                 return datetime.strptime(pubdate_str.strip(), pubdate_format)
#             except (ValueError, TypeError):
#                 continue


#     def start_requests(self):
        
#         # proxy_middleware = ProxyMiddleware()
#         for url in self.start_urls:
#             # proxy_url = proxy_middleware.get_proxy(self)

#             # playwright_proxy = (
#             #     proxy_middleware.build_playwright_proxy(
#             #         proxy_url
#             #     )
#             # )

#             yield scrapy.Request(
#                 url=url,

#                 meta={
#                     "playwright": True,

#                     "playwright_page_methods": [
#                         PageMethod(
#                             "wait_for_load_state",
#                             "networkidle"
#                         ),
#                     ],
#                 },

#                 callback=self.parse,
#                 dont_filter=True,
#             )

#     def parse(self, response):

#         try:
#             json_data = json.loads(response.text)
#         except Exception:
#             self.logger.warning(f"Invalid JSON: {response.url}")
#             return

#         # Some years may return empty list
#         if not json_data:
#             self.logger.info(f"No data found: {response.url}")
#             return

#         for row in json_data:
#             style_of_cause = row.get("styleOfCause")
#             citation = row.get("citation")
#             judgment_date = row.get("judgmentDate")
#             relative_url = row.get("url")

#             full_url = (
#                 response.urljoin(relative_url)
#                 if relative_url
#                 else None
#             )

#             item = {
#                 **self.static_data,
#                 "link": full_url,
#                 "title": style_of_cause.strip() if style_of_cause else None,
#                 "created_at": datetime.now(),
#                 "pubDate": self.parse_pubdate(judgment_date),
#                 "article_description": citation,
#                 "article_body": citation,
#                 "link_type": "HTML",
#                 "html_src": None,
#                 "json_dump": row,
#                 "links": None,
#             }

#             yield item

class UsaoPressReleasesSpider_article(BaseSpider):
    proxy_off = True
    name = "usao-ct-press-releases-article"
    integration_id = "USAO_CT"
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
            yield scrapy.Request(
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

