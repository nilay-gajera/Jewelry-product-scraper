from __future__ import annotations

import os


BOT_NAME = "lgd_scraper"

SPIDER_MODULES = ["lgd_scraper.spiders"]
NEWSPIDER_MODULE = "lgd_scraper.spiders"

ROBOTSTXT_OBEY = True
COOKIES_ENABLED = True

# Conservative defaults: this is a focused catalog export, not a broad crawl.
CONCURRENT_REQUESTS = int(os.getenv("SCRAPER_CONCURRENCY", "2"))
CONCURRENT_REQUESTS_PER_DOMAIN = int(os.getenv("SCRAPER_CONCURRENCY", "2"))
DOWNLOAD_DELAY = float(os.getenv("SCRAPER_DOWNLOAD_DELAY", "1.0"))
RANDOMIZE_DOWNLOAD_DELAY = True
DOWNLOAD_TIMEOUT = 45

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0

RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [408, 425, 429, 500, 502, 503, 504, 522, 524]

HTTPCACHE_ENABLED = True
HTTPCACHE_DIR = "work/httpcache"
HTTPCACHE_IGNORE_HTTP_CODES = [401, 403, 408, 425, 429, 500, 502, 503, 504]

JOBDIR = os.getenv("SCRAPER_JOBDIR", "work/jobs/catalog")

USER_AGENT = os.getenv(
    "SCRAPER_USER_AGENT",
    "AuthorizedCatalogExporter/1.0 (+WooCommerce catalog migration)",
)
DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ITEM_PIPELINES = {
    "lgd_scraper.s3sync.S3ArtifactPipeline": 50,
    "lgd_scraper.pipelines.CatalogMediaPipeline": 100,
    "lgd_scraper.pipelines.CatalogWriterPipeline": 300,
}

FILES_STORE = os.getenv("SCRAPER_MEDIA_STORE", "outputs/catalog/media")
FILES_EXPIRES = 0
FILES_STORE_S3_ACL = os.getenv("S3_MEDIA_ACL", "private")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN")
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL")
AWS_REGION_NAME = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION"))

FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = os.getenv("SCRAPER_LOG_LEVEL", "INFO")
TELNETCONSOLE_ENABLED = False

# Enabled only for requests whose meta["playwright"] is true.
DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = 2
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 45_000
