from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lgd_scraper.mysql_backend import mysql_read_connect, mysql_enabled
from lgd_scraper.s3sync import (
    load_json_object,
    presigned_media_url,
    save_json_object,
)

_LOGGER = logging.getLogger(__name__)


DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": "https://www.loosegrowndiamond.com/",
    "mode": "test",
    "max_products": 5,
    "concurrency": 2,
    "download_delay": 1.0,
    "download_timeout": 45,
    "retry_times": 3,
    "enrich_html": True,
    "download_media": True,
    "resume_checkpoint": True,
    "obey_robots": False,
    "category_ids": [],
}


def effective_settings(settings_path: Path) -> dict[str, Any]:
    values = dict(DEFAULT_SETTINGS)
    values.update(
        {
            "base_url": os.getenv("SCRAPER_BASE_URL", values["base_url"]),
            "concurrency": int(
                os.getenv("SCRAPER_CONCURRENCY", str(values["concurrency"]))
            ),
            "download_delay": float(
                os.getenv("SCRAPER_DOWNLOAD_DELAY", str(values["download_delay"]))
            ),
            "download_timeout": int(
                os.getenv("SCRAPER_DOWNLOAD_TIMEOUT", str(values["download_timeout"]))
            ),
            "retry_times": int(
                os.getenv("SCRAPER_RETRY_TIMES", str(values["retry_times"]))
            ),
        }
    )
    persisted: Any = None
    if settings_path.exists():
        try:
            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            persisted = None
    if persisted is None:
        persisted = load_json_object("admin", "settings.json")
        if isinstance(persisted, dict):
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(persisted, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    if isinstance(persisted, dict):
        values.update(
            {key: value for key, value in persisted.items() if key in values}
        )
    return values


def persist_settings(settings_path: Path, values: dict[str, Any]) -> dict[str, Any]:
    safe_values = {
        key: value for key, value in values.items() if key in DEFAULT_SETTINGS
    }
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(safe_values, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(settings_path)
    save_json_object(safe_values, "admin", "settings.json")
    return safe_values


def secret_presence() -> dict[str, bool]:
    return {
        "aws_access_key": bool(os.getenv("AWS_ACCESS_KEY_ID")),
        "aws_secret_key": bool(os.getenv("AWS_SECRET_ACCESS_KEY")),
        "woocommerce_key": bool(os.getenv("WC_CONSUMER_KEY")),
        "woocommerce_secret": bool(os.getenv("WC_CONSUMER_SECRET")),
        "proxy": bool(os.getenv("SCRAPER_PROXY_URL")),
        "s3_bucket": bool(os.getenv("S3_BUCKET")),
    }


def storage_settings() -> dict[str, Any]:
    configured_base = (os.getenv("S3_PUBLIC_BASE_URL") or "").rstrip("/")
    public_base = configured_base if _valid_public_media_base(configured_base) else ""
    return {
        "bucket": os.getenv("S3_BUCKET") or "",
        "prefix": os.getenv("S3_PREFIX", "jewelry-product-scraper"),
        "region": os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "",
        "public_media_url": public_base,
        "media_delivery": (
            "cdn"
            if public_base
            else "private_s3_signed"
            if os.getenv("S3_BUCKET")
            else "source"
        ),
        "public_media_url_ignored": bool(configured_base and not public_base),
        "endpoint_configured": bool(os.getenv("AWS_ENDPOINT_URL")),
    }


def _connect(database_path: Path) -> sqlite3.Connection | None:
    if not database_path.exists():
        return None
    connection = sqlite3.connect(
        f"file:{quote(str(database_path))}?mode=ro", uri=True, timeout=2
    )
    connection.row_factory = sqlite3.Row
    return connection


# When MySQL is configured but down, every connection attempt costs its full
# connect timeout. The dashboard polls several endpoints every five seconds, so
# that alone can saturate the request threadpool. Stop trying for a short while
# after a failure and serve the local checkpoint instead.
_MYSQL_RETRY_AFTER_SECONDS = float(os.getenv("MYSQL_RETRY_AFTER_SECONDS", "60"))
_mysql_unavailable_until = 0.0


def _connect_mysql(prefer_sqlite: bool = False):
    """Return a MySQL connection or None if MySQL is not configured/reachable.

    ``prefer_sqlite`` forces the local checkpoint. MySQL is only a read replica
    refreshed after a crawl finishes, so while a crawl is writing local SQLite
    the replica is stale and must not be used.
    """
    global _mysql_unavailable_until

    if prefer_sqlite or not mysql_enabled():
        return None
    if time.monotonic() < _mysql_unavailable_until:
        return None
    try:
        connection = mysql_read_connect()
    except Exception as exc:
        _mysql_unavailable_until = time.monotonic() + _MYSQL_RETRY_AFTER_SECONDS
        _LOGGER.warning(
            "MySQL unavailable, serving the local checkpoint for %.0fs: %s",
            _MYSQL_RETRY_AFTER_SECONDS,
            exc,
        )
        return None
    _mysql_unavailable_until = 0.0
    return connection


def _mysql_table_exists(connection, table: str) -> bool:
    cursor = connection.cursor()
    cursor.execute("SHOW TABLES LIKE %s", (table,))
    result = cursor.fetchone() is not None
    cursor.close()
    return result


def _mysql_fetchone(connection, sql: str, params=()) -> tuple | None:
    cursor = connection.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone()
    cursor.close()
    return row


def _mysql_fetchall(connection, sql: str, params=()) -> list[tuple]:
    cursor = connection.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def public_media_url(media: dict[str, Any]) -> str:
    local_path = str(media.get("local_path") or "").lstrip("/")
    base = (os.getenv("S3_PUBLIC_BASE_URL") or "").rstrip("/")
    if _valid_public_media_base(base) and local_path:
        return f"{base}/{local_path}"
    if local_path:
        signed = presigned_media_url(local_path)
        if signed:
            return signed
    return str(media.get("source_url") or "")


def _valid_public_media_base(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(
        normalized.startswith(("https://", "http://"))
        and "your-cdn-domain" not in normalized
    )


def quality_for(product: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    media = product.get("media") or []
    categories = product.get("categories") or []
    attributes = product.get("attributes") or []
    variations = product.get("variations") or []
    if not product.get("name"):
        missing.append("name")
    if not (product.get("description") or product.get("description_html")):
        missing.append("description")
    if not media:
        missing.append("images")
    if not categories:
        missing.append("categories")
    if not attributes:
        missing.append("attributes")
    if product.get("type") == "variable" and not variations:
        missing.append("variations")
    variation_issues = 0
    for variation in variations:
        if not variation.get("attributes") or not (
            variation.get("image_url") or variation.get("gallery")
        ):
            variation_issues += 1
    score = max(0, round(100 - (len(missing) * 14) - (variation_issues * 2)))
    return {
        "score": score,
        "missing": missing,
        "variation_issues": variation_issues,
        "complete": not missing and variation_issues == 0,
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "products": 0,
        "variations": 0,
        "images": 0,
        "categories": 0,
        "attributes": 0,
        "diagnostics": 0,
        "quality": {
            "missing_images": 0,
            "missing_categories": 0,
            "missing_attributes": 0,
            "missing_variations": 0,
        },
        "storefront": {"live": 0, "offline": 0, "unknown": 0},
        "enrichment": {
            "schema_version": 1,
            "candidates": 0,
            "completed": 0,
            "remaining": 0,
            "diamonds": 0,
            "diamonds_completed": 0,
            "variable_products": 0,
            "variable_products_completed": 0,
            "variations_with_gallery": 0,
            "variation_gallery_images": 0,
            "media_missing_storage": 0,
            "products_with_media_failures": 0,
        },
    }


def catalog_summary(
    database_path: Path, *, prefer_sqlite: bool = False
) -> dict[str, Any]:
    mysql_conn = _connect_mysql(prefer_sqlite)
    if mysql_conn is not None:
        try:
            return _catalog_summary_mysql(mysql_conn)
        except Exception as exc:
            _LOGGER.warning("MySQL catalog_summary failed, falling back: %s", exc)
    return _catalog_summary_sqlite(database_path)


def _catalog_summary_mysql(connection) -> dict[str, Any]:
    empty = _empty_summary()
    tables_to_count = ["products", "variations", "images", "categories", "attributes", "diagnostics"]
    for table in tables_to_count:
        if _mysql_table_exists(connection, table):
            row = _mysql_fetchone(connection, f"SELECT COUNT(*) FROM {table}")
            empty[table] = row[0] if row else 0

    if _mysql_table_exists(connection, "products"):
        row = _mysql_fetchone(
            connection,
            """
            SELECT
                COALESCE(SUM(product_family = 'loose_diamond' OR product_type = 'variable'), 0),
                COALESCE(SUM((product_family = 'loose_diamond' OR product_type = 'variable')
                             AND enrichment_schema_version >= 1), 0),
                COALESCE(SUM(product_family = 'loose_diamond'), 0),
                COALESCE(SUM(product_family = 'loose_diamond' AND enrichment_schema_version >= 1), 0),
                COALESCE(SUM(product_type = 'variable'), 0),
                COALESCE(SUM(product_type = 'variable' AND enrichment_schema_version >= 1), 0)
            FROM products
            """
        )
        if row:
            enrichment = empty["enrichment"]
            (
                enrichment["candidates"],
                enrichment["completed"],
                enrichment["diamonds"],
                enrichment["diamonds_completed"],
                enrichment["variable_products"],
                enrichment["variable_products_completed"],
            ) = tuple(row)
            enrichment["remaining"] = max(
                0, enrichment["candidates"] - enrichment["completed"]
            )

    if _mysql_table_exists(connection, "variations"):
        row = _mysql_fetchone(
            connection,
            "SELECT COUNT(*) FROM variations WHERE JSON_VALID(raw_json) AND COALESCE(JSON_LENGTH(JSON_EXTRACT(raw_json, '$.gallery')), 0) > 0"
        )
        if row:
            empty["enrichment"]["variations_with_gallery"] = row[0]

    if _mysql_table_exists(connection, "images"):
        row = _mysql_fetchone(
            connection,
            """
            SELECT
                COALESCE(SUM(role = 'variation_gallery'), 0),
                COALESCE(SUM(local_path IS NULL OR TRIM(local_path) = ''), 0)
            FROM images
            """
        )
        if row:
            empty["enrichment"]["variation_gallery_images"] = row[0]
            empty["enrichment"]["media_missing_storage"] = row[1]

    if _mysql_table_exists(connection, "products"):
        row = _mysql_fetchone(
            connection,
            """
            SELECT
                COALESCE(SUM(s = 'live'), 0),
                COALESCE(SUM(s = 'offline'), 0),
                COALESCE(SUM(s IS NULL OR s NOT IN ('live', 'offline')), 0)
            FROM (
                SELECT JSON_UNQUOTE(
                    JSON_EXTRACT(raw_json, '$.storefront_status')
                ) AS s FROM products
            ) AS storefront
            """,
        )
        if row:
            empty["storefront"] = {
                "live": int(row[0]),
                "offline": int(row[1]),
                "unknown": int(row[2]),
            }

    quality_checks = {
        "missing_images": "SELECT COUNT(*) FROM products p WHERE NOT EXISTS (SELECT 1 FROM images i WHERE i.product_id = p.id)",
        "missing_categories": "SELECT COUNT(*) FROM products p WHERE NOT EXISTS (SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id)",
        "missing_attributes": "SELECT COUNT(*) FROM products p WHERE NOT EXISTS (SELECT 1 FROM product_attributes pa WHERE pa.product_id = p.id)",
        "missing_variations": "SELECT COUNT(*) FROM products p WHERE p.product_type = 'variable' AND NOT EXISTS (SELECT 1 FROM variations v WHERE v.product_id = p.id)",
    }
    for key, query in quality_checks.items():
        try:
            row = _mysql_fetchone(connection, query)
            empty["quality"][key] = row[0] if row else 0
        except Exception:
            pass

    return empty


def _catalog_summary_sqlite(database_path: Path) -> dict[str, Any]:
    connection = _connect(database_path)
    empty = _empty_summary()
    if connection is None:
        return empty
    try:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for key, table in (
            ("products", "products"),
            ("variations", "variations"),
            ("images", "images"),
            ("categories", "categories"),
            ("attributes", "attributes"),
            ("diagnostics", "diagnostics"),
        ):
            if table in table_names:
                empty[key] = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]

        if "products" in table_names:
            enrichment_row = connection.execute(
                """
                WITH catalog AS (
                    SELECT
                        product_type,
                        CASE WHEN json_valid(raw_json)
                            THEN json_extract(raw_json, '$.product_family') END AS family,
                        COALESCE(CASE WHEN json_valid(raw_json)
                            THEN json_extract(raw_json, '$.enrichment_schema_version') END, 0)
                            AS schema_version,
                        COALESCE(CASE WHEN json_valid(raw_json)
                            THEN json_array_length(raw_json, '$.variations') END, 0)
                            AS embedded_variations,
                        COALESCE(CASE WHEN json_valid(raw_json)
                            THEN json_array_length(raw_json, '$.media_download_failures') END, 0)
                            AS media_failures
                    FROM products
                ), candidates AS (
                    SELECT *,
                        (family = 'loose_diamond' OR product_type = 'variable'
                         OR embedded_variations > 0) AS is_candidate
                    FROM catalog
                )
                SELECT
                    COALESCE(SUM(is_candidate), 0),
                    COALESCE(SUM(is_candidate AND schema_version >= 1), 0),
                    COALESCE(SUM(family = 'loose_diamond'), 0),
                    COALESCE(SUM(family = 'loose_diamond' AND schema_version >= 1), 0),
                    COALESCE(SUM(product_type = 'variable'), 0),
                    COALESCE(SUM(product_type = 'variable' AND schema_version >= 1), 0),
                    COALESCE(SUM(media_failures > 0), 0)
                FROM candidates
                """
            ).fetchone()
            enrichment = empty["enrichment"]
            (
                enrichment["candidates"],
                enrichment["completed"],
                enrichment["diamonds"],
                enrichment["diamonds_completed"],
                enrichment["variable_products"],
                enrichment["variable_products_completed"],
                enrichment["products_with_media_failures"],
            ) = tuple(enrichment_row)
            enrichment["remaining"] = max(
                0, enrichment["candidates"] - enrichment["completed"]
            )

        if "products" in table_names:
            # ponytail: json_extract scan, add a storefront_status column if the
            # catalog outgrows a few hundred thousand products.
            storefront_row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(status = 'live'), 0),
                    COALESCE(SUM(status = 'offline'), 0),
                    COALESCE(SUM(status IS NULL OR status NOT IN ('live', 'offline')), 0)
                FROM (
                    SELECT CASE WHEN json_valid(raw_json)
                        THEN json_extract(raw_json, '$.storefront_status') END AS status
                    FROM products
                )
                """
            ).fetchone()
            empty["storefront"] = {
                "live": storefront_row[0],
                "offline": storefront_row[1],
                "unknown": storefront_row[2],
            }

        variation_columns = (
            {
                row[1]
                for row in connection.execute("PRAGMA table_info(variations)").fetchall()
            }
            if "variations" in table_names
            else set()
        )
        if "raw_json" in variation_columns:
            empty["enrichment"]["variations_with_gallery"] = connection.execute(
                """
                SELECT COUNT(*) FROM variations
                WHERE json_valid(raw_json)
                  AND COALESCE(json_array_length(raw_json, '$.gallery'), 0) > 0
                """
            ).fetchone()[0]
        image_columns = (
            {
                row[1]
                for row in connection.execute("PRAGMA table_info(images)").fetchall()
            }
            if "images" in table_names
            else set()
        )
        if {"role", "local_path"}.issubset(image_columns):
            media_row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(role = 'variation_gallery'), 0),
                    COALESCE(SUM(local_path IS NULL OR TRIM(local_path) = ''), 0)
                FROM images
                """
            ).fetchone()
            empty["enrichment"]["variation_gallery_images"] = media_row[0]
            empty["enrichment"]["media_missing_storage"] = media_row[1]

        relationship_tables = {
            "images",
            "product_categories",
            "product_attributes",
            "variations",
        }
        if relationship_tables.issubset(table_names):
            checks = {
                "missing_images": """
                    SELECT COUNT(*) FROM products p
                    WHERE NOT EXISTS (SELECT 1 FROM images i WHERE i.product_id = p.id)
                """,
                "missing_categories": """
                    SELECT COUNT(*) FROM products p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id
                    )
                """,
                "missing_attributes": """
                    SELECT COUNT(*) FROM products p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM product_attributes pa WHERE pa.product_id = p.id
                    )
                """,
                "missing_variations": """
                    SELECT COUNT(*) FROM products p
                    WHERE p.product_type = 'variable'
                      AND NOT EXISTS (
                          SELECT 1 FROM variations v WHERE v.product_id = p.id
                      )
                """,
            }
            for key, query in checks.items():
                empty["quality"][key] = connection.execute(query).fetchone()[0]
        else:
            rows = connection.execute("SELECT raw_json FROM products").fetchall()
            for row in rows:
                product = _json(row["raw_json"], {})
                missing = set(quality_for(product)["missing"])
                for field in ("images", "categories", "attributes", "variations"):
                    if field in missing:
                        empty["quality"][f"missing_{field}"] += 1
        return empty
    finally:
        connection.close()


def list_products(
    database_path: Path,
    *,
    query: str = "",
    product_type: str = "",
    category_id: str = "",
    stock_status: str = "",
    coverage: str = "",
    storefront: str = "",
    sort: str = "name_asc",
    page: int = 1,
    page_size: int = 25,
    prefer_sqlite: bool = False,
) -> dict[str, Any]:
    mysql_conn = _connect_mysql(prefer_sqlite)
    if mysql_conn is not None:
        try:
            return _list_products_mysql(
                mysql_conn, query=query, product_type=product_type,
                category_id=category_id, stock_status=stock_status,
                coverage=coverage, storefront=storefront,
                sort=sort, page=page, page_size=page_size,
            )
        except Exception as exc:
            _LOGGER.warning("MySQL list_products failed, falling back: %s", exc)
    return _list_products_sqlite(
        database_path, query=query, product_type=product_type,
        category_id=category_id, stock_status=stock_status,
        coverage=coverage, storefront=storefront,
        sort=sort, page=page, page_size=page_size,
    )


def _build_product_item(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Build a product list item from a row with raw_json."""
    product = _json(row_dict.get("raw_json"), {})
    media = product.get("media") or []
    unique_media_count = len(
        {item.get("source_url") for item in media if item.get("source_url")}
    )
    featured = next(
        (item for item in media if item.get("role") == "featured"),
        media[0] if media else None,
    )
    return {
        "id": row_dict.get("id"),
        "name": row_dict.get("name"),
        "sku": row_dict.get("sku"),
        "type": row_dict.get("product_type"),
        "price": row_dict.get("price"),
        "currency": product.get("currency") or "",
        "stock_status": row_dict.get("stock_status"),
        "source": row_dict.get("source"),
        "thumbnail": public_media_url(featured) if featured else "",
        "variation_count": len(product.get("variations") or []),
        "image_count": unique_media_count,
        "category_count": len(product.get("categories") or []),
        "categories": [
            item.get("name") for item in product.get("categories") or []
        ],
        "updated": product.get("date_modified"),
        "storefront_status": product.get("storefront_status") or "unknown",
        "storefront_checked_at": product.get("storefront_checked_at"),
        "quality": quality_for(product),
    }


def _list_products_mysql(
    connection,
    *,
    query: str,
    product_type: str,
    category_id: str,
    stock_status: str,
    coverage: str,
    storefront: str,
    sort: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    where: list[str] = []
    parameters: list[Any] = []
    if query:
        where.append("(p.name LIKE %s OR p.sku LIKE %s OR p.id LIKE %s)")
        pattern = f"%{query}%"
        parameters.extend([pattern, pattern, pattern])
    if product_type:
        where.append("p.product_type = %s")
        parameters.append(product_type)
    if category_id:
        where.append(
            "EXISTS (SELECT 1 FROM product_categories pc "
            "WHERE pc.product_id = p.id AND pc.category_id = %s)"
        )
        parameters.append(category_id)
    if stock_status:
        where.append("p.stock_status = %s")
        parameters.append(stock_status)
    coverage_filters = {
        "complete": "EXISTS (SELECT 1 FROM images i WHERE i.product_id = p.id) AND EXISTS (SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id) AND EXISTS (SELECT 1 FROM product_attributes pa WHERE pa.product_id = p.id) AND (p.product_type != 'variable' OR EXISTS (SELECT 1 FROM variations v WHERE v.product_id = p.id))",
        "missing_images": "NOT EXISTS (SELECT 1 FROM images i WHERE i.product_id = p.id)",
        "missing_categories": "NOT EXISTS (SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id)",
        "missing_attributes": "NOT EXISTS (SELECT 1 FROM product_attributes pa WHERE pa.product_id = p.id)",
        "missing_variations": "p.product_type = 'variable' AND NOT EXISTS (SELECT 1 FROM variations v WHERE v.product_id = p.id)",
    }
    if coverage in coverage_filters:
        where.append(f"({coverage_filters[coverage]})")
    if storefront in {"live", "offline"}:
        where.append("JSON_UNQUOTE(JSON_EXTRACT(p.raw_json, '$.storefront_status')) = %s")
        parameters.append(storefront)
    elif storefront == "unknown":
        where.append(
            "(JSON_UNQUOTE(JSON_EXTRACT(p.raw_json, '$.storefront_status')) IS NULL OR JSON_UNQUOTE(JSON_EXTRACT(p.raw_json, '$.storefront_status')) NOT IN ('live', 'offline'))"
        )

    name_asc = "COALESCE(NULLIF(p.name, ''), p.id) ASC, p.id ASC"
    name_desc = "COALESCE(NULLIF(p.name, ''), p.id) DESC, p.id DESC"
    sort_orders = {
        "name_asc": name_asc,
        "name_desc": name_desc,
        "price_asc": f"CASE WHEN NULLIF(p.price, '') IS NULL THEN 1 ELSE 0 END, CAST(p.price AS DECIMAL(20,2)) ASC, {name_asc}",
        "price_desc": f"CASE WHEN NULLIF(p.price, '') IS NULL THEN 1 ELSE 0 END, CAST(p.price AS DECIMAL(20,2)) DESC, {name_asc}",
        "quality_desc": name_asc,
        "quality_asc": name_asc,
        "images_desc": name_asc,
        "images_asc": name_asc,
        "variations_desc": name_asc,
        "variations_asc": name_asc,
    }
    order_by = sort_orders.get(sort, name_asc)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    import pymysql.cursors
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    cursor.execute(f"SELECT COUNT(*) AS cnt FROM products p{clause}", parameters)
    total = cursor.fetchone()["cnt"]
    cursor.execute(
        f"SELECT p.id, p.name, p.sku, p.product_type, p.price, "
        f"p.stock_status, p.source, p.raw_json "
        f"FROM products p{clause} ORDER BY {order_by} LIMIT %s OFFSET %s",
        [*parameters, page_size, (page - 1) * page_size],
    )
    rows = cursor.fetchall()
    cursor.close()
    items = [_build_product_item(row) for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _list_products_sqlite(
    database_path: Path,
    *,
    query: str,
    product_type: str,
    category_id: str,
    stock_status: str,
    coverage: str,
    storefront: str,
    sort: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    connection = _connect(database_path)
    if connection is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    where: list[str] = []
    parameters: list[Any] = []
    if query:
        where.append("(p.name LIKE ? OR p.sku LIKE ? OR p.id LIKE ?)")
        pattern = f"%{query}%"
        parameters.extend([pattern, pattern, pattern])
    if product_type:
        where.append("p.product_type = ?")
        parameters.append(product_type)
    if category_id:
        where.append(
            "EXISTS (SELECT 1 FROM product_categories pc "
            "WHERE pc.product_id = p.id AND pc.category_id = ?)"
        )
        parameters.append(category_id)
    if stock_status:
        where.append("p.stock_status = ?")
        parameters.append(stock_status)

    coverage_filters = {
        "complete": """
            EXISTS (SELECT 1 FROM images i WHERE i.product_id = p.id)
            AND EXISTS (SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id)
            AND EXISTS (SELECT 1 FROM product_attributes pa WHERE pa.product_id = p.id)
            AND (p.product_type != 'variable' OR EXISTS (
                SELECT 1 FROM variations v WHERE v.product_id = p.id
            ))
        """,
        "missing_images": "NOT EXISTS (SELECT 1 FROM images i WHERE i.product_id = p.id)",
        "missing_categories": "NOT EXISTS (SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id)",
        "missing_attributes": "NOT EXISTS (SELECT 1 FROM product_attributes pa WHERE pa.product_id = p.id)",
        "missing_variations": """
            p.product_type = 'variable' AND NOT EXISTS (
                SELECT 1 FROM variations v WHERE v.product_id = p.id
            )
        """,
    }
    if coverage in coverage_filters:
        where.append(f"({coverage_filters[coverage]})")
    if storefront in {"live", "offline"}:
        where.append(f"CASE WHEN json_valid(p.raw_json) THEN json_extract(p.raw_json, '$.storefront_status') END = ?")
        parameters.append(storefront)
    elif storefront == "unknown":
        where.append(
            f"(CASE WHEN json_valid(p.raw_json) THEN json_extract(p.raw_json, '$.storefront_status') END IS NULL OR CASE WHEN json_valid(p.raw_json) THEN json_extract(p.raw_json, '$.storefront_status') END NOT IN ('live', 'offline'))"
        )

    missing_coverage_count = """
        (CASE WHEN EXISTS (SELECT 1 FROM images i WHERE i.product_id = p.id) THEN 0 ELSE 1 END
        + CASE WHEN EXISTS (SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id) THEN 0 ELSE 1 END
        + CASE WHEN EXISTS (SELECT 1 FROM product_attributes pa WHERE pa.product_id = p.id) THEN 0 ELSE 1 END
        + CASE WHEN p.product_type != 'variable' OR EXISTS (
            SELECT 1 FROM variations v WHERE v.product_id = p.id
          ) THEN 0 ELSE 1 END)
    """
    name_asc = "COALESCE(NULLIF(p.name, ''), p.id) COLLATE NOCASE ASC, p.id ASC"
    name_desc = "COALESCE(NULLIF(p.name, ''), p.id) COLLATE NOCASE DESC, p.id DESC"
    sort_orders = {
        "name_asc": name_asc,
        "name_desc": name_desc,
        "price_asc": f"CASE WHEN NULLIF(p.price, '') IS NULL THEN 1 ELSE 0 END, CAST(p.price AS REAL) ASC, {name_asc}",
        "price_desc": f"CASE WHEN NULLIF(p.price, '') IS NULL THEN 1 ELSE 0 END, CAST(p.price AS REAL) DESC, {name_asc}",
        "quality_desc": f"{missing_coverage_count} ASC, {name_asc}",
        "quality_asc": f"{missing_coverage_count} DESC, {name_asc}",
        "images_desc": f"(SELECT COUNT(DISTINCT i.source_url) FROM images i WHERE i.product_id = p.id) DESC, {name_asc}",
        "images_asc": f"(SELECT COUNT(DISTINCT i.source_url) FROM images i WHERE i.product_id = p.id) ASC, {name_asc}",
        "variations_desc": f"(SELECT COUNT(*) FROM variations v WHERE v.product_id = p.id) DESC, {name_asc}",
        "variations_asc": f"(SELECT COUNT(*) FROM variations v WHERE v.product_id = p.id) ASC, {name_asc}",
    }
    order_by = sort_orders.get(sort, sort_orders["name_asc"])
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    try:
        total = connection.execute(
            f"SELECT COUNT(*) FROM products p{clause}", parameters
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT p.id, p.name, p.sku, p.product_type, p.price,
                   p.stock_status, p.source, p.raw_json
            FROM products p{clause}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
        items = [_build_product_item(dict(row)) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        connection.close()


def product_filter_options(
    database_path: Path, *, prefer_sqlite: bool = False
) -> dict[str, Any]:
    """Return compact filter facets for the product administration page."""

    mysql_conn = _connect_mysql(prefer_sqlite)
    if mysql_conn is not None:
        try:
            return _product_filter_options_mysql(mysql_conn)
        except Exception as exc:
            _LOGGER.warning("MySQL product_filter_options failed: %s", exc)
    return _product_filter_options_sqlite(database_path)


def _product_filter_options_mysql(connection) -> dict[str, Any]:
    import pymysql.cursors
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    cursor.execute(
        "SELECT product_type AS value, COUNT(*) AS count FROM products "
        "WHERE COALESCE(product_type, '') != '' GROUP BY product_type ORDER BY product_type"
    )
    types = list(cursor.fetchall())
    cursor.execute(
        "SELECT stock_status AS value, COUNT(*) AS count FROM products "
        "WHERE COALESCE(stock_status, '') != '' GROUP BY stock_status ORDER BY stock_status"
    )
    stock_statuses = list(cursor.fetchall())
    categories = []
    if _mysql_table_exists(connection, "product_categories"):
        cursor.execute(
            "SELECT pc.category_id AS id, "
            "COALESCE(MAX(NULLIF(pc.category_name, '')), pc.category_id) AS name, "
            "COUNT(DISTINCT pc.product_id) AS count "
            "FROM product_categories pc JOIN products p ON p.id = pc.product_id "
            "GROUP BY pc.category_id ORDER BY name"
        )
        categories = list(cursor.fetchall())
    cursor.close()
    return {"categories": categories, "types": types, "stock_statuses": stock_statuses}


def _product_filter_options_sqlite(database_path: Path) -> dict[str, Any]:
    empty = {"categories": [], "types": [], "stock_statuses": []}
    connection = _connect(database_path)
    if connection is None:
        return empty
    try:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        types = connection.execute(
            """
            SELECT product_type AS value, COUNT(*) AS count
            FROM products
            WHERE COALESCE(product_type, '') != ''
            GROUP BY product_type ORDER BY product_type COLLATE NOCASE
            """
        ).fetchall()
        stock_statuses = connection.execute(
            """
            SELECT stock_status AS value, COUNT(*) AS count
            FROM products
            WHERE COALESCE(stock_status, '') != ''
            GROUP BY stock_status ORDER BY stock_status COLLATE NOCASE
            """
        ).fetchall()
        categories: list[sqlite3.Row] = []
        if "product_categories" in table_names:
            categories = connection.execute(
                """
                SELECT pc.category_id AS id,
                       COALESCE(MAX(NULLIF(pc.category_name, '')), pc.category_id) AS name,
                       COUNT(DISTINCT pc.product_id) AS count
                FROM product_categories pc
                JOIN products p ON p.id = pc.product_id
                GROUP BY pc.category_id
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()
        return {
            "categories": [dict(row) for row in categories],
            "types": [dict(row) for row in types],
            "stock_statuses": [dict(row) for row in stock_statuses],
        }
    finally:
        connection.close()


def product_detail(
    database_path: Path, product_id: str, *, prefer_sqlite: bool = False
) -> dict[str, Any] | None:
    mysql_conn = _connect_mysql(prefer_sqlite)
    if mysql_conn is not None:
        try:
            row = _mysql_fetchone(
                mysql_conn,
                "SELECT raw_json FROM products WHERE id = %s",
                (product_id,),
            )
            if not row:
                return None
            product = _json(row[0], {})
            for media in product.get("media") or []:
                media["display_url"] = public_media_url(media)
            product["quality"] = quality_for(product)
            return product
        except Exception as exc:
            _LOGGER.warning("MySQL product_detail failed: %s", exc)
    connection = _connect(database_path)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT raw_json FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if not row:
            return None
        product = _json(row["raw_json"], {})
        for media in product.get("media") or []:
            media["display_url"] = public_media_url(media)
        product["quality"] = quality_for(product)
        return product
    finally:
        connection.close()


def diagnostics(
    database_path: Path, limit: int = 100, *, prefer_sqlite: bool = False
) -> list[dict[str, Any]]:
    mysql_conn = _connect_mysql(prefer_sqlite)
    if mysql_conn is not None:
        try:
            rows = _mysql_fetchall(
                mysql_conn,
                "SELECT created_at, kind, url, status, message, raw_json "
                "FROM diagnostics ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            return [
                {
                    "created_at": row[0],
                    "kind": row[1],
                    "url": row[2],
                    "status": row[3],
                    "message": row[4],
                    "details": _json(row[5], {}),
                }
                for row in rows
            ]
        except Exception as exc:
            _LOGGER.warning("MySQL diagnostics failed: %s", exc)
    connection = _connect(database_path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT created_at, kind, url, status, message, raw_json
            FROM diagnostics ORDER BY rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "created_at": row["created_at"],
                "kind": row["kind"],
                "url": row["url"],
                "status": row["status"],
                "message": row["message"],
                "details": _json(row["raw_json"], {}),
            }
            for row in rows
        ]
    finally:
        connection.close()
