from __future__ import annotations

import base64
import html
import json
import os
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin, urlparse

import scrapy
from scrapy import Request
from scrapy.http import Response
from scrapy_playwright.page import PageMethod

from lgd_scraper.catalog_rules import assign_loose_diamond_category


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _text(values: Iterable[str | None]) -> str:
    return " ".join(value.strip() for value in values if value and value.strip()).strip()


def _clean_html(value: Any) -> str:
    if not value:
        return ""
    return _text(scrapy.Selector(text=str(value)).xpath("//text()").getall())


class WooCommerceCatalogSpider(scrapy.Spider):
    name = "catalog"

    # Bump this whenever the source-page enrichment contract changes. Saved
    # products with an older version are scheduled again, while completed
    # products are skipped after a restart instead of wasting another request.
    ENRICHMENT_SCHEMA_VERSION = 1

    custom_settings = {
        "HTTPERROR_ALLOW_ALL": True,
    }

    DIAMOND_NAME_RE = re.compile(
        r"^(?P<shape>.+?)\s+Cut\s+(?P<carat>\d+(?:\.\d+)?)\s+Carat\s+"
        r"(?P<color>[A-Z]+)\s+Color\s+(?P<clarity>[A-Z0-9]+)\s+Clarity\s+"
        r"Lab\s+Grown\s+Diamond$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        base_url: str = "https://www.loosegrowndiamond.com/",
        output_dir: str = "outputs/catalog",
        max_products: int | str = 0,
        use_playwright: str | bool = False,
        enrich_html: str | bool = True,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        category_ids: str | Iterable[str] = "",
        resume_existing: str | bool = False,
        enrichment_mode: str | bool = False,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/") + "/"
        self.output_dir = output_dir
        self.max_products = int(max_products or 0)
        self.use_playwright = _truthy(use_playwright)
        self.enrich_html = _truthy(enrich_html)
        self.consumer_key = consumer_key or os.getenv("WC_CONSUMER_KEY")
        self.consumer_secret = consumer_secret or os.getenv("WC_CONSUMER_SECRET")
        self.authenticated = bool(self.consumer_key and self.consumer_secret)
        if isinstance(category_ids, str):
            raw_category_ids = category_ids.split(",")
        else:
            raw_category_ids = category_ids
        self.category_ids = list(
            dict.fromkeys(
                str(value).strip()
                for value in raw_category_ids
                if str(value).strip()
            )
        )
        self.allowed_domains = [urlparse(self.base_url).hostname or ""]
        self.resume_existing = _truthy(resume_existing)
        self.enrichment_mode = _truthy(enrichment_mode)
        if self.enrichment_mode:
            # Enrichment is an authoritative update of saved records. Loading
            # these IDs would make the final emission gate discard every
            # product that this mode is specifically intended to update.
            self.existing_product_ids, self.refresh_product_ids = set(), set()
        else:
            (
                self.existing_product_ids,
                self.refresh_product_ids,
            ) = self._load_existing_product_ids()

        self.products_scheduled = 0
        self.products_emitted = 0
        self.products_already_enriched = 0
        self.fallback_started = False
        self.second_sitemap_attempted = False
        self.access_blocked = False
        self.block_reason: str | None = None
        self.seen_product_ids: set[str] = set()

        if self.authenticated:
            raw = f"{self.consumer_key}:{self.consumer_secret}".encode("utf-8")
            self.authorization = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            self.authorization = None

    async def start(self):
        for request in self.start_requests():
            yield request

    def _load_existing_product_ids(self) -> tuple[set[str], set[str]]:
        if not self.resume_existing:
            return set(), set()
        database_path = Path(self.output_dir).resolve() / "catalog.sqlite"
        if not database_path.exists():
            return set(), set()
        try:
            connection = sqlite3.connect(
                f"file:{database_path}?mode=ro", uri=True, timeout=5
            )
            try:
                existing: set[str] = set()
                refresh: set[str] = set()
                for product_id, raw_json in connection.execute(
                    "SELECT id, raw_json FROM products"
                ):
                    normalized_id = str(product_id)
                    existing.add(normalized_id)
                    try:
                        product = json.loads(raw_json)
                    except (json.JSONDecodeError, TypeError):
                        refresh.add(normalized_id)
                        continue
                    if (
                        not product.get("attributes")
                        and self.DIAMOND_NAME_RE.match(
                            str(product.get("name") or "").strip()
                        )
                    ):
                        refresh.add(normalized_id)
                        continue
                    variations = product.get("variations") or []
                    if product.get("type") == "variable" and (
                        not variations
                        or any(
                            not variation.get("attributes")
                            or (
                                variation.get("sku")
                                and variation.get("sku") == product.get("sku")
                            )
                            for variation in variations
                        )
                    ):
                        refresh.add(normalized_id)
                return existing, refresh
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self.logger.warning("Could not read resumed product IDs: %s", exc)
            return set(), set()

    def start_requests(self):
        if self.enrichment_mode:
            yield from self._start_enrichment_requests()
            return

        if self.authenticated:
            product_categories = self.category_ids or [None]
            for category_id in product_categories:
                yield self._api_request(
                    self._url(
                        "wp-json/wc/v3/products",
                        per_page=100,
                        page=1,
                        category=category_id,
                    ),
                    self.parse_products_api,
                    meta={"api_kind": "rest", "page": 1},
                )
            yield self._api_request(
                self._url("wp-json/wc/v3/products/categories", per_page=100, page=1),
                self.parse_categories_api,
                meta={"api_kind": "rest", "page": 1},
            )
            yield self._api_request(
                self._url("wp-json/wc/v3/products/attributes", per_page=100, page=1),
                self.parse_attributes_api,
                meta={"api_kind": "rest", "page": 1},
            )
        else:
            yield self._api_request(
                self._url(
                    "wp-json/wc/store/v1/products",
                    per_page=100,
                    page=1,
                    category=",".join(self.category_ids) or None,
                    category_operator="in" if self.category_ids else None,
                ),
                self.parse_products_api,
                meta={"api_kind": "store", "page": 1},
            )
            yield self._api_request(
                self._url("wp-json/wc/store/v1/products/categories", per_page=100, page=1),
                self.parse_categories_api,
                meta={"api_kind": "store", "page": 1},
            )
            yield self._api_request(
                self._url("wp-json/wc/store/v1/products/attributes", per_page=100, page=1),
                self.parse_attributes_api,
                meta={"api_kind": "store", "page": 1},
            )

    def _start_enrichment_requests(self):
        """Refresh saved diamonds and variable products without recrawling the catalog."""

        database_path = Path(self.output_dir).resolve() / "catalog.sqlite"
        self.logger.info(
            "Enrichment: opening catalog checkpoint at %s (exists=%s)",
            database_path,
            database_path.exists(),
        )
        if not database_path.exists():
            self.logger.warning(
                "Enrichment: catalog checkpoint not found at %s", database_path
            )
            yield {
                "_record_type": "diagnostic",
                "kind": "enrichment_catalog_missing",
                "url": self.base_url,
                "status": None,
                "message": "An existing catalog checkpoint is required for enrichment.",
            }
            return

        try:
            connection = sqlite3.connect(
                f"file:{database_path}?mode=ro", uri=True, timeout=5
            )
            try:
                total_count = connection.execute(
                    "SELECT COUNT(*) FROM products"
                ).fetchone()[0]
                self.logger.info(
                    "Enrichment: catalog contains %d products", total_count
                )
                cursor = connection.execute(
                    "SELECT raw_json FROM products ORDER BY id"
                )
                skipped_no_url = 0
                skipped_family = 0
                for (raw_json,) in cursor:
                    if self.max_products and self.products_scheduled >= self.max_products:
                        break
                    try:
                        product = json.loads(raw_json)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(product, dict) or not product.get("url"):
                        skipped_no_url += 1
                        continue
                    if not (
                        product.get("product_family") == "loose_diamond"
                        or product.get("type") == "variable"
                        or product.get("variations")
                    ):
                        skipped_family += 1
                        continue
                    if not self._needs_enrichment(product):
                        self.products_already_enriched += 1
                        continue
                    product["_record_type"] = "product"
                    self.products_scheduled += 1
                    yield self._page_request(
                        str(product["url"]), product, dont_filter=True
                    )
                self.logger.info(
                    "Enrichment: scheduled=%d, already_enriched=%d, "
                    "skipped_no_url=%d, skipped_family=%d",
                    self.products_scheduled,
                    self.products_already_enriched,
                    skipped_no_url,
                    skipped_family,
                )
            finally:
                connection.close()
        except sqlite3.Error as exc:
            self.logger.error("Enrichment: catalog read failed: %s", exc)
            yield {
                "_record_type": "diagnostic",
                "kind": "enrichment_catalog_unreadable",
                "url": self.base_url,
                "status": None,
                "message": f"The existing catalog could not be read: {exc}",
            }
            return

    def _needs_enrichment(self, product: dict[str, Any]) -> bool:
        try:
            saved_version = int(product.get("enrichment_schema_version") or 0)
        except (TypeError, ValueError):
            saved_version = 0
        return saved_version < self.ENRICHMENT_SCHEMA_VERSION


    def parse_products_api(self, response: Response):
        payload = self._json_list(response)
        api_kind = response.meta["api_kind"]
        if payload is None:
            yield self._diagnostic_for_response(
                response, "products_api_unavailable", "Product API was not accessible."
            )
            if not self.fallback_started and not self.category_ids:
                self.fallback_started = True
                yield Request(
                    urljoin(self.base_url, "wp-sitemap.xml"),
                    callback=self.parse_sitemap,
                    errback=self.errback_request,
                    dont_filter=True,
                )
            return

        for raw_product in payload:
            if self.max_products and self.products_scheduled >= self.max_products:
                break
            product_id = str(raw_product.get("id") or "")
            if (
                product_id
                and product_id in self.existing_product_ids
                and product_id not in self.refresh_product_ids
            ):
                continue
            if product_id and product_id in self.seen_product_ids:
                continue
            if product_id:
                self.seen_product_ids.add(product_id)
            self.products_scheduled += 1
            product = self._normalize_product(raw_product, api_kind)

            if api_kind == "rest" and product.get("type") == "variable":
                yield self._api_request(
                    self._url(
                        f"wp-json/wc/v3/products/{product['id']}/variations",
                        per_page=100,
                        page=1,
                    ),
                    self.parse_rest_variations,
                    meta={
                        "api_kind": "rest",
                        "page": 1,
                        "product": product,
                        "variations": [],
                    },
                )
            elif self._should_enrich_product(product):
                yield self._page_request(product["url"], product)
            elif product.get("type") == "variable":
                yield self._store_variation_request(product, page=1, variations=[])
            else:
                yield from self._emit_product(product)

        yield from self._paginate(response, self.parse_products_api, payload)

    def parse_rest_variations(self, response: Response):
        payload = self._json_list(response)
        product = response.meta["product"]
        accumulated = list(response.meta.get("variations", []))
        if payload is None:
            yield self._diagnostic_for_response(
                response,
                "variations_api_unavailable",
                f"Variations API failed for product {product.get('id')}.",
            )
            if self._should_enrich_product(product):
                yield self._page_request(product["url"], product)
            else:
                self._promote_structured_variations(product, accumulated)
                yield from self._finish_product(product)
            return

        accumulated.extend(
            self._normalize_variation(variation, product["name"], "rest")
            for variation in payload
        )
        total_pages = int(response.headers.get("X-WP-TotalPages", b"1") or 1)
        page = int(response.meta.get("page", 1))
        if page < total_pages:
            yield self._api_request(
                self._url(
                    f"wp-json/wc/v3/products/{product['id']}/variations",
                    per_page=100,
                    page=page + 1,
                ),
                self.parse_rest_variations,
                meta={
                    "api_kind": "rest",
                    "page": page + 1,
                    "product": product,
                    "variations": accumulated,
                },
            )
            return

        product["variations"] = accumulated
        if self._should_enrich_product(product):
            yield self._page_request(product["url"], product)
        else:
            self._merge_parent_variation_attributes(product, accumulated)
            self._promote_structured_variations(product, accumulated)
            yield from self._finish_product(product)

    def parse_store_variations(self, response: Response):
        product = response.meta["product"]
        accumulated = list(response.meta.get("variations", []))
        payload = self._json_list(response)
        if payload is None:
            yield self._diagnostic_for_response(
                response,
                "store_variations_unavailable",
                f"Public variation lookup failed for product {product.get('id')}.",
            )
            self._promote_structured_variations(product, accumulated)
            yield from self._finish_product(product)
            return

        accumulated.extend(
            self._normalize_variation(variation, product["name"], "store")
            for variation in payload
        )
        total_pages = int(response.headers.get("X-WP-TotalPages", b"1") or 1)
        page = int(response.meta.get("page", 1))
        if page < total_pages:
            yield self._store_variation_request(product, page + 1, accumulated)
            return

        self._merge_parent_variation_attributes(product, accumulated)
        self._promote_structured_variations(product, accumulated)
        yield from self._finish_product(product)

    @staticmethod
    def _promote_structured_variations(
        product: dict[str, Any], accumulated: list[dict[str, Any]]
    ) -> None:
        if accumulated:
            product["variations"] = accumulated
            return
        structured = product.get("structured_variations") or []
        product["variations"] = structured
        if structured:
            product["variation_source_fallback"] = "json_ld"

    @staticmethod
    def _merge_parent_variation_attributes(
        product: dict[str, Any], variations: list[dict[str, Any]]
    ) -> None:
        """Merge Store API's parent-level variation attributes by variation ID."""

        parent_variations = (product.get("raw_api") or {}).get("variations") or []
        attributes_by_id: dict[str, dict[str, Any]] = {}
        for raw_variation in parent_variations:
            variation_id = str(raw_variation.get("id") or "")
            raw_attributes = raw_variation.get("attributes") or []
            if not variation_id or not isinstance(raw_attributes, list):
                continue
            attributes = {
                str(
                    item.get("name")
                    or item.get("taxonomy")
                    or item.get("id")
                ): (
                    item.get("option")
                    or item.get("value")
                    or item.get("term")
                    or item.get("name")
                )
                for item in raw_attributes
                if isinstance(item, dict)
            }
            if attributes:
                attributes_by_id[variation_id] = attributes

        product_name = str(product.get("name") or "")
        for variation in variations:
            if variation.get("attributes"):
                continue
            attributes = attributes_by_id.get(str(variation.get("id") or ""))
            if not attributes:
                continue
            variation["attributes"] = attributes
            parts = [f"{key}: {value}" for key, value in attributes.items() if value]
            if parts:
                variation["name"] = f"{product_name} — {' / '.join(parts)}"

    def parse_product_page(self, response: Response, api_product: dict[str, Any] | None = None):
        product = dict(api_product or self._product_shell(response))
        if response.status == 404 and api_product is not None:
            # Some Store API diamond records publish a legacy detail permalink
            # that no longer has an HTML route. The API record remains valid and
            # complete enough to export, so treat HTML enrichment as optional.
            product["html_enrichment"] = {
                "status": "unavailable",
                "http_status": 404,
            }
            # A real 404 is a completed enrichment attempt for this schema.
            # Do not request the same permanently unavailable page on every
            # resumed run; the stored API record remains exportable.
            product["enrichment_schema_version"] = self.ENRICHMENT_SCHEMA_VERSION
            if (
                not self.authenticated
                and product.get("type") == "variable"
                and not product.get("variations")
            ):
                yield self._store_variation_request(product, page=1, variations=[])
            elif product.get("variations"):
                yield from self._finish_product(product)
            else:
                yield from self._emit_product(product)
            return
        if response.status != 200 or self._is_blocked(response):
            blocked = self._is_blocked(response)
            self._record_block(response)
            yield self._diagnostic_for_response(
                response,
                "product_page_unavailable",
                f"Product page could not be read (HTTP {response.status}).",
            )
            if blocked:
                # A geographic/firewall denial applies to the whole run. Keep
                # the saved product unchanged and stop before scheduling tens
                # of thousands of requests that cannot succeed.
                return
            if (
                not self.authenticated
                and product.get("type") == "variable"
                and not product.get("variations")
            ):
                yield self._store_variation_request(product, page=1, variations=[])
            elif product.get("variations"):
                yield from self._finish_product(product)
            else:
                yield from self._emit_product(product)
            return

        product["source"] = (
            f"{product.get('source')}+html" if product.get("source") else "html"
        )
        product["url"] = response.url
        product["name"] = product.get("name") or _text(
            response.css(
                "h1.product_title *::text, h1.product_title::text, "
                "h1[itemprop='name'] *::text, h1[itemprop='name']::text, "
                "main h1 *::text, main h1::text"
            ).getall()
        ) or response.css('meta[property="og:title"]::attr(content)').get()
        product["sku"] = product.get("sku") or _text(
            response.css(
                ".sku::text, [itemprop='sku']::attr(content), "
                "[itemprop='sku']::text, [data-product-sku]::attr(data-product-sku)"
            ).getall()
        )
        product["id"] = product.get("id") or self._html_product_id(response)
        product["slug"] = product.get("slug") or self._slug(response.url)
        product["description"] = product.get("description") or _text(
            response.css(
                "#tab-description *::text, .woocommerce-Tabs-panel--description *::text, "
                "#product-description *::text, .product-description *::text, "
                "[itemprop='description'] *::text, "
                ".woocommerce-product-details__short-description *::text"
            ).getall()
        )
        description_node = response.css(
            "#tab-description, .woocommerce-Tabs-panel--description, "
            "#product-description, .product-description, [itemprop='description']"
        )
        short_node = response.css(
            ".woocommerce-product-details__short-description, .product-short-description"
        )
        product["description_html"] = product.get("description_html") or (
            description_node.get() or ""
        )
        product["short_description_html"] = product.get(
            "short_description_html"
        ) or (short_node.get() or "")
        product["seo"] = {
            "title": response.css("title::text").get(),
            "meta_description": response.css(
                'meta[name="description"]::attr(content)'
            ).get(),
            "canonical_url": response.css(
                'link[rel="canonical"]::attr(href)'
            ).get(),
            "og_image": response.css(
                'meta[property="og:image"]::attr(content)'
            ).get(),
        }

        self._merge_json_ld(product, response)
        self._merge_html_categories(product, response)
        self._merge_html_tags_and_brands(product, response)
        self._merge_html_attributes(product, response)
        self._merge_html_media(product, response)
        self._merge_diamond_details(product, response)
        if product.get("product_family") == "loose_diamond" and not (
            (product.get("diamond_details") or {}).get("fields")
        ):
            yield {
                "_record_type": "diagnostic",
                "kind": "diamond_details_missing",
                "url": response.url,
                "status": response.status,
                "message": "The product page loaded but contained no diamond-details grid.",
            }
        product["html_enrichment"] = {
            "status": "complete",
            "http_status": response.status,
            "source_url": response.url,
        }
        product["enrichment_schema_version"] = self.ENRICHMENT_SCHEMA_VERSION

        variations, variation_source, variation_error = self._embedded_product_variations(
            response
        )
        if variation_error:
            yield {
                "_record_type": "diagnostic",
                "kind": "variation_json_invalid",
                "url": response.url,
                "status": response.status,
                "message": variation_error,
            }
        if variations:
            product["type"] = "variable"
            product["variations"] = [
                self._normalize_variation(item, product["name"], variation_source)
                for item in variations
            ]
            missing_gallery_ids = [
                str(variation.get("id") or "unknown")
                for variation in product["variations"]
                if not variation.get("gallery")
            ]
            if missing_gallery_ids:
                yield {
                    "_record_type": "diagnostic",
                    "kind": "variation_gallery_missing",
                    "url": response.url,
                    "status": response.status,
                    "message": (
                        f"{len(missing_gallery_ids)} inline variation(s) had no gallery: "
                        + ", ".join(missing_gallery_ids[:20])
                    ),
                }
        elif response.css("form.variations_form"):
            yield {
                "_record_type": "diagnostic",
                "kind": "inline_variations_missing",
                "url": response.url,
                "status": response.status,
                "message": "A variation form was present but no authoritative inline variation payload was found.",
            }

        if (
            self.authenticated
            and product.get("type") == "variable"
            and not product.get("variations")
            and product.get("structured_variations")
        ):
            product["variations"] = product["structured_variations"]
            product["variation_source_fallback"] = "json_ld"

        product["media"] = self._dedupe_media(product.get("media", []))

        if (
            not self.authenticated
            and product.get("type") == "variable"
            and not product.get("variations")
        ):
            yield self._store_variation_request(product, page=1, variations=[])
        elif product.get("variations"):
            yield from self._finish_product(product)
        else:
            yield from self._emit_product(product)

    @staticmethod
    def _embedded_product_variations(
        response: Response,
    ) -> tuple[list[dict[str, Any]], str, str | None]:
        """Read authoritative Woo variation JSON from attributes or inline JS."""

        embedded = response.css(
            "[data-product_variations]::attr(data-product_variations)"
        ).get()
        if embedded and embedded.strip() not in {"", "false"}:
            try:
                value = json.loads(html.unescape(embedded))
            except (json.JSONDecodeError, TypeError) as exc:
                return [], "html_data_attribute", (
                    f"The data-product_variations payload was invalid JSON: {exc}"
                )
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)], (
                    "html_data_attribute"
                ), None

        decoder = json.JSONDecoder()
        assignment = re.compile(
            r"\b(?:var|let|const)\s+(?:productVariations|product_variations)\s*=\s*"
        )
        for script in response.css("script:not([src])::text").getall():
            match = assignment.search(script)
            if not match:
                continue
            try:
                value, _ = decoder.raw_decode(
                    html.unescape(script[match.end() :]).lstrip()
                )
            except (json.JSONDecodeError, TypeError) as exc:
                return [], "html_inline_product_variations", (
                    f"The inline productVariations payload was invalid JSON: {exc}"
                )
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)], (
                    "html_inline_product_variations"
                ), None
        return [], "html", None

    def parse_categories_api(self, response: Response):
        payload = self._json_list(response)
        if payload is None:
            yield self._diagnostic_for_response(
                response, "categories_api_unavailable", "Category API was not accessible."
            )
            return
        for category in payload:
            category = dict(category)
            category["_record_type"] = "category"
            yield category
        yield from self._paginate(response, self.parse_categories_api, payload)

    def parse_attributes_api(self, response: Response):
        payload = self._json_list(response)
        if payload is None:
            yield self._diagnostic_for_response(
                response, "attributes_api_unavailable", "Attribute API was not accessible."
            )
            return
        api_kind = response.meta["api_kind"]
        for attribute in payload:
            if not attribute.get("id"):
                attribute = dict(attribute)
                attribute["_record_type"] = "attribute"
                yield attribute
                continue
            if api_kind == "rest":
                endpoint = f"wp-json/wc/v3/products/attributes/{attribute['id']}/terms"
            else:
                endpoint = (
                    f"wp-json/wc/store/v1/products/attributes/{attribute['id']}/terms"
                )
            yield self._api_request(
                self._url(endpoint, per_page=100, page=1),
                self.parse_attribute_terms,
                meta={
                    "api_kind": api_kind,
                    "page": 1,
                    "attribute": dict(attribute),
                    "terms": [],
                },
            )
        yield from self._paginate(response, self.parse_attributes_api, payload)

    def parse_attribute_terms(self, response: Response):
        attribute = response.meta["attribute"]
        terms = list(response.meta.get("terms", []))
        payload = self._json_list(response)
        if payload is None:
            attribute["_record_type"] = "attribute"
            attribute["terms"] = terms
            yield attribute
            return

        terms.extend(payload)
        total_pages = int(response.headers.get("X-WP-TotalPages", b"1") or 1)
        page = int(response.meta.get("page", 1))
        if page < total_pages:
            api_kind = response.meta["api_kind"]
            if api_kind == "rest":
                endpoint = (
                    f"wp-json/wc/v3/products/attributes/{attribute['id']}/terms"
                )
            else:
                endpoint = (
                    f"wp-json/wc/store/v1/products/attributes/{attribute['id']}/terms"
                )
            yield self._api_request(
                self._url(endpoint, per_page=100, page=page + 1),
                self.parse_attribute_terms,
                meta={
                    "api_kind": api_kind,
                    "page": page + 1,
                    "attribute": attribute,
                    "terms": terms,
                },
            )
            return

        attribute["_record_type"] = "attribute"
        attribute["terms"] = terms
        yield attribute

    def parse_sitemap(self, response: Response):
        if response.status != 200 or self._is_blocked(response):
            self._record_block(response)
            yield self._diagnostic_for_response(
                response,
                "sitemap_unavailable",
                f"Sitemap could not be read (HTTP {response.status}).",
            )
            if not self.second_sitemap_attempted:
                self.second_sitemap_attempted = True
                yield Request(
                    urljoin(self.base_url, "sitemap_index.xml"),
                    callback=self.parse_sitemap,
                    errback=self.errback_request,
                    dont_filter=True,
                )
            return

        sitemap_urls = response.xpath(
            "//*[local-name()='sitemap']/*[local-name()='loc']/text()"
        ).getall()
        product_urls = response.xpath(
            "//*[local-name()='url']/*[local-name()='loc']/text()"
        ).getall()

        if sitemap_urls:
            matching = [url for url in sitemap_urls if "product" in url.lower()]
            for url in matching:
                yield Request(
                    url,
                    callback=self.parse_sitemap,
                    errback=self.errback_request,
                )
            if not matching:
                yield {
                    "_record_type": "diagnostic",
                    "kind": "product_sitemap_not_found",
                    "url": response.url,
                    "status": response.status,
                    "message": "Sitemap index contained no product-named child sitemap.",
                }
            return

        for url in product_urls:
            if "/product/" not in url:
                continue
            if self.max_products and self.products_scheduled >= self.max_products:
                break
            self.products_scheduled += 1
            yield self._page_request(url, None)

    def _paginate(self, response: Response, callback, payload: list[dict[str, Any]]):
        if not payload:
            return
        total_pages = int(response.headers.get("X-WP-TotalPages", b"1") or 1)
        page = int(response.meta.get("page", 1))
        if page >= total_pages:
            return
        if self.max_products and self.products_scheduled >= self.max_products:
            return
        next_url = response.url
        next_url = re.sub(r"([?&])page=\d+", rf"\g<1>page={page + 1}", next_url)
        if next_url == response.url:
            separator = "&" if "?" in response.url else "?"
            next_url = f"{response.url}{separator}page={page + 1}"
        yield self._api_request(
            next_url,
            callback,
            meta={"api_kind": response.meta["api_kind"], "page": page + 1},
        )

    def _normalize_product(self, raw: dict[str, Any], source: str) -> dict[str, Any]:
        prices = raw.get("prices") or {}
        images = raw.get("images") or []
        price = raw.get("price")
        regular_price = raw.get("regular_price")
        sale_price = raw.get("sale_price")
        currency = raw.get("currency") or prices.get("currency_code")

        if prices:
            price = self._minor_price(prices.get("price"), prices.get("currency_minor_unit"))
            regular_price = self._minor_price(
                prices.get("regular_price"), prices.get("currency_minor_unit")
            )
            sale_price = self._minor_price(
                prices.get("sale_price"), prices.get("currency_minor_unit")
            )

        product_type = raw.get("type")
        if not product_type:
            add_to_cart = raw.get("add_to_cart") or {}
            text = str(add_to_cart.get("text") or "").lower()
            product_type = "variable" if "select" in text or "option" in text else "simple"

        stock_status = raw.get("stock_status")
        if stock_status is None and raw.get("is_in_stock") is not None:
            stock_status = "instock" if raw.get("is_in_stock") else "outofstock"

        product = {
            "_record_type": "product",
            "source": f"woocommerce_{source}_api",
            "id": raw.get("id"),
            "name": _clean_html(raw.get("name")),
            "slug": raw.get("slug"),
            "url": raw.get("permalink") or raw.get("url"),
            "sku": raw.get("sku"),
            "type": product_type,
            "status": raw.get("status"),
            "catalog_visibility": raw.get("catalog_visibility"),
            "description": _clean_html(raw.get("description")),
            "short_description": _clean_html(raw.get("short_description")),
            "description_html": raw.get("description") or "",
            "short_description_html": raw.get("short_description") or "",
            "currency": currency,
            "price": price,
            "regular_price": regular_price,
            "sale_price": sale_price,
            "price_html": raw.get("price_html"),
            "stock_status": stock_status,
            "stock_quantity": raw.get("stock_quantity"),
            "manage_stock": raw.get("manage_stock"),
            "backorders": raw.get("backorders"),
            "backorders_allowed": raw.get("backorders_allowed"),
            "backordered": raw.get("backordered"),
            "low_stock_amount": raw.get("low_stock_amount"),
            "sold_individually": raw.get("sold_individually"),
            "tax_status": raw.get("tax_status"),
            "tax_class": raw.get("tax_class"),
            "weight": raw.get("weight"),
            "dimensions": raw.get("dimensions") or {},
            "shipping_class": raw.get("shipping_class"),
            "shipping_class_id": raw.get("shipping_class_id"),
            "virtual": raw.get("virtual"),
            "downloadable": raw.get("downloadable"),
            "downloads": raw.get("downloads") or [],
            "download_limit": raw.get("download_limit"),
            "download_expiry": raw.get("download_expiry"),
            "external_url": raw.get("external_url"),
            "button_text": raw.get("button_text"),
            "purchase_note": raw.get("purchase_note"),
            "reviews_allowed": raw.get("reviews_allowed"),
            "average_rating": raw.get("average_rating"),
            "rating_count": raw.get("rating_count"),
            "total_sales": raw.get("total_sales"),
            "menu_order": raw.get("menu_order"),
            "upsell_ids": raw.get("upsell_ids") or [],
            "cross_sell_ids": raw.get("cross_sell_ids") or [],
            "grouped_products": raw.get("grouped_products") or [],
            "related_ids": raw.get("related_ids") or [],
            "categories": list(raw.get("categories") or []),
            "tags": list(raw.get("tags") or []),
            "brands": list(raw.get("brands") or []),
            "attributes": list(raw.get("attributes") or []),
            "default_attributes": list(raw.get("default_attributes") or []),
            "variations": [],
            "media": [],
            "date_created": raw.get("date_created"),
            "date_modified": raw.get("date_modified"),
            "date_on_sale_from": raw.get("date_on_sale_from"),
            "date_on_sale_to": raw.get("date_on_sale_to"),
            "meta_data": raw.get("meta_data") or [],
            "raw_api": raw,
        }
        self._identify_diamond_product(product)
        for position, image in enumerate(images):
            url = (
                image.get("src")
                or image.get("full_src")
                or image.get("url")
                or image.get("thumbnail")
            )
            if not url:
                continue
            product["media"].append(
                {
                    "role": "featured" if position == 0 else "gallery",
                    "position": position,
                    "source_url": url,
                    "alt": image.get("alt"),
                    "name": image.get("name"),
                    "width": image.get("width"),
                    "height": image.get("height"),
                }
            )
        return product

    def _identify_diamond_product(self, product: dict[str, Any]) -> None:
        """Identify source diamond records without inventing catalog attributes."""

        match = self.DIAMOND_NAME_RE.match(str(product.get("name") or "").strip())
        if not match:
            return
        product["product_family"] = "loose_diamond"
        assign_loose_diamond_category(product)
        product["html_enrichment"] = {
            "status": "skipped",
            "reason": "run_separate_catalog_enrichment",
        }

    @staticmethod
    def _diamond_label(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @staticmethod
    def _diamond_number(value: Any) -> float | None:
        if value in (None, "", "-"):
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(match.group(0)) if match else None

    def _merge_diamond_details(
        self, product: dict[str, Any], response: Response
    ) -> None:
        """Capture rendered diamond specifications omitted by the Store API."""

        if product.get("product_family") != "loose_diamond" and not self.DIAMOND_NAME_RE.match(
            str(product.get("name") or "").strip()
        ):
            return

        label_aliases = {
            "carat": "carat",
            "carat weight": "carat",
            "l x w x h": "size_mm",
            "size mm": "size_mm",
            "measurements": "size_mm",
            "measurement": "size_mm",
            "measurements mm": "size_mm",
            "cut": "cut",
            "cut grade": "cut",
            "color": "color",
            "colour": "color",
            "clarity": "clarity",
            "in the box": "in_the_box",
            "table depth": "table_depth",
            "l w ratio": "length_width_ratio",
            "lw ratio": "length_width_ratio",
            "length width ratio": "length_width_ratio",
            "sku": "sku",
            "stock number": "sku",
            "growth type": "growth_type",
            "growth method": "growth_type",
            "girdle thickness": "girdle_thickness",
            "girdle": "girdle_thickness",
            "crown angle": "crown_angle",
            "pavilion angle": "pavilion_angle",
            "fluorescence": "fluorescence",
            "fluorescence intensity": "fluorescence",
            "polish": "polish",
            "polished": "polish",
            "symmetry": "symmetry",
            "culet": "culet",
            "table": "table_percent",
            "table percent": "table_percent",
            "table percentage": "table_percent",
            "depth": "depth_percent",
            "depth percent": "depth_percent",
            "depth percentage": "depth_percent",
            "certificate lab": "certificate_lab",
            "lab": "certificate_lab",
        }
        values: dict[str, Any] = {}
        fields: dict[str, dict[str, str]] = {}
        for item in response.css(".diamond-details-grid .dd-item"):
            label = _text(item.css(".dd-label *::text, .dd-label::text").getall())
            value_node = item.css(".dd-value")
            if not label or not value_node:
                continue
            description = _text(value_node.css(".small *::text, .small::text").getall())
            primary = _text(
                value_node.css(".fw-bold *::text, .fw-bold::text").getall()
            )
            if not primary:
                primary = _text(
                    value_node.xpath(
                        ".//text()[not(ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' small ')])]"
                    ).getall()
                )
            if not primary:
                continue
            field_key = re.sub(
                r"[^a-z0-9]+", "_", self._diamond_label(label)
            ).strip("_")
            if not field_key:
                continue
            field = {"label": label, "value": primary}
            if description:
                field["description"] = description
            fields[field_key] = field
            canonical_key = label_aliases.get(self._diamond_label(label))
            if canonical_key:
                values.setdefault(canonical_key, primary)

        if not fields:
            return

        details = dict(product.get("diamond_details") or {})
        raw_values = dict(details.get("raw_values") or {})
        raw_values.update(
            {field["label"]: field["value"] for field in fields.values()}
        )
        details.update(
            {
                "source": "rendered_product_page",
                "source_url": response.url,
                "fields": fields,
                "raw_values": raw_values,
            }
        )
        for key in (
            "cut",
            "color",
            "clarity",
            "growth_type",
            "girdle_thickness",
            "fluorescence",
            "polish",
            "symmetry",
            "culet",
            "sku",
            "certificate_lab",
        ):
            if key in values:
                details[key] = (
                    None
                    if str(values[key]).strip() in {"-", "—", "–"}
                    else values[key]
                )

        if "carat" in values:
            details["carat"] = self._diamond_number(values["carat"])
        if "size_mm" in values:
            measurements = re.findall(r"\d+(?:\.\d+)?", str(values["size_mm"]))
            details["size_mm"] = {
                "raw": values["size_mm"],
                "length": float(measurements[0]) if len(measurements) > 0 else None,
                "width": float(measurements[1]) if len(measurements) > 1 else None,
                "depth": float(measurements[2]) if len(measurements) > 2 else None,
            }
        if "table_depth" in values:
            table_depth = re.findall(r"\d+(?:\.\d+)?", str(values["table_depth"]))
            details["table_percent"] = (
                float(table_depth[0]) if len(table_depth) > 0 else None
            )
            details["depth_percent"] = (
                float(table_depth[1]) if len(table_depth) > 1 else None
            )
        for key in (
            "table_percent",
            "depth_percent",
            "length_width_ratio",
            "crown_angle",
            "pavilion_angle",
        ):
            if key in values and key not in details:
                details[key] = self._diamond_number(values[key])
        if "in_the_box" in values:
            details["in_the_box"] = [
                item.strip()
                for item in str(values["in_the_box"]).split(",")
                if item.strip()
            ]

        product["diamond_details"] = details
        product["product_family"] = "loose_diamond"
        assign_loose_diamond_category(product)
        self._merge_diamond_detail_attributes(product, details)

    @staticmethod
    def _merge_diamond_detail_attributes(
        product: dict[str, Any], details: dict[str, Any]
    ) -> None:
        attribute_fields = {
            "carat": "Carat",
            "cut": "Cut",
            "color": "Color",
            "clarity": "Clarity",
            "growth_type": "Growth Type",
            "fluorescence": "Fluorescence",
            "polish": "Polish",
            "symmetry": "Symmetry",
        }
        attributes = list(product.get("attributes") or [])
        by_name = {
            str(attribute.get("name") or "").strip().lower(): attribute
            for attribute in attributes
        }
        for key, name in attribute_fields.items():
            value = details.get(key)
            if value in (None, "", [], {}):
                continue
            normalized = name.lower()
            if normalized in by_name:
                by_name[normalized]["options"] = [str(value)]
                continue
            attribute = {
                "id": f"diamond_{key}",
                "name": name,
                "visible": True,
                "variation": False,
                "options": [str(value)],
                "source": "rendered_product_page",
            }
            attributes.append(attribute)
            by_name[normalized] = attribute
        product["attributes"] = attributes

    def _should_enrich_product(self, product: dict[str, Any]) -> bool:
        return bool(
            self.enrich_html
            and product.get("url")
            and product.get("product_family") != "loose_diamond"
        )

    def _normalize_variation(
        self, raw: dict[str, Any], product_name: str, source: str
    ) -> dict[str, Any]:
        raw_attributes = raw.get("attributes") or {}
        if isinstance(raw_attributes, list):
            attributes = {
                item.get("name") or item.get("taxonomy") or str(item.get("id")): (
                    item.get("option")
                    or item.get("value")
                    or item.get("term")
                    or item.get("name")
                )
                for item in raw_attributes
            }
        else:
            attributes = {
                re.sub(r"^attribute_", "", key).replace("pa_", "").replace("-", " "): value
                for key, value in raw_attributes.items()
            }

        prices = raw.get("prices") or {}
        if prices:
            price = self._minor_price(prices.get("price"), prices.get("currency_minor_unit"))
            regular_price = self._minor_price(
                prices.get("regular_price"), prices.get("currency_minor_unit")
            )
            sale_price = self._minor_price(
                prices.get("sale_price"), prices.get("currency_minor_unit")
            )
        else:
            price = raw.get("price", raw.get("display_price"))
            regular_price = raw.get(
                "regular_price", raw.get("display_regular_price")
            )
            sale_price = raw.get("sale_price")

        image = raw.get("image") or {}
        if isinstance(image, str):
            image = {"src": image}
        image_url = (
            image.get("full_src")
            or image.get("src")
            or image.get("url")
            or image.get("thumbnail")
        )
        gallery = self._variation_gallery(raw, image_url)
        parts = [f"{key}: {value}" for key, value in attributes.items() if value]
        variation_id = raw.get("id") or raw.get("variation_id")

        stock_status = raw.get("stock_status")
        if not stock_status:
            if raw.get("is_in_stock") is True:
                stock_status = "instock"
            elif raw.get("is_in_stock") is False:
                stock_status = "outofstock"

        return {
            "source": source,
            "id": variation_id,
            "name": f"{product_name} — {' / '.join(parts)}" if parts else product_name,
            "sku": raw.get("sku"),
            "attributes": attributes,
            "price": str(price) if price is not None else None,
            "regular_price": str(regular_price) if regular_price is not None else None,
            "sale_price": str(sale_price) if sale_price is not None else None,
            "stock_status": stock_status,
            "stock_quantity": (
                raw.get("stock_quantity")
                if raw.get("stock_quantity") is not None
                else raw.get("max_qty")
            ),
            "purchasable": raw.get("purchasable", raw.get("is_purchasable")),
            "visible": raw.get("visible", raw.get("variation_is_visible")),
            "image_url": image_url,
            "image_alt": image.get("alt"),
            "image_width": image.get("width"),
            "image_height": image.get("height"),
            "gallery": gallery,
            "gallery_image_ids": self._variation_gallery_ids(raw),
            "description": _clean_html(
                raw.get("description") or raw.get("variation_description")
            ),
            "details": self._variation_details(raw),
            "weight": raw.get("weight"),
            "dimensions": raw.get("dimensions"),
            "raw": raw,
        }

    @staticmethod
    def _variation_details(raw: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Preserve dynamically named per-variation specifications."""

        details: dict[str, dict[str, str]] = {}

        def retain(label: Any, value: Any) -> None:
            label_text = _clean_html(label).strip()
            value_text = _clean_html(value).strip()
            key = re.sub(r"[^a-z0-9]+", "_", label_text.lower()).strip("_")
            if key and value_text:
                details[key] = {"label": label_text, "value": value_text}

        for field_name in ("section1data", "section2data", "product_details"):
            payload = raw.get(field_name)
            if isinstance(payload, dict):
                for label, value in payload.items():
                    if not isinstance(value, (dict, list, tuple)):
                        retain(label, value)
            elif isinstance(payload, (list, tuple)):
                for item in payload:
                    if isinstance(item, dict):
                        label = item.get("label") or item.get("name") or item.get("key")
                        value = item.get("value") or item.get("text")
                        if label and value not in (None, ""):
                            retain(label, value)
                    elif isinstance(item, str) and ":" in item:
                        label, value = item.split(":", 1)
                        retain(label, value)
        return details

    def _variation_gallery(
        self, raw: dict[str, Any], featured_url: str | None
    ) -> list[dict[str, Any]]:
        """Collect common variation-gallery plugin fields without executing code."""

        values = self._variation_gallery_values(raw)

        images: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if not value:
                return
            if isinstance(value, dict):
                srcset_url = None
                srcset_width = -1
                srcset = html.unescape(str(value.get("srcset") or ""))
                for candidate in srcset.split(","):
                    parts = candidate.strip().rsplit(None, 1)
                    if not parts:
                        continue
                    width_match = re.fullmatch(r"(\d+)w", parts[-1])
                    width = int(width_match.group(1)) if width_match else 0
                    candidate_url = parts[0] if width_match else candidate.strip()
                    if candidate_url.startswith(("http://", "https://")) and width > srcset_width:
                        srcset_url = candidate_url
                        srcset_width = width
                url = (
                    value.get("full_src")
                    or srcset_url
                    or value.get("src")
                    or value.get("url")
                    or value.get("thumbnail")
                    or value.get("gallery_image_src")
                    or value.get("archive_src")
                    or value.get("thumb_src")
                    or value.get("large")
                    or value.get("full")
                )
                if isinstance(url, (dict, list, tuple)):
                    collect(url)
                    url = None
                if isinstance(url, str) and url:
                    image_item = {
                        "source_url": url,
                        "alt": value.get("alt"),
                        "width": value.get("width"),
                        "height": value.get("height"),
                    }
                    attachment_id = (
                        value.get("attachment_id")
                        or value.get("image_id")
                        or value.get("id")
                    )
                    if str(attachment_id or "").isdigit():
                        image_item["attachment_id"] = str(attachment_id)
                    images.append(image_item)
                else:
                    for nested in value.values():
                        collect(nested)
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    collect(nested)
                return
            if isinstance(value, str):
                stripped = html.unescape(value).strip()
                if stripped.startswith(("[", "{")):
                    try:
                        collect(json.loads(stripped))
                        return
                    except json.JSONDecodeError:
                        pass
                for candidate in re.split(r"[\s,|]+", stripped):
                    if candidate.startswith(("http://", "https://")):
                        images.append({"source_url": candidate})

        for value in values:
            collect(value)

        unique: dict[str, dict[str, Any]] = {}
        for image_item in images:
            url = str(image_item.get("source_url") or "")
            if url and url != featured_url:
                existing = unique.get(url)
                if existing is None or sum(
                    value not in (None, "") for value in image_item.values()
                ) > sum(
                    value not in (None, "") for value in existing.values()
                ):
                    unique[url] = image_item
        gallery = list(unique.values())
        for position, image_item in enumerate(gallery):
            image_item["position"] = position
        return gallery

    def _variation_gallery_values(self, raw: dict[str, Any]) -> list[Any]:
        values: list[Any] = []
        for key in (
            "images",
            "gallery",
            "gallery_images",
            "wvg_images",
            "variation_images",
            "variation_gallery_images",
            "woo_variation_gallery_images",
            "additional_images",
        ):
            if raw.get(key):
                values.append(raw[key])
        for entry in raw.get("meta_data") or []:
            key = str(entry.get("key") or "").lower()
            if "gallery" in key and ("image" in key or "variation" in key):
                values.append(entry.get("value"))

        def scan_gallery_fields(value: Any, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = str(key).lower()
                    if any(token in normalized for token in ("gallery", "images", "media")):
                        values.append(nested)
                    if isinstance(nested, (dict, list, tuple)):
                        scan_gallery_fields(nested, depth + 1)
                    elif isinstance(nested, str) and nested.strip().startswith(("[", "{")):
                        try:
                            scan_gallery_fields(json.loads(html.unescape(nested)), depth + 1)
                        except json.JSONDecodeError:
                            pass
            elif isinstance(value, (list, tuple)):
                for nested in value[:500]:
                    scan_gallery_fields(nested, depth + 1)

        scan_gallery_fields(raw)
        return values

    def _variation_gallery_ids(self, raw: dict[str, Any]) -> list[str]:
        references: list[str] = []

        def collect(value: Any) -> None:
            if value in (None, "", False):
                return
            if isinstance(value, dict):
                for key in ("id", "image_id", "attachment_id"):
                    candidate = value.get(key)
                    if str(candidate or "").isdigit():
                        references.append(str(candidate))
                for key, nested in value.items():
                    if isinstance(nested, (dict, list, tuple)) or (
                        "id" in str(key).lower() and isinstance(nested, (str, int))
                    ):
                        collect(nested)
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    collect(nested)
                return
            if isinstance(value, int):
                references.append(str(value))
                return
            if isinstance(value, str):
                stripped = html.unescape(value).strip()
                if stripped.startswith(("[", "{")):
                    try:
                        collect(json.loads(stripped))
                        return
                    except json.JSONDecodeError:
                        pass
                if re.fullmatch(r"\d+(?:[\s,|]+\d+)*", stripped):
                    references.extend(re.findall(r"\d+", stripped))

        for value in self._variation_gallery_values(raw):
            collect(value)
        return list(dict.fromkeys(references))

    def _merge_json_ld(self, product: dict[str, Any], response: Response) -> None:
        for value in response.css(
            'script[type="application/ld+json"]::text'
        ).getall():
            try:
                payload = json.loads(value)
            except json.JSONDecodeError:
                continue
            candidates = payload.get("@graph", []) if isinstance(payload, dict) else payload
            if isinstance(candidates, dict):
                candidates = [candidates]
            if isinstance(payload, dict) and payload.get("@type") in {
                "Product",
                "ProductGroup",
            }:
                candidates = [payload]
            for candidate in candidates or []:
                if not isinstance(candidate, dict):
                    continue
                candidate_type = candidate.get("@type")
                accepted_types = {"Product", "ProductGroup"}
                if candidate_type not in accepted_types and not (
                    isinstance(candidate_type, list)
                    and accepted_types.intersection(candidate_type)
                ):
                    continue
                product.setdefault("structured_data", []).append(candidate)
                product["name"] = product.get("name") or candidate.get("name")
                product["sku"] = product.get("sku") or candidate.get("sku")
                product["description"] = (
                    product.get("description") or candidate.get("description")
                )
                category = candidate.get("category")
                if category and not product.get("categories"):
                    product["categories"] = [
                        {
                            "id": str(category),
                            "name": str(category),
                            "slug": self._slug(str(category)),
                            "assignment_source": "json_ld",
                        }
                    ]
                offers = candidate.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    product["price"] = (
                        product.get("price")
                        or offers.get("price")
                        or offers.get("lowPrice")
                        or offers.get("highPrice")
                    )
                    product["currency"] = product.get("currency") or offers.get(
                        "priceCurrency"
                    )
                    availability = str(offers.get("availability") or "").lower()
                    if not product.get("stock_status") and availability:
                        product["stock_status"] = (
                            "outofstock" if "outofstock" in availability else "instock"
                        )
                rating = candidate.get("aggregateRating") or {}
                if isinstance(rating, dict):
                    product["average_rating"] = product.get(
                        "average_rating"
                    ) or rating.get("ratingValue")
                    product["rating_count"] = product.get("rating_count") or rating.get(
                        "ratingCount"
                    )
                images = candidate.get("image") or []
                if isinstance(images, (str, dict)):
                    images = [images]
                for position, image_url in enumerate(images):
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url") or image_url.get("contentUrl")
                    if image_url:
                        product.setdefault("media", []).append(
                            {
                                "role": "json_ld",
                                "position": position,
                                "source_url": image_url,
                            }
                        )
                structured_variations = [
                    self._normalize_json_ld_variation(item, product.get("name") or "")
                    for item in candidate.get("hasVariant") or []
                    if isinstance(item, dict)
                ]
                structured_variations = [
                    item for item in structured_variations if item.get("id")
                ]
                if structured_variations:
                    product["type"] = "variable"
                    product["structured_variations"] = structured_variations

    def _normalize_json_ld_variation(
        self, raw: dict[str, Any], product_name: str
    ) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        for key in ("color", "size", "material", "pattern"):
            if raw.get(key):
                attributes[key] = raw[key]
        for property_item in raw.get("additionalProperty") or []:
            if not isinstance(property_item, dict):
                continue
            name = property_item.get("name") or property_item.get("propertyID")
            value = property_item.get("value") or property_item.get("valueReference")
            if name and value not in (None, ""):
                attributes[str(name)] = value

        offers = raw.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        offers = offers if isinstance(offers, dict) else {}
        images = raw.get("image") or []
        if isinstance(images, (str, dict)):
            images = [images]
        normalized_images: list[dict[str, Any]] = []
        for image_item in images:
            if isinstance(image_item, str):
                normalized_images.append({"source_url": image_item})
            elif isinstance(image_item, dict):
                url = image_item.get("url") or image_item.get("contentUrl")
                if url:
                    normalized_images.append(
                        {"source_url": url, "alt": image_item.get("caption")}
                    )
        availability = str(offers.get("availability") or "").lower()
        variation_id = raw.get("productID") or raw.get("sku") or raw.get("@id")
        parts = [f"{key}: {value}" for key, value in attributes.items()]
        return {
            "source": "json_ld",
            "id": variation_id,
            "name": raw.get("name")
            or (f"{product_name} — {' / '.join(parts)}" if parts else product_name),
            "sku": raw.get("sku"),
            "attributes": attributes,
            "price": str(offers.get("price"))
            if offers.get("price") is not None
            else None,
            "regular_price": None,
            "sale_price": None,
            "currency": offers.get("priceCurrency"),
            "stock_status": (
                "outofstock" if "outofstock" in availability else "instock"
            )
            if availability
            else None,
            "image_url": normalized_images[0]["source_url"]
            if normalized_images
            else None,
            "image_alt": normalized_images[0].get("alt")
            if normalized_images
            else None,
            "gallery": [
                {**image_item, "position": position}
                for position, image_item in enumerate(normalized_images[1:])
            ],
            "gallery_image_ids": [],
            "description": _clean_html(raw.get("description")),
            "raw": raw,
        }

    def _merge_html_categories(self, product: dict[str, Any], response: Response) -> None:
        categories = list(product.get("categories") or [])
        for link in response.css(".posted_in a"):
            name = _text(link.css("*::text, ::text").getall())
            url = link.attrib.get("href")
            categories.append(
                {
                    "id": self._slug(url or name),
                    "name": name,
                    "slug": self._slug(url or name),
                    "url": url,
                    "assignment_source": "product_page_assigned",
                }
            )
        if not categories:
            for link in response.css(
                ".woocommerce-breadcrumb a[href*='/product-category/'], "
                ".breadcrumb a[href*='/product-category/']"
            ):
                name = _text(link.css("*::text, ::text").getall())
                url = link.attrib.get("href")
                categories.append(
                    {
                        "id": self._slug(url or name),
                        "name": name,
                        "slug": self._slug(url or name),
                        "url": url,
                        "assignment_source": "breadcrumb_inferred",
                    }
                )
        unique: dict[str, dict[str, Any]] = {}
        for category in categories:
            key = str(category.get("id") or category.get("slug") or category.get("name"))
            unique[key] = category
        product["categories"] = list(unique.values())

    def _merge_html_tags_and_brands(
        self, product: dict[str, Any], response: Response
    ) -> None:
        tags = list(product.get("tags") or [])
        for link in response.css(".tagged_as a, .product_meta a[rel='tag']"):
            name = _text(link.css("*::text, ::text").getall())
            url = link.attrib.get("href")
            if name:
                tags.append(
                    {
                        "id": self._slug(url or name),
                        "name": name,
                        "slug": self._slug(url or name),
                        "url": url,
                        "assignment_source": "product_page_assigned",
                    }
                )
        product["tags"] = list(
            {
                str(item.get("id") or item.get("slug") or item.get("name")): item
                for item in tags
                if isinstance(item, dict)
            }.values()
        )

        brands = list(product.get("brands") or [])
        for link in response.css(
            ".pwb-single-product-brands a, .woocommerce-product-brand a, "
            ".product-brands a"
        ):
            name = _text(link.css("*::text, ::text").getall())
            url = link.attrib.get("href")
            if name:
                brands.append(
                    {
                        "id": self._slug(url or name),
                        "name": name,
                        "slug": self._slug(url or name),
                        "url": url,
                        "assignment_source": "product_page_assigned",
                    }
                )
        brand_meta = response.css(
            '[itemprop="brand"]::attr(content), meta[property="product:brand"]::attr(content)'
        ).get()
        if brand_meta and not brands:
            brands.append(
                {
                    "id": self._slug(brand_meta),
                    "name": brand_meta,
                    "slug": self._slug(brand_meta),
                    "assignment_source": "structured_meta",
                }
            )
        product["brands"] = list(
            {
                str(item.get("id") or item.get("slug") or item.get("name")): item
                for item in brands
                if isinstance(item, dict)
            }.values()
        )

    def _merge_html_attributes(self, product: dict[str, Any], response: Response) -> None:
        attributes = list(product.get("attributes") or [])
        by_name = {
            str(attribute.get("name") or "").strip().lower(): attribute
            for attribute in attributes
        }

        for row in response.css(
            "table.woocommerce-product-attributes tr, table.shop_attributes tr"
        ):
            name = _text(
                row.css(
                    "th::text, th *::text, .woocommerce-product-attributes-item__label::text"
                ).getall()
            ).rstrip(":")
            values = [
                value.strip()
                for value in row.css(
                    "td p::text, td a::text, td::text, "
                    ".woocommerce-product-attributes-item__value *::text"
                ).getall()
                if value.strip()
            ]
            if not name:
                continue
            key = name.lower()
            if key in by_name:
                existing = by_name[key]
                existing["options"] = existing.get("options") or values
                existing["visible"] = True
            else:
                attribute = {
                    "id": name,
                    "name": name,
                    "visible": True,
                    "variation": False,
                    "options": values,
                    "source": "additional_information_table",
                }
                attributes.append(attribute)
                by_name[key] = attribute

        for select in response.css("form.variations_form select"):
            select_name = select.attrib.get("name", "")
            label = response.css(f"label[for='{select.attrib.get('id', '')}']::text").get()
            name = (
                (label or "")
                .strip()
                .rstrip(":")
                or select_name.replace("attribute_", "").replace("pa_", "").replace("-", " ")
            )
            options = [
                _text(option.css("::text").getall()) or option.attrib.get("value")
                for option in select.css("option")
                if option.attrib.get("value")
            ]
            key = name.lower()
            if key in by_name:
                existing = by_name[key]
                existing["variation"] = True
                existing["options"] = existing.get("options") or options
            else:
                attribute = {
                    "id": select_name or name,
                    "name": name,
                    "visible": True,
                    "variation": True,
                    "options": options,
                    "source": "variation_select",
                }
                attributes.append(attribute)
                by_name[key] = attribute
        product["attributes"] = attributes

    def _merge_html_media(self, product: dict[str, Any], response: Response) -> None:
        media = list(product.get("media") or [])
        gallery = response.css(
            ".woocommerce-product-gallery img, "
            ".product-images img, "
            ".woocommerce-product-gallery__image a"
        )
        for position, node in enumerate(gallery):
            url = (
                node.attrib.get("data-large_image")
                or node.attrib.get("data-src")
                or node.attrib.get("href")
                or node.attrib.get("src")
            )
            if not url:
                continue
            media.append(
                {
                    "role": "featured" if position == 0 else "gallery",
                    "position": position,
                    "source_url": response.urljoin(url),
                    "alt": node.attrib.get("alt"),
                    "width": self._int_or_none(
                        node.attrib.get("data-large_image_width")
                        or node.attrib.get("width")
                    ),
                    "height": self._int_or_none(
                        node.attrib.get("data-large_image_height")
                        or node.attrib.get("height")
                    ),
                }
            )
        product["media"] = media

    def _add_variation_media(self, product: dict[str, Any]) -> None:
        media = list(product.get("media") or [])
        for position, variation in enumerate(product.get("variations") or []):
            if variation.get("image_url"):
                media.append(
                    {
                        "role": "variation",
                        "position": position,
                        "variation_id": variation.get("id"),
                        "source_url": variation["image_url"],
                        "alt": variation.get("image_alt"),
                        "width": variation.get("image_width"),
                        "height": variation.get("image_height"),
                    }
                )
            for gallery_position, gallery_item in enumerate(
                variation.get("gallery") or []
            ):
                media.append(
                    {
                        "role": "variation_gallery",
                        "position": gallery_position,
                        "variation_id": variation.get("id"),
                        "source_url": gallery_item.get("source_url"),
                        "alt": gallery_item.get("alt"),
                        "width": gallery_item.get("width"),
                        "height": gallery_item.get("height"),
                    }
                )
        product["media"] = self._dedupe_media(media)

    def _finish_product(self, product: dict[str, Any]):
        """Resolve attachment-only galleries, then emit one complete product."""

        known_media = {
            str(item.get("attachment_id")): dict(item)
            for variation in product.get("variations") or []
            for item in variation.get("gallery") or []
            if item.get("attachment_id")
        }
        self._apply_resolved_variation_galleries(product, known_media)
        attachment_ids = list(
            dict.fromkeys(
                str(attachment_id)
                for variation in product.get("variations") or []
                for attachment_id in variation.get("gallery_image_ids") or []
                if str(attachment_id).isdigit()
                and str(attachment_id) not in known_media
            )
        )
        if not attachment_ids:
            self._add_variation_media(product)
            yield from self._emit_product(product)
            return

        chunks = [
            attachment_ids[index : index + 100]
            for index in range(0, len(attachment_ids), 100)
        ]
        yield self._variation_gallery_media_request(
            product, chunks[0], chunks[1:], {}
        )

    def _variation_gallery_media_request(
        self,
        product: dict[str, Any],
        attachment_ids: list[str],
        remaining_chunks: list[list[str]],
        resolved_media: dict[str, dict[str, Any]],
    ) -> Request:
        return self._api_request(
            self._url(
                "wp-json/wp/v2/media",
                include=",".join(attachment_ids),
                per_page=100,
                orderby="include",
            ),
            self.parse_variation_gallery_media,
            meta={
                "api_kind": "wp_media",
                "request_kind": "variation_gallery_media",
                "product": product,
                "remaining_chunks": remaining_chunks,
                "resolved_media": resolved_media,
            },
            errback=self.errback_variation_gallery_media,
            include_authorization=False,
        )

    def parse_variation_gallery_media(self, response: Response):
        product = response.meta["product"]
        remaining_chunks = list(response.meta.get("remaining_chunks") or [])
        resolved_media = dict(response.meta.get("resolved_media") or {})
        payload = self._json_list(response)
        if payload is None:
            yield self._diagnostic_for_response(
                response,
                "variation_gallery_media_unavailable",
                f"Variation gallery attachments could not be resolved for product {product.get('id')}.",
            )
        else:
            for raw_media in payload:
                attachment_id = str(raw_media.get("id") or "")
                media_details = raw_media.get("media_details") or {}
                sizes = media_details.get("sizes") or {}
                full_size = sizes.get("full") or {}
                guid = raw_media.get("guid") or {}
                source_url = (
                    raw_media.get("source_url")
                    or full_size.get("source_url")
                    or (guid.get("rendered") if isinstance(guid, dict) else None)
                )
                if attachment_id and source_url:
                    resolved_media[attachment_id] = {
                        "attachment_id": attachment_id,
                        "source_url": source_url,
                        "alt": raw_media.get("alt_text"),
                        "width": media_details.get("width") or full_size.get("width"),
                        "height": media_details.get("height") or full_size.get("height"),
                    }

        if remaining_chunks:
            yield self._variation_gallery_media_request(
                product,
                remaining_chunks[0],
                remaining_chunks[1:],
                resolved_media,
            )
            return

        self._apply_resolved_variation_galleries(product, resolved_media)
        self._add_variation_media(product)
        yield from self._emit_product(product)

    def errback_variation_gallery_media(self, failure):
        request = failure.request
        product = request.meta["product"]
        remaining_chunks = list(request.meta.get("remaining_chunks") or [])
        resolved_media = dict(request.meta.get("resolved_media") or {})
        yield {
            "_record_type": "diagnostic",
            "kind": "variation_gallery_media_failed",
            "url": request.url,
            "status": None,
            "message": str(failure.value),
            "request_kind": "variation_gallery_media",
        }
        if remaining_chunks:
            yield self._variation_gallery_media_request(
                product,
                remaining_chunks[0],
                remaining_chunks[1:],
                resolved_media,
            )
            return
        self._apply_resolved_variation_galleries(product, resolved_media)
        self._add_variation_media(product)
        yield from self._emit_product(product)

    def _emit_product(self, product: dict[str, Any]):
        """Enforce the run limit at the final output boundary.

        A resumed Scrapy JOBDIR can already contain product-detail requests from
        a prior full crawl.  Limiting only newly scheduled API records therefore
        allows a test run to exceed its configured size.  Every completed
        product passes this gate, including restored requests.
        """

        if self.max_products and self.products_emitted >= self.max_products:
            return
        product_id = str(
            product.get("id")
            or product.get("slug")
            or product.get("url")
            or ""
        )
        if (
            not self.enrichment_mode
            and
            product_id
            and product_id in self.existing_product_ids
            and product_id not in self.refresh_product_ids
        ):
            return
        self.products_emitted += 1
        yield product

    @staticmethod
    def _apply_resolved_variation_galleries(
        product: dict[str, Any], resolved_media: dict[str, dict[str, Any]]
    ) -> None:
        for variation in product.get("variations") or []:
            gallery = list(variation.get("gallery") or [])
            existing_urls = {
                str(item.get("source_url")) for item in gallery if item.get("source_url")
            }
            featured_url = str(variation.get("image_url") or "")
            for attachment_id in variation.get("gallery_image_ids") or []:
                resolved = resolved_media.get(str(attachment_id))
                if not resolved:
                    continue
                source_url = str(resolved.get("source_url") or "")
                if not source_url or source_url == featured_url or source_url in existing_urls:
                    continue
                gallery.append({**resolved, "position": len(gallery)})
                existing_urls.add(source_url)
            variation["gallery"] = gallery

    @staticmethod
    def _dedupe_media(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
        role_priority = {
            "featured": 0,
            "variation": 0,
            "gallery": 1,
            "variation_gallery": 1,
            "json_ld": 2,
        }
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in media:
            url = str(item.get("source_url") or "")
            if not url:
                continue
            key = (str(item.get("variation_id") or ""), url)
            existing = unique.get(key)
            if existing is None:
                unique[key] = dict(item)
                continue
            existing_role = str(existing.get("role") or "gallery")
            incoming_role = str(item.get("role") or "gallery")
            if role_priority.get(incoming_role, 10) < role_priority.get(existing_role, 10):
                replacement = dict(item)
                for field in ("alt", "width", "height", "local_path", "checksum"):
                    replacement[field] = replacement.get(field) or existing.get(field)
                unique[key] = replacement
            else:
                for field in ("alt", "width", "height", "local_path", "checksum"):
                    if not existing.get(field) and item.get(field):
                        existing[field] = item[field]
        return list(unique.values())

    def _page_request(
        self, url: str, product: dict[str, Any] | None, dont_filter: bool = False
    ) -> Request:
        meta: dict[str, Any] = {"request_kind": "product_page"}
        if self.use_playwright:
            meta.update(
                {
                    "playwright": True,
                    "playwright_include_page": False,
                    "playwright_page_goto_kwargs": {
                        "wait_until": "domcontentloaded",
                        "timeout": 45_000,
                    },
                    "playwright_page_methods": [
                        PageMethod(
                            "wait_for_timeout",
                            int(os.getenv("SCRAPER_RENDER_WAIT_MS", "2500")),
                        )
                    ],
                }
            )
        return Request(
            url,
            callback=self.parse_product_page,
            cb_kwargs={"api_product": product},
            meta=meta,
            errback=self.errback_request,
            dont_filter=dont_filter,
        )

    def _store_variation_request(
        self, product: dict[str, Any], page: int, variations: list[dict[str, Any]]
    ) -> Request:
        return self._api_request(
            self._url(
                "wp-json/wc/store/v1/products",
                type="variation",
                parent=product.get("id"),
                per_page=100,
                page=page,
            ),
            self.parse_store_variations,
            meta={
                "api_kind": "store",
                "page": page,
                "product": product,
                "variations": variations,
            },
        )

    def _api_request(
        self,
        url: str,
        callback,
        meta: dict[str, Any],
        errback=None,
        include_authorization: bool = True,
    ) -> Request:
        headers = {"Accept": "application/json"}
        if self.authorization and include_authorization:
            headers["Authorization"] = self.authorization
        meta = {**meta, "request_kind": meta.get("request_kind", "api")}
        return Request(
            url,
            callback=callback,
            headers=headers,
            meta=meta,
            errback=errback or self.errback_request,
            dont_filter=True,
        )

    def _json_list(self, response: Response) -> list[dict[str, Any]] | None:
        if response.status != 200 or self._is_blocked(response):
            self._record_block(response)
            return None
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, list) else None

    def _record_block(self, response: Response) -> None:
        if not self._is_blocked(response):
            return
        already_blocked = self.access_blocked
        self.access_blocked = True
        reason_text = _text(
            response.xpath(
                "//td[contains("
                "translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'block reason'"
                ")]/following-sibling::td[1]//text()"
            ).getall()
        )
        if reason_text:
            self.block_reason = reason_text
        elif "access from your country" in response.text.lower():
            self.block_reason = "Access from the crawler's country was disabled."
        else:
            self.block_reason = "Sucuri website firewall denied access."
        crawler = getattr(self, "crawler", None)
        engine = getattr(crawler, "engine", None)
        if engine is not None and not already_blocked:
            engine.close_spider(self, reason="access_blocked")

    def errback_request(self, failure):
        request = failure.request
        yield {
            "_record_type": "diagnostic",
            "kind": "request_failed",
            "url": request.url,
            "status": None,
            "message": str(failure.value),
            "request_kind": request.meta.get("request_kind"),
        }
        if (
            request.meta.get("api_kind")
            and "products?" in request.url
            and not self.fallback_started
            and not self.category_ids
        ):
            self.fallback_started = True
            yield Request(
                urljoin(self.base_url, "wp-sitemap.xml"),
                callback=self.parse_sitemap,
                errback=self.errback_request,
                dont_filter=True,
            )

    @staticmethod
    def _is_blocked(response: Response) -> bool:
        sample = response.body[:120_000].lower()
        return (
            b"sucuri website firewall" in sample
            or b"access denied - sucuri" in sample
            or b"block id: geo02" in sample
            or b"sucuri_cloudproxy_js" in sample
        )

    def _diagnostic_for_response(
        self, response: Response, kind: str, message: str
    ) -> dict[str, Any]:
        return {
            "_record_type": "diagnostic",
            "kind": kind,
            "url": response.url,
            "status": response.status,
            "message": message,
            "blocked": self._is_blocked(response),
        }

    def _product_shell(self, response: Response) -> dict[str, Any]:
        return {
            "_record_type": "product",
            "source": "html",
            "id": self._html_product_id(response),
            "name": None,
            "slug": self._slug(response.url),
            "url": response.url,
            "sku": None,
            "type": "simple",
            "status": "publish",
            "categories": [],
            "tags": [],
            "brands": [],
            "attributes": [],
            "variations": [],
            "media": [],
        }

    @staticmethod
    def _html_product_id(response: Response) -> str:
        product_id = response.css(
            "form.cart input[name='product_id']::attr(value), "
            "button[name='add-to-cart']::attr(value)"
        ).get()
        if product_id:
            return product_id
        body_class = response.css("body::attr(class)").get("") or ""
        match = re.search(r"\bpostid-(\d+)\b", body_class)
        return match.group(1) if match else WooCommerceCatalogSpider._slug(response.url)

    def _url(self, path: str, **query: Any) -> str:
        url = urljoin(self.base_url, path)
        filtered = {key: value for key, value in query.items() if value is not None}
        return f"{url}?{urlencode(filtered)}" if filtered else url

    @staticmethod
    def _slug(value: str) -> str:
        parsed = urlparse(value)
        source = parsed.path if parsed.scheme else value
        segment = source.rstrip("/").split("/")[-1]
        return re.sub(r"[^a-z0-9]+", "-", segment.lower()).strip("-")

    @staticmethod
    def _minor_price(value: Any, minor_unit: Any) -> str | None:
        if value in (None, ""):
            return None
        try:
            divisor = Decimal(10) ** int(minor_unit or 0)
            return format(Decimal(str(value)) / divisor, "f")
        except (ValueError, TypeError):
            return str(value)

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
