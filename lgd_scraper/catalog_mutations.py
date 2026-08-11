from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import zipfile
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


def delete_products(
    database_path: Path, product_ids: list[str]
) -> list[dict[str, Any]]:
    """Delete products and their normalized relationships in one transaction."""

    requested_ids = list(
        dict.fromkeys(
            str(product_id).strip()
            for product_id in product_ids
            if str(product_id).strip()
        )
    )
    if not requested_ids:
        return []
    if len(requested_ids) > 500:
        raise ValueError("A maximum of 500 products can be deleted at once.")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ", ".join("?" for _ in requested_ids)
        rows = connection.execute(
            f"SELECT id, raw_json FROM products WHERE id IN ({placeholders})",
            requested_ids,
        ).fetchall()
        products: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                product = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                product = {}
            products[str(row["id"])] = product
        found_ids = [product_id for product_id in requested_ids if product_id in products]
        if not found_ids:
            return []

        found_placeholders = ", ".join("?" for _ in found_ids)
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
                        f"DELETE FROM {table} WHERE product_id IN ({found_placeholders})",
                        found_ids,
                    )
            connection.execute(
                f"DELETE FROM products WHERE id IN ({found_placeholders})", found_ids
            )

        deleted = []
        for product_id in found_ids:
            product = products[product_id]
            media_paths = list(
                dict.fromkeys(
                    str(item.get("local_path"))
                    for item in product.get("media") or []
                    if item.get("local_path")
                )
            )
            deleted.append(
                {
                    "id": product_id,
                    "name": product.get("name") or product_id,
                    "media_paths": media_paths,
                }
            )
        return deleted
    finally:
        connection.close()


def delete_product(database_path: Path, product_id: str) -> dict[str, Any] | None:
    """Delete one product and its normalized relationships transactionally."""

    deleted = delete_products(database_path, [product_id])
    return deleted[0] if deleted else None


def unreferenced_media_paths(
    database_path: Path, candidate_paths: list[str]
) -> list[str]:
    """Return only media objects no remaining product references after deletion."""

    normalized = list(
        dict.fromkeys(str(path).strip() for path in candidate_paths if str(path).strip())
    )
    if not normalized:
        return []
    referenced: set[str] = set()
    connection = sqlite3.connect(database_path)
    try:
        image_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(images)")
        }
        if "local_path" not in image_columns:
            return normalized
        for index in range(0, len(normalized), 500):
            batch = normalized[index : index + 500]
            placeholders = ", ".join("?" for _ in batch)
            referenced.update(
                str(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT local_path FROM images "
                    f"WHERE local_path IN ({placeholders})",
                    batch,
                ).fetchall()
            )
    finally:
        connection.close()
    return [path for path in normalized if path not in referenced]


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
            writer.writerows(cursor)


def write_jsonl_exports(connection: sqlite3.Connection, output_dir: Path) -> None:
    """Rewrite nested exports from SQLite so resumed runs cannot duplicate rows."""

    for filename, (table, order_by) in JSONL_EXPORTS.items():
        target = output_dir / filename
        temporary = target.with_name(f".{target.name}.tmp")
        rows = connection.execute(
            f"SELECT raw_json FROM {table} ORDER BY {order_by}"
        )
        with temporary.open("w", encoding="utf-8") as handle:
            for (raw_json,) in rows:
                handle.write(str(raw_json).rstrip("\n") + "\n")
        temporary.replace(target)


def build_catalog_archive(output_dir: Path) -> Path:
    """Build an atomic archive outside its input tree to avoid self-inclusion."""

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "catalog-export.zip"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".catalog-export-",
            suffix=".zip",
            dir=output_dir.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in sorted(output_dir.rglob("*")):
                if not path.is_file() or path == target:
                    continue
                if path.name.endswith(("-wal", "-shm")):
                    continue
                archive.write(path, arcname=path.relative_to(output_dir))
        temporary_path.replace(target)
        return target
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def rebuild_catalog_artifacts(database_path: Path, output_dir: Path) -> dict[str, Any]:
    """Rebuild mutable exports after a catalog deletion."""

    output_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        write_normalized_csvs(connection, output_dir)
        write_jsonl_exports(connection, output_dir)

        woocommerce_counts = export_woocommerce_csvs(
            connection,
            output_dir,
            public_base_url=os.getenv("S3_PUBLIC_BASE_URL"),
        )
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

    build_catalog_archive(output_dir)
    return {
        "database_counts": database_counts,
        "woocommerce_export": woocommerce_counts,
    }
