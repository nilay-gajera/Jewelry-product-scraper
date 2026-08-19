from __future__ import annotations

import os


BOT_NAME = "lgd_scraper"

SPIDER_MODULES = ["lgd_scraper.spiders"]
NEWSPIDER_MODULE = "lgd_scraper.spiders"

ROBOTSTXT_OBEY = os.getenv("SCRAPER_OBEY_ROBOTS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
COOKIES_ENABLED = True

# Conservative defaults: this is a focused catalog export, not a broad crawl.
CONCURRENT_REQUESTS = int(os.getenv("SCRAPER_CONCURRENCY", "2"))
CONCURRENT_REQUESTS_PER_DOMAIN = int(os.getenv("SCRAPER_CONCURRENCY", "2"))
DOWNLOAD_DELAY = float(os.getenv("SCRAPER_DOWNLOAD_DELAY", "1.0"))
RANDOMIZE_DOWNLOAD_DELAY = True
DOWNLOAD_TIMEOUT = int(os.getenv("SCRAPER_DOWNLOAD_TIMEOUT", "45"))

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
# Must match CONCURRENT_REQUESTS so AutoThrottle does not silently
# reduce effective parallelism below the configured level.
AUTOTHROTTLE_TARGET_CONCURRENCY = float(os.getenv("SCRAPER_CONCURRENCY", "2"))

RETRY_ENABLED = True
RETRY_TIMES = int(os.getenv("SCRAPER_RETRY_TIMES", "3"))
RETRY_HTTP_CODES = [408, 425, 429, 500, 502, 503, 504, 522, 524]

# Off by default. The cache stores every response body with no size cap, so a
# 40K-product crawl writes several GB to an ephemeral container disk. Enable it
# deliberately (SCRAPER_HTTPCACHE=1) for local development against a live site.
HTTPCACHE_ENABLED = os.getenv("SCRAPER_HTTPCACHE", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
HTTPCACHE_DIR = "work/httpcache"
HTTPCACHE_EXPIRATION_SECS = int(os.getenv("SCRAPER_HTTPCACHE_EXPIRY", "86400"))  # 24h
HTTPCACHE_IGNORE_HTTP_CODES = [307, 401, 403, 408, 425, 429, 500, 502, 503, 504]

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
    "lgd_scraper.pipelines.CatalogMediaPipeline": 100,
    "lgd_scraper.pipelines.CatalogWriterPipeline": 300,
    # Upload only after the writer has committed the current product.
    "lgd_scraper.s3sync.S3ArtifactPipeline": 400,
}
CATALOG_DOWNLOAD_MEDIA = os.getenv("SCRAPER_DOWNLOAD_MEDIA", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DOWNLOADER_MIDDLEWARES = {
    "lgd_scraper.middlewares.SucuriCookieChallengeMiddleware": 650,
}
SUCURI_CHALLENGE_MAX_RETRIES = 2

FILES_STORE = os.getenv("SCRAPER_MEDIA_STORE", "outputs/catalog/media")
FILES_EXPIRES = 0
FILES_STORE_S3_ACL = os.getenv("S3_MEDIA_ACL", "none")

# Do not copy credentials into Scrapy settings. Scrapy prints overridden
# settings at startup, while botocore can securely discover the same values
# directly from the process environment.
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL")
AWS_REGION_NAME = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION"))

FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = os.getenv("SCRAPER_LOG_LEVEL", "INFO")
TELNETCONSOLE_ENABLED = False

# Do not initialize Chromium/download handlers for the ordinary WooCommerce
# catalog crawl. On Render's 512 MB Free instance, merely starting both
# handlers consumes a meaningful part of the container memory even though no
# request has meta["playwright"]. The separate enrichment task opts in.
PLAYWRIGHT_ENABLED = os.getenv("SCRAPER_USE_PLAYWRIGHT", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if PLAYWRIGHT_ENABLED:
    DOWNLOAD_HANDLERS = {
        "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    }
    TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
    PLAYWRIGHT_BROWSER_TYPE = "chromium"
    PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = 1
    PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 45_000
