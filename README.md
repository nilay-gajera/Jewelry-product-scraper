# WooCommerce jewelry catalog scraper

This project exports the public or authorized WooCommerce catalog at
`loosegrowndiamond.com`, including:

- Parent products and product metadata
- Product images plus per-variation galleries resolved from URLs and WordPress attachment IDs
- Variation names, IDs, SKUs, prices, stock, and attribute combinations
- Descriptions, short descriptions, SEO metadata, tags, brands, dimensions, and stock fields
- Product attributes, global attribute terms, categories, and category hierarchy
- Normalized SQLite, JSONL, CSV, S3 media, and WooCommerce import sheets

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

The spider first requests the unauthenticated WooCommerce Store API. Jewelry
pages are enriched from the source page, including the site's inline
`productVariations` payload. That payload contains the exact metal variation,
featured image, `wvg_images` gallery, and dynamically named setting details.

Public mode can retrieve only published storefront data. It cannot guarantee
draft/private products or admin-only plugin metadata.

For custom themes and gallery plugins, the parser also reads Store API
`extensions`, common gallery metadata keys, embedded `data-product_variations`
JSON, JSON-LD `ProductGroup` variants, structured prices/availability, and
custom-theme product description/SKU selectors. WooCommerce API variations stay
authoritative; structured variants are used only when that lookup fails.

Loose diamonds intentionally use a second checkpoint-enrichment pass. The fast
catalog pass stores every product first; enrichment then reads each saved diamond
page and captures every `.diamond-details-grid .dd-item` label, value, and optional
description. Unknown future labels are retained automatically and become dynamic
WooCommerce `meta:_diamond_*` columns instead of being discarded.

The Store API leaves individual loose-diamond category assignments empty even
though WooCommerce reports them under its generic Uncategorized bucket. Records
explicitly identified as `product_family=loose_diamond` are therefore assigned to
`Lab Grown Diamonds`; the assignment source is retained in dynamic metadata.
Variation attributes otherwise remain source-authoritative. Invalid placeholder
pairs such as `Ring Size = Ring Size` and `Back Setting = Back Setting` are omitted
from variation rows because they carry no selectable source value.

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

## Render Free deployment

The repository includes a Docker-based Render Blueprint and a small authenticated
control panel.

1. In Render, choose **New > Blueprint**.
2. Connect this repository.
3. Render reads `render.yaml` and creates a Free web service in Virginia.
4. Add the S3 and optional WooCommerce secrets listed below.
5. Open the service URL, enter the generated `CONTROL_TOKEN`, and start with five
   products.

Render Free has an ephemeral filesystem and can stop an idle service. Keep the
control page open while a crawl runs. The page polls live status as a normal
user-facing operation. Compressed S3 checkpoints preserve the normalized database
every 100 catalog products and every 500 enrichment updates by default. On a restart
or deploy, the service automatically restores the latest compressed checkpoint (or
the legacy uncompressed checkpoint) before serving the admin API. If a crawl is active during a
deploy, the service asks Scrapy to close cleanly so its final database is uploaded.

The Render container deliberately uses static HTTP/API extraction and does not
install a Playwright browser, which keeps memory usage suitable for the Free
instance.

### Required Render environment variables

Do not commit or paste secret values into source files.

| Variable | Required | Description |
|---|---:|---|
| `CONTROL_TOKEN` | Yes | Generated automatically by the Blueprint; protects start, stop, logs, and downloads. |
| `S3_BUCKET` | Yes | Destination bucket name. |
| `S3_PREFIX` | Yes | Object prefix; defaults to `jewelry-product-scraper`. |
| `AWS_ACCESS_KEY_ID` | Yes | IAM access key limited to the selected bucket/prefix. |
| `AWS_SECRET_ACCESS_KEY` | Yes | Matching IAM secret. |
| `AWS_REGION` | Yes | Bucket region, such as `us-east-1`. |
| `AWS_ENDPOINT_URL` | No | Custom S3-compatible endpoint for R2, B2, MinIO, and similar providers. |
| `S3_PUBLIC_BASE_URL` | Optional | Real public CDN/base URL ending at the media prefix, used in WooCommerce image columns. Never enter a placeholder URL. |
| `WC_CONSUMER_KEY` | Recommended | Read-only WooCommerce REST API consumer key. |
| `WC_CONSUMER_SECRET` | Recommended | Read-only WooCommerce REST API secret. |

The IAM identity needs `s3:GetObject`, `s3:PutObject`, and `s3:ListBucket` for
the configured prefix. Add `s3:DeleteObject` when the optional **delete downloaded
media** action should be available. Catalog archives remain private and are
downloaded through a one-hour presigned URL.

WooCommerce must be able to retrieve product images from direct online URLs.
Prefer a CloudFront/CDN URL in `S3_PUBLIC_BASE_URL`. If the bucket is private and
no public media URL is configured, the sheet retains the original source image
URLs instead. The admin panel does not require a public bucket or CDN: it creates
temporary signed S3 URLs for downloaded media.

### Count first and crawl selected categories

In **Crawl runs**, choose **Refresh inventory counts**. The dashboard reports
WooCommerce products separately from the storefront's advertised loose-diamond
inventory. Category selection applies to WooCommerce jewelry products; the
millions of loose diamonds are served by a separate dynamic inventory system.
Select one or more WooCommerce categories and then start the crawl. Products
assigned to overlapping selected categories are de-duplicated automatically.

