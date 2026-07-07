from datetime import datetime
from functools import wraps
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import requests
from scrapy import Spider, Request, Selector
from scrapy.http import Response
import csv
from io import StringIO
import pandas as pd
import os
from scrapy.exceptions import CloseSpider
from scrapy.http import Response, TextResponse

from bots import settings
from bots.pipelines import GUIDPipeline
from bots.utils import get_db_cursor

import uuid
from psycopg2.extras import execute_values

# import threading
from collections import deque

class SitemapLinksExtractor(Spider):
    name = "www.justice.gov"
    start_urls = ["https://www.justice.gov/sitemap.xml"]
    sitemap_page_pattern = r"https:\/\/www\.justice\.gov\/sitemap\.xml\?page=\d+"
    article_pattern = r"https:\/\/www\.justice\.gov\/[^\/]+\/pr\/[a-z0-9-]+"

    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'sec-ch-ua': '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
        'Accept-Encoding': 'gzip, deflate',
    }
    
    custom_settings = {
        "DOWNLOAD_DELAY": 2,
        "ITEM_PIPELINES": {
            "bots.pipelines.GUIDPipeline": 100,
            "bots.pipelines.PostgresLinkPipeline": 200,
        },
    }

    def start_requests(self):
        for url in self.start_urls:
            yield Request(url, headers=self.headers, callback=self.parse)

    def is_sitemap(self, url: str) -> bool:
        return bool(re.match(self.sitemap_page_pattern, url))

    def is_article(self, url: str) -> bool:
        return bool(re.match(self.article_pattern, url))

    def extract_urls(self, response: Response) -> list[str]:
        pattern = r"https?:\/\/[^\s\"'<>]+"
        return set(re.findall(pattern, response.text))

    def parse(self, response: Response):
        urls = self.extract_urls(response)
        for url in urls:
            if self.is_sitemap(url):
                yield Request(url=url, callback=self.parse)
            elif self.is_article(url):
                yield {
                    "parent_url": response.url,
                    "url": url,
                    "database": self.name,
                    "crawled": False,
                    "last_crawled_at": None,
                    "discovered_at": datetime.now(),
                    "html": None,
                }

class PaginatedLinksExtractor(Spider):
    name = "paginated-links-extractor-base"
    start_urls = ["https://www.justice.gov/usao/pressreleases"]
    article_links_selector = "div.views-row article.news-content-listing.node-press-release h2.news-title a"
    next_page_selector = None
    extra_headers = {}
    static_data = {}
    proxy_off = False

    base_headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'sec-ch-ua': '"Not)A;Brand";v="99", "Microsoft Edge";v="127", "Chromium";v="127"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0'
    }
    custom_settings = {
        "DOWNLOAD_DELAY": 2,
        "ITEM_PIPELINES": {
            "bots.pipelines.GUIDPipeline": 100,
            "bots.pipelines.PostgresLinkPipeline": 500,
        },
        "DUPEFILTER_CLASS": "scrapy.dupefilters.BaseDupeFilter",
    }

    def xml_to_dict(self, element):
            """
            Convert an XML element (including attributes) to a Python dictionary.
            """
            data = {}

            # 1) Add attributes (prefixed with @ like xmltodict)
            if element.attrib:
                for key, value in element.attrib.items():
                    data["@" + key] = value

            # 2) If element has child elements
            children = list(element)
            if children:
                for child in children:
                    child_dict = self.xml_to_dict(child)

                    # Handle repeated tags → convert to list
                    if child.tag in data:
                        if not isinstance(data[child.tag], list):
                            data[child.tag] = [data[child.tag]]
                        data[child.tag].append(child_dict)
                    else:
                        data[child.tag] = child_dict

                return data

            # 3) Leaf node → return text, but if attributes exist, include "#text"
            text = (element.text or "").strip()

            if element.attrib:
                data["#text"] = text
                return data

            # No attributes → return plain text
            return text

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        if cls.proxy_off:
            mw = crawler.settings.getdict('DOWNLOADER_MIDDLEWARES')
            mw['bots.middlewares.ProxyMiddleware'] = None
            crawler.settings.set('DOWNLOADER_MIDDLEWARES', mw, priority='spider')
        return super(PaginatedLinksExtractor, cls).from_crawler(crawler, *args, **kwargs)

    @property
    def headers(self):
        return {**self.base_headers, **self.extra_headers}

    def start_requests(self):
        start_urls = self.settings.get("START_URLS")
        start_urls = [start_urls] if start_urls else self.start_urls
        for url in start_urls:
            yield Request(url, headers=self.headers, callback=self.parse)

    def absolute_url(self, response: Response, url: str):
        response_url = urlparse(response.url)
        domain = f"{response_url.scheme}://{response_url.netloc}"
        base_url = f"{domain}{response_url.path}"
        # If root relative url
        if url[0] == "/":
            return domain + url
        # If absolute url
        elif url.startswith("https://") or url.startswith("http://"):
            return url
        # If relative url
        return base_url + url
    
    def get_article_links(self, response: Response):
        for a in response.css(self.article_links_selector):
            yield {
                "url": response.urljoin(a.attrib["href"]),
                "text": "".join(a.css('::text').getall()).strip(),
            }
    
    def get_next_page_link(self, response: Response):
        if not self.next_page_selector: return
        a = response.css(self.next_page_selector)
        if a:
            return response.urljoin(a[0].attrib["href"])
        
    def get_link_type(self, url: str):
        return "PDF" if url.lower().endswith(".pdf") else "HTML"

    def parse(self, response: Response):
        for url_item in self.get_article_links(response):
            obj = {
                **self.static_data,
                "link": url_item["url"],
                "title": url_item["text"],
                "pubDate": url_item.get("pubdate"),
                "article_description": url_item.get("article_description"),
                "article_body": url_item.get("article_body"),
                "created_at": datetime.now(),
                "html_src": url_item.get("html_src") or url_item.get("article_body"),
                "link_type": self.get_link_type(url_item["url"]),
                "json_dump": url_item.get("extra"),
                "links": url_item.get("links"),
            }
            yield obj
        next_page_link = self.get_next_page_link(response)
        if next_page_link:
            yield Request(url=next_page_link, callback=self.parse,headers=self.headers)


