"""Storefront availability scan and run-phase reporting."""

from __future__ import annotations

import json
import sqlite3

from scrapy.http import HtmlResponse, Request

from lgd_scraper.run_progress import (
    CRAWL_PHASES,
    ENRICH_PHASES,
    SERVICE_PHASES,
    PhaseTracker,
    merge_timeline,
    phases_for_mode,
)
from lgd_scraper.spiders.catalog import WooCommerceCatalogSpider


BASE = "https://example.test/"


def saved_product(product_id: str, **overrides) -> dict:
    return {
        "_record_type": "product",
        "id": product_id,
        "name": f"Ring {product_id}",
        "sku": f"SKU-{product_id}",
        "url": f"{BASE}product/ring-{product_id}/",
        "type": "simple",
        "media": [],
        "variations": [],
        **overrides,
    }


def seed_catalog(tmp_path, products: list[dict]):
    database = tmp_path / "catalog.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE products (id TEXT PRIMARY KEY, url TEXT, raw_json TEXT)"
    )
    connection.executemany(
        "INSERT INTO products VALUES (?, ?, ?)",
        [(item["id"], item["url"], json.dumps(item)) for item in products],
    )
    connection.commit()
    connection.close()
    return database


def index_response(request, product_ids, total_pages: int = 1, status: int = 200):
    return HtmlResponse(
        url=request.url,
        body=json.dumps([{"id": pid, "sku": f"SKU-{pid}"} for pid in product_ids]).encode(),
        encoding="utf-8",
        status=status,
        headers={"X-WP-TotalPages": str(total_pages)},
        request=request,
    )


def enrich_spider(tmp_path):
    return WooCommerceCatalogSpider(
        base_url=BASE,
        output_dir=str(tmp_path),
        enrichment_mode=True,
        resume_existing=True,
        enrich_html=True,
    )


def split(results):
    """Separate scheduled requests from emitted items.

    ``results`` is a generator, so it is materialised once before splitting.
    """

    produced = list(results)
    requests = [item for item in produced if hasattr(item, "url")]
    items = [item for item in produced if isinstance(item, dict)]
    return requests, items


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------


def test_enrichment_requests_only_products_the_storefront_still_publishes(tmp_path):
    seed_catalog(tmp_path, [saved_product(str(i)) for i in range(1, 6)])
    spider = enrich_spider(tmp_path)

    scan = list(spider.start_requests())
    assert len(scan) == 1, "enrichment must start with one storefront index request"

    # 2 and 4 have been switched off in the storefront.
    requests, items = split(
        spider.parse_storefront_index(index_response(scan[0], [1, 3, 5]))
    )

    assert [request.url for request in requests] == [
        f"{BASE}product/ring-1/",
        f"{BASE}product/ring-3/",
        f"{BASE}product/ring-5/",
    ]
    assert spider.products_scheduled == 3
    assert spider.products_skipped_offline == 2

    offline = {item["id"]: item for item in items if item.get("_record_type") == "product"}
    assert set(offline) == {"2", "4"}
    assert all(item["storefront_status"] == "offline" for item in offline.values())
    assert all(item["storefront_checked_at"] for item in offline.values())


def test_offline_products_are_not_rewritten_when_the_flag_is_unchanged(tmp_path):
    """A second run must not re-save rows whose availability did not change."""

    seed_catalog(
        tmp_path,
        [
            saved_product("1"),
            saved_product("2", storefront_status="offline"),
        ],
    )
    spider = enrich_spider(tmp_path)
    scan = list(spider.start_requests())
    _, items = split(spider.parse_storefront_index(index_response(scan[0], [1])))

    assert [item for item in items if item.get("_record_type") == "product"] == []
    assert spider.products_skipped_offline == 1
    assert spider.storefront_summary()["newly_offline"] == 0


def test_a_product_switched_back_on_is_enriched_again(tmp_path):
    seed_catalog(tmp_path, [saved_product("2", storefront_status="offline")])
    spider = enrich_spider(tmp_path)
    scan = list(spider.start_requests())
    requests, _ = split(spider.parse_storefront_index(index_response(scan[0], [2])))

    assert [request.url for request in requests] == [f"{BASE}product/ring-2/"]
    assert spider.products_skipped_offline == 0