The equivalent command-line filter is:

```bash
scrapy crawl catalog -a category_ids=11,12
```

### Delete products

Open a product in **Products** and select the trash button, or use the row
checkboxes and **Delete selected** to remove up to 500 products in one batch.
Deletion removes each parent, its variations, image records, and
category/attribute assignments from the active catalog. A batch replaces the S3
checkpoint and latest import files once before reporting success, so deleted
products do not return after a Render restart.

Downloaded S3 media is retained by default. Select **Also delete downloaded media
from S3** when those objects should be removed too. Historical `runs/` archives
are always preserved for recovery, and deletion is disabled while a crawl is
active.

## Safe first run

For the local admin app, copy `.env.local.example` to `.env.local`, insert a
newly rotated AWS key/secret, then double-click `run-local.command`. The secrets
file is ignored by Git. The launcher prints a strong control token and keeps the
service bound to `127.0.0.1`.

Start with five products:

```bash
scrapy crawl catalog -a max_products=5
```

After the catalog checkpoint exists, test enrichment on five saved diamonds or
variable jewelry products:

```bash
SCRAPER_JOBDIR=work/jobs/enrichment-test \
scrapy crawl catalog \
  -a output_dir=outputs/catalog \
  -a enrichment_mode=true \
  -a max_products=5
```

Then run the complete saved catalog enrichment:

```bash
SCRAPER_JOBDIR=work/jobs/enrichment-full \
scrapy crawl catalog \
  -a output_dir=outputs/catalog \
  -a enrichment_mode=true
```

In the admin panel, the equivalent choice is **Enrich saved catalog** with
**Resume from checkpoint** enabled. This mode updates products in place, downloads
only newly discovered gallery media, rebuilds the one master CSV, and writes the
updated checkpoint back to S3.

A Chrome VPN extension applies only to Chrome tabs. The background Scrapy process
must be on a system-wide VPN or use `SCRAPER_PROXY_URL` when the source blocks the
machine or server IP.

Enable JavaScript rendering only if the static page does not expose variation
data:

```bash
scrapy crawl catalog -a max_products=5 -a use_playwright=true
```

Run the full WooCommerce catalog (this does not include the separately served
2,500,000+ dynamic loose-diamond inventory feed):

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
├── woocommerce-master.csv
├── crawl-summary.json
└── media/
    └── products/<product-id>/
```

To change the data and media locations together:

```bash
SCRAPER_MEDIA_STORE=outputs/run-2/media \
scrapy crawl catalog -a output_dir=outputs/run-2
```

### WooCommerce master import

The export follows WooCommerce's built-in Product CSV Importer schema. Import
`woocommerce-master.csv` once; it contains simple products, variable parents,
and their variation rows in parent-first order.

Parent and variation SKUs are generated when the source lacks one. Category
hierarchies use `Parent > Child`, variation rows reference the parent SKU, the
first parent image is featured, and each variation receives its assigned image.
The exporter does not fill missing catalog fields with guessed defaults. Category
values come only from normalized assignments, and the `Images` column contains
only durable URLs derived from `S3_PUBLIC_BASE_URL`; it never falls back to the
original site. Every metadata key discovered on a product or variation becomes
a dynamic `meta:<key>` column, including newly encountered keys and structured
values. Always test a small import on a staging store before importing the full
catalog.

#### Fast local import with remote S3 media

For large local imports, install
`wordpress/mu-plugins/lgd-fast-catalog-import.php` in the destination site's
`wp-content/mu-plugins/` directory before starting WooCommerce's Product CSV
Importer. The must-use plugin increases the importer batch size to 200 and
creates lightweight WordPress attachment records that point directly to the
public S3 URLs. It does not download or resize the catalog images locally.

Keep the plugin active while importing and while the store uses those remote
attachments. This workflow requires every URL in the CSV `Images` column to be
publicly readable.

### Build the master CSV locally

Render is not required when the durable S3 checkpoint exists. Either configure
AWS credentials and download the checkpoint automatically:

```bash
S3_BUCKET=your-bucket \
S3_PREFIX=jewelry-product-scraper \
AWS_REGION=us-west-1 \
S3_PUBLIC_BASE_URL=https://your-public-media-base \
python -m lgd_scraper.local_export
```

Or download `checkpoints/catalog.sqlite` in the AWS Console and build from that
file without any AWS credentials:

```bash
python -m lgd_scraper.local_export \
  --database /path/to/catalog.sqlite \
  --public-base-url https://your-public-media-base
```

Both paths validate the SQLite checkpoint, generate only
`outputs/local-woocommerce-export/woocommerce-master.csv`, and run the strict
WooCommerce preflight before reporting success.

### S3 object layout

```text
s3://<bucket>/<prefix>/
├── checkpoints/catalog.sqlite
├── latest/catalog-export.zip
├── latest/woocommerce-master.csv
├── media/products/<product-id>/
└── runs/<timestamp>/
```

## Responsible crawling

- Run only where you have authorization to copy the catalog and images.
- Keep the default concurrency and AutoThrottle settings unless the site owner
  approves higher traffic.
- Run from an allowed/whitelisted server. A geographic Sucuri block should be
  resolved by the site owner or hosting administrator, not bypassed.
- Review `crawl-summary.json` and `diagnostics.jsonl` before treating an export
  as complete.
