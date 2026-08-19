"""Regression tests for the issues found in the 2026-08 audit.

Each test fails if the corresponding fix is reverted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from scrapy.http import HtmlResponse, Request

import service
from lgd_scraper import mysql_backend, s3sync
from lgd_scraper.pipelines import CatalogWriterPipeline
from lgd_scraper.spiders.catalog import WooCommerceCatalogSpider


def response_for(url: str, body: str, status: int = 200) -> HtmlResponse:
    return HtmlResponse(
        url=url,
        body=body.encode("utf-8"),
        encoding="utf-8",
        status=status,
        request=Request(url=url),
    )


# ---------------------------------------------------------------------------
# Log tailing must not read the whole file
# ---------------------------------------------------------------------------


def test_tail_returns_last_lines_without_reading_whole_file(tmp_path):
    log = tmp_path / "crawl.log"
    log.write_text("\n".join(f"line {index}" for index in range(10_000)))

    tail = service._tail(log, 250).splitlines()

    assert len(tail) == 250
    assert tail[0] == "line 9750"
    assert tail[-1] == "line 9999"


def test_tail_handles_short_missing_and_empty_files(tmp_path):
    short = tmp_path / "short.log"
    short.write_text("first\nsecond\n")
    empty = tmp_path / "empty.log"
    empty.write_text("")

    assert service._tail(short, 250) == "first\nsecond"
    assert service._tail(empty, 250) == ""
    assert service._tail(tmp_path / "missing.log", 250) == ""


def test_tail_does_not_split_multibyte_characters(tmp_path):
    log = tmp_path / "crawl.log"
    log.write_text("\n".join(f"café {index} ✓" for index in range(5_000)))

    tail = service._tail(log, 10, block_size=64).splitlines()

    assert len(tail) == 10
    assert tail[-1] == "café 4999 ✓"


# ---------------------------------------------------------------------------
# Run listing returned the OLDEST runs
# ---------------------------------------------------------------------------


class PagingS3Client:
    """Mimics S3: listings truncate in ascending key order."""

    def __init__(self, keys: list[str], page_size: int = 100):
        self.keys = sorted(keys)
        self.page_size = page_size
        self.fetched: list[str] = []

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix, PaginationConfig=None):
        matching = [key for key in self.keys if key.startswith(Prefix)]
        for index in range(0, len(matching), self.page_size):
            yield {
                "Contents": [
                    {"Key": key} for key in matching[index : index + self.page_size]
                ]
            }

    def get_object(self, Bucket, Key):
        self.fetched.append(Key)
        run_id = Key.rsplit("/", 1)[-1].removesuffix(".json")
        body = json.dumps({"run_id": run_id, "state": "completed"}).encode("utf-8")

        class Body:
            @staticmethod
            def read():
                return body

        return {"Body": Body}


def test_list_admin_runs_returns_the_newest_runs(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "catalog-bucket")
    monkeypatch.setenv("S3_PREFIX", "jewelry")
    # 300 runs: far more than the requested page, so truncation order matters.
    keys = [
        f"jewelry/admin/runs/2026{month:02d}{day:02d}T120000Z.json"
        for month in range(1, 13)
        for day in range(1, 26)
    ]
    client = PagingS3Client(keys)
    monkeypatch.setattr(s3sync, "s3_client", lambda: client)

    records = s3sync.list_admin_runs(25)

    assert len(records) == 25
    assert records[0]["run_id"] == "20261225T120000Z"
    assert records[-1]["run_id"] == "20261201T120000Z"
    # Only the returned page is fetched, not every historical record.
    assert len(client.fetched) == 25


# ---------------------------------------------------------------------------
# MySQL connections must never be shared between threads
# ---------------------------------------------------------------------------


class FakeMySQLConnection:
    def __init__(self):
        self.closed = False

    def ping(self, reconnect=True):
        if self.closed:
            raise RuntimeError("connection closed")

    def close(self):
        self.closed = True


def test_mysql_read_connect_is_per_thread(monkeypatch):
    monkeypatch.setattr(
        mysql_backend, "mysql_connect", lambda **_: FakeMySQLConnection()
    )
    monkeypatch.setattr(mysql_backend, "_read_state", threading.local())

    connections: dict[str, object] = {}

    def capture(name):
        connections[name] = mysql_backend.mysql_read_connect()

    first = threading.Thread(target=capture, args=("a",))
    second = threading.Thread(target=capture, args=("b",))
    first.start()
    second.start()
    first.join()
    second.join()

    assert connections["a"] is not connections["b"]


def test_mysql_read_connect_reuses_the_connection_within_one_thread(monkeypatch):
    monkeypatch.setattr(
        mysql_backend, "mysql_connect", lambda **_: FakeMySQLConnection()
    )
    monkeypatch.setattr(mysql_backend, "_read_state", threading.local())

    assert mysql_backend.mysql_read_connect() is mysql_backend.mysql_read_connect()


# ---------------------------------------------------------------------------
# Admin reads must bypass the stale MySQL replica while a crawl is running
# ---------------------------------------------------------------------------


def _seed_catalog(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE products (id TEXT PRIMARY KEY, name TEXT, sku TEXT, "
        "product_type TEXT, price TEXT, stock_status TEXT, source TEXT, "
        "raw_json TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO products VALUES ('7', 'Live Ring', 'SKU7', 'simple', "
        "'10', 'instock', 'api', ?)",
        (json.dumps({"id": "7", "name": "Live Ring"}),),
    )
    connection.commit()
    connection.close()


def test_prefer_sqlite_skips_mysql(tmp_path, monkeypatch):
    from lgd_scraper import admin_data

    database = tmp_path / "catalog.sqlite"
    _seed_catalog(database)

    def explode():
        raise AssertionError("MySQL must not be consulted while a crawl is running")

    monkeypatch.setattr(admin_data, "mysql_enabled", lambda: True)
    monkeypatch.setattr(admin_data, "mysql_read_connect", explode)

    assert admin_data.catalog_summary(database, prefer_sqlite=True)["products"] == 1
    assert admin_data.list_products(database, prefer_sqlite=True)["total"] == 1
    assert (
        admin_data.product_detail(database, "7", prefer_sqlite=True)["name"]
        == "Live Ring"
    )


# ---------------------------------------------------------------------------
# Deleting a product must also remove it from the MySQL replica
# ---------------------------------------------------------------------------


class RecordingMySQLCursor:
    def __init__(self, statements):
        self.statements = statements
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), list(params)))

    def close(self):
        pass


class RecordingMySQLConnection:
    def __init__(self):
        self.statements: list[tuple[str, list]] = []
        self.committed = False

    def cursor(self):
        return RecordingMySQLCursor(self.statements)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def test_delete_products_mysql_clears_every_relationship(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "db.test")
    connection = RecordingMySQLConnection()
    monkeypatch.setattr(mysql_backend, "mysql_connect", lambda **_: connection)

    mysql_backend.delete_products_mysql(["7", "9", "7", " "])

    tables = [sql.split()[2] for sql, _ in connection.statements]
    assert tables == [
        "product_categories",
        "product_attributes",
        "variations",
        "images",
        "products",
    ]
    for _, params in connection.statements:
        assert params == ["7", "9"]
    assert connection.committed


def test_delete_products_mysql_is_a_noop_without_mysql(monkeypatch):
    monkeypatch.delenv("MYSQL_HOST", raising=False)

    def explode(**_):
        raise AssertionError("must not connect when MySQL is unconfigured")

    monkeypatch.setattr(mysql_backend, "mysql_connect", explode)

    assert mysql_backend.delete_products_mysql(["7"]) == 0


# ---------------------------------------------------------------------------
# A failed export must not cost the run its S3 checkpoint
# ---------------------------------------------------------------------------


def test_close_spider_uploads_even_when_the_export_fails(tmp_path, monkeypatch):
    import lgd_scraper.pipelines as pipelines

    uploaded: list[Path] = []
    monkeypatch.setattr(
        pipelines, "upload_final_artifacts", lambda directory: uploaded.append(directory)
    )

    def failing_export(*args, **kwargs):
        raise ValueError("WooCommerce preflight failed")

    monkeypatch.setattr(pipelines, "export_woocommerce_csvs", failing_export)

    pipeline = CatalogWriterPipeline()
    spider = type("Spider", (), {"output_dir": str(tmp_path)})()
    pipeline.open_spider(spider)
    pipeline.process_item(
        {"_record_type": "product", "id": "7", "name": "Ring"}, spider
    )
    pipeline.close_spider(spider)

    assert uploaded == [tmp_path.resolve()]
    summary = json.loads((tmp_path / "crawl-summary.json").read_text())
    assert "WooCommerce preflight failed" in summary["export_error"]

    # The failure is recorded in the database, and the database is closed.
    connection = sqlite3.connect(tmp_path / "catalog.sqlite")
    kinds = [
        row[0] for row in connection.execute("SELECT kind FROM diagnostics").fetchall()
    ]
    connection.close()
    assert "export_failed" in kinds


def test_close_spider_still_uploads_on_a_clean_run(tmp_path, monkeypatch):
    import lgd_scraper.pipelines as pipelines

    uploaded: list[Path] = []
    monkeypatch.setattr(
        pipelines, "upload_final_artifacts", lambda directory: uploaded.append(directory)
    )

    pipeline = CatalogWriterPipeline()
    spider = type("Spider", (), {"output_dir": str(tmp_path)})()
    pipeline.open_spider(spider)
    pipeline.process_item(
        {
            "_record_type": "product",
            "id": "7",
            "name": "Ring",
            "sku": "SKU-7",
            "type": "simple",
        },
        spider,
    )
    pipeline.close_spider(spider)

    assert uploaded == [tmp_path.resolve()]
    summary = json.loads((tmp_path / "crawl-summary.json").read_text())
    assert summary["export_error"] is None
    assert summary["woocommerce_export"]["master_rows"] == 1


# ---------------------------------------------------------------------------
# Sitemap products must not be stored from an error or redirect page
# ---------------------------------------------------------------------------


def test_sitemap_404_does_not_store_a_nameless_product():
    spider = WooCommerceCatalogSpider(enrich_html=False)
    response = response_for(
        "https://example.test/product/gone/", "<html><body>Not found</body></html>", 404
    )

    results = list(spider.parse_product_page(response, api_product=None))

    assert all(item["_record_type"] == "diagnostic" for item in results)
    assert spider.products_emitted == 0


def test_sitemap_redirect_to_homepage_does_not_store_a_product():
    spider = WooCommerceCatalogSpider(enrich_html=False)
    response = response_for(
        "https://example.test/", "<html><body class='home'></body></html>", 200
    )

    results = list(spider.parse_product_page(response, api_product=None))

    assert all(item["_record_type"] == "diagnostic" for item in results)
    assert spider.products_emitted == 0


def test_api_product_survives_a_404_detail_page():
    """An API record is authoritative; only the HTML enrichment is optional."""

    spider = WooCommerceCatalogSpider(enrich_html=False)
    response = response_for(
        "https://example.test/diamond/D-1/", "<html><body>Gone</body></html>", 404
    )
    api_product = {
        "_record_type": "product",
        "id": "1",
        "name": "Round Cut 1.0 Carat E Color VS1 Clarity Lab Grown Diamond",
        "sku": "D-1",
        "url": "https://example.test/diamond/D-1/",
        "type": "simple",
    }

    products = [
        item
        for item in spider.parse_product_page(response, api_product=api_product)
        if item.get("_record_type") == "product"
    ]

    assert len(products) == 1
    assert products[0]["id"] == "1"


# ---------------------------------------------------------------------------
# Scraped markup must not be able to break a selector
# ---------------------------------------------------------------------------


def test_variation_select_label_lookup_survives_a_quoted_id():
    spider = WooCommerceCatalogSpider(enrich_html=False)
    body = """
    <html><body>
      <form class="variations_form">
        <label for="size'--x">Ring Size</label>
        <select id="size'--x" name="attribute_pa_size">
          <option value="6">6</option>
          <option value="7">7</option>
        </select>
      </form>
    </body></html>
    """
    product: dict = {"attributes": []}

    # Must not raise SelectorSyntaxError.
    spider._merge_html_attributes(product, response_for("https://example.test/p/", body))

    names = {attribute["name"] for attribute in product["attributes"]}
    assert names == {"Ring Size"}


# ---------------------------------------------------------------------------
# Long catalog work must not block the status poll
# ---------------------------------------------------------------------------


def test_master_export_does_not_hold_the_state_lock(monkeypatch, tmp_path):
    """/api/status must stay responsive while the master CSV is being built."""

    database = tmp_path / "catalog.sqlite"
    _seed_catalog(database)
    monkeypatch.setattr(service, "DATABASE_PATH", database)
    monkeypatch.setattr(service, "EXPORT_DIR", tmp_path)

    inside_export = threading.Event()
    release_export = threading.Event()

    def slow_export(connection, output_dir, public_base_url=None):
        inside_export.set()
        release_export.wait(timeout=5)
        (tmp_path / service.MASTER_FILENAME).write_text("Type,SKU\n", encoding="utf-8")
        return {"parent_rows": 0, "variation_rows": 0, "master_rows": 0}

    monkeypatch.setattr(service, "export_woocommerce_csvs", slow_export)

    exporter = threading.Thread(target=service.download_master, daemon=True)
    exporter.start()
    assert inside_export.wait(timeout=5)

    # The export is mid-flight. state_lock must be free.
    assert service.state_lock.acquire(timeout=2), "state_lock held across the export"
    service.state_lock.release()

    release_export.set()
    exporter.join(timeout=5)


def test_prune_old_jobdirs_keeps_the_newest_and_the_current(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)
    jobs = tmp_path / "jobs"
    names = [f"2026010{index}T120000Z" for index in range(1, 7)]
    for name in names:
        (jobs / name).mkdir(parents=True)
        (jobs / name / "requests.seen").write_text("x")

    service._prune_old_jobdirs(names[0], keep=3)

    remaining = sorted(path.name for path in jobs.iterdir())
    # Three newest, plus the current run even though it is the oldest.
    assert remaining == sorted(names[-3:] + [names[0]])


# ---------------------------------------------------------------------------
# Token comparison must not raise on a non-ASCII token
# ---------------------------------------------------------------------------


def test_control_token_check_handles_non_ascii(monkeypatch):
    monkeypatch.setenv("CONTROL_TOKEN", "sécret-tøken")

    service._require_control("Bearer sécret-tøken")

    with pytest.raises(Exception) as error:
        service._require_control("Bearer wrong")
    assert error.value.status_code == 401
