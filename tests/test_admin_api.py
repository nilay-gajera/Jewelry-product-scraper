from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

import service
from lgd_scraper.admin_data import catalog_summary, list_products, product_detail


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
            }
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
    assert detail is not None
    assert detail["quality"]["complete"] is True
    assert detail["media"][0]["display_url"].startswith("https://cdn.test/")


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
    monkeypatch.setattr(service, "PROGRESS_PATH", export / "progress.json")

    with TestClient(service.app) as client:
        assert client.get("/api/status").status_code == 401
        headers = {"Authorization": "Bearer test-control-token"}
        session = client.get("/api/session", headers=headers)
        status = client.get("/api/status", headers=headers)
        products = client.get("/api/products?q=Adriana", headers=headers)
        detail = client.get("/api/products/99", headers=headers)

    assert session.status_code == 200
    assert status.json()["catalog"]["products"] == 1
    assert products.json()["items"][0]["name"] == "Adriana Ring"
    assert detail.json()["variations"][0]["id"] == 501
