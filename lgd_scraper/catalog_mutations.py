from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lgd_scraper.woocommerce_csv import export_woocommerce_csvs


NORMALIZED_EXPORTS = {
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

JSONL_EXPORTS = {
    "products.jsonl": ("products", "id"),
    "categories.jsonl": ("categories", "id"),
    "attributes.jsonl": ("attributes", "id"),
    "diagnostics.jsonl": ("diagnostics", "rowid"),
}

COUNT_TABLES = (
    "products",
    "variations",
    "categories",
    "attributes",
    "product_categories",
    "product_attributes",
    "images",
    "diagnostics",
)


def copy_database(source_path: Path, destination_path: Path) -> None:
    """Create a consistent SQLite copy, including committed WAL contents."""

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def delete_product(database_path: Path, product_id: str) -> dict[str, Any] | None:
    """Delete one product and its normalized relationships transactionally."""

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT raw_json FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            product = json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError):
            product = {}
        media_paths = list(
            dict.fromkeys(
                str(item.get("local_path"))
                for item in product.get("media") or []
                if item.get("local_path")
            )
        )
        table_names = {
            item[0]
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        with connection:
            for table in (
                "product_categories",
                "product_attributes",
                "variations",
                "images",
            ):
                if table in table_names:
                    connection.execute(
                        f"DELETE FROM {table} WHERE product_id = ?", (product_id,)
                    )
            connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return {
            "id": product_id,
            "name": product.get("name") or product_id,
            "media_paths": media_paths,
        }
    finally:
        connection.close()


def write_normalized_csvs(
    connection: sqlite3.Connection, output_dir: Path
) -> None:
    for filename, query in NORMALIZED_EXPORTS.items():
        cursor = connection.execute(query)
        with (output_dir / filename).open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.writer(handle)
            writer.writerow([column[0] for column in cursor.description])
            writer.writerows(cursor.fetchall())


def rebuild_catalog_artifacts(database_path: Path, output_dir: Path) -> dict[str, Any]:
    """Rebuild mutable exports after a catalog deletion."""

    output_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        write_normalized_csvs(connection, output_dir)
        for filename, (table, order_by) in JSONL_EXPORTS.items():
            rows = connection.execute(
                f"SELECT raw_json FROM {table} ORDER BY {order_by}"
            ).fetchall()
            with (output_dir / filename).open("w", encoding="utf-8") as handle:
                for (raw_json,) in rows:
                    handle.write(str(raw_json).rstrip("\n") + "\n")

        woocommerce_counts = export_woocommerce_csvs(connection, output_dir)
        database_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in COUNT_TABLES
        }
    finally:
        connection.close()

    summary_path = output_dir / "crawl-summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        summary = {}
    summary.update(
        {
            "database_counts": database_counts,
            "woocommerce_export": woocommerce_counts,
            "last_catalog_mutation": datetime.now(UTC).isoformat(),
        }
    )
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    archive = output_dir / "catalog-export.zip"
    archive.unlink(missing_ok=True)
    shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=output_dir)
    return {
        "database_counts": database_counts,
        "woocommerce_export": woocommerce_counts,
    }
