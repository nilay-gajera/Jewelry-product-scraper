from __future__ import annotations

import sqlite3
from pathlib import Path

from lgd_scraper import s3sync


class FakeS3Client:
    def __init__(self):
        self.uploads: list[tuple[str, str, bytes]] = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.uploads.append((bucket, key, Path(filename).read_bytes()))


def test_uploads_checkpoint_final_archive_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "catalog-bucket")
    monkeypatch.setenv("S3_PREFIX", "exports/jewelry")
    fake = FakeS3Client()
    monkeypatch.setattr(s3sync, "s3_client", lambda: fake)

    database = sqlite3.connect(tmp_path / "catalog.sqlite")
    database.execute("CREATE TABLE products (id TEXT PRIMARY KEY)")
    database.execute("INSERT INTO products (id) VALUES ('99')")
    database.commit()
    database.close()
    (tmp_path / "woocommerce-products.csv").write_text(
        "Type,SKU\nvariable,LGD-P-99\n", encoding="utf-8"
    )

    pipeline = s3sync.S3ArtifactPipeline()
    pipeline.output_dir = tmp_path
    pipeline._upload_checkpoint()
    pipeline._upload_final_artifacts()

    keys = [key for _, key, _ in fake.uploads]
    assert "exports/jewelry/checkpoints/catalog.sqlite" in keys
    assert "exports/jewelry/latest/catalog.sqlite" in keys
    assert "exports/jewelry/latest/woocommerce-products.csv" in keys
    assert "exports/jewelry/latest/catalog-export.zip" in keys
    assert "exports/jewelry/latest/s3-upload.json" in keys
    assert (tmp_path / "s3-upload.json").exists()