def test_already_current_product_still_has_its_availability_refreshed(tmp_path):
    """A product that needs no enrichment must still get an up-to-date flag."""

    seed_catalog(
        tmp_path,
        [saved_product("1", enrichment_schema_version=WooCommerceCatalogSpider.ENRICHMENT_SCHEMA_VERSION)],
    )
    spider = enrich_spider(tmp_path)
    scan = list(spider.start_requests())
    requests, items = split(spider.parse_storefront_index(index_response(scan[0], [1])))

    assert requests == []
    assert spider.products_already_enriched == 1
    products = [item for item in items if item.get("_record_type") == "product"]
    assert len(products) == 1
    assert products[0]["storefront_status"] == "live"


def test_the_scan_pages_through_a_large_index(tmp_path):
    seed_catalog(tmp_path, [saved_product(str(i)) for i in range(1, 4)])
    spider = enrich_spider(tmp_path)
    scan = list(spider.start_requests())

    first = list(spider.parse_storefront_index(index_response(scan[0], [1], total_pages=2)))
    # Page one only asks for page two; nothing is scheduled yet.
    assert len(first) == 1 and hasattr(first[0], "url")
    assert "page=2" in first[0].url
    assert spider.products_scheduled == 0

    requests, _ = split(
        spider.parse_storefront_index(index_response(first[0], [2], total_pages=2))
    )
    assert spider.live_product_ids == {"1", "2"}
    assert [request.url for request in requests] == [
        f"{BASE}product/ring-1/",
        f"{BASE}product/ring-2/",
    ]
    assert spider.products_skipped_offline == 1


# ---------------------------------------------------------------------------
# Safety: an unusable scan must never mark real products offline
# ---------------------------------------------------------------------------


def test_a_blocked_index_falls_back_to_enriching_everything(tmp_path):
    seed_catalog(tmp_path, [saved_product(str(i)) for i in range(1, 4)])
    spider = enrich_spider(tmp_path)
    scan = list(spider.start_requests())

    requests, items = split(
        spider.parse_storefront_index(index_response(scan[0], [], status=403))
    )

    assert spider.live_product_ids is None
    assert len(requests) == 3, "every saved product must still be tried"
    assert spider.products_skipped_offline == 0
    assert not [
        item
        for item in items
        if item.get("_record_type") == "product"
        and item.get("storefront_status") == "offline"
    ]
    assert any(
        item.get("kind") == "storefront_index_unavailable"
        for item in items
        if item.get("_record_type") == "diagnostic"
    )


def test_an_unreachable_index_still_schedules_enrichment(tmp_path):
    """A transport failure used to end the run at the scan, refreshing nothing."""

    seed_catalog(tmp_path, [saved_product(str(i)) for i in range(1, 4)])
    spider = enrich_spider(tmp_path)
    scan = list(spider.start_requests())

    failure = type(
        "Failure",
        (),
        {"request": scan[0], "value": ConnectionRefusedError("refused")},
    )()
    requests, items = split(spider.errback_storefront_index(failure))

    assert len(requests) == 3
    assert spider.live_product_ids is None
    assert spider.products_skipped_offline == 0
    assert any(
        item.get("kind") == "storefront_index_failed"
        for item in items
        if item.get("_record_type") == "diagnostic"
    )
    assert spider.storefront_summary()["scanned"] is False


def test_a_half_finished_scan_is_discarded(tmp_path):
    """Ids the scan never reached must not look switched off."""

    seed_catalog(tmp_path, [saved_product(str(i)) for i in range(1, 4)])
    spider = enrich_spider(tmp_path)
    scan = list(spider.start_requests())
    page_two = list(
        spider.parse_storefront_index(index_response(scan[0], [1], total_pages=2))
    )[0]

    failure = type(
        "Failure", (), {"request": page_two, "value": ConnectionRefusedError("refused")}
    )()
    requests, _ = split(spider.errback_storefront_index(failure))

    assert spider.live_product_ids is None
    assert len(requests) == 3


# ---------------------------------------------------------------------------
# Normal crawls mark what they see as live
# ---------------------------------------------------------------------------


