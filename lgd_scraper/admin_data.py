from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lgd_scraper.s3sync import (
    load_json_object,
    presigned_media_url,
    save_json_object,
)


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
    "obey_robots": True,
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


def catalog_summary(database_path: Path) -> dict[str, Any]:
    connection = _connect(database_path)
    empty = {
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
    }
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
    sort: str = "name_asc",
    page: int = 1,
    page_size: int = 25,
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
        items: list[dict[str, Any]] = []
        for row in rows:
            product = _json(row["raw_json"], {})
            media = product.get("media") or []
            unique_media_count = len(
                {
                    item.get("source_url")
                    for item in media
                    if item.get("source_url")
                }
            )
            featured = next(
                (item for item in media if item.get("role") == "featured"),
                media[0] if media else None,
            )
            items.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "sku": row["sku"],
                    "type": row["product_type"],
                    "price": row["price"],
                    "currency": product.get("currency") or "",
                    "stock_status": row["stock_status"],
                    "source": row["source"],
                    "thumbnail": public_media_url(featured) if featured else "",
                    "variation_count": len(product.get("variations") or []),
                    "image_count": unique_media_count,
                    "category_count": len(product.get("categories") or []),
                    "categories": [
                        item.get("name") for item in product.get("categories") or []
                    ],
                    "updated": product.get("date_modified"),
                    "quality": quality_for(product),
                }
            )
        return {"items": items, "total": total, "page": page, "page_size": page_size}
    finally:
        connection.close()


def product_filter_options(database_path: Path) -> dict[str, Any]:
    """Return compact filter facets for the product administration page."""

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


def product_detail(database_path: Path, product_id: str) -> dict[str, Any] | None:
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


def diagnostics(database_path: Path, limit: int = 100) -> list[dict[str, Any]]:
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