class RemovedItemsMixin:
    """
    Mixin to detect and handle removed items from a list between crawls.
    Assumes each item has a unique `data_id`
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        original_open = getattr(cls, "open_spider", None)

        @wraps(original_open)
        def wrapped_open(self, spider):
            self.current_ids = set()
            if original_open:
                return original_open(self, spider)

        cls.open_spider = wrapped_open

        original_closed = getattr(cls, "closed", None)

        @wraps(original_closed)
        def wrapped_closed(self, reason):
            current_ids = getattr(self, "current_ids", set())
            existing_ids = self._get_existing_ids()
            removed_ids = list(existing_ids - current_ids)

            # self._upsert_current_ids(current_ids)
            self._mark_removed_ids(removed_ids)

            if removed_ids:
                self.logger.info(f"{len(removed_ids)} items removed.")
                self.logger.debug(f"Removed IDs: {removed_ids}")

            if original_closed:
                return original_closed(self, reason)

        cls.closed = wrapped_closed


    def _get_existing_ids(self):
        table = getattr(self, "out_table", "public.urls")
        source_id = getattr(self, "source_id", None)
        if source_id:
            with get_db_cursor() as cur:
                cur.execute(f"SELECT data_id FROM {table} WHERE source_id = {source_id}")
                return set(row[0] for row in cur.fetchall())
        return set()

    def _mark_removed_ids(self, removed_ids):
        if not removed_ids:
            return

        deleted_table = "deleted_items"
        spider_name = getattr(self, "name", "unknown_spider")

        from datetime import datetime
        deleted_at = datetime.utcnow()

        with get_db_cursor() as cur:
            execute_values(cur,
                f"""
                INSERT INTO {deleted_table} (data_id, spider, deleted_at)
                VALUES %s
                ON CONFLICT (data_id) DO NOTHING
                """,
                [(data_id, spider_name, deleted_at) for data_id in removed_ids]
            )


    def _upsert_current_ids(self, ids):
        table = getattr(self, "out_table", "public.urls")

        with get_db_cursor() as cur:
            execute_values(cur,
                f"""
                INSERT INTO {table} (data_id, is_active)
                VALUES %s
                ON CONFLICT (data_id) DO UPDATE SET is_active = TRUE
                """,
                [(i, True) for i in ids]
            )

    # def closed(self, reason):
    #     current_ids = getattr(self, "current_ids", set())
    #     existing_ids = self._get_existing_ids()
    #     removed_ids = list(existing_ids - current_ids)

    #     self._upsert_current_ids(current_ids)
    #     self._mark_removed_ids(removed_ids)

    #     if removed_ids:
    #         self.logger.info(f"{len(removed_ids)} items removed.")
    #         self.logger.debug(f"Removed IDs: {removed_ids}")

    #     # if self._original_closed:
    #     #     self._original_closed(self, reason)


    #     if self._original_closed:
    #         self._original_closed(reason)



class DeltaModeMixin:

    def in_delta_mode(self):
        # return True
        mode = self.settings.get("SPIDER_MODE")
        return type(mode) is str and mode.upper() == "DELTA"


class DeltaMixin(DeltaModeMixin):
    """
    When a spider is run in DELTA mode, it closes the crawler when the first entry is 
    found in DB.
    This should be used for spiders where the article links are extracted in descending 
    order by publication date.
    """
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._original_parse = cls.parse
        cls.parse = DeltaMixin.parse

    def entry_exists(self, item):
        GUIDPipeline().process_item(item, self)
        table_name = getattr(self, "out_table", "public.urls")

        with get_db_cursor() as cursor:
            query = f'SELECT 1 FROM {table_name} where data_id= %s limit 1;'
            cursor.execute(query, (item["data_id"],))
            return cursor.fetchone() is not None

    def parse(self, *args, **kwargs):
        super_parse = self._original_parse
        if super_parse == self.parse:
            super_parse = super().parse
        delta_mode = self.in_delta_mode()
        for obj in super_parse(*args, **kwargs):
            if delta_mode and type(obj) is dict and self.entry_exists(obj):
                raise CloseSpider("Delta crawling completed!")
            yield obj


class UnorderedDeltaMixin(DeltaModeMixin):
    """
    This is a mixin class for any scrapy.Spider
    When spider runs in delta mode, it only insert those article links which are 
    not in database. It checks for whether articles links are in DB in batches,
    the batch size by default is 1000, which can be overrided in your spider. 
    It does not stop the crawler in the middle when entry is found in DB as for
    some sources, the latest articles are not at the top. For example, the articles
    might be ordered alphabetically by title. 
    """
    batch_size = 1000

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._original_parse = cls.parse
        cls.parse = UnorderedDeltaMixin.parse

    def get_new_items(self, items: list[dict]):
        for item in items:
            GUIDPipeline().process_item(item, self)
        data_ids = [d['data_id'] for d in items]
        with get_db_cursor() as cursor:
            query = "SELECT data_id FROM urls WHERE data_id = ANY(%s)"
            cursor.execute(query, (data_ids,))
            existing_data_ids = cursor.fetchall()
            existing_data_ids = {id[0] for id in existing_data_ids}
            new_items = [d for d in items if d['data_id'] not in existing_data_ids]
            return new_items

    def parse(self, *args, **kwargs):
        delta_mode = self.in_delta_mode()
        super_parse = self._original_parse
        if super_parse == self.parse:
            super_parse = super().parse
        super_iter = super_parse(*args, **kwargs)
        if not delta_mode:
            yield from super_iter
        batch_items = []
        for item in super_iter:
            if type(item) is dict:
                batch_items.append(item)
            else:
                yield item
            if len(batch_items) >= self.batch_size:
                yield from self.get_new_items(batch_items)
                batch_items = []
        if batch_items:
            yield from self.get_new_items(batch_items)

class ChangeTrackerMixin(DeltaModeMixin):
    batch_size = 1000

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scraped_links = set()  # Store scraped links in-memory

    def get_new_items(self, items: list[dict]):
        for item in items:
            GUIDPipeline().process_item(item, self)
            self.scraped_links.add(item['link'])  # Track links in-memory
        
        data_ids = [d['data_id'] for d in items]

        with get_db_cursor() as cursor:
            # Fetch only the data we need
            query = "SELECT data_id, link FROM urls WHERE data_id = ANY(%s)"
            cursor.execute(query, (data_ids,))
            existing_data = cursor.fetchall()

            existing_data_ids = {row[0] for row in existing_data}
            new_items = [d for d in items if d['data_id'] not in existing_data_ids]

            # Insert/Update directly; Postgres trigger handles change detection
            for item in items:
                if item['data_id'] in existing_data_ids:
                    cursor.execute("""
                        UPDATE urls
                        SET content_hash = %s
                        WHERE link = %s
                    """, (item['data_id'], item['link']))
                else:
                    cursor.execute("""
                        INSERT INTO urls (link, content_hash, json_dump)
                        VALUES (%s, %s, %s)
                    """, (item['link'], item['data_id'], item['json_dump']))

        return new_items

    def detect_deleted_items(self):
        """Detect and notify for deleted items using in-memory tracking."""
        if not self.scraped_links:
            return  # Nothing scraped, skip detection

        with get_db_cursor() as cursor:
            # Fetch links that no longer exist in the current scrape
            cursor.execute("""
                SELECT link FROM urls
                WHERE link NOT IN %s
                AND source_id = %s
            """, (tuple(self.scraped_links), self.static_data['source_id']))

            deleted_links = [row[0] for row in cursor.fetchall()]

            # Notify about deleted links
            for link in deleted_links:
                send_alert(f"Item deleted: {link}")



# class ChangeTrackerMixin(DeltaModeMixin):
#     """
#     This is a mixin class for any scrapy.Spider
#     It checks for whether articles links are in DB in batches,
#     the batch size by default is 1000, which can be overrided in your spider. 
#     It does not stop the crawler in the middle when entry is found in DB as for
#     some sources, the latest articles are not at the top. For example, the articles
#     might be ordered alphabetically by title. 
#     NOTE: Goal is to detect changes {new, updated, deleted} in the source website.
#     updated is detected by checking if the article hash is already in the database and the new_hash is different.
#     deleted is detected by checking if there is any item in db which is not found in the current run
#     """
#     batch_size = 1000

