from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from scrapy import Request
from scrapy.pipelines.files import FilesPipeline


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _safe_segment(value: Any, fallback: str = "unknown") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return cleaned[:120] or fallback


class CatalogMediaPipeline(FilesPipeline):
    """Download each unique product/variation image and retain its associations."""

    def get_media_requests(self, item, info):
        if item.get("_record_type") != "product":
            return

        seen: set[str] = set()
        for media in item.get("media", []):
            url = media.get("source_url")
            if not url or url in seen:
                continue
            seen.add(url)
            yield Request(
                url,
                headers={"Referer": item.get("url", "")},
                meta={
                    "catalog_product_id": str(item.get("id") or item.get("slug") or "unknown"),
                },
                dont_filter=True,
            )

    def file_path(self, request, response=None, info=None, *, item=None):
        product_id = _safe_segment(request.meta.get("catalog_product_id"))
        parsed = urlparse(request.url)
        basename = _safe_segment(unquote(Path(parsed.path).name), "image")
        suffix = Path(basename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg"}:
            suffix = ".bin"
        stem = _safe_segment(Path(basename).stem, "image")[:70]
        digest = hashlib.sha1(request.url.encode("utf-8")).hexdigest()[:12]
        return f"products/{product_id}/{stem}-{digest}{suffix}"

    def item_completed(self, results, item, info):
        by_url: dict[str, dict[str, Any]] = {}
        for ok, result in results:
            if not ok:
                continue
            by_url[result["url"]] = {
                "local_path": result.get("path"),
                "checksum": result.get("checksum"),
                "download_status": result.get("status"),
            }

        for media in item.get("media", []):
            media.update(by_url.get(media.get("source_url", ""), {}))
        return item


class CatalogWriterPipeline:
    """Write nested JSONL, normalized SQLite, flat CSVs, and a crawl summary."""

    def __init__(self):
        self.output_dir = Path("outputs/catalog")
        self.connection: sqlite3.Connection | None = None
        self.handles: dict[str, Any] = {}
        self.counts: Counter[str] = Counter()
        self.crawler = None

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        instance.crawler = crawler
        return instance

    def open_spider(self):
        spider = self.crawler.spider
        self.output_dir = Path(getattr(spider, "output_dir", "outputs/catalog")).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        for record_type in ("products", "categories", "attributes", "diagnostics"):
            self.handles[record_type] = (self.output_dir / f"{record_type}.jsonl").open(
                "a", encoding="utf-8"
            )

        self.connection = sqlite3.connect(self.output_dir / "catalog.sqlite")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT,
                slug TEXT,
                url TEXT,
                sku TEXT,
                product_type TEXT,
                currency TEXT,
                price TEXT,
                regular_price TEXT,
                sale_price TEXT,
                stock_status TEXT,
                source TEXT,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS variations (
                product_id TEXT NOT NULL,
                id TEXT NOT NULL,
                name TEXT,
                sku TEXT,
                price TEXT,
                regular_price TEXT,
                sale_price TEXT,
                stock_status TEXT,
                image_url TEXT,
                attributes_json TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (product_id, id)
            );
            CREATE TABLE IF NOT EXISTS categories (
                id TEXT PRIMARY KEY,
                name TEXT,
                slug TEXT,
                parent_id TEXT,
                description TEXT,
                image_url TEXT,
                product_count INTEGER,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS product_categories (
                product_id TEXT NOT NULL,
                category_id TEXT NOT NULL,
                category_name TEXT,
                category_slug TEXT,
                PRIMARY KEY (product_id, category_id)
            );
            CREATE TABLE IF NOT EXISTS attributes (
                id TEXT PRIMARY KEY,
                name TEXT,
                taxonomy TEXT,
                attribute_type TEXT,
                order_by TEXT,
                has_archives INTEGER,
                terms_json TEXT,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS product_attributes (
                product_id TEXT NOT NULL,
                attribute_key TEXT NOT NULL,
                attribute_name TEXT,
                variation INTEGER,
                visible INTEGER,
                options_json TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (product_id, attribute_key)
            );
            CREATE TABLE IF NOT EXISTS images (
                product_id TEXT NOT NULL,
                variation_id TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                source_url TEXT NOT NULL,
                local_path TEXT,
                alt TEXT,
                width INTEGER,
                height INTEGER,
                checksum TEXT,
                PRIMARY KEY (product_id, variation_id, role, position, source_url)
            );
            CREATE TABLE IF NOT EXISTS diagnostics (
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                kind TEXT,
                url TEXT,
                status INTEGER,
                message TEXT,
                raw_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def process_item(self, item):
        record_type = item.get("_record_type", "product")
        self.counts[record_type] += 1

        if record_type == "product":
            self._write_jsonl("products", item)
            self._store_product(item)
        elif record_type == "category":
            self._write_jsonl("categories", item)
            self._store_category(item)
        elif record_type == "attribute":
            self._write_jsonl("attributes", item)
            self._store_attribute(item)
        else:
            self._write_jsonl("diagnostics", item)
            self._store_diagnostic(item)

        assert self.connection is not None
        self.connection.commit()
        return item

    def _write_jsonl(self, target: str, item: dict[str, Any]) -> None:
        self.handles[target].write(_json(item) + "\n")
        self.handles[target].flush()

    def _store_product(self, product: dict[str, Any]) -> None:
        assert self.connection is not None
        product_id = str(product.get("id") or product.get("slug") or product.get("url"))
        self.connection.execute(
            """
            INSERT INTO products (
                id, name, slug, url, sku, product_type, currency, price,
                regular_price, sale_price, stock_status, source, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, slug=excluded.slug, url=excluded.url,
                sku=excluded.sku, product_type=excluded.product_type,
                currency=excluded.currency, price=excluded.price,
                regular_price=excluded.regular_price, sale_price=excluded.sale_price,
                stock_status=excluded.stock_status, source=excluded.source,
                raw_json=excluded.raw_json
            """,
            (
                product_id,
                product.get("name"),
                product.get("slug"),
                product.get("url"),
                product.get("sku"),
                product.get("type"),
                product.get("currency"),
                product.get("price"),
                product.get("regular_price"),
                product.get("sale_price"),
                product.get("stock_status"),
                product.get("source"),
                _json(product),
            ),
        )

        for category in product.get("categories", []):
            category_id = str(
                category.get("id") or category.get("slug") or category.get("name")
            )
            self.connection.execute(
                """
                INSERT INTO product_categories
                    (product_id, category_id, category_name, category_slug)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(product_id, category_id) DO UPDATE SET
                    category_name=excluded.category_name,
                    category_slug=excluded.category_slug
                """,
                (
                    product_id,
                    category_id,
                    category.get("name"),
                    category.get("slug"),
                ),
            )

        for index, attribute in enumerate(product.get("attributes", [])):
            attribute_key = str(
                attribute.get("id")
                or attribute.get("taxonomy")
                or attribute.get("slug")
                or attribute.get("name")
                or index
            )
            options = attribute.get("options") or attribute.get("terms") or []
            self.connection.execute(
                """
                INSERT INTO product_attributes (
                    product_id, attribute_key, attribute_name, variation,
                    visible, options_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, attribute_key) DO UPDATE SET
                    attribute_name=excluded.attribute_name,
                    variation=excluded.variation, visible=excluded.visible,
                    options_json=excluded.options_json, raw_json=excluded.raw_json
                """,
                (
                    product_id,
                    attribute_key,
                    attribute.get("name"),
                    int(bool(attribute.get("variation") or attribute.get("has_variations"))),
                    int(bool(attribute.get("visible", True))),
                    _json(options),
                    _json(attribute),
                ),
            )

        for index, variation in enumerate(product.get("variations", [])):
            variation_id = str(
                variation.get("id")
                or hashlib.sha1(
                    _json(variation.get("attributes", {})).encode("utf-8")
                ).hexdigest()[:16]
            )
            self.connection.execute(
                """
                INSERT INTO variations (
                    product_id, id, name, sku, price, regular_price, sale_price,
                    stock_status, image_url, attributes_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, id) DO UPDATE SET
                    name=excluded.name, sku=excluded.sku, price=excluded.price,
                    regular_price=excluded.regular_price,
                    sale_price=excluded.sale_price,
                    stock_status=excluded.stock_status,
                    image_url=excluded.image_url,
                    attributes_json=excluded.attributes_json,
                    raw_json=excluded.raw_json
                """,
                (
                    product_id,
                    variation_id,
                    variation.get("name"),
                    variation.get("sku"),
                    variation.get("price"),
                    variation.get("regular_price"),
                    variation.get("sale_price"),
                    variation.get("stock_status"),
                    variation.get("image_url"),
                    _json(variation.get("attributes", {})),
                    _json(variation),
                ),
            )

        for index, media in enumerate(product.get("media", [])):
            self.connection.execute(
                """
                INSERT INTO images (
                    product_id, variation_id, role, position, source_url,
                    local_path, alt, width, height, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, variation_id, role, position, source_url)
                DO UPDATE SET
                    local_path=excluded.local_path, alt=excluded.alt,
                    width=excluded.width, height=excluded.height,
                    checksum=excluded.checksum
                """,
                (
                    product_id,
                    str(media.get("variation_id") or ""),
                    media.get("role") or "gallery",
                    int(media.get("position", index)),
                    media.get("source_url"),
                    media.get("local_path"),
                    media.get("alt"),
                    media.get("width"),
                    media.get("height"),
                    media.get("checksum"),
                ),
            )

    def _store_category(self, category: dict[str, Any]) -> None:
        assert self.connection is not None
        category_id = str(category.get("id") or category.get("slug") or category.get("name"))
        image = category.get("image") or {}
        self.connection.execute(
            """
            INSERT INTO categories (
                id, name, slug, parent_id, description, image_url,
                product_count, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, slug=excluded.slug,
                parent_id=excluded.parent_id, description=excluded.description,
                image_url=excluded.image_url, product_count=excluded.product_count,
                raw_json=excluded.raw_json
            """,
            (
                category_id,
                category.get("name"),
                category.get("slug"),
                str(category.get("parent") or ""),
                category.get("description"),
                image.get("src") if isinstance(image, dict) else image,
                category.get("count"),
                _json(category),
            ),
        )

    def _store_attribute(self, attribute: dict[str, Any]) -> None:
        assert self.connection is not None
        attribute_id = str(
            attribute.get("id")
            or attribute.get("taxonomy")
            or attribute.get("slug")
            or attribute.get("name")
        )
        self.connection.execute(
            """
            INSERT INTO attributes (
                id, name, taxonomy, attribute_type, order_by,
                has_archives, terms_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, taxonomy=excluded.taxonomy,
                attribute_type=excluded.attribute_type,
                order_by=excluded.order_by, has_archives=excluded.has_archives,
                terms_json=excluded.terms_json, raw_json=excluded.raw_json
            """,
            (
                attribute_id,
                attribute.get("name"),
                attribute.get("taxonomy") or attribute.get("slug"),
                attribute.get("type"),
                attribute.get("order_by"),
                int(bool(attribute.get("has_archives"))),
                _json(attribute.get("terms", [])),
                _json(attribute),
            ),
        )

    def _store_diagnostic(self, diagnostic: dict[str, Any]) -> None:
        assert self.connection is not None
        self.connection.execute(
            """
            INSERT INTO diagnostics (kind, url, status, message, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                diagnostic.get("kind"),
                diagnostic.get("url"),
                diagnostic.get("status"),
                diagnostic.get("message"),
                _json(diagnostic),
            ),
        )

    def close_spider(self):
        spider = self.crawler.spider
        assert self.connection is not None
        self._export_csvs()
        summary = {
            "base_url": getattr(spider, "base_url", None),
            "records_seen_this_run": dict(self.counts),
            "database_counts": {
                table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "products",
                    "variations",
                    "categories",
                    "attributes",
                    "product_categories",
                    "product_attributes",
                    "images",
                    "diagnostics",
                )
            },
            "access_blocked": bool(getattr(spider, "access_blocked", False)),
            "block_reason": getattr(spider, "block_reason", None),
        }
        (self.output_dir / "crawl-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self.connection.commit()
        self.connection.close()
        for handle in self.handles.values():
            handle.close()

    def _export_csvs(self) -> None:
        assert self.connection is not None
        exports = {
            "products.csv": """
                SELECT id, name, slug, url, sku, product_type, currency,
                       price, regular_price, sale_price, stock_status, source
                FROM products ORDER BY id
            """,
            "variations.csv": """
                SELECT product_id, id, name, sku, price, regular_price,
                       sale_price, stock_status, image_url, attributes_json
                FROM variations ORDER BY product_id, id
            """,
            "categories.csv": """
                SELECT id, name, slug, parent_id, description, image_url, product_count
                FROM categories ORDER BY parent_id, name
            """,
            "product-categories.csv": """
                SELECT product_id, category_id, category_name, category_slug
                FROM product_categories ORDER BY product_id, category_name
            """,
            "attributes.csv": """
                SELECT id, name, taxonomy, attribute_type, order_by,
                       has_archives, terms_json
                FROM attributes ORDER BY name
            """,
            "images.csv": """
                SELECT product_id, variation_id, role, position, source_url,
                       local_path, alt, width, height, checksum
                FROM images ORDER BY product_id, variation_id, role, position
            """,
        }
        for filename, query in exports.items():
            cursor = self.connection.execute(query)
            with (self.output_dir / filename).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([column[0] for column in cursor.description])
                writer.writerows(cursor.fetchall())
