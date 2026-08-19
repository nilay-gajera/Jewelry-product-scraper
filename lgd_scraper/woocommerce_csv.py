from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from lgd_scraper.catalog_rules import LOOSE_DIAMOND_CATEGORY


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
    "GTIN, UPC, EAN, or ISBN",
    "Name",
    "Published",
    "Is featured?",
    "Visibility in catalog",
    "Short description",
    "Description",
    "Date sale price starts",
    "Date sale price ends",
    "Tax status",
    "Tax class",
    "In stock?",
    "Stock",
    "Low stock amount",
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
    "Brands",
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
    "Cost of goods",
]


VALID_BASE_TYPES = {"simple", "variable", "grouped", "external", "variation"}
VALID_TYPE_MODIFIERS = {"virtual", "downloadable"}
BOOLEAN_COLUMNS = {
    "Is featured?",
    "In stock?",
    "Sold individually?",
    "Allow customer reviews?",
}
NUMERIC_COLUMNS = {
    "Stock",
    "Low stock amount",
    "Weight (kg)",
    "Length (cm)",
    "Width (cm)",
    "Height (cm)",
    "Sale price",
    "Regular price",
    "Cost of goods",
    "Download limit",
    "Download expiry days",
    "Position",
}


class WooCommerceExportValidationError(ValueError):
    """Raised before an invalid master file can replace the last good export."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        sample = "; ".join(errors[:10])
        remainder = len(errors) - 10
        if remainder > 0:
            sample = f"{sample}; and {remainder} more error(s)"
        super().__init__(f"WooCommerce CSV preflight failed: {sample}")


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


def _present(container: dict[str, Any], key: str) -> bool:
    return key in container and container[key] not in (None, "")


def _product_field(product: dict[str, Any], key: str) -> Any:
    """Read the normalized field, or its exact source API counterpart."""

    if _present(product, key):
        return product[key]
    api = _raw_api(product)
    return api.get(key) if _present(api, key) else ""


def _variation_field(variation: dict[str, Any], key: str) -> Any:
    """Read the normalized variation field, or its exact raw counterpart."""

    if _present(variation, key):
        return variation[key]
    raw = variation.get("raw")
    if isinstance(raw, dict) and _present(raw, key):
        return raw[key]
    return ""


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


def _escape_list_value(value: Any) -> str:
    return str(value or "").replace(",", r"\,")


def _split_list_values(value: Any) -> list[str]:
    return [
        part.replace(r"\,", ",").strip()
        for part in re.split(r"(?<!\\),", str(value or ""))
        if part.strip()
    ]


def _product_sku(product: dict[str, Any]) -> str:
    source_sku = str(product.get("sku") or "").strip()
    return source_sku or f"LGD-P-{product.get('id')}"


def _product_name(product: dict[str, Any]) -> str:
    """Return only a source-provided import name, including its exact slug."""

    source_name = str(product.get("name") or "").strip()
    if source_name:
        return source_name
    return str(product.get("slug") or "").strip()


def _variation_sku(
    product: dict[str, Any],
    variation: dict[str, Any],
    parent_sku: str | None = None,
) -> str:
    variation_id = variation.get("id") or _slug(
        json.dumps(variation.get("attributes", {}), sort_keys=True)
    )[:24]
    source_parent_sku = _product_sku(product)
    resolved_parent_sku = parent_sku or source_parent_sku
    source_sku = str(variation.get("sku") or "").strip()
    if source_sku and source_sku not in {
        source_parent_sku,
        resolved_parent_sku,
    }:
        return source_sku
    return f"{resolved_parent_sku}-V-{variation_id}"


def _reserve_unique_sku(
    requested_sku: str,
    used_skus: set[str],
    collision_suffix: str,
) -> str:
    """Reserve a stable WooCommerce-safe unique SKU without losing source data."""

    if requested_sku not in used_skus:
        used_skus.add(requested_sku)
        return requested_sku

    stem = f"{requested_sku}-{collision_suffix}"
    candidate = stem
    counter = 2
    while candidate in used_skus:
        candidate = f"{stem}-{counter}"
        counter += 1
    used_skus.add(candidate)
    return candidate


def _published(status: Any) -> int | str:
    if status in ("publish", "published"):
        return 1
    if status == "private":
        return 0
    if status == "pending":
        return 2
    if status:
        return -1
    # Store API catalog records are public products but do not expose the
    # authenticated REST API status field. WooCommerce interprets a blank CSV
    # value as draft, so preserve their public catalog state explicitly.
    return 1


def _stock_flag(stock_status: Any) -> int | str:
    normalized = str(stock_status or "").lower()
    if normalized in {"outofstock", "out-of-stock"}:
        return 0
    if normalized in {"instock", "in-stock", "onbackorder", "on-backorder"}:
        return 1
    return ""


def _catalog_visibility(value: Any) -> str:
    """Return WooCommerce's valid default when source visibility is absent."""

    normalized = str(value or "").strip().lower()
    return normalized or "visible"


