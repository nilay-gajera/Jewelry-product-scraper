from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any


MASTER_FILENAME = "woocommerce-master.csv"
LEGACY_WOOCOMMERCE_FILENAMES = (
    "woocommerce-products.csv",
    "woocommerce-parents.csv",
    "woocommerce-variations.csv",
)


BASE_COLUMNS = [
    "ID",
    "Type",
    "SKU",
    "Name",
    "Published",
    "Is featured?",
    "Visibility in catalog",
    "Short description",
    "Description",
    "Tax status",
    "Tax class",
    "In stock?",
    "Stock",
    "Backorders allowed?",
    "Sold individually?",
    "Weight (kg)",
    "Length (cm)",
    "Width (cm)",
    "Height (cm)",
    "Allow customer reviews?",
    "Purchase note",
    "Sale price",
    "Regular price",
    "Categories",
    "Tags",
    "Shipping class",
    "Images",
    "Download limit",
    "Download expiry days",
    "Parent",
    "Grouped products",
    "Upsells",
    "Cross-sells",
    "External URL",
    "Button text",
    "Position",
    "meta:_source_product_id",
    "meta:_source_variation_id",
    "meta:_source_url",
    "meta:_variation_gallery_urls",
    "meta:_s3_media_paths",
]


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _option_name(option: Any) -> str:
    if isinstance(option, dict):
        return str(
            option.get("name")
            or option.get("option")
            or option.get("value")
            or option.get("slug")
            or ""
        )
    return str(option or "")


def _product_sku(product: dict[str, Any]) -> str:
    return str(product.get("sku") or f"LGD-P-{product.get('id')}")


def _variation_sku(product: dict[str, Any], variation: dict[str, Any]) -> str:
    variation_id = variation.get("id") or _slug(
        json.dumps(variation.get("attributes", {}), sort_keys=True)
    )[:24]
    parent_sku = _product_sku(product)
    source_sku = str(variation.get("sku") or "")
    if source_sku and source_sku != parent_sku:
        return source_sku
    return f"{parent_sku}-V-{variation_id}"


def _published(status: Any) -> int:
    if status in (None, "", "publish", "published"):
        return 1
    if status == "private":
        return 0
    return -1


def _stock_flag(stock_status: Any) -> int:
    return 0 if str(stock_status or "").lower() in {"outofstock", "out-of-stock"} else 1


def _raw_api(product: dict[str, Any]) -> dict[str, Any]:
    raw = product.get("raw_api")
    return raw if isinstance(raw, dict) else {}


def _media_url(media: dict[str, Any], public_base_url: str | None) -> str:
    local_path = str(media.get("local_path") or "").lstrip("/")
    normalized_base = str(public_base_url or "").strip().lower()
    valid_public_base = bool(
        normalized_base.startswith(("https://", "http://"))
        and "your-cdn-domain" not in normalized_base
    )
    if local_path and valid_public_base:
        return f"{public_base_url.rstrip('/')}/{local_path}"
    return str(media.get("source_url") or "")


def _product_image_urls(
    product: dict[str, Any], public_base_url: str | None
) -> list[str]:
    priority = {"featured": 0, "gallery": 1, "json_ld": 2, "variation": 3}
    media = sorted(
        product.get("media") or [],
        key=lambda item: (
            priority.get(item.get("role"), 9),
            int(item.get("position") or 0),
        ),
    )
    seen: set[str] = set()
    result: list[str] = []
    for item in media:
        if item.get("role") in {"variation", "variation_gallery"}:
            continue
        url = _media_url(item, public_base_url)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _media_paths(
    product: dict[str, Any], variation_id: str | None = None
) -> list[str]:
    result: list[str] = []
    for media in product.get("media") or []:
        media_variation_id = str(media.get("variation_id") or "")
        if variation_id is None:
            if media.get("role") in {"variation", "variation_gallery"}:
                continue
        elif media_variation_id != str(variation_id):
            continue
        path = str(media.get("local_path") or "").lstrip("/")
        if path and path not in result:
            result.append(path)
    return result


