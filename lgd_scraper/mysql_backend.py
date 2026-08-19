"""MySQL backend for the admin dashboard.

Provides a persistent read-replica on Hostinger so the dashboard loads
instantly even after a Render cold start — no S3 checkpoint download needed.

The Scrapy pipeline still writes to local SQLite during crawling.  After
each crawl completes, ``sync_from_sqlite`` pushes the full catalog into
MySQL where the admin API reads from.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def mysql_enabled() -> bool:
    """Return True when MySQL environment variables are configured."""
    return bool(os.getenv("MYSQL_HOST"))


def mysql_connect(*, autocommit: bool = False):
    """Return a PyMySQL connection using environment variables.

    Imports pymysql lazily so the rest of the application can run in
    environments that do not have it installed (e.g. lightweight test
    environments).
    """
    import pymysql

    return pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        charset="utf8mb4",
        autocommit=autocommit,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=60,
    )


# Module-level cached connection for read-only admin dashboard queries.
# Avoids creating a new TCP connection (~50ms to Hostinger) per API call.
_read_connection = None


def mysql_read_connect():
    """Return a cached read-only connection, reconnecting if stale."""
    global _read_connection
    if _read_connection is not None:
        try:
            _read_connection.ping(reconnect=True)
            return _read_connection
        except Exception:
            try:
                _read_connection.close()
            except Exception:
                pass
            _read_connection = None
    _read_connection = mysql_connect(autocommit=True)
    return _read_connection


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS products (
    id VARCHAR(255) PRIMARY KEY,
    name TEXT,
    slug VARCHAR(500),
    url TEXT,
    sku VARCHAR(255),
    product_type VARCHAR(50),
    currency VARCHAR(10),
    price VARCHAR(50),
    regular_price VARCHAR(50),
    sale_price VARCHAR(50),
    stock_status VARCHAR(50),
    source VARCHAR(100),
    product_family VARCHAR(50),
    enrichment_schema_version INT DEFAULT 0,
    raw_json LONGTEXT NOT NULL,
    INDEX idx_products_type (product_type),
    INDEX idx_products_sku (sku),
    INDEX idx_products_family (product_family),
    INDEX idx_products_stock (stock_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS variations (
    product_id VARCHAR(255) NOT NULL,
    id VARCHAR(255) NOT NULL,
    name TEXT,
    sku VARCHAR(255),
    price VARCHAR(50),
    regular_price VARCHAR(50),
    sale_price VARCHAR(50),
    stock_status VARCHAR(50),
    image_url TEXT,
    attributes_json TEXT,
    raw_json LONGTEXT NOT NULL,
    PRIMARY KEY (product_id, id),
    INDEX idx_variations_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS categories (
    id VARCHAR(255) PRIMARY KEY,
    name TEXT,
    slug VARCHAR(500),
    parent_id VARCHAR(255),
    description TEXT,
    image_url TEXT,
    product_count INT,
    raw_json LONGTEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS product_categories (
    product_id VARCHAR(255) NOT NULL,
    category_id VARCHAR(255) NOT NULL,
    category_name TEXT,
    category_slug VARCHAR(500),
    PRIMARY KEY (product_id, category_id),
    INDEX idx_pc_category (category_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS attributes (
    id VARCHAR(255) PRIMARY KEY,
    name TEXT,
    taxonomy VARCHAR(255),
    attribute_type VARCHAR(50),
    order_by VARCHAR(50),
    has_archives TINYINT(1),
    terms_json TEXT,
    raw_json LONGTEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS product_attributes (
    product_id VARCHAR(255) NOT NULL,
    attribute_key VARCHAR(255) NOT NULL,
    attribute_name TEXT,
    variation TINYINT(1),
    visible TINYINT(1),
    options_json TEXT,
    raw_json LONGTEXT NOT NULL,
    PRIMARY KEY (product_id, attribute_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS images (
    product_id VARCHAR(255) NOT NULL,
    variation_id VARCHAR(255) NOT NULL DEFAULT '',
    role VARCHAR(50) NOT NULL,
    position INT NOT NULL DEFAULT 0,
    source_url TEXT NOT NULL,
    local_path TEXT,
    alt TEXT,
    width INT,
    height INT,
    checksum VARCHAR(64),
    PRIMARY KEY (product_id, variation_id, role, position, source_url(200)),
    INDEX idx_images_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS diagnostics (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    kind VARCHAR(100),
    url TEXT,
    status INT,
    message TEXT,
    raw_json LONGTEXT NOT NULL,
    INDEX idx_diag_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def create_tables(connection=None) -> None:
    """Create all catalog tables in MySQL if they do not exist."""

    own_connection = connection is None
    if own_connection:
        connection = mysql_connect(autocommit=True)
    try:
        cursor = connection.cursor()
        for statement in _CREATE_TABLES.split(";"):
            statement = statement.strip()
            if statement:
                cursor.execute(statement)
        cursor.close()
    finally:
        if own_connection:
            connection.close()


# ---------------------------------------------------------------------------
# Sync SQLite → MySQL
# ---------------------------------------------------------------------------

_BATCH_SIZE = 500


def sync_from_sqlite(sqlite_path: Path) -> dict[str, int]:
    """Push the full SQLite catalog into MySQL.

    Returns a dict of table name → row count synced.
    """

    if not sqlite_path.exists():
        LOGGER.warning("MySQL sync: SQLite database not found at %s", sqlite_path)
        return {}
    if not mysql_enabled():
        LOGGER.info("MySQL sync: skipped (MYSQL_HOST not configured)")
        return {}

    LOGGER.info("MySQL sync: starting from %s", sqlite_path)
    sqlite_conn = sqlite3.connect(
        f"file:{sqlite_path}?mode=ro", uri=True, timeout=5
    )
    sqlite_conn.row_factory = sqlite3.Row

    mysql_conn = mysql_connect()
    create_tables(mysql_conn)
    counts: dict[str, int] = {}

    try:
        counts["products"] = _sync_products(sqlite_conn, mysql_conn)
        counts["variations"] = _sync_table(
            sqlite_conn, mysql_conn, "variations",
            columns=[
                "product_id", "id", "name", "sku", "price",
                "regular_price", "sale_price", "stock_status",
                "image_url", "attributes_json", "raw_json",
            ],
            key_columns=["product_id", "id"],
        )
        counts["categories"] = _sync_table(
            sqlite_conn, mysql_conn, "categories",
            columns=[
                "id", "name", "slug", "parent_id", "description",
                "image_url", "product_count", "raw_json",
            ],
            key_columns=["id"],
        )
        counts["product_categories"] = _sync_table(
            sqlite_conn, mysql_conn, "product_categories",
            columns=["product_id", "category_id", "category_name", "category_slug"],
            key_columns=["product_id", "category_id"],
        )
        counts["attributes"] = _sync_table(
            sqlite_conn, mysql_conn, "attributes",
            columns=[
                "id", "name", "taxonomy", "attribute_type",
                "order_by", "has_archives", "terms_json", "raw_json",
            ],
            key_columns=["id"],
        )
        counts["product_attributes"] = _sync_table(
            sqlite_conn, mysql_conn, "product_attributes",
            columns=[
                "product_id", "attribute_key", "attribute_name",
                "variation", "visible", "options_json", "raw_json",
            ],
            key_columns=["product_id", "attribute_key"],
        )
        counts["images"] = _sync_images(sqlite_conn, mysql_conn)
        counts["diagnostics"] = _sync_diagnostics(sqlite_conn, mysql_conn)

        mysql_conn.commit()
        LOGGER.info("MySQL sync: completed — %s", counts)
        return counts
    except Exception:
        mysql_conn.rollback()
        LOGGER.exception("MySQL sync: failed, rolled back")
        raise
    finally:
        sqlite_conn.close()
        mysql_conn.close()


def _sync_products(sqlite_conn: sqlite3.Connection, mysql_conn) -> int:
    """Sync products with extracted product_family and enrichment_schema_version."""

    cursor = sqlite_conn.execute(
        "SELECT id, name, slug, url, sku, product_type, currency, price, "
        "regular_price, sale_price, stock_status, source, raw_json FROM products"
    )
    mysql_cur = mysql_conn.cursor()

    # Clear and replace for a clean sync
    mysql_cur.execute("DELETE FROM products")
    count = 0
    batch: list[tuple] = []

    for row in cursor:
        raw_json = row["raw_json"] or "{}"
        try:
            product = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            product = {}
        product_family = product.get("product_family") or None
        schema_version = 0
        try:
            schema_version = int(product.get("enrichment_schema_version") or 0)
        except (TypeError, ValueError):
            pass

        batch.append((
            row["id"], row["name"], row["slug"], row["url"], row["sku"],
            row["product_type"], row["currency"], row["price"],
            row["regular_price"], row["sale_price"], row["stock_status"],
            row["source"], product_family, schema_version, raw_json,
        ))
        count += 1

        if len(batch) >= _BATCH_SIZE:
            _insert_products_batch(mysql_cur, batch)
            batch.clear()

    if batch:
        _insert_products_batch(mysql_cur, batch)

    mysql_cur.close()
    return count


def _insert_products_batch(cursor, batch: list[tuple]) -> None:
    cursor.executemany(
        """
        INSERT INTO products (
            id, name, slug, url, sku, product_type, currency, price,
            regular_price, sale_price, stock_status, source,
            product_family, enrichment_schema_version, raw_json
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            name=VALUES(name), slug=VALUES(slug), url=VALUES(url),
            sku=VALUES(sku), product_type=VALUES(product_type),
            currency=VALUES(currency), price=VALUES(price),
            regular_price=VALUES(regular_price), sale_price=VALUES(sale_price),
            stock_status=VALUES(stock_status), source=VALUES(source),
            product_family=VALUES(product_family),
            enrichment_schema_version=VALUES(enrichment_schema_version),
            raw_json=VALUES(raw_json)
        """,
        batch,
    )


def _sync_table(
    sqlite_conn: sqlite3.Connection,
    mysql_conn,
    table: str,
    columns: list[str],
    key_columns: list[str],
) -> int:
    """Generic sync for simple tables."""

    col_list = ", ".join(columns)
    cursor = sqlite_conn.execute(f"SELECT {col_list} FROM {table}")
    mysql_cur = mysql_conn.cursor()
    mysql_cur.execute(f"DELETE FROM {table}")

    count = 0
    batch: list[tuple] = []
    placeholders = ", ".join(["%s"] * len(columns))
    update_cols = [c for c in columns if c not in key_columns]
    update_clause = ", ".join(f"{c}=VALUES({c})" for c in update_cols) if update_cols else f"{key_columns[0]}={key_columns[0]}"

    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )

    for row in cursor:
        batch.append(tuple(row[c] for c in columns))
        count += 1
        if len(batch) >= _BATCH_SIZE:
            mysql_cur.executemany(sql, batch)
            batch.clear()

    if batch:
        mysql_cur.executemany(sql, batch)

    mysql_cur.close()
    return count


def _sync_images(sqlite_conn: sqlite3.Connection, mysql_conn) -> int:
    """Sync images table, handling the composite key with source_url truncation."""

    columns = [
        "product_id", "variation_id", "role", "position",
        "source_url", "local_path", "alt", "width", "height", "checksum",
    ]
    # Check which columns exist in the SQLite table
    sqlite_columns = {
        row[1]
        for row in sqlite_conn.execute("PRAGMA table_info(images)").fetchall()
    }
    available = [c for c in columns if c in sqlite_columns]
    if not available:
        return 0

    col_list = ", ".join(available)
    cursor = sqlite_conn.execute(f"SELECT {col_list} FROM images")
    mysql_cur = mysql_conn.cursor()
    mysql_cur.execute("DELETE FROM images")

    count = 0
    batch: list[tuple] = []
    placeholders = ", ".join(["%s"] * len(available))
    sql = f"INSERT IGNORE INTO images ({col_list}) VALUES ({placeholders})"

    for row in cursor:
        batch.append(tuple(row[c] for c in available))
        count += 1
        if len(batch) >= _BATCH_SIZE:
            mysql_cur.executemany(sql, batch)
            batch.clear()

    if batch:
        mysql_cur.executemany(sql, batch)

    mysql_cur.close()
    return count


def _sync_diagnostics(sqlite_conn: sqlite3.Connection, mysql_conn) -> int:
    """Sync diagnostics (auto-increment, no composite key)."""

    cursor = sqlite_conn.execute(
        "SELECT created_at, kind, url, status, message, raw_json FROM diagnostics"
    )
    mysql_cur = mysql_conn.cursor()
    mysql_cur.execute("DELETE FROM diagnostics")

    count = 0
    batch: list[tuple] = []

    for row in cursor:
        batch.append((
            row["created_at"], row["kind"], row["url"],
            row["status"], row["message"], row["raw_json"],
        ))
        count += 1
        if len(batch) >= _BATCH_SIZE:
            mysql_cur.executemany(
                "INSERT INTO diagnostics (created_at, kind, url, status, message, raw_json) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                batch,
            )
            batch.clear()

    if batch:
        mysql_cur.executemany(
            "INSERT INTO diagnostics (created_at, kind, url, status, message, raw_json) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            batch,
        )

    mysql_cur.close()
    return count
