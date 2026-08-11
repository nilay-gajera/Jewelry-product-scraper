from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from lgd_scraper.woocommerce_csv import (
    LEGACY_WOOCOMMERCE_FILENAMES,
    MASTER_FILENAME,
    WooCommerceExportValidationError,
    _media_url,
    _variation_sku,
    export_woocommerce_csvs,
    validate_woocommerce_csv,
)


def test_placeholder_cdn_is_not_written_to_woocommerce_images():
    media = {
        "local_path": "products/99/ring.jpg",
        "source_url": "https://source.test/ring.jpg",
    }

    assert _media_url(
        media, "https://your-cdn-domain/jewelry-product-scraper/media"
    ) == ""
    assert _media_url(media, None) == ""


def test_variation_inheriting_parent_sku_gets_a_unique_import_sku():
    product = {"id": "99", "sku": "LGD-99"}
    variation = {"id": "501", "sku": "LGD-99"}

    assert _variation_sku(product, variation) == "LGD-99-V-501"


def test_exports_parent_and_variation_rows_with_s3_images(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT NOT NULL);
        CREATE TABLE categories (
            id TEXT PRIMARY KEY, name TEXT, parent_id TEXT
        );
        CREATE TABLE product_categories (
            product_id TEXT, category_id TEXT, category_name TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO categories (id, name, parent_id) VALUES (?, ?, ?)",
        [
            ("10", "Rings", ""),
            ("11", "Engagement Rings", "10"),
        ],
    )
    connection.execute(
        """
        INSERT INTO product_categories
            (product_id, category_id, category_name)
        VALUES ('99', '11', 'Engagement Rings')
        """
    )
    product = {
        "id": "99",
        "name": "Adriana Ring",
        "url": "https://source.test/product/adriana/",
        "sku": "",
        "type": "variable",
        "status": "publish",
        "stock_status": "instock",
        "regular_price": "1200",
        "categories": [{"id": 11, "name": "Engagement Rings"}],
        "attributes": [
            {
                "id": 5,
                "name": "Metal",
                "taxonomy": "pa_metal",
                "visible": True,
                "variation": True,
                "options": ["14K White Gold", "18K Yellow Gold"],
            }
        ],
        "default_attributes": [
            {"name": "Metal", "option": "14K White Gold"}
        ],
        "diamond_details": {
            "Carat": "1.96",
            "Growth Type": "HPHT",
        },
        "meta_data": [
            {"key": "custom_badge", "value": "Made to order"},
            {"key": "custom_badge", "value": "Limited edition"},
            {"key": "complex_config", "value": {"enabled": True}},
        ],
        "media": [
            {
                "role": "featured",
                "position": 0,
                "source_url": "https://source.test/ring.jpg",
                "local_path": "products/99/ring.jpg",
            },
            {
                "role": "variation",
                "position": 0,
                "variation_id": 501,
                "source_url": "https://source.test/ring-white.jpg",
                "local_path": "products/99/ring-white.jpg",
            },
            {
                "role": "variation_gallery",
                "position": 0,
                "variation_id": 501,
                "source_url": "https://source.test/ring-white-side.jpg",
                "local_path": "products/99/ring-white-side.jpg",
            },
        ],
        "variations": [
            {
                "id": 501,
                "name": "Adriana Ring — Metal: 14K White Gold",
                "sku": "",
                "attributes": {"metal": "14K White Gold"},
                "regular_price": "1200",
                "stock_status": "instock",
                "visible": True,
                "image_url": "https://source.test/ring-white.jpg",
                "meta_data": [
                    {"key": "custom_finish_note", "value": "Mirror"}
                ],
                "raw": {
                    "meta_data": [
                        {"key": "vendor_gallery_layout", "value": "slider"}
                    ]
                },
            }
        ],
        "raw_api": {
            "description": "<p>Solitaire engagement ring.</p>",
            "short_description": "<p>Made to order.</p>",
            "meta_data": [
                {"key": "seo_material", "value": "recycled gold"}
            ],
        },
    }
    connection.execute(
        "INSERT INTO products (id, raw_json) VALUES (?, ?)",
        ("99", json.dumps(product)),
    )

    counts = export_woocommerce_csvs(
        connection,
        tmp_path,
        public_base_url="https://cdn.example.test/catalog/media",
    )

    with (tmp_path / MASTER_FILENAME).open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert counts == {
        "parent_rows": 1,
        "variation_rows": 1,
        "master_rows": 2,
    }
    assert not any((tmp_path / name).exists() for name in LEGACY_WOOCOMMERCE_FILENAMES)
    assert rows[0]["Type"] == "variable"
    assert rows[0]["SKU"] == "LGD-P-99"
    assert rows[0]["Categories"] == "Rings > Engagement Rings"
    assert rows[0]["Images"] == (
        "https://cdn.example.test/catalog/media/products/99/ring.jpg"
    )
    assert rows[0]["Attribute 1 name"] == "Metal"
    assert rows[0]["Attribute 1 value(s)"] == (
        "14K White Gold, 18K Yellow Gold"
    )
    assert rows[0]["Attribute 1 default"] == "14K White Gold"
    assert rows[0]["Attribute 1 global"] == "1"
    assert rows[0]["meta:_diamond_carat"] == "1.96"
    assert rows[0]["meta:_diamond_growth_type"] == "HPHT"
    assert rows[0]["meta:custom_badge"] == (
        '["Made to order","Limited edition"]'
    )
    assert rows[0]["meta:complex_config"] == '{"enabled":true}'
    assert rows[0]["meta:seo_material"] == "recycled gold"
    assert rows[0]["meta:custom_finish_note"] == ""
    assert rows[0]["meta:_s3_media_paths"] == "products/99/ring.jpg"

    assert rows[1]["Type"] == "variation"
    assert rows[1]["Parent"] == "LGD-P-99"
    assert rows[1]["SKU"] == "LGD-P-99-V-501"
    assert rows[1]["Attribute 1 value(s)"] == "14K White Gold"
    assert rows[1]["Images"] == (
        "https://cdn.example.test/catalog/media/products/99/ring-white.jpg, "
        "https://cdn.example.test/catalog/media/products/99/ring-white-side.jpg"
    )
    assert rows[1]["meta:custom_finish_note"] == "Mirror"
    assert rows[1]["meta:vendor_gallery_layout"] == "slider"
    assert rows[1]["meta:custom_badge"] == ""
    assert rows[1]["meta:_variation_gallery_source_urls"] == (
        "https://source.test/ring-white-side.jpg"
    )
    assert rows[1]["meta:_s3_media_paths"] == (
        "products/99/ring-white.jpg | products/99/ring-white-side.jpg"
    )