def _variation_image_urls(
    product: dict[str, Any],
    variation: dict[str, Any],
    public_base_url: str | None,
) -> list[str]:
    variation_id = str(variation.get("id") or "")
    result: list[str] = []
    for media in product.get("media") or []:
        if media.get("role") not in {"variation", "variation_gallery"}:
            continue
        if str(media.get("variation_id") or "") != variation_id:
            continue
        url = _media_url(media, public_base_url)
        if url and url not in result:
            result.append(url)
    fallback = str(variation.get("image_url") or "")
    if not result and fallback:
        result.append(fallback)
    for image in variation.get("gallery") or []:
        url = _media_url(image, public_base_url)
        if url and url not in result:
            result.append(url)
    return result


def _category_paths(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        "SELECT id, name, parent_id FROM categories"
    ).fetchall()
    categories = {
        str(category_id): {"name": name or "", "parent": str(parent_id or "")}
        for category_id, name, parent_id in rows
    }

    def path_for(category_id: str, seen: set[str] | None = None) -> str:
        seen = set(seen or ())
        if category_id in seen or category_id not in categories:
            return categories.get(category_id, {}).get("name", "")
        seen.add(category_id)
        category = categories[category_id]
        parent = category["parent"]
        if not parent or parent == "0" or parent not in categories:
            return category["name"]
        parent_path = path_for(parent, seen)
        return f"{parent_path} > {category['name']}" if parent_path else category["name"]

    return {category_id: path_for(category_id) for category_id in categories}


def _product_categories(
    connection: sqlite3.Connection,
    product_id: str,
    category_paths: dict[str, str],
) -> str:
    rows = connection.execute(
        """
        SELECT category_id, category_name
        FROM product_categories
        WHERE product_id = ?
        ORDER BY category_name
        """,
        (product_id,),
    ).fetchall()
    values: list[str] = []
    for category_id, category_name in rows:
        value = category_paths.get(str(category_id)) or category_name or ""
        if value and value not in values:
            values.append(value)
    return ", ".join(values)


def _raw_product_categories(
    product: dict[str, Any], category_paths: dict[str, str]
) -> str:
    values: list[str] = []
    for category in product.get("categories") or []:
        if not isinstance(category, dict):
            value = str(category or "").strip()
        else:
            category_id = str(category.get("id") or "")
            value = (
                category_paths.get(category_id)
                or str(category.get("name") or category.get("slug") or "").strip()
            )
        if value and value not in values:
            values.append(value)
    return ", ".join(values)


def _meta_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _diamond_details(product: dict[str, Any]) -> dict[str, Any]:
    details = product.get("diamond_details")
    return details if isinstance(details, dict) else {}


def _attribute_defs(product: dict[str, Any]) -> list[dict[str, Any]]:
    definitions: OrderedDict[str, dict[str, Any]] = OrderedDict()
    variations = product.get("variations") or []

    for index, attribute in enumerate(product.get("attributes") or []):
        name = str(attribute.get("name") or attribute.get("taxonomy") or f"Attribute {index + 1}")
        taxonomy = str(attribute.get("taxonomy") or attribute.get("slug") or "")
        key = _slug(taxonomy.replace("pa_", "") or name)
        values = [
            value
            for value in (_option_name(option) for option in _as_list(
                attribute.get("options") or attribute.get("terms")
            ))
            if value
        ]
        raw_id = attribute.get("id")
        numeric_global = str(raw_id or "").isdigit() and int(raw_id or 0) > 0
        definitions[key] = {
            "name": name,
            "key": key,
            "values": list(dict.fromkeys(values)),
            "visible": int(bool(attribute.get("visible", True))),
            "global": int(
                taxonomy.startswith("pa_")
                or bool(attribute.get("taxonomy"))
                or numeric_global
            ),
            "default": "",
        }

    for variation in variations:
        attributes = variation.get("attributes") or {}
        for raw_key, raw_value in attributes.items():
            key = _slug(str(raw_key).replace("attribute_", "").replace("pa_", ""))
            if not key:
                continue
            if key not in definitions:
                definitions[key] = {
                    "name": str(raw_key).replace("attribute_", "").replace("pa_", "").replace("-", " ").title(),
                    "key": key,
                    "values": [],
                    "visible": 1,
                    "global": int(str(raw_key).startswith("pa_")),
                    "default": "",
                }
            value = _option_name(raw_value)
            if value and value not in definitions[key]["values"]:
                definitions[key]["values"].append(value)

    for default in product.get("default_attributes") or []:
        if not isinstance(default, dict):
            continue
        key = _slug(
            str(default.get("name") or default.get("taxonomy") or "")
            .replace("attribute_", "")
            .replace("pa_", "")
        )
        if key in definitions:
            definitions[key]["default"] = _option_name(
                default.get("option") or default.get("value")
            )

    return list(definitions.values())


