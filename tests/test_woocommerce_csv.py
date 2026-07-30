from __future__ import annotations

import csv
import json
import sqlite3

from lgd_scraper.woocommerce_csv import export_woocommerce_csvs


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
            }
        ],
        "raw_api": {
            "description": "<p>Solitaire engagement ring.</p>",
            "short_description": "<p>Made to order.</p>",
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

    with (tmp_path / "woocommerce-products.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert counts == {"parent_rows": 1, "variation_rows": 1}
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

    assert rows[1]["Type"] == "variation"
    assert rows[1]["Parent"] == "LGD-P-99"
    assert rows[1]["SKU"] == "LGD-P-99-V-501"
    assert rows[1]["Attribute 1 value(s)"] == "14K White Gold"
    assert rows[1]["Images"] == (
        "https://cdn.example.test/catalog/media/products/99/ring-white.jpg"
    )
