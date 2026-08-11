from __future__ import annotations

import sqlite3
from pathlib import Path

from lgd_scraper import s3sync


class FakeS3Client:
    def __init__(self):
        self.uploads: list[tuple[str, str, bytes]] = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.uploads.append((bucket, key, Path(filename).read_bytes()))

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://signed.test/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"


class DownloadS3Client:
    def __init__(self, payload: bytes):
        self.payload = payload

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.payload)


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
    (tmp_path / "woocommerce-master.csv").write_text(
        "Type,SKU\nvariable,LGD-P-99\n", encoding="utf-8"
    )

    pipeline = s3sync.S3ArtifactPipeline()
    pipeline.output_dir = tmp_path
    pipeline._upload_checkpoint()
    pipeline._upload_final_artifacts()

    keys = [key for _, key, _ in fake.uploads]
    assert "exports/jewelry/checkpoints/catalog.sqlite" in keys
    assert "exports/jewelry/latest/catalog.sqlite" in keys
    assert "exports/jewelry/latest/woocommerce-master.csv" in keys
    assert "exports/jewelry/latest/catalog-export.zip" in keys
    assert "exports/jewelry/latest/s3-upload.json" in keys
    assert (tmp_path / "s3-upload.json").exists()


def test_large_catalog_checkpoint_interval_defaults_to_one_hundred(monkeypatch):
    monkeypatch.delenv("S3_UPLOAD_EVERY", raising=False)

    pipeline = s3sync.S3ArtifactPipeline()

    assert pipeline.upload_every == 100


def test_private_media_presigned_url_uses_media_prefix(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "catalog-bucket")
    monkeypatch.setenv("S3_PREFIX", "exports/jewelry")
    monkeypatch.setattr(s3sync, "s3_client", FakeS3Client)

    url = s3sync.presigned_media_url("products/99/ring.jpg", expires_in=900)

    assert url == (
        "https://signed.test/catalog-bucket/exports/jewelry/media/"
        "products/99/ring.jpg?ttl=900"
    )
    assert s3sync.presigned_media_url("../secret.txt") is None
    assert s3sync.presigned_artifact_url(expires_in=600) == (
        "https://signed.test/catalog-bucket/exports/jewelry/latest/"
        "catalog-export.zip?ttl=600"
    )


def test_delete_media_objects_uses_scoped_s3_keys(monkeypatch):
    requests = []

    class FakeS3:
        def delete_objects(self, **kwargs):
            requests.append(kwargs)
            return {}

    monkeypatch.setenv("S3_BUCKET", "catalog-bucket")
    monkeypatch.setenv("S3_PREFIX", "jewelry-product-scraper")
    monkeypatch.setattr(s3sync, "s3_client", lambda: FakeS3())

    deleted = s3sync.delete_media_objects(
        [
            "products/99/ring.jpg",
            "products/99/ring.jpg",
            "../outside.jpg",
            "https://source.test/original.jpg",
        ]
    )

    assert deleted == 1
    assert requests == [
        {
            "Bucket": "catalog-bucket",
            "Delete": {
                "Objects": [
                    {
                        "Key": "jewelry-product-scraper/media/products/99/ring.jpg"
                    }
                ],
                "Quiet": True,
            },
        }
    ]


def test_checkpoint_restore_validates_then_atomically_replaces_database(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE products (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO products VALUES ('new')")
    connection.commit()
    connection.close()

    output = tmp_path / "output"
    output.mkdir()
    target = output / "catalog.sqlite"
    target.write_bytes(b"old catalog remains until validation succeeds")
    (output / "catalog.sqlite-wal").write_bytes(b"stale wal")
    (output / "catalog.sqlite-shm").write_bytes(b"stale shm")
    monkeypatch.setenv("S3_BUCKET", "catalog-bucket")
    monkeypatch.setattr(
        s3sync, "s3_client", lambda: DownloadS3Client(source.read_bytes())
    )

    assert s3sync.download_checkpoint(output) is True

    restored = sqlite3.connect(target)
    assert restored.execute("SELECT id FROM products").fetchone()[0] == "new"
    restored.close()
    assert not (output / "catalog.sqlite-wal").exists()
    assert not (output / "catalog.sqlite-shm").exists()
    assert not list(output.glob(".catalog-checkpoint-*.sqlite"))


def test_invalid_checkpoint_never_damages_existing_database(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    target = output / "catalog.sqlite"
    original = b"existing catalog"
    target.write_bytes(original)
    monkeypatch.setenv("S3_BUCKET", "catalog-bucket")
    monkeypatch.setattr(
        s3sync, "s3_client", lambda: DownloadS3Client(b"not sqlite")
    )

    assert s3sync.download_checkpoint(output) is False
    assert target.read_bytes() == original
    assert not list(output.glob(".catalog-checkpoint-*.sqlite"))


def test_strict_checkpoint_restore_refuses_to_start_from_corrupt_data(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("S3_BUCKET", "catalog-bucket")
    monkeypatch.setattr(
        s3sync, "s3_client", lambda: DownloadS3Client(b"not sqlite")
    )

    try:
        s3sync.download_checkpoint(tmp_path, strict=True)
    except RuntimeError as error:
        assert "was not started" in str(error)
    else:
        raise AssertionError("strict restore accepted a corrupt checkpoint")
