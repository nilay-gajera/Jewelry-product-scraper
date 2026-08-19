from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from lgd_scraper.local_export import build_local_master, inspect_checkpoint
from lgd_scraper.woocommerce_csv import MASTER_FILENAME


def _checkpoint(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE products (id TEXT PRIMARY KEY, raw_json TEXT NOT NULL);
        CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, parent_id TEXT);
        CREATE TABLE product_categories (
            product_id TEXT, category_id TEXT, category_name TEXT
        );
        CREATE TABLE variations (id TEXT);
        CREATE TABLE images (id TEXT);
        """
    )
    product = {
        "id": "99",
        "name": "Local Export Ring",
        "sku": "LOCAL-99",
        "type": "simple",
        "attributes": [],
        "variations": [],
        "media": [
            {
                "role": "featured",
                "position": 0,
                "local_path": "products/99/ring.jpg",
            }
        ],
    }
    connection.execute(
        "INSERT INTO products VALUES (?, ?)",
        (product["id"], json.dumps(product)),
    )
    connection.commit()
    connection.close()


def test_build_local_master_from_checkpoint(tmp_path):
    database = tmp_path / "catalog.sqlite"
    output = tmp_path / "export"
    _checkpoint(database)

    report = build_local_master(
        database,
        output,
        public_base_url="https://cdn.example.test/catalog/media",
    )

    assert report["database_counts"]["products"] == 1
    assert report["woocommerce_export"] == {
        "parent_rows": 1,
        "variation_rows": 0,
        "master_rows": 1,
    }
    assert report["preflight"]["errors"] == 0
    with (output / MASTER_FILENAME).open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["SKU"] == "LOCAL-99"
    assert row["Images"] == (
        "https://cdn.example.test/catalog/media/products/99/ring.jpg"
    )


def test_inspect_checkpoint_rejects_non_catalog_database(tmp_path):
    database = tmp_path / "wrong.sqlite"
    sqlite3.connect(database).close()

    with pytest.raises(sqlite3.DatabaseError, match="missing table"):
        inspect_checkpoint(database)