def test_products_from_the_catalog_api_are_marked_live():
    spider = WooCommerceCatalogSpider(base_url=BASE, enrich_html=False)
    request = Request(url=f"{BASE}wp-json/wc/store/v1/products?page=1")
    request.meta["api_kind"] = "store"
    request.meta["page"] = 1
    response = HtmlResponse(
        url=request.url,
        body=json.dumps([{"id": 9, "name": "Ring", "permalink": f"{BASE}p/9/"}]).encode(),
        encoding="utf-8",
        request=request,
    )

    products = [
        item
        for item in spider.parse_products_api(response)
        if isinstance(item, dict) and item.get("_record_type") == "product"
    ]

    assert products and products[0]["storefront_status"] == "live"


# ---------------------------------------------------------------------------
# Phase tracking
# ---------------------------------------------------------------------------


def test_phase_tracker_records_an_ordered_run(tmp_path):
    path = tmp_path / "progress.json"
    tracker = PhaseTracker(path, ENRICH_PHASES, run_id="r1", mode="enrich")

    tracker.start("storefront_scan", "listing", total=3)
    tracker.update("storefront_scan", processed=2)
    snapshot = json.loads(path.read_text())
    assert snapshot["phase"] == "storefront_scan"
    assert snapshot["phase_index"] == 1
    assert snapshot["phase_total"] == len(ENRICH_PHASES)

    tracker.finish("storefront_scan", "9 live")
    tracker.start("enrich")
    snapshot = json.loads(path.read_text())
    states = {phase["key"]: phase["state"] for phase in snapshot["phases"]}
    assert states["storefront_scan"] == "done"
    assert states["enrich"] == "active"
    assert states["upload"] == "pending"


def test_starting_a_later_phase_closes_an_open_earlier_one(tmp_path):
    tracker = PhaseTracker(tmp_path / "p.json", ENRICH_PHASES)
    tracker.start("storefront_scan")
    tracker.start("export")

    states = {phase["key"]: phase["state"] for phase in tracker.snapshot()["phases"]}
    assert states["storefront_scan"] == "done"
    assert states["export"] == "active"


def test_extra_fields_survive_the_write_throttle(tmp_path):
    """Final counts are written even when they land inside the throttle window."""

    path = tmp_path / "p.json"
    tracker = PhaseTracker(path, ENRICH_PHASES, min_write_interval=600)
    tracker.start("enrich")
    tracker.set_extra(records_seen={"product": 42})

    assert json.loads(path.read_text()).get("records_seen") is None
    tracker.flush()
    assert json.loads(path.read_text())["records_seen"] == {"product": 42}


def test_merge_timeline_splices_crawl_phases_into_the_service_run(tmp_path):
    service = PhaseTracker(tmp_path / "s.json", SERVICE_PHASES)
    service.start("crawl")
    crawl = PhaseTracker(tmp_path / "c.json", CRAWL_PHASES)
    crawl.start("products")

    merged = merge_timeline(service.snapshot(), crawl.snapshot())
    keys = [phase["key"] for phase in merged]

    assert "crawl" not in keys, "the placeholder is replaced by the real phases"
    assert keys == ["prepare", "restore", "launch", *[k for k, _ in CRAWL_PHASES], "archive", "replica"]


def test_merge_timeline_keeps_the_placeholder_until_the_crawl_reports(tmp_path):
    service = PhaseTracker(tmp_path / "s.json", SERVICE_PHASES)
    service.start("launch")

    merged = merge_timeline(service.snapshot(), None)

    assert [phase["key"] for phase in merged] == [key for key, _ in SERVICE_PHASES]


def test_phases_for_mode():
    assert phases_for_mode("enrich") == ENRICH_PHASES
    assert phases_for_mode("full") == CRAWL_PHASES
    assert phases_for_mode("test") == CRAWL_PHASES


# ---------------------------------------------------------------------------
# The dashboard's view of it all
# ---------------------------------------------------------------------------


def api_catalog(tmp_path, statuses: dict[str, str | None]):
    """Seed a catalog whose products carry the given storefront statuses."""

    database = tmp_path / "catalog.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE products (id TEXT PRIMARY KEY, name TEXT, sku TEXT, "
        "product_type TEXT, price TEXT, stock_status TEXT, source TEXT, "
        "raw_json TEXT NOT NULL)"
    )
    for product_id, status in statuses.items():
        payload = {"id": product_id, "name": f"Ring {product_id}"}
        if status is not None:
            payload["storefront_status"] = status
        connection.execute(
            "INSERT INTO products VALUES (?, ?, ?, 'simple', '10', 'instock', 'api', ?)",
            (product_id, f"Ring {product_id}", f"SKU-{product_id}", json.dumps(payload)),
        )
    connection.commit()
    connection.close()
    return database


