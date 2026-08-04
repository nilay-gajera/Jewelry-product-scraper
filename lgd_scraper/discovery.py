from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from lgd_scraper.middlewares import parse_sucuri_cookie


class CatalogDiscoveryError(RuntimeError):
    pass


def discover_catalog(
    base_url: str,
    *,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Read catalog totals and category counts without scraping product details."""

    owned_client = client is None
    api_client = client or httpx.Client(
        timeout=45,
        follow_redirects=False,
        headers={
            "Accept": "application/json",
            "User-Agent": "AuthorizedCatalogExporter/1.0 (+WooCommerce catalog migration)",
        },
    )
    try:
        authenticated = bool(consumer_key and consumer_secret)
        headers: dict[str, str] = {}
        if authenticated:
            raw = f"{consumer_key}:{consumer_secret}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
            root = "wp-json/wc/v3"
        else:
            root = "wp-json/wc/store/v1"

        product_response = _get(
            api_client,
            urljoin(base_url.rstrip("/") + "/", f"{root}/products"),
            params={"per_page": 1, "page": 1},
            headers=headers,
        )
        total_products = _total_header(product_response)
        categories = _collection(
            api_client,
            urljoin(base_url.rstrip("/") + "/", f"{root}/products/categories"),
            headers=headers,
        )
        normalized = _normalize_categories(categories)
        return {
            "base_url": base_url.rstrip("/") + "/",
            "discovered_at": datetime.now(UTC).isoformat(),
            "source": "rest" if authenticated else "store",
            "total_products": total_products,
            "total_categories": len(normalized),
            "categories": normalized,
        }
    finally:
        if owned_client:
            api_client.close()


def _get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    current_url = url
    current_params = params
    for _ in range(7):
        try:
            response = client.get(current_url, params=current_params, headers=headers)
        except httpx.HTTPError as exc:
            raise CatalogDiscoveryError(
                f"Catalog discovery could not reach WooCommerce: {exc}"
            ) from exc
        current_params = None
        if response.status_code == 307:
            challenge = parse_sucuri_cookie(response.text)
            if challenge:
                name, value = challenge
                client.cookies.set(name, value)
                continue
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if location:
                current_url = urljoin(str(response.url), location)
                continue
        if response.status_code != 200:
            sample = response.text[:240].replace("\n", " ").strip()
            raise CatalogDiscoveryError(
                f"Catalog discovery failed with HTTP {response.status_code}: {sample}"
            )
        return response
    raise CatalogDiscoveryError("Catalog discovery exceeded the redirect/challenge limit.")


def _collection(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = 1
    while True:
        response = _get(
            client,
            url,
            params={"per_page": 100, "page": page},
            headers=headers,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CatalogDiscoveryError(
                "WooCommerce returned invalid JSON for the category list."
            ) from exc
        if not isinstance(payload, list):
            raise CatalogDiscoveryError("WooCommerce returned an invalid category list.")
        records.extend(item for item in payload if isinstance(item, dict))
        total_pages = int(response.headers.get("X-WP-TotalPages", "1") or "1")
        if page >= total_pages:
            return records
        page += 1


def _total_header(response: httpx.Response) -> int:
    value = response.headers.get("X-WP-Total")
    if value is None:
        raise CatalogDiscoveryError("WooCommerce did not return the catalog total header.")
    try:
        return int(value)
    except ValueError as exc:
        raise CatalogDiscoveryError("WooCommerce returned an invalid catalog total.") from exc


def _normalize_categories(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(item.get("id")): item for item in categories if item.get("id")}

    def path_for(item: dict[str, Any]) -> str:
        names = [str(item.get("name") or item.get("slug") or item.get("id"))]
        seen = {str(item.get("id"))}
        parent = str(item.get("parent") or "")
        while parent and parent != "0" and parent not in seen and parent in by_id:
            seen.add(parent)
            parent_item = by_id[parent]
            names.append(
                str(parent_item.get("name") or parent_item.get("slug") or parent)
            )
            parent = str(parent_item.get("parent") or "")
        return " / ".join(reversed(names))

    values = [
        {
            "id": str(item.get("id")),
            "name": str(item.get("name") or item.get("slug") or item.get("id")),
            "slug": str(item.get("slug") or ""),
            "parent": str(item.get("parent") or "0"),
            "count": int(item.get("count") or 0),
            "path": path_for(item),
        }
        for item in categories
        if item.get("id")
    ]
    return sorted(values, key=lambda item: (item["path"].lower(), item["id"]))
