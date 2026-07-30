# WooCommerce jewelry catalog scraper

This project exports the public or authorized WooCommerce catalog at
`loosegrowndiamond.com`, including:

- Parent products and product metadata
- Product and variation images
- Variation names, IDs, SKUs, prices, stock, and attribute combinations
- Product attributes and global attribute terms
- Product categories and category hierarchy
- Normalized SQLite, JSONL, CSV, and downloaded media

The crawler uses [Scrapy](https://docs.scrapy.org/en/latest/) for scheduling,
throttling, retries, caching, and resumable jobs. It can optionally render product
pages through
[scrapy-playwright](https://github.com/scrapy-plugins/scrapy-playwright).

## Access modes

### 1. Read-only WooCommerce REST API — recommended

Set read-only credentials in the environment. Credentials are sent through an
HTTPS Basic Authorization header and are not written to output files.

```bash
export WC_CONSUMER_KEY='ck_...'
export WC_CONSUMER_SECRET='cs_...'
scrapy crawl catalog
```

This mode can retrieve exact category assignments, registered attributes, every
variation, private metadata allowed by the key, and other fields that may not be
rendered on public pages.

### 2. Public Store API and storefront crawl

```bash
scrapy crawl catalog
```

The spider first requests the unauthenticated WooCommerce Store API. It enriches
each result from the product page, including embedded WooCommerce variation JSON
and the product gallery. If the Store API is unavailable, it falls back to the
site's product sitemap.

Public mode can retrieve only published storefront data. It cannot guarantee
draft/private products or admin-only plugin metadata.

## Installation

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m playwright install chromium
```

Playwright's Chromium install is optional unless `use_playwright=true` is used.

## Safe first run

Start with five products:

```bash
scrapy crawl catalog -a max_products=5
```

Enable JavaScript rendering only if the static page does not expose variation
data:

```bash
scrapy crawl catalog -a max_products=5 -a use_playwright=true
```

Run the full catalog:

```bash
scrapy crawl catalog
```

The persistent Scrapy job directory is `work/jobs/catalog`, so an interrupted
run can resume. To intentionally start a separate crawl, set a different job
directory:

```bash
SCRAPER_JOBDIR=work/jobs/catalog-2 scrapy crawl catalog
```

## Output

By default, data is written under `outputs/catalog/`:

```text
outputs/catalog/
├── catalog.sqlite
├── products.jsonl
├── categories.jsonl
├── attributes.jsonl
├── diagnostics.jsonl
├── products.csv
├── variations.csv
├── categories.csv
├── product-categories.csv
├── attributes.csv
├── images.csv
├── crawl-summary.json
└── media/
    └── products/<product-id>/
```

To change the data and media locations together:

```bash
SCRAPER_MEDIA_STORE=outputs/run-2/media \
scrapy crawl catalog -a output_dir=outputs/run-2
```

## Responsible crawling

- Run only where you have authorization to copy the catalog and images.
- Keep the default concurrency and AutoThrottle settings unless the site owner
  approves higher traffic.
- Run from an allowed/whitelisted server. A geographic Sucuri block should be
  resolved by the site owner or hosting administrator, not bypassed.
- Review `crawl-summary.json` and `diagnostics.jsonl` before treating an export
  as complete.