def _variation_attribute_value(
    variation: dict[str, Any], definition: dict[str, Any]
) -> str:
    attributes = variation.get("attributes") or {}
    target = definition["key"]
    for key, value in attributes.items():
        normalized = _slug(
            str(key).replace("attribute_", "").replace("pa_", "")
        )
        if normalized == target:
            return _option_name(value)
    return ""


def _base_product_row(
    connection: sqlite3.Connection,
    product: dict[str, Any],
    category_paths: dict[str, str],
    public_base_url: str | None,
) -> dict[str, Any]:
    api = _raw_api(product)
    product_id = str(product.get("id") or "")
    tags = product.get("tags") or []
    tag_names = [
        _option_name(tag)
        for tag in tags
        if _option_name(tag)
    ]
    dimensions = api.get("dimensions") or {}
    images = _product_image_urls(product, public_base_url)
    variations = product.get("variations") or []
    product_type = "variable" if variations else str(product.get("type") or "simple")

    categories = _product_categories(connection, product_id, category_paths)
    if not categories:
        categories = _raw_product_categories(product, category_paths)

    return {
        "ID": "",
        "Type": product_type,
        "SKU": _product_sku(product),
        "Name": product.get("name") or "",
        "Published": _published(product.get("status")),
        "Is featured?": int(bool(api.get("featured"))),
        "Visibility in catalog": product.get("catalog_visibility") or "visible",
        "Short description": api.get("short_description") or product.get("short_description_html") or product.get("short_description") or "",
        "Description": api.get("description") or product.get("description_html") or product.get("description") or "",
        "Tax status": api.get("tax_status") or "taxable",
        "Tax class": api.get("tax_class") or "",
        "In stock?": _stock_flag(product.get("stock_status")),
        "Stock": product.get("stock_quantity") if product.get("stock_quantity") is not None else "",
        "Backorders allowed?": int(str(api.get("backorders") or "no") != "no"),
        "Sold individually?": int(bool(api.get("sold_individually"))),
        "Weight (kg)": api.get("weight") or "",
        "Length (cm)": dimensions.get("length") or "",
        "Width (cm)": dimensions.get("width") or "",
        "Height (cm)": dimensions.get("height") or "",
        "Allow customer reviews?": int(api.get("reviews_allowed", True)),
        "Purchase note": api.get("purchase_note") or "",
        "Sale price": product.get("sale_price") or "",
        "Regular price": product.get("regular_price") or product.get("price") or "",
        "Categories": categories,
        "Tags": ", ".join(tag_names),
        "Shipping class": api.get("shipping_class") or "",
        "Images": ", ".join(images),
        "Download limit": api.get("download_limit") or "",
        "Download expiry days": api.get("download_expiry") or "",
        "Parent": "",
        "Grouped products": "",
        "Upsells": "",
        "Cross-sells": "",
        "External URL": api.get("external_url") or "",
        "Button text": api.get("button_text") or "",
        "Position": api.get("menu_order") or 0,
        "meta:_source_product_id": product_id,
        "meta:_source_variation_id": "",
        "meta:_source_url": product.get("url") or "",
        "meta:_variation_gallery_urls": "",
        "meta:_s3_media_paths": " | ".join(_media_paths(product)),
    }