#     def __init_subclass__(cls, **kwargs):
#         super().__init_subclass__(**kwargs)
#         cls._original_parse = cls.parse
#         cls.parse = UnorderedDeltaMixin.parse

#     def get_new_items(self, items: list[dict]):
#         for item in items:
#             GUIDPipeline().process_item(item, self)
#         data_ids = [d['data_id'] for d in items]
#         with get_db_cursor() as cursor:
#             query = "SELECT data_id FROM urls WHERE data_id = ANY(%s)"
#             cursor.execute(query, (data_ids,))
#             existing_data_ids = cursor.fetchall()
#             existing_data_ids = {id[0] for id in existing_data_ids}
#             new_items = [d for d in items if d['data_id'] not in existing_data_ids]
#             return new_items

#     def parse(self, *args, **kwargs):
#         delta_mode = self.in_delta_mode()
#         super_parse = self._original_parse
#         if super_parse == self.parse:
#             super_parse = super().parse
#         super_iter = super_parse(*args, **kwargs)
#         # if not delta_mode:
#         #     yield from super_iter
#         batch_items = []
#         for item in super_iter:
#             if type(item) is dict:
#                 batch_items.append(item)
#             else:
#                 yield item
#             if len(batch_items) >= self.batch_size:
#                 yield from self.get_new_items(batch_items)
#                 batch_items = []
#         if batch_items:
#             yield from self.get_new_items(batch_items)
 

