from __future__ import annotations

import csv
import json
import sqlite3

from lgd_scraper.woocommerce_csv import (
    LEGACY_WOOCOMMERCE_FILENAMES,
    MASTER_FILENAME,
    _media_url,
    _variation_sku,
    export_woocommerce_csvs,
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
    assert rows[0]["meta:custom_badge"] == "Made to order"
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