def test_catalog_summary_counts_storefront_availability(tmp_path):
    from lgd_scraper.admin_data import catalog_summary

    database = api_catalog(
        tmp_path, {"1": "live", "2": "live", "3": "offline", "4": None}
    )

    assert catalog_summary(database)["storefront"] == {
        "live": 2,
        "offline": 1,
        "unknown": 1,
    }


def test_list_products_filters_by_storefront_availability(tmp_path):
    from lgd_scraper.admin_data import list_products

    database = api_catalog(
        tmp_path, {"1": "live", "2": "live", "3": "offline", "4": None}
    )

    assert list_products(database, storefront="live")["total"] == 2
    assert list_products(database, storefront="offline")["total"] == 1
    assert list_products(database, storefront="unknown")["total"] == 1
    assert list_products(database, storefront="")["total"] == 4
    # An unknown value must not silently filter everything out.
    assert list_products(database, storefront="nonsense")["total"] == 4


def test_list_products_reports_each_product_status(tmp_path):
    from lgd_scraper.admin_data import list_products

    database = api_catalog(tmp_path, {"1": "offline", "2": None})
    by_id = {item["id"]: item for item in list_products(database)["items"]}

    assert by_id["1"]["storefront_status"] == "offline"
    assert by_id["2"]["storefront_status"] == "unknown"


def test_status_endpoint_exposes_a_merged_timeline(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import service

    monkeypatch.setenv("CONTROL_TOKEN", "t")
    monkeypatch.setattr(service, "DATABASE_PATH", api_catalog(tmp_path, {"1": "live"}))
    monkeypatch.setattr(service, "SERVICE_PROGRESS_PATH", tmp_path / "service.json")
    monkeypatch.setattr(service, "PROGRESS_PATH", tmp_path / "progress.json")

    progress = service._new_service_progress("run-1", "enrich")
    progress.start("crawl")
    crawl = PhaseTracker(
        tmp_path / "progress.json", ENRICH_PHASES, run_id="run-1", mode="enrich"
    )
    crawl.start("enrich", "Reading live product pages")
    crawl.set_extra(storefront={"scanned": True, "live_products": 9, "skipped_offline": 3})
    crawl.flush()

    with TestClient(service.app) as client:
        body = client.get("/api/status", headers={"Authorization": "Bearer t"}).json()

    keys = [phase["key"] for phase in body["timeline"]["phases"]]
    assert "crawl" not in keys
    assert "storefront_scan" in keys and "enrich" in keys
    assert body["timeline"]["current_label"] == "Reading live product pages"
    assert body["timeline"]["steps"] == len(keys)
    assert body["storefront"]["live_products"] == 9
    assert body["catalog"]["storefront"]["live"] == 1


def test_status_ignores_progress_left_over_from_another_run(tmp_path, monkeypatch):
    """A stale progress.json must not be shown as the current run's steps."""

    from fastapi.testclient import TestClient

    import service

    monkeypatch.setenv("CONTROL_TOKEN", "t")
    monkeypatch.setattr(service, "DATABASE_PATH", tmp_path / "missing.sqlite")
    monkeypatch.setattr(service, "SERVICE_PROGRESS_PATH", tmp_path / "service.json")
    monkeypatch.setattr(service, "PROGRESS_PATH", tmp_path / "progress.json")
    monkeypatch.setattr(service, "STATUS_PATH", tmp_path / "status.json")
    service._write_state({**service._default_state(), "run_id": "run-2"})

    progress = service._new_service_progress("run-2", "full")
    progress.start("crawl")
    stale = PhaseTracker(
        tmp_path / "progress.json", ENRICH_PHASES, run_id="run-1", mode="enrich"
    )
    stale.start("enrich", "Leftover from the previous run")
    stale.flush()

    with TestClient(service.app) as client:
        body = client.get("/api/status", headers={"Authorization": "Bearer t"}).json()

    assert [phase["key"] for phase in body["timeline"]["phases"]] == [
        key for key, _ in SERVICE_PHASES
    ]