class SeparateLinkTitleColumnsMixin:
    """
    For cases when the link and title of articles are in separate columns, for example
    <table>
        <tr>
            <td><a href="link/to/article1.html"></a></td>
            <td><span>Article 1</span></td>
            <td><span>2021-09-07</span></td>
        </tr>
        <tr>
            <td><a href="link/to/article2.html"></a></td>
            <td><span>Article 2</span></td>
            <td><span>2021-09-07</span></td>
        </tr>
    </table>
    The selector would be:

    link_parent_selector = "table tr"
    link_selector = "td:nth-of-type(1) a"
    title_selector = "td:nth-of-type(2) span"
    extra_selectors = {
        # "order_date": "td:nth-of-type(3) span",
    }
 
    NOTE: The selector for link_selector and title_selector are relative to parent
    """
    link_parent_selector = None  # The parent element containing the article link
    link_selector = None  # The css selector of the link relative to the parent
    title_selector = None  # The css selector of the title relative to the parent
    pubdate_selector = None
    pubdate_format = None
    article_description_selector = None
    article_body_selector = None
    other_links_selector = []
    extra_selectors = {}

    def parse_pubdate(self, pubdate_str: str | None) -> datetime | None:
        
        if pubdate_str is None or self.pubdate_format is None:
            return
        if type(self.pubdate_format) is str:
            self.pubdate_format = [self.pubdate_format]
        for pubdate_format in self.pubdate_format:
            try:
                print(pubdate_format, pubdate_str)
                return datetime.strptime(pubdate_str.strip(), pubdate_format)
            except (ValueError, TypeError):
                continue

    def get_article_links(self, response: Response):
        for parent in response.css(self.link_parent_selector):
            link = parent.css(self.link_selector)
            title = parent.css(self.title_selector)
            if self.article_description_selector:
                article_description = parent.css(self.article_description_selector)
                article_description = "".join(dict.fromkeys(article_description.css('::text').getall())).strip()
            else:
                article_description = None
            if self.article_body_selector:
                article_body = parent.css(self.article_body_selector)
                article_body = "".join(dict.fromkeys(article_body.css('::text').getall())).strip()
            else:
                article_body = None
            pubdate = None
            if self.pubdate_selector:
                
                pubdates= parent.css(self.pubdate_selector).css("::text").getall()
                """
                using get() might fail in case of empty preceeding text
                """
                if pubdates:
                    pubdate_str = next((d for d in pubdates if d.strip()), None)
                    pubdate = self.parse_pubdate(pubdate_str)
            extra = {}
            if type(self.extra_selectors) is dict:
                for key, selector in self.extra_selectors.items():
                    extra[key] = "".join(dict.fromkeys(parent.css(selector).css("::text").getall())).strip()
            try:
                url = response.urljoin(link.attrib["href"])
            except KeyError:
                # No link found, skip
                continue

            links = []
            if(self.other_links_selector):
                for other_link in self.other_links_selector:
                    for a in parent.css(other_link).getall():
                        href = Selector(text=a).css('::attr(href)').get()
                        text = Selector(text=a).css('::text').get()
                        if href:
                            links.append({'text': text, 'link': response.urljoin(href)})

            obj = {
                "url": url,
                "text": "".join(dict.fromkeys(title.css('::text').getall())).strip(),
                "article_description": article_description,
                "article_body": article_body,
                "pubdate": pubdate,
                "links": links,
            }
            if extra:
                obj["extra"] = extra
            yield obj