def test_does_not_fallback_to_raw_categories_or_source_images(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT NOT NULL);
        CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, parent_id TEXT);
        CREATE TABLE product_categories (
            product_id TEXT, category_id TEXT, category_name TEXT
        );
        """
    )
    product = {
        "id": "diamond-1",
        "name": "Loose Diamond",
        "type": "simple",
        "categories": [{"id": "diamond", "name": "Lab Grown Diamonds"}],
        "attributes": [],
        "variations": [],
        "media": [
            {
                "role": "featured",
                "source_url": "https://source.test/diamond.jpg",
                "local_path": "products/diamond-1/diamond.jpg",
            }
        ],
    }
    connection.execute(
        "INSERT INTO products VALUES (?, ?)",
        (product["id"], json.dumps(product)),
    )

    export_woocommerce_csvs(connection, tmp_path)

    with (tmp_path / MASTER_FILENAME).open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["Categories"] == ""
    assert row["Images"] == ""
    assert row["Published"] == ""
    assert row["In stock?"] == ""
    assert row["Regular price"] == ""
    assert row["meta:_source_image_urls"] == "https://source.test/diamond.jpg"
    assert row["meta:_s3_media_paths"] == "products/diamond-1/diamond.jpg"


def test_exports_all_core_product_types_downloads_and_relationships(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT NOT NULL);
        CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, parent_id TEXT);
        CREATE TABLE product_categories (
            product_id TEXT, category_id TEXT, category_name TEXT
        );
        """
    )
    products = [
        {
            "id": "100",
            "name": "Downloadable CAD Ring",
            "sku": "RING-CAD",
            "global_unique_id": "0123456789012",
            "type": "simple",
            "virtual": True,
            "downloadable": True,
            "downloads": [
                {
                    "id": "cad-file",
                    "name": "CAD file",
                    "file": "https://cdn.example.test/files/ring.zip",
                },
                {
                    "name": "Certificate",
                    "file": "https://cdn.example.test/files/certificate.pdf",
                },
            ],
            "download_limit": -1,
            "download_expiry": "n/a",
            "date_on_sale_from": "2026-08-01T10:20:00",
            "date_on_sale_to": "2026-08-31T23:59:59",
            "backorders": "notify",
            "low_stock_amount": 2,
            "cogs_value": "12.50",
            "brands": [{"name": "Voscart Fine Jewelry"}],
            "upsell_ids": ["200"],
            "cross_sell_ids": [300],
            "meta_data": {
                "future_dynamic_key": "preserved",
                "future_object": {"enabled": True},
            },
            "attributes": [],
            "variations": [],
            "media": [],
        },
        {
            "id": "200",
            "name": "Partner Warranty",
            "sku": "WARRANTY-EXT",
            "type": "external",
            "external_url": "https://partner.example.test/warranty",
            "button_text": "Buy warranty",
            "attributes": [],
            "variations": [],
            "media": [],
        },
        {
            "id": "300",
            "name": "Ring Set",
            "sku": "RING-SET",
            "type": "grouped",
            "grouped_products": ["100", {"id": "200"}],
            "attributes": [],
            "variations": [],
            "media": [],
        },
    ]
    connection.executemany(
        "INSERT INTO products VALUES (?, ?)",
        [(product["id"], json.dumps(product)) for product in products],
    )

    counts = export_woocommerce_csvs(connection, tmp_path)
    report = validate_woocommerce_csv(tmp_path / MASTER_FILENAME)
    with (tmp_path / MASTER_FILENAME).open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = {row["SKU"]: row for row in csv.DictReader(handle)}

    assert counts == {"parent_rows": 3, "variation_rows": 0, "master_rows": 3}
    assert report == {
        "rows": 3,
        "parent_rows": 3,
        "variation_rows": 0,
        "errors": 0,
    }
    downloadable = rows["RING-CAD"]
    assert downloadable["Type"] == "simple, downloadable, virtual"
    assert downloadable["GTIN, UPC, EAN, or ISBN"] == "0123456789012"
    assert downloadable["Date sale price starts"] == "2026-08-01"
    assert downloadable["Date sale price ends"] == "2026-08-31"
    assert downloadable["Backorders allowed?"] == "notify"
    assert downloadable["Low stock amount"] == "2"
    assert downloadable["Cost of goods"] == "12.50"
    assert downloadable["Brands"] == "Voscart Fine Jewelry"
    assert downloadable["Upsells"] == "WARRANTY-EXT"
    assert downloadable["Cross-sells"] == "RING-SET"
    assert downloadable["meta:_source_upsell_ids"] == '["200"]'
    assert downloadable["meta:_source_cross_sell_ids"] == "[300]"
    assert downloadable["Download 1 name"] == "CAD file"
    assert downloadable["Download 1 ID"] == "cad-file"
    assert downloadable["Download 1 URL"] == (
        "https://cdn.example.test/files/ring.zip"
    )
    assert downloadable["Download 2 name"] == "Certificate"
    assert downloadable["meta:future_dynamic_key"] == "preserved"
    assert downloadable["meta:future_object"] == '{"enabled":true}'
    assert rows["WARRANTY-EXT"]["Type"] == "external"
    assert rows["WARRANTY-EXT"]["External URL"] == (
        "https://partner.example.test/warranty"
    )
    assert rows["RING-SET"]["Type"] == "grouped"
    assert rows["RING-SET"]["Grouped products"] == (
        "RING-CAD, WARRANTY-EXT"
    )