def _variation_row(
    product: dict[str, Any],
    variation: dict[str, Any],
    definitions: list[dict[str, Any]],
    public_base_url: str | None,
) -> dict[str, Any]:
    dimensions = variation.get("dimensions") or {}
    row = {
        column: "" for column in BASE_COLUMNS
    }
    variation_id = str(variation.get("id") or "")
    variation_images = _variation_image_urls(product, variation, public_base_url)
    row.update(
        {
            "Type": "variation",
            "SKU": _variation_sku(product, variation),
            "Name": variation.get("name") or product.get("name") or "",
            "Published": 1 if variation.get("visible", True) is not False else 0,
            "Visibility in catalog": "visible",
            "Tax status": "taxable",
            "In stock?": _stock_flag(variation.get("stock_status")),
            "Stock": variation.get("stock_quantity")
            if variation.get("stock_quantity") is not None
            else "",
            "Weight (kg)": variation.get("weight") or "",
            "Length (cm)": dimensions.get("length") or "",
            "Width (cm)": dimensions.get("width") or "",
            "Height (cm)": dimensions.get("height") or "",
            "Sale price": variation.get("sale_price") or "",
            "Regular price": variation.get("regular_price")
            or variation.get("price")
            or "",
            "Images": ", ".join(variation_images),
            "Parent": _product_sku(product),
            "meta:_source_product_id": product.get("id") or "",
            "meta:_source_variation_id": variation.get("id") or "",
            "meta:_source_url": product.get("url") or "",
            "meta:_variation_gallery_urls": " | ".join(variation_images[1:]),
            "meta:_s3_media_paths": " | ".join(
                _media_paths(product, variation_id)
            ),
        }
    )
    for index, definition in enumerate(definitions, 1):
        row[f"Attribute {index} name"] = definition["name"]
        row[f"Attribute {index} value(s)"] = _variation_attribute_value(
            variation, definition
        )
        row[f"Attribute {index} default"] = ""
        row[f"Attribute {index} visible"] = definition["visible"]
        row[f"Attribute {index} global"] = definition["global"]
    return row


def export_woocommerce_csvs(
    connection: sqlite3.Connection,
    output_dir: Path,
    public_base_url: str | None = None,
) -> dict[str, int]:
    """Create one WooCommerce core-importer master CSV.

    The file contains simple products, variable parents, and variation rows in
    parent-first order.  Arbitrary diamond details are retained as importable
    ``meta:_diamond_*`` columns.
    """

    category_paths = _category_paths(connection)
    maximum_attributes = 0
    diamond_detail_keys: OrderedDict[str, str] = OrderedDict()
    for (raw_json,) in connection.execute(
        "SELECT raw_json FROM products ORDER BY id"
    ):
        product = json.loads(raw_json)
        maximum_attributes = max(maximum_attributes, len(_attribute_defs(product)))
        for name in _diamond_details(product):
            key = _slug(name).replace("-", "_")
            if key and key not in diamond_detail_keys:
                diamond_detail_keys[key] = str(name)
    attribute_columns: list[str] = []
    for index in range(1, maximum_attributes + 1):
        attribute_columns.extend(
            [
                f"Attribute {index} name",
                f"Attribute {index} value(s)",
                f"Attribute {index} default",
                f"Attribute {index} visible",
                f"Attribute {index} global",
            ]
        )
    diamond_columns = [
        f"meta:_diamond_{key}" for key in diamond_detail_keys
    ]
    columns = BASE_COLUMNS + attribute_columns + diamond_columns

    parent_count = 0
    variation_count = 0
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in LEGACY_WOOCOMMERCE_FILENAMES:
        (output_dir / filename).unlink(missing_ok=True)

    target = output_dir / MASTER_FILENAME
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=columns, extrasaction="ignore"
            )
            writer.writeheader()
            for (raw_json,) in connection.execute(
                "SELECT raw_json FROM products ORDER BY id"
            ):
                product = json.loads(raw_json)
                definitions = _attribute_defs(product)
                parent = _base_product_row(
                    connection, product, category_paths, public_base_url
                )
                for index, definition in enumerate(definitions, 1):
                    parent[f"Attribute {index} name"] = definition["name"]
                    parent[f"Attribute {index} value(s)"] = ", ".join(
                        definition["values"]
                    )
                    parent[f"Attribute {index} default"] = definition["default"]
                    parent[f"Attribute {index} visible"] = definition["visible"]
                    parent[f"Attribute {index} global"] = definition["global"]
                details = _diamond_details(product)
                normalized_details = {
                    _slug(key).replace("-", "_"): value
                    for key, value in details.items()
                }
                for key in diamond_detail_keys:
                    parent[f"meta:_diamond_{key}"] = _meta_value(
                        normalized_details.get(key)
                    )
                writer.writerow(parent)
                parent_count += 1

                for variation in product.get("variations") or []:
                    variation_row = _variation_row(
                        product, variation, definitions, public_base_url
                    )
                    writer.writerow(variation_row)
                    variation_count += 1
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "parent_rows": parent_count,
        "variation_rows": variation_count,
        "master_rows": parent_count + variation_count,
    }