class CSVLinksExtractor(Spider):
    name = "csv-links-extractor-base"
    start_urls = []
    csv_selectors = ""
    extra_headers = {}
    static_data = {}
    proxy_off = False
 
    base_headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'sec-ch-ua': '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
        'Accept-Encoding': 'gzip, deflate',
    }
    custom_settings = {
        "DOWNLOAD_DELAY": 2,
        "ITEM_PIPELINES": {
        },
        "DUPEFILTER_CLASS": "scrapy.dupefilters.BaseDupeFilter",
    }
 
    def __init__(self, *args , **kwargs ):
        super(CSVLinksExtractor, self).__init__(*args, **kwargs)
        if self.proxy_off:
            self.custom_settings["DOWNLOADER_MIDDLEWARES"]["bots.middlewares.ProxyMiddleware"] = None
 
    @property
    def headers(self):
        return {**self.base_headers, **self.extra_headers}
 
    def parse(self, response: Response):
        for a in response.css(self.csv_selectors):
            csv_link = a.attrib['href']
            if csv_link.lower().endswith(".xlsx"):
                yield Request(response.urljoin(csv_link), callback=self.parse_xlsx_file, meta={'url': csv_link})
            else:
                yield Request(response.urljoin(csv_link), callback=self.parse_csv_file, meta={'url': csv_link})
            
           
    def parse_csv_file(self, response: Response):
        filename = str(uuid.uuid4())
        path = os.path.join('/tmp', filename)
        self.logger.info('Saving CSV %s', path)
        with open(path, 'wb') as f:
            f.write(response.body)
       
        df = pd.read_csv(path,encoding_errors='ignore')

        for index, row in df.iterrows():
            yield row.to_dict()
    
    def parse_xlsx_file(self, response: Response):
        filename = str(uuid.uuid4())
        path = os.path.join('/tmp', filename)
        self.logger.info('Saving XLSX %s', path)
        with open(path, 'wb') as f:
            f.write(response.body)

        df = pd.read_excel(path)

        for index, row in df.iterrows():
            yield row.to_dict()

    

       
class DynamicStartURLsMixin:
    """
    In some sites, the articles list is grouped by some meta data like the
    year in which it was published, or the starting letter of the article
    title (in cases when article title is entity's name). In these sites,
    there can be multiple start urls, this mixin help in identifying all
    the start urls.

    start_urls_selector = css selector of the start url links
    """

    start_urls_selector: str | None = None

    def start_requests(self):
        if not self.start_urls_selector or type(self.start_urls_selector) is not str:
            raise ValueError(f'The DynamicStartURLsMixin.start_urls_selector is invalid!')
        start_urls = self.get_start_urls()
        for url in start_urls:
            yield Request(url, headers=self.headers, callback=self.parse)

    def get_start_urls(self):
        start_urls = [*self.start_urls]
        for initial_url in self.start_urls:
            r = requests.get(initial_url, headers=self.headers)
            response = TextResponse(url=initial_url, headers=self.headers, status=r.status_code, body=r.content)
            a_tags = response.css(self.start_urls_selector)
            for a_tag in a_tags:
                start_urls.append(response.urljoin(a_tag.attrib["href"]))
        return list(dict.fromkeys(start_urls))