def _bool_flag(value: Any) -> int | str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return int(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return 1
    if normalized in {"0", "false", "no", "off"}:
        return 0
    return ""


def _published_flag(value: Any) -> int:
    flag = _bool_flag(value)
    return flag if flag != "" else 1


def _backorders_value(value: Any) -> int | str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return int(value)
    normalized = str(value).strip().lower()
    if normalized in {"no", "0", "false"}:
        return 0
    if normalized in {"yes", "1", "true"}:
        return 1
    if normalized == "notify":
        return "notify"
    return ""


def _date_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else text


def _type_value(
    product: dict[str, Any], variation: dict[str, Any] | None = None
) -> str:
    if variation is not None:
        base_type = "variation"
        source = variation
        field_reader = _variation_field
    else:
        source = product
        field_reader = _product_field
        base_type = "variable" if product.get("variations") else str(
            field_reader(product, "type") or ""
        ).strip().lower()

    values = [base_type] if base_type else []
    downloads = _downloads(source, variation is not None)
    if _bool_flag(field_reader(source, "downloadable")) == 1 or downloads:
        values.append("downloadable")
    if _bool_flag(field_reader(source, "virtual")) == 1:
        values.append("virtual")
    return ", ".join(dict.fromkeys(values))


def _downloads(
    source: dict[str, Any], is_variation: bool = False
) -> list[dict[str, str]]:
    raw_downloads = (
        _variation_field(source, "downloads")
        if is_variation
        else _product_field(source, "downloads")
    )
    result: list[dict[str, str]] = []
    for index, item in enumerate(_as_list(raw_downloads), 1):
        if isinstance(item, dict):
            url = str(item.get("file") or item.get("url") or "").strip()
            name = str(item.get("name") or "").strip()
            download_id = str(item.get("id") or "").strip()
        else:
            url = str(item or "").strip()
            name = ""
            download_id = ""
        if url:
            result.append(
                {
                    "id": download_id,
                    "name": name or f"Download {index}",
                    "url": url,
                }
            )
    return result


def _relative_values(value: Any, product_skus: dict[str, str]) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for item in _as_list(value):
        candidate = ""
        if isinstance(item, dict):
            source_sku = str(item.get("sku") or "").strip()
            source_id = str(item.get("id") or "").strip()
            candidate = source_sku or product_skus.get(source_id, "")
        else:
            text = str(item or "").strip()
            if text.startswith("id:"):
                candidate = product_skus.get(text[3:].strip(), "")
            elif text.isdigit():
                candidate = product_skus.get(text, "")
            else:
                candidate = text
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(_escape_list_value(candidate))
    return ", ".join(result)


def _csv_safe(value: Any) -> Any:
    """Match WooCommerce's CSV formula-injection escaping convention."""

    if not isinstance(value, str) or not value:
        return value
    if value[0] not in "=+-@":
        return value
    try:
        Decimal(value)
    except InvalidOperation:
        return f"'{value}"
    return value


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
    return ""


def _media_identity(media: dict[str, Any], resolved_url: str = "") -> str:
    """Identify the underlying image even when source hosts/filenames differ."""

    source_url = str(media.get("source_url") or "").strip()
    if source_url:
        source_path = unquote(urlparse(source_url).path).rstrip("/").lower()
        if source_path:
            # The product page and JSON-LD commonly expose the same upload via
            # www and CDN hosts. CDN re-encoding changes the byte checksum, but
            # the host-independent WordPress upload path remains authoritative.
            return f"source_path:{source_path}"

    checksum = str(media.get("checksum") or "").strip().lower()
    if checksum:
        return f"checksum:{checksum}"
    url = str(resolved_url or media.get("source_url") or "").strip()
    if url:
        return f"url:{url}"
    local_path = str(media.get("local_path") or "").strip().lstrip("/")
    return f"path:{local_path}" if local_path else ""


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
        identity = _media_identity(item, url)
        if url and identity not in seen:
            seen.add(identity)
            result.append(url)
    return result


def _media_paths(
    product: dict[str, Any], variation_id: str | None = None
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for media in product.get("media") or []:
        media_variation_id = str(media.get("variation_id") or "")
        if variation_id is None:
            if media.get("role") in {"variation", "variation_gallery"}:
                continue
        elif media_variation_id != str(variation_id):
            continue
        path = str(media.get("local_path") or "").lstrip("/")
        identity = _media_identity(media)
        if path and identity not in seen:
            seen.add(identity)
            result.append(path)
    return result


def _variation_image_urls(
    product: dict[str, Any],
    variation: dict[str, Any],
    public_base_url: str | None,
) -> list[str]:
    variation_id = str(variation.get("id") or "")
    result: list[str] = []
    seen: set[str] = set()
    for media in product.get("media") or []:
        if media.get("role") not in {"variation", "variation_gallery"}:
            continue
        if str(media.get("variation_id") or "") != variation_id:
            continue
        url = _media_url(media, public_base_url)
        identity = _media_identity(media, url)
        if url and identity not in seen:
            seen.add(identity)
            result.append(url)
    for image in variation.get("gallery") or []:
        url = _media_url(image, public_base_url)
        identity = _media_identity(image, url)
        if url and identity not in seen:
            seen.add(identity)
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
    product: dict[str, Any] | None = None,
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
        # Product assignments are authoritative even when a synthetic/import
        # category has no separate row in the source category index.
        value = category_paths.get(str(category_id), "") or str(
            category_name or ""
        )
        if value and value not in values:
            values.append(value)
    if (
        product
        and product.get("product_family") == "loose_diamond"
        and not values
    ):
        # The Store API identifies these records precisely but publishes no
        # category assignment, causing WooCommerce to place every imported
        # diamond in Uncategorized. This is an explicit product-family mapping,
        # retained in row metadata below for auditability.
        values.append(LOOSE_DIAMOND_CATEGORY)
    return ", ".join(_escape_list_value(value) for value in values)


def _meta_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _metadata_entries(value: Any) -> list[tuple[str, Any]]:
    """Return arbitrary metadata entries without maintaining a fixed key list."""

    entries: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        entries.extend(
            (str(key).strip(), item)
            for key, item in value.items()
            if str(key).strip()
        )
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or item.get("name")
            if key not in (None, ""):
                entries.append((str(key).strip(), item.get("value")))
    return entries


def _source_media_urls(
    product: dict[str, Any], variation: dict[str, Any] | None = None
) -> list[str]:
    variation_id = str((variation or {}).get("id") or "")
    result: list[str] = []
    for media in product.get("media") or []:
        role = media.get("role")
        media_variation_id = str(media.get("variation_id") or "")
        if variation is None and role in {"variation", "variation_gallery"}:
            continue
        if variation is not None and media_variation_id != variation_id:
            continue
        url = str(media.get("source_url") or "")
        if url and url not in result:
            result.append(url)
    if variation is not None:
        for value in [variation.get("image_url"), *(variation.get("gallery") or [])]:
            url = (
                str(value.get("source_url") or value.get("src") or "")
                if isinstance(value, dict)
                else str(value or "")
            )
            if url and url not in result:
                result.append(url)
    return result


def _row_metadata(
    product: dict[str, Any], variation: dict[str, Any] | None = None
) -> OrderedDict[str, Any]:
    """Collect source and scraper metadata into dynamic WooCommerce columns."""

    metadata: OrderedDict[str, Any] = OrderedDict()

    containers: list[dict[str, Any]] = [product, _raw_api(product)]
    if variation is not None:
        containers = [variation]
        raw_variation = variation.get("raw")
        if isinstance(raw_variation, dict):
            containers.append(raw_variation)

    def retain(key: str, value: Any) -> None:
        normalized = key.removeprefix("meta:")
        if normalized not in metadata:
            metadata[normalized] = value
        elif metadata[normalized] != value:
            metadata[normalized] = [metadata[normalized], value]

    for container in containers:
        for field in ("meta_data", "metadata", "meta"):
            for key, value in _metadata_entries(container.get(field)):
                retain(key, value)

    if variation is None:
        details = product.get("diamond_details")
        if isinstance(details, dict):
            dynamic_fields = details.get("fields")
            if isinstance(dynamic_fields, dict):
                for field_key, field in dynamic_fields.items():
                    if not isinstance(field, dict):
                        continue
                    normalized = _slug(field_key).replace("-", "_")
                    if not normalized:
                        continue
                    retain(f"_diamond_{normalized}", field.get("value"))
                    if field.get("description"):
                        retain(
                            f"_diamond_{normalized}_description",
                            field.get("description"),
                        )
            for key, value in details.items():
                if key in {"fields", "raw_values"}:
                    continue
                normalized = _slug(key).replace("-", "_")
                if normalized:
                    metadata.setdefault(f"_diamond_{normalized}", value)
        if product.get("product_family"):
            retain("_source_product_family", product.get("product_family"))
        category_assignment = next(
            (
                category.get("assignment_source")
                for category in product.get("categories") or []
                if isinstance(category, dict) and category.get("assignment_source")
            ),
            None,
        )
        if product.get("product_family") == "loose_diamond" and (
            category_assignment or not (product.get("categories") or [])
        ):
            retain(
                "_category_assignment_source",
                category_assignment or "product_family:loose_diamond",
            )
        seo = product.get("seo")
        if isinstance(seo, dict):
            for key, value in seo.items():
                normalized = _slug(key).replace("-", "_")
                if normalized and value not in (None, "", [], {}):
                    retain(f"_seo_{normalized}", value)
    else:
        details = variation.get("details")
        if isinstance(details, dict):
            for field_key, field in details.items():
                if not isinstance(field, dict):
                    continue
                normalized = _slug(field_key).replace("-", "_")
                if normalized and field.get("value") not in (None, ""):
                    retain(f"_variation_detail_{normalized}", field.get("value"))

    system_metadata: OrderedDict[str, Any] = OrderedDict(
        [
            ("_source_product_id", product.get("id")),
            (
                "_source_variation_id",
                variation.get("id") if variation is not None else None,
            ),
            ("_source_url", product.get("url")),
            (
                "_source_image_urls",
                " | ".join(_source_media_urls(product, variation)),
            ),
            (
                "_s3_media_paths",
                " | ".join(
                    _media_paths(
                        product,
                        str(variation.get("id") or "")
                        if variation is not None
                        else None,
                    )
                ),
            ),
        ]
    )
    if variation is not None:
        source_urls = _source_media_urls(product, variation)
        system_metadata["_variation_gallery_source_urls"] = " | ".join(
            source_urls[1:]
        )
    else:
        system_metadata["_source_grouped_product_ids"] = _product_field(
            product, "grouped_products"
        )
        system_metadata["_source_upsell_ids"] = _product_field(
            product, "upsell_ids"
        )
        system_metadata["_source_cross_sell_ids"] = _product_field(
            product, "cross_sell_ids"
        )

    for key, value in system_metadata.items():
        if value not in (None, "", [], {}):
            metadata.setdefault(key, value)
    return metadata


def _attribute_defs(product: dict[str, Any]) -> list[dict[str, Any]]:
    definitions: OrderedDict[str, dict[str, Any]] = OrderedDict()
    variations = product.get("variations") or []
    real_variation_values: dict[str, list[str]] = {}
    seen_variation_keys: set[str] = set()
    for variation in variations:
        for raw_key, raw_value in (variation.get("attributes") or {}).items():
            key = _slug(str(raw_key).replace("attribute_", "").replace("pa_", ""))
            if not key:
                continue
            seen_variation_keys.add(key)
            value = _option_name(raw_value)
            # Some source rows contain placeholders like "Ring Size = Ring
            # Size" or "Back Setting = Back Setting". They describe no real
            # option and must not make WooCommerce mark the attribute as used
            # for variations.
            if value and _slug(value) != key:
                real_variation_values.setdefault(key, []).append(value)

    for index, attribute in enumerate(product.get("attributes") or []):
        name = str(attribute.get("name") or attribute.get("taxonomy") or f"Attribute {index + 1}")
        taxonomy = str(attribute.get("taxonomy") or attribute.get("slug") or "")
        key = _slug(taxonomy.replace("pa_", "") or name)
        terms = _as_list(attribute.get("terms"))
        options = _as_list(attribute.get("options"))
        source_values = terms if any(
            isinstance(term, dict) and _option_name(term) for term in terms
        ) else options or terms
        values = [
            value
            for value in (_option_name(option) for option in source_values)
            if value
        ]
        raw_id = attribute.get("id")
        numeric_global = str(raw_id or "").isdigit() and int(raw_id or 0) > 0
        variation_flag = _bool_flag(attribute.get("variation"))
        if variation_flag == "":
            variation_flag = _bool_flag(attribute.get("has_variations"))
        if variation_flag == "":
            variation_flag = 0
        if key in seen_variation_keys and not real_variation_values.get(key):
            variation_flag = 0
        definitions[key] = {
            "name": name,
            "key": key,
            "values": list(dict.fromkeys(values)),
            "visible": _bool_flag(attribute.get("visible")),
            "global": int(
                taxonomy.startswith("pa_")
                or bool(attribute.get("taxonomy"))
                or numeric_global
            ),
            "variation": variation_flag,
            "default": "",
        }
        for term in terms:
            if isinstance(term, dict) and _bool_flag(term.get("default")) == 1:
                definitions[key]["default"] = _option_name(term)
                break

    for variation in variations:
        attributes = variation.get("attributes") or {}
        for raw_key, raw_value in attributes.items():
            key = _slug(str(raw_key).replace("attribute_", "").replace("pa_", ""))
            if not key:
                continue
            value = _option_name(raw_value)
            if not value or _slug(value) == key:
                continue
            if key not in definitions:
                definitions[key] = {
                    "name": str(raw_key).replace("attribute_", "").replace("pa_", "").replace("-", " ").title(),
                    "key": key,
                    "values": [],
                    "visible": "",
                    "global": int(str(raw_key).startswith("pa_")),
                    "variation": 1,
                    "default": "",
                }
            if value and not any(
                _slug(existing) == _slug(value)
                for existing in definitions[key]["values"]
            ):
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
            source_default = _option_name(
                default.get("option") or default.get("value")
            )
            default_slug = _slug(source_default)
            definitions[key]["default"] = next(
                (
                    str(display_value)
                    for display_value in definitions[key]["values"]
                    if _slug(display_value) == default_slug
                ),
                source_default,
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
            source_value = _option_name(value)
            source_slug = _slug(source_value)
            for display_value in definition.get("values") or []:
                if _slug(display_value) == source_slug:
                    return str(display_value)
            return source_value
    return ""


def _base_product_row(
    connection: sqlite3.Connection,
    product: dict[str, Any],
    category_paths: dict[str, str],
    product_skus: dict[str, str],
    public_base_url: str | None,
) -> dict[str, Any]:
    product_id = str(product.get("id") or "")
    tags = product.get("tags") or []
    tag_names = [
        _option_name(tag)
        for tag in tags
        if _option_name(tag)
    ]
    brand_names = [
        _option_name(brand)
        for brand in _as_list(_product_field(product, "brands"))
        if _option_name(brand)
    ]
    dimensions = _product_field(product, "dimensions") or {}
    if not isinstance(dimensions, dict):
        dimensions = {}
    images = _product_image_urls(product, public_base_url)
    categories = _product_categories(
        connection, product_id, category_paths, product
    )
    product_type = _type_value(product)
    backorders = _product_field(product, "backorders")

    return {
        "ID": "",
        "Type": product_type,
        "SKU": _product_sku(product),
        "GTIN, UPC, EAN, or ISBN": _product_field(
            product, "global_unique_id"
        ),
        "Name": _product_name(product),
        "Published": _published(product.get("status")),
        "Is featured?": _bool_flag(_product_field(product, "featured")),
        "Visibility in catalog": _catalog_visibility(
            _product_field(product, "catalog_visibility")
        ),
        "Short description": _product_field(product, "short_description"),
        "Description": _product_field(product, "description"),
        "Date sale price starts": _date_value(
            _product_field(product, "date_on_sale_from")
        ),
        "Date sale price ends": _date_value(
            _product_field(product, "date_on_sale_to")
        ),
        "Tax status": _product_field(product, "tax_status"),
        "Tax class": _product_field(product, "tax_class"),
        "In stock?": _stock_flag(_product_field(product, "stock_status")),
        "Stock": _product_field(product, "stock_quantity"),
        "Backorders allowed?": _backorders_value(backorders),
        "Low stock amount": _product_field(product, "low_stock_amount"),
        "Sold individually?": _bool_flag(
            _product_field(product, "sold_individually")
        ),
        "Weight (kg)": _product_field(product, "weight"),
        "Length (cm)": dimensions.get("length") or "",
        "Width (cm)": dimensions.get("width") or "",
        "Height (cm)": dimensions.get("height") or "",
        "Allow customer reviews?": _bool_flag(
            _product_field(product, "reviews_allowed")
        ),
        "Purchase note": _product_field(product, "purchase_note"),
        "Sale price": _product_field(product, "sale_price"),
        "Regular price": _product_field(product, "regular_price"),
        "Categories": categories,
        "Tags": ", ".join(_escape_list_value(value) for value in tag_names),
        "Brands": ", ".join(
            _escape_list_value(value) for value in dict.fromkeys(brand_names)
        ),
        "Shipping class": _product_field(product, "shipping_class"),
        "Images": ", ".join(images),
        "Download limit": _product_field(product, "download_limit"),
        "Download expiry days": _product_field(product, "download_expiry"),
        "Parent": "",
        "Grouped products": _relative_values(
            _product_field(product, "grouped_products"), product_skus
        ),
        "Upsells": _relative_values(
            _product_field(product, "upsell_ids"), product_skus
        ),
        "Cross-sells": _relative_values(
            _product_field(product, "cross_sell_ids"), product_skus
        ),
        "External URL": _product_field(product, "external_url"),
        "Button text": _product_field(product, "button_text"),
        "Position": _product_field(product, "menu_order"),
        "Cost of goods": _product_field(product, "cogs_value"),
    }


def _variation_row(
    product: dict[str, Any],
    variation: dict[str, Any],
    definitions: list[dict[str, Any]],
    public_base_url: str | None,
) -> dict[str, Any]:
    dimensions = _variation_field(variation, "dimensions") or {}
    if not isinstance(dimensions, dict):
        dimensions = {}
    row = {column: "" for column in BASE_COLUMNS}
    variation_images = _variation_image_urls(product, variation, public_base_url)
    manage_stock = _variation_field(variation, "manage_stock")
    stock_quantity = _variation_field(variation, "stock_quantity")
    if str(manage_stock).strip().lower() == "parent":
        stock_quantity = "parent"
    row.update(
        {
            "Type": _type_value(product, variation),
            "SKU": _variation_sku(product, variation),
            "GTIN, UPC, EAN, or ISBN": _variation_field(
                variation, "global_unique_id"
            ),
            "Name": variation.get("name") or "",
            "Published": _published_flag(
                _variation_field(variation, "visible")
            ),
            "Visibility in catalog": _catalog_visibility(
                _variation_field(variation, "catalog_visibility")
            ),
            "Short description": _variation_field(
                variation, "short_description"
            ),
            "Description": _variation_field(variation, "description"),
            "Date sale price starts": _date_value(
                _variation_field(variation, "date_on_sale_from")
            ),
            "Date sale price ends": _date_value(
                _variation_field(variation, "date_on_sale_to")
            ),
            "Tax status": _variation_field(variation, "tax_status"),
            "Tax class": _variation_field(variation, "tax_class"),
            "In stock?": _stock_flag(
                _variation_field(variation, "stock_status")
            ),
            "Stock": stock_quantity,
            "Backorders allowed?": _backorders_value(
                _variation_field(variation, "backorders")
            ),
            "Low stock amount": _variation_field(
                variation, "low_stock_amount"
            ),
            "Sold individually?": _bool_flag(
                _variation_field(variation, "sold_individually")
            ),
            "Weight (kg)": _variation_field(variation, "weight"),
            "Length (cm)": dimensions.get("length") or "",
            "Width (cm)": dimensions.get("width") or "",
            "Height (cm)": dimensions.get("height") or "",
            "Sale price": _variation_field(variation, "sale_price"),
            "Regular price": _variation_field(variation, "regular_price"),
            "Download limit": _variation_field(variation, "download_limit"),
            "Download expiry days": _variation_field(
                variation, "download_expiry"
            ),
            "Images": ", ".join(variation_images),
            "Parent": _product_sku(product),
            "Position": _variation_field(variation, "menu_order"),
            "Cost of goods": _variation_field(variation, "cogs_value"),
        }
    )
    for index, definition in enumerate(definitions, 1):
        if not definition["variation"]:
            continue
        row[f"Attribute {index} name"] = definition["name"]
        row[f"Attribute {index} value(s)"] = _escape_list_value(
            _variation_attribute_value(variation, definition)
        )
        row[f"Attribute {index} default"] = ""
        row[f"Attribute {index} visible"] = definition["visible"]
        row[f"Attribute {index} global"] = definition["global"]
    return row


def _apply_downloads(
    row: dict[str, Any], downloads: list[dict[str, str]]
) -> None:
    for index, download in enumerate(downloads, 1):
        row[f"Download {index} ID"] = download["id"]
        row[f"Download {index} name"] = download["name"]
        row[f"Download {index} URL"] = download["url"]


def _valid_public_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_woocommerce_csv(path: Path) -> dict[str, int]:
    """Stream a strict WooCommerce-core preflight over an exported CSV."""

    errors: list[str] = []
    seen_skus: set[str] = set()
    seen_global_unique_ids: set[str] = set()
    parent_types: dict[str, str] = {}
    parent_attributes: dict[str, dict[str, set[str]]] = {}
    row_count = 0
    variation_count = 0

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [column for column in BASE_COLUMNS if column not in headers]
        if missing:
            errors.append(f"missing required header(s): {', '.join(missing)}")
        if len(headers) != len(set(headers)):
            errors.append("duplicate CSV headers")

        for line_number, row in enumerate(reader, 2):
            row_count += 1
            sku = str(row.get("SKU") or "").strip()
            name = str(row.get("Name") or "").strip()
            type_parts = [
                value.strip().lower()
                for value in str(row.get("Type") or "").split(",")
                if value.strip()
            ]
            bases = [value for value in type_parts if value in VALID_BASE_TYPES]
            invalid_types = [
                value
                for value in type_parts
                if value not in VALID_BASE_TYPES | VALID_TYPE_MODIFIERS
            ]
            if len(bases) != 1 or invalid_types:
                errors.append(
                    f"row {line_number}: invalid Type {row.get('Type')!r}"
                )
                base_type = ""
            else:
                base_type = bases[0]

            if not sku:
                errors.append(f"row {line_number}: SKU is empty")
            elif "," in sku:
                errors.append(
                    f"row {line_number}: SKU contains a comma and cannot be linked safely"
                )
            elif sku in seen_skus:
                errors.append(f"row {line_number}: duplicate SKU {sku!r}")
            else:
                seen_skus.add(sku)

            global_unique_id = str(
                row.get("GTIN, UPC, EAN, or ISBN") or ""
            ).strip()
            if global_unique_id:
                if not re.match(r"^[0-9-]+X?$", global_unique_id):
                    errors.append(
                        f"row {line_number}: invalid GTIN/UPC/EAN/ISBN {global_unique_id!r}"
                    )
                elif global_unique_id in seen_global_unique_ids:
                    errors.append(
                        f"row {line_number}: duplicate GTIN/UPC/EAN/ISBN {global_unique_id!r}"
                    )
                else:
                    seen_global_unique_ids.add(global_unique_id)

            if base_type != "variation" and not name:
                errors.append(f"row {line_number}: Name is empty")

            published = str(row.get("Published") or "").strip()
            if published not in {"-1", "0", "1", "2"}:
                errors.append(
                    f"row {line_number}: invalid Published value {published!r}"
                )
            for column in BOOLEAN_COLUMNS:
                value = str(row.get(column) or "").strip()
                if value not in {"", "0", "1"}:
                    errors.append(
                        f"row {line_number}: invalid {column} value {value!r}"
                    )
            backorders = str(row.get("Backorders allowed?") or "").strip()
            if backorders not in {"", "0", "1", "notify"}:
                errors.append(
                    f"row {line_number}: invalid backorders value {backorders!r}"
                )
            visibility = str(row.get("Visibility in catalog") or "").strip()
            if visibility not in {"visible", "catalog", "search", "hidden"}:
                errors.append(
                    f"row {line_number}: invalid catalog visibility {visibility!r}"
                )
            for column in NUMERIC_COLUMNS:
                value = str(row.get(column) or "").strip()
                if not value or (column == "Stock" and value == "parent"):
                    continue
                if column in {"Download limit", "Download expiry days"} and value == "n/a":
                    continue
                try:
                    Decimal(value.lstrip("'"))
                except InvalidOperation:
                    errors.append(
                        f"row {line_number}: {column} is not numeric: {value!r}"
                    )

            for column in ("Date sale price starts", "Date sale price ends"):
                value = str(row.get(column) or "").strip()
                if value and not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
                    errors.append(
                        f"row {line_number}: invalid {column} value {value!r}"
                    )

            for image_url in [
                value.strip()
                for value in str(row.get("Images") or "").split(",")
                if value.strip()
            ]:
                if not _valid_public_url(image_url):
                    errors.append(
                        f"row {line_number}: invalid image URL {image_url!r}"
                    )

            download_indexes = {
                match.group(1)
                for header in headers
                if (
                    match := re.match(
                        r"^Download (\d+) (?:ID|name|URL)$", header
                    )
                )
            }
            for index in download_indexes:
                download_name = str(row.get(f"Download {index} name") or "").strip()
                download_url = str(row.get(f"Download {index} URL") or "").strip()
                if bool(download_name) != bool(download_url):
                    errors.append(
                        f"row {line_number}: Download {index} name/URL pair is incomplete"
                    )
                if download_url and not (
                    _valid_public_url(download_url)
                    or download_url.startswith(("/", "["))
                ):
                    errors.append(
                        f"row {line_number}: invalid Download {index} URL {download_url!r}"
                    )

            attributes: dict[str, set[str]] = {}
            for header in headers:
                match = re.match(r"^Attribute (\d+) name$", header)
                if not match:
                    continue
                index = match.group(1)
                attribute_name = str(row.get(header) or "").strip()
                value_cell = str(
                    row.get(f"Attribute {index} value(s)") or ""
                ).strip()
                if value_cell and not attribute_name:
                    errors.append(
                        f"row {line_number}: Attribute {index} has values but no name"
                    )
                if attribute_name:
                    attributes[attribute_name.casefold()] = {
                        value.strip().casefold()
                        for value in _split_list_values(value_cell)
                        if value.strip()
                    }

            if base_type == "variation":
                variation_count += 1
                parent = str(row.get("Parent") or "").strip()
                if not parent:
                    errors.append(f"row {line_number}: variation Parent is empty")
                elif parent not in parent_types:
                    errors.append(
                        f"row {line_number}: variation Parent {parent!r} does not precede it"
                    )
                elif parent_types[parent] != "variable":
                    errors.append(
                        f"row {line_number}: Parent {parent!r} is not variable"
                    )
                else:
                    allowed = parent_attributes.get(parent, {})
                    for attribute_name, values in attributes.items():
                        if not values:
                            continue
                        if attribute_name not in allowed:
                            errors.append(
                                f"row {line_number}: attribute {attribute_name!r} is absent from parent"
                            )
                        elif not values.issubset(allowed[attribute_name]):
                            errors.append(
                                f"row {line_number}: variation value is absent from parent attribute {attribute_name!r}"
                            )
            elif sku:
                parent_types[sku] = base_type
                parent_attributes[sku] = attributes

            if base_type == "external" and not _valid_public_url(
                str(row.get("External URL") or "")
            ):
                errors.append(
                    f"row {line_number}: external product has no valid External URL"
                )

    if errors:
        raise WooCommerceExportValidationError(errors)
    return {
        "rows": row_count,
        "parent_rows": row_count - variation_count,
        "variation_rows": variation_count,
        "errors": 0,
    }


def export_woocommerce_csvs(
    connection: sqlite3.Connection,
    output_dir: Path,
    public_base_url: str | None = None,
) -> dict[str, int]:
    """Create one WooCommerce core-importer master CSV.

    The file contains simple products, variable parents, and variation rows in
    parent-first order. Every discovered product and variation metadata key is
    retained as a dynamic ``meta:*`` column.
    """

    category_paths = _category_paths(connection)
    maximum_attributes = 0
    maximum_downloads = 0
    product_skus: dict[str, str] = {}
    variation_skus: dict[tuple[str, int], str] = {}
    used_skus: set[str] = set()
    metadata_keys: OrderedDict[str, None] = OrderedDict()

    # WooCommerce requires one global SKU namespace across parents and
    # variations. Reserve every parent first so variation collisions can never
    # break Parent references or product-to-product relationships.
    for (raw_json,) in connection.execute(
        "SELECT raw_json FROM products ORDER BY id"
    ):
        product = json.loads(raw_json)
        product_id = str(product.get("id") or "")
        source_sku = _product_sku(product)
        product_skus[product_id] = _reserve_unique_sku(
            source_sku,
            used_skus,
            f"P-{product_id}",
        )

    for (raw_json,) in connection.execute(
        "SELECT raw_json FROM products ORDER BY id"
    ):
        product = json.loads(raw_json)
        product_id = str(product.get("id") or "")
        resolved_product_sku = product_skus[product_id]
        raw_product_sku = str(product.get("sku") or "").strip()
        if raw_product_sku and raw_product_sku != resolved_product_sku:
            metadata_keys.setdefault("_source_sku", None)
        maximum_attributes = max(maximum_attributes, len(_attribute_defs(product)))
        maximum_downloads = max(maximum_downloads, len(_downloads(product)))
        for key in _row_metadata(product):
            metadata_keys.setdefault(key, None)
        for variation_index, variation in enumerate(
            product.get("variations") or []
        ):
            requested_variation_sku = _variation_sku(
                product,
                variation,
                resolved_product_sku,
            )
            variation_id = variation.get("id") or variation_index + 1
            resolved_variation_sku = _reserve_unique_sku(
                requested_variation_sku,
                used_skus,
                f"V-{product_id}-{variation_id}",
            )
            variation_skus[(product_id, variation_index)] = (
                resolved_variation_sku
            )
            raw_variation_sku = str(variation.get("sku") or "").strip()
            if (
                raw_variation_sku
                and raw_variation_sku != resolved_variation_sku
            ):
                metadata_keys.setdefault("_source_sku", None)
            maximum_downloads = max(
                maximum_downloads, len(_downloads(variation, True))
            )
            for key in _row_metadata(product, variation):
                metadata_keys.setdefault(key, None)
    download_columns: list[str] = []
    for index in range(1, maximum_downloads + 1):
        download_columns.extend(
            [
                f"Download {index} ID",
                f"Download {index} name",
                f"Download {index} URL",
            ]
        )
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
    metadata_columns = [f"meta:{key}" for key in metadata_keys]
    columns = BASE_COLUMNS + download_columns + attribute_columns + metadata_columns

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
                handle,
                fieldnames=columns,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for (raw_json,) in connection.execute(
                "SELECT raw_json FROM products ORDER BY id"
            ):
                product = json.loads(raw_json)
                definitions = _attribute_defs(product)
                parent = _base_product_row(
                    connection,
                    product,
                    category_paths,
                    product_skus,
                    public_base_url,
                )
                product_id = str(product.get("id") or "")
                resolved_product_sku = product_skus[product_id]
                parent["SKU"] = resolved_product_sku
                _apply_downloads(parent, _downloads(product))
                for index, definition in enumerate(definitions, 1):
                    parent[f"Attribute {index} name"] = definition["name"]
                    parent[f"Attribute {index} value(s)"] = ", ".join(
                        _escape_list_value(value)
                        for value in definition["values"]
                    )
                    parent[f"Attribute {index} default"] = _escape_list_value(
                        definition["default"]
                    )
                    parent[f"Attribute {index} visible"] = definition["visible"]
                    parent[f"Attribute {index} global"] = definition["global"]
                parent_metadata = _row_metadata(product)
                raw_product_sku = str(product.get("sku") or "").strip()
                if raw_product_sku and raw_product_sku != resolved_product_sku:
                    parent_metadata.setdefault("_source_sku", raw_product_sku)
                for key, value in parent_metadata.items():
                    parent[f"meta:{key}"] = _meta_value(value)
                writer.writerow(
                    {key: _csv_safe(value) for key, value in parent.items()}
                )
                parent_count += 1

                for variation_index, variation in enumerate(
                    product.get("variations") or []
                ):
                    variation_row = _variation_row(
                        product, variation, definitions, public_base_url
                    )
                    resolved_variation_sku = variation_skus[
                        (product_id, variation_index)
                    ]
                    variation_row["SKU"] = resolved_variation_sku
                    variation_row["Parent"] = resolved_product_sku
                    _apply_downloads(
                        variation_row, _downloads(variation, True)
                    )
                    variation_metadata = _row_metadata(product, variation)
                    raw_variation_sku = str(
                        variation.get("sku") or ""
                    ).strip()
                    if (
                        raw_variation_sku
                        and raw_variation_sku != resolved_variation_sku
                    ):
                        variation_metadata.setdefault(
                            "_source_sku", raw_variation_sku
                        )
                    for key, value in variation_metadata.items():
                        variation_row[f"meta:{key}"] = _meta_value(value)
                    writer.writerow(
                        {
                            key: _csv_safe(value)
                            for key, value in variation_row.items()
                        }
                    )
                    variation_count += 1
        validation = validate_woocommerce_csv(temporary)
        expected_rows = parent_count + variation_count
        if validation["rows"] != expected_rows:
            raise WooCommerceExportValidationError(
                [
                    "row count mismatch: "
                    f"wrote {expected_rows}, validated {validation['rows']}"
                ]
            )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "parent_rows": parent_count,
        "variation_rows": variation_count,
        "master_rows": parent_count + variation_count,
    }