def test_variation_uses_raw_inventory_download_and_gallery_fields(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT NOT NULL);
        CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, parent_id TEXT);
        CREATE TABLE product_categories (
            product_id TEXT, category_id TEXT, category_name TEXT
        );
        """
    )
    product = {
        "id": "variable-1",
        "name": "Digital Ring Model",
        "sku": "DIGITAL-RING",
        "type": "variable",
        "attributes": [
            {
                "name": "Metal",
                "visible": True,
                "variation": True,
                "options": ["White, Gold"],
            }
        ],
        "variations": [
            {
                "id": "variation-1",
                "name": "Digital Ring Model - White, Gold",
                "attributes": {"metal": "White, Gold"},
                "gallery": [],
                "raw": {
                    "virtual": True,
                    "downloadable": True,
                    "downloads": [
                        {
                            "name": "3D model",
                            "file": "https://cdn.example.test/files/model.stl",
                        }
                    ],
                    "download_limit": 3,
                    "download_expiry": 30,
                    "manage_stock": "parent",
                    "backorders": "notify",
                    "tax_class": "parent",
                    "date_on_sale_from": "2026-08-02",
                    "date_on_sale_to": "2026-08-20",
                    "regular_price": "49.95",
                    "visible": True,
                },
            }
        ],
        "media": [
            {
                "role": "variation",
                "variation_id": "variation-1",
                "position": 0,
                "local_path": "products/variable-1/front.jpg",
            },
            {
                "role": "variation_gallery",
                "variation_id": "variation-1",
                "position": 1,
                "local_path": "products/variable-1/side.jpg",
            },
        ],
    }
    connection.execute(
        "INSERT INTO products VALUES (?, ?)",
        (product["id"], json.dumps(product)),
    )

    export_woocommerce_csvs(
        connection,
        tmp_path,
        public_base_url="https://cdn.example.test/catalog/media",
    )
    with (tmp_path / MASTER_FILENAME).open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))

    variation = rows[1]
    assert variation["Type"] == "variation, downloadable, virtual"
    assert variation["Stock"] == "parent"
    assert variation["Backorders allowed?"] == "notify"
    assert variation["Tax class"] == "parent"
    assert variation["Download limit"] == "3"
    assert variation["Download expiry days"] == "30"
    assert variation["Download 1 name"] == "3D model"
    assert variation["Download 1 URL"] == (
        "https://cdn.example.test/files/model.stl"
    )
    assert rows[0]["Attribute 1 value(s)"] == r"White\, Gold"
    assert variation["Attribute 1 value(s)"] == r"White\, Gold"
    assert variation["Images"] == (
        "https://cdn.example.test/catalog/media/products/variable-1/front.jpg, "
        "https://cdn.example.test/catalog/media/products/variable-1/side.jpg"
    )


def test_invalid_export_never_replaces_last_good_master(tmp_path):
    target = tmp_path / MASTER_FILENAME
    target.write_text("last good export", encoding="utf-8")
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT NOT NULL);
        CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, parent_id TEXT);
        CREATE TABLE product_categories (
            product_id TEXT, category_id TEXT, category_name TEXT
        );
        """
    )
    products = [
        {
            "id": "1",
            "name": "First",
            "sku": "DUPLICATE",
            "type": "simple",
            "attributes": [],
            "variations": [],
        },
        {
            "id": "2",
            "name": "Second",
            "sku": "DUPLICATE",
            "type": "simple",
            "attributes": [],
            "variations": [],
        },
    ]
    connection.executemany(
        "INSERT INTO products VALUES (?, ?)",
        [(product["id"], json.dumps(product)) for product in products],
    )

    with pytest.raises(WooCommerceExportValidationError, match="duplicate SKU"):
        export_woocommerce_csvs(connection, tmp_path)

    assert target.read_text(encoding="utf-8") == "last good export"
    assert not (tmp_path / f".{MASTER_FILENAME}.tmp").exists()