class DrupalAjaxLinksExtractor(SeparateLinkTitleColumnsMixin, PaginatedLinksExtractor):
    
    def increment_page_param(self, url):
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        query_params['page'] = [str(int(query_params.get('page', ['0'])[0]) + 1)]
        new_query_string = urlencode(query_params, doseq=True)
        return urlunparse(parsed_url._replace(query=new_query_string))

    def get_next_page_link(self, response: Response):
        return self.increment_page_param(response.url)
    
    def response_to_json(self, response: Response):
        text = "".join(response.css("textarea").css('::text').getall()).strip()
        text = text.strip()
        return json.loads(text)

    def parse(self, response: Response):
        drupal_ajax_response = self.response_to_json(response)
        links_found = False
        for command in drupal_ajax_response:
            if command['command'] == 'insert' and command.get('data'):
                decoded_html = command["data"].encode('utf-8').decode('unicode-escape')
                decoded_html = re.sub(r'\\/', '/', decoded_html)     # Replace escaped slashes
                decoded_html = re.sub(r'>\s+<', '><', decoded_html)  # removes newlines between tags
                decoded_html = re.sub(r'\s+>', '>', decoded_html)    # removes newlines before closing tags
                decoded_html = re.sub(r'\n\s*', ' ', decoded_html)   # removes newlines within tags
                html_respoonse = TextResponse(
                    url=response.url,
                    headers=self.headers,
                    status=response.status,
                    body=decoded_html.encode("utf-8"),
                )
                for url_item in self.get_article_links(html_respoonse):
                    links_found = True
                    item = {
                        **self.static_data,
                        "link": url_item["url"],
                        "title": url_item["text"],
                        "pubDate": url_item.get("pubdate"),
                        "article_description": url_item.get("article_description"),
                        "created_at": datetime.now(),
                        "html_src": None,
                        "link_type": self.get_link_type(url_item["url"]),
                        "json_dump": url_item.get("extra"),
                        "links": url_item.get("links"),
                    }
                    yield item
        # Check last page
        if not links_found:
            raise CloseSpider("Drupal AJAX last page!")
        next_page_link = self.get_next_page_link(response)
        yield Request(url=next_page_link, callback=self.parse, headers=self.headers)
          

class WindowedDeltaMixin(DeltaModeMixin):
    """
    Delta mode with order-aware duplicate tracking.
    Uses a sliding window to track the last N items processed.
    """
    DELTA_THRESHOLD = 5
    
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._original_parse = cls.parse
        cls.parse = WindowedDeltaMixin.parse
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keep track of last N items (True=duplicate, False=new)
        threshold = getattr(self, 'DELTA_THRESHOLD', 5)
        self._recent_items = deque(maxlen=threshold)

    def entry_exists(self, item):
        GUIDPipeline().process_item(item, self)
        table_name = getattr(self, "out_table", "public.urls")

        with get_db_cursor() as cursor:
            query = f'SELECT 1 FROM {table_name} WHERE data_id = %s LIMIT 1;'
            cursor.execute(query, (item["data_id"],))
            return cursor.fetchone() is not None

    def parse(self, *args, **kwargs):
        super_parse = self._original_parse
        if super_parse == self.parse:
            super_parse = super().parse
        
        delta_mode = self.in_delta_mode()
        threshold = getattr(self, 'DELTA_THRESHOLD', 5)
        
        for obj in super_parse(*args, **kwargs):
            if delta_mode and isinstance(obj, dict):
                exists = self.entry_exists(obj)
                
                # Add to sliding window
                self._recent_items.append(exists)
                
                # Check if ALL recent items are duplicates
                if (len(self._recent_items) == threshold and 
                    all(self._recent_items)):
                    
                    self.logger.info(
                        f"Last {threshold} items all duplicates. Stopping."
                    )
                    raise CloseSpider(
                        f"Delta crawling completed! "
                        f"Last {threshold} items were all duplicates."
                    )
                
                duplicate_count = sum(self._recent_items)
                self.logger.debug(
                    f"Item {'exists' if exists else 'new'}. "
                    f"Window: {duplicate_count}/{len(self._recent_items)} duplicates"
                )
            
            yield obj


