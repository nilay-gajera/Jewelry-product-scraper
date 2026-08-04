from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import service
from lgd_scraper import admin_data
from lgd_scraper.admin_data import catalog_summary, list_products, product_detail
from lgd_scraper.catalog_mutations import copy_database, delete_product, delete_products


def _catalog(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE products (
            id TEXT PRIMARY KEY, name TEXT, sku TEXT, product_type TEXT,
            price TEXT, stock_status TEXT, source TEXT, raw_json TEXT NOT NULL
        );
        CREATE TABLE variations (product_id TEXT, id TEXT);
        CREATE TABLE images (product_id TEXT, source_url TEXT);
        CREATE TABLE categories (id TEXT);
        CREATE TABLE attributes (id TEXT);
        CREATE TABLE diagnostics (
            created_at TEXT, kind TEXT, url TEXT, status INTEGER,
            message TEXT, raw_json TEXT NOT NULL
        );
        """
    )
    product = {
        "_record_type": "product",
        "id": "99",
        "name": "Adriana Ring",
        "sku": "LGD-99",
        "type": "variable",
        "price": "1200",
        "stock_status": "instock",
        "description": "Solitaire engagement ring.",
        "source": "store+html",
        "categories": [{"id": 11, "name": "Engagement Rings"}],
        "attributes": [{"id": 5, "name": "Metal", "options": ["Gold"]}],
        "variations": [
            {
                "id": 501,
                "name": "Adriana Ring — Gold",
                "attributes": {"metal": "Gold"},
                "image_url": "https://source.test/variation.jpg",
            }
        ],
        "media": [
            {
                "role": "featured",
                "source_url": "https://source.test/ring.jpg",
                "local_path": "products/99/ring.jpg",
            },
            {
                "role": "json_ld",
                "source_url": "https://source.test/ring.jpg",
                "local_path": "products/99/ring.jpg",
            },
        ],
    }
    connection.execute(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "99",
            product["name"],
            product["sku"],
            product["type"],
            product["price"],
            product["stock_status"],
            product["source"],
            json.dumps(product),
        ),
    )
    connection.execute("INSERT INTO variations VALUES ('99', '501')")
    connection.execute(
        "INSERT INTO images VALUES ('99', 'https://source.test/ring.jpg')"
    )
    connection.execute("INSERT INTO categories VALUES ('11')")
    connection.execute("INSERT INTO attributes VALUES ('5')")
    connection.commit()
    connection.close()


def _add_product(path, product_id="100", name="Bianca Ring"):
    product = {
        "_record_type": "product",
        "id": product_id,
        "name": name,
        "sku": f"LGD-{product_id}",
        "type": "simple",
        "price": "900",
        "stock_status": "instock",
        "source": "store",
        "categories": [],
        "attributes": [],
        "variations": [],
        "media": [
            {
                "role": "featured",
                "source_url": f"https://source.test/{product_id}.jpg",
                "local_path": f"products/{product_id}/featured.jpg",
            }
        ],
    }
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            product_id,
            name,
            product["sku"],
            product["type"],
            product["price"],
            product["stock_status"],
            product["source"],
            json.dumps(product),
        ),
    )
    connection.execute(
        "INSERT INTO images VALUES (?, ?)",
        (product_id, f"https://source.test/{product_id}.jpg"),
    )
    connection.commit()
    connection.close()


def test_admin_data_summary_list_and_detail(tmp_path, monkeypatch):
    database = tmp_path / "catalog.sqlite"
    _catalog(database)
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://cdn.test/media")

    summary = catalog_summary(database)
    products = list_products(database, query="Adriana")
    detail = product_detail(database, "99")

    assert summary["products"] == 1
    assert summary["variations"] == 1
    assert summary["quality"] == {
        "missing_images": 0,
        "missing_categories": 0,
        "missing_attributes": 0,
        "missing_variations": 0,
    }
    assert products["total"] == 1
    assert products["items"][0]["thumbnail"] == (
        "https://cdn.test/media/products/99/ring.jpg"
    )
    assert products["items"][0]["image_count"] == 1
    assert detail is not None
    assert detail["quality"]["complete"] is True
    assert detail["media"][0]["display_url"].startswith("https://cdn.test/")


def test_admin_images_use_private_s3_signed_url_when_cdn_is_placeholder(
    tmp_path, monkeypatch
):
    database = tmp_path / "catalog.sqlite"
    _catalog(database)
    monkeypatch.setenv("S3_BUCKET", "private-media")
    monkeypatch.setenv(
        "S3_PUBLIC_BASE_URL", "https://your-cdn-domain/jewelry-product-scraper/media"
    )
    monkeypatch.setattr(
        admin_data,
        "presigned_media_url",
        lambda path: f"https://signed.test/{path}?signature=temporary",
    )

    products = list_products(database)
    settings = admin_data.storage_settings()

    assert products["items"][0]["thumbnail"].startswith("https://signed.test/")
    assert settings["public_media_url"] == ""
    assert settings["media_delivery"] == "private_s3_signed"
    assert settings["public_media_url_ignored"] is True


def test_service_restores_s3_checkpoint_on_startup(tmp_path, monkeypatch):
    export = tmp_path / "export"
    database = export / "catalog.sqlite"
    calls = []

    def restore(destination):
        calls.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        _catalog(destination / "catalog.sqlite")
        return True

    monkeypatch.setattr(service, "EXPORT_DIR", export)
    monkeypatch.setattr(service, "DATABASE_PATH", database)
    monkeypatch.setattr(service, "download_checkpoint", restore)

    assert service._restore_runtime_checkpoint() is True
    assert database.exists()
    assert service._restore_runtime_checkpoint() is False
    assert calls == [export]


def test_service_gracefully_stops_active_crawl(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.wait_timeouts = []

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return 0

    current = FakeProcess()
    monkeypatch.setattr(service, "process", current)

    service._graceful_shutdown_crawl()

    assert current.terminated is True
    assert current.wait_timeouts == [240]


def test_stale_stopping_state_recovers_as_stopped(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"state": "stopping", "run_id": "run-1", "finished_at": None}),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "STATUS_PATH", status_path)
    monkeypatch.setattr(service, "process", None)
    monkeypatch.setattr(service, "save_json_object", lambda *args, **kwargs: True)

    state = service._recover_stale_process_state()

    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert state["state"] == "stopped"
    assert persisted["state"] == "stopped"
    assert persisted["finished_at"]


def test_forced_stop_kills_process_after_timeout():
    class FakeProcess:
        def __init__(self):
            self.killed = False

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("scrapy", timeout)

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    current = FakeProcess()

    service._force_stop_after_timeout(current, timeout=1)

    assert current.killed is True


def test_process_watcher_records_requested_stop(tmp_path, monkeypatch):
    class FinishedProcess:
        def wait(self, timeout=None):
            return -15

        def poll(self):
            return -15

    export = tmp_path / "export"
    export.mkdir()
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"state": "stopping", "run_id": "run-2"}), encoding="utf-8"
    )
    current = FinishedProcess()
    monkeypatch.setattr(service, "EXPORT_DIR", export)
    monkeypatch.setattr(service, "STATUS_PATH", status_path)
    monkeypatch.setattr(service, "process", current)
    monkeypatch.setattr(service, "save_json_object", lambda *args, **kwargs: True)

    service._watch_process(current, "run-2")

    state = json.loads(status_path.read_text(encoding="utf-8"))
    assert state["state"] == "stopped"
    assert state["exit_code"] == -15


def test_product_deletion_uses_a_copy_and_removes_related_records(tmp_path):
    source = tmp_path / "source.sqlite"
    candidate = tmp_path / "candidate.sqlite"
    _catalog(source)
    copy_database(source, candidate)

    deleted = delete_product(candidate, "99")

    assert deleted == {
        "id": "99",
        "name": "Adriana Ring",
        "media_paths": ["products/99/ring.jpg"],
    }
    assert catalog_summary(source)["products"] == 1
    assert catalog_summary(candidate)["products"] == 0
    connection = sqlite3.connect(candidate)
    assert connection.execute("SELECT COUNT(*) FROM variations").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
    connection.close()


def test_bulk_product_deletion_is_transactional_and_deduplicated(tmp_path):
    database = tmp_path / "catalog.sqlite"
    _catalog(database)
    _add_product(database)

    deleted = delete_products(database, ["99", "100", "99", "missing"])

    assert [item["id"] for item in deleted] == ["99", "100"]
    assert catalog_summary(database)["products"] == 0
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM variations").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
    connection.close()


def test_bulk_delete_api_updates_checkpoint_once(tmp_path, monkeypatch):
    export = tmp_path / "export"
    export.mkdir()
    database = export / "catalog.sqlite"
    _catalog(database)
    _add_product(database)
    checkpoint_uploads = []
    deleted_media = []
    monkeypatch.setenv("CONTROL_TOKEN", "test-control-token")
    monkeypatch.setattr(service, "EXPORT_DIR", export)
    monkeypatch.setattr(service, "DATABASE_PATH", database)
    monkeypatch.setattr(service, "process", None)
    monkeypatch.setattr(
        service,
        "upload_database_checkpoint",
        lambda path: checkpoint_uploads.append(path.read_bytes()) or True,
    )
    monkeypatch.setattr(service, "upload_latest_artifacts", lambda *args: True)
    monkeypatch.setattr(
        service,
        "delete_media_objects",
        lambda paths: deleted_media.extend(paths) or len(paths),
    )
    monkeypatch.setattr(
        service,
        "rebuild_catalog_artifacts",
        lambda *args: {"database_counts": {"products": 0}},
    )

    with TestClient(service.app) as client:
        response = client.post(
            "/api/products/bulk-delete",
            headers={"Authorization": "Bearer test-control-token"},
            json={"product_ids": ["99", "100", "missing"], "delete_media": True},
        )

    assert response.status_code == 200
    assert response.json()["deleted_count"] == 2
    assert response.json()["not_found_ids"] == ["missing"]
    assert len(checkpoint_uploads) == 1
    assert deleted_media == ["products/99/ring.jpg", "products/100/featured.jpg"]
    assert catalog_summary(database)["products"] == 0


def test_failed_checkpoint_upload_leaves_active_catalog_untouched(
    tmp_path, monkeypatch
):
    export = tmp_path / "export"
    export.mkdir()
    database = export / "catalog.sqlite"
    _catalog(database)
    monkeypatch.setattr(service, "EXPORT_DIR", export)
    monkeypatch.setattr(service, "DATABASE_PATH", database)
    monkeypatch.setattr(service, "process", None)
    monkeypatch.setattr(
        service,
        "upload_database_checkpoint",
        lambda *args: (_ for _ in ()).throw(RuntimeError("S3 unavailable")),
    )

    with pytest.raises(HTTPException) as error:
        service.remove_product("99", delete_media=False)

    assert error.value.status_code == 502
    assert catalog_summary(database)["products"] == 1


def test_delete_candidate_uses_database_filesystem(tmp_path, monkeypatch):
    export = tmp_path / "render-runtime" / "export"
    export.mkdir(parents=True)
    database = export / "catalog.sqlite"
    _catalog(database)
    candidate_parents = []
    monkeypatch.setattr(service, "EXPORT_DIR", export)
    monkeypatch.setattr(service, "DATABASE_PATH", database)
    monkeypatch.setattr(service, "process", None)
    monkeypatch.setattr(
        service,
        "upload_database_checkpoint",
        lambda path: candidate_parents.append(path.parent) or True,
    )
    monkeypatch.setattr(service, "upload_latest_artifacts", lambda *args: True)
    monkeypatch.setattr(
        service,
        "rebuild_catalog_artifacts",
        lambda *args: {"database_counts": {"products": 0}},
    )

    result = service.remove_product("99", delete_media=False)

    assert result["deleted"] is True
    assert len(candidate_parents) == 1
    assert candidate_parents[0].parent == database.parent
    assert catalog_summary(database)["products"] == 0


def test_admin_api_requires_token_and_returns_real_catalog(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    export = runtime / "export"
    export.mkdir(parents=True)
    database = export / "catalog.sqlite"
    _catalog(database)

    monkeypatch.setenv("CONTROL_TOKEN", "test-control-token")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(service, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(service, "EXPORT_DIR", export)
    monkeypatch.setattr(service, "DATABASE_PATH", database)
    monkeypatch.setattr(service, "LOG_PATH", runtime / "crawl.log")
    monkeypatch.setattr(service, "STATUS_PATH", runtime / "status.json")
    monkeypatch.setattr(service, "SETTINGS_PATH", runtime / "admin-settings.json")
    monkeypatch.setattr(service, "DISCOVERY_PATH", runtime / "catalog-discovery.json")
    monkeypatch.setattr(service, "PROGRESS_PATH", export / "progress.json")
    discovered = {
        "base_url": "https://example.test/",
        "total_products": 88,
        "total_categories": 1,
        "categories": [{"id": "11", "name": "Rings", "count": 88}],
    }
    monkeypatch.setattr(service, "discover_catalog", lambda *args, **kwargs: discovered)
    monkeypatch.setattr(service, "save_json_object", lambda *args, **kwargs: True)
    checkpoint_uploads = []
    media_deletions = []
    monkeypatch.setattr(
        service,
        "upload_database_checkpoint",
        lambda path: checkpoint_uploads.append(path.read_bytes()) or True,
    )
    monkeypatch.setattr(service, "upload_latest_artifacts", lambda *args: True)
    monkeypatch.setattr(
        service,
        "delete_media_objects",
        lambda paths: media_deletions.extend(paths) or len(paths),
    )
    monkeypatch.setattr(
        service,
        "rebuild_catalog_artifacts",
        lambda *args: {"database_counts": {"products": 0}},
    )

    with TestClient(service.app) as client:
        assert client.get("/api/status").status_code == 401
        headers = {"Authorization": "Bearer test-control-token"}
        session = client.get("/api/session", headers=headers)
        status = client.get("/api/status", headers=headers)
        products = client.get("/api/products?q=Adriana", headers=headers)
        detail = client.get("/api/products/99", headers=headers)
        empty_discovery = client.get("/api/discovery", headers=headers)
        refreshed_discovery = client.post(
            "/api/discovery/refresh",
            headers=headers,
            json={"base_url": "https://example.test/"},
        )
        deleted = client.delete(
            "/api/products/99?delete_media=true", headers=headers
        )
        missing_product = client.get("/api/products/99", headers=headers)
        remaining_products = client.get("/api/products", headers=headers)

    assert session.status_code == 200
    assert status.json()["catalog"]["products"] == 1
    assert products.json()["items"][0]["name"] == "Adriana Ring"
    assert detail.json()["variations"][0]["id"] == 501
    assert empty_discovery.json()["total_products"] == 0
    assert refreshed_discovery.json()["total_products"] == 88
    assert (runtime / "catalog-discovery.json").exists()
    assert deleted.status_code == 200
    assert deleted.json()["checkpoint_updated"] is True
    assert deleted.json()["media_deleted"] == 1
    assert missing_product.status_code == 404
    assert remaining_products.json()["total"] == 0
    assert len(checkpoint_uploads) == 1
    assert media_deletions == ["products/99/ring.jpg"]
