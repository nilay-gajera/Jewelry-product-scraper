from __future__ import annotations

import httpx

from lgd_scraper.discovery import discover_catalog


def test_discovery_counts_catalog_and_builds_category_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products/categories"):
            return httpx.Response(
                200,
                headers={"X-WP-TotalPages": "1"},
                json=[
                    {"id": 10, "name": "Rings", "slug": "rings", "parent": 0, "count": 120},
                    {"id": 11, "name": "Solitaire", "slug": "solitaire", "parent": 10, "count": 48},
                ],
            )
        if request.url.path.endswith("/products"):
            return httpx.Response(
                200,
                headers={"X-WP-Total": "347", "X-WP-TotalPages": "347"},
                json=[{"id": 1}],
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = discover_catalog("https://example.test/", client=client)
    client.close()

    assert result["total_products"] == 347
    assert result["total_categories"] == 2
    assert result["categories"][1]["path"] == "Rings / Solitaire"
    assert result["categories"][1]["count"] == 48
