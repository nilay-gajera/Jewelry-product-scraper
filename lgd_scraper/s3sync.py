from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


def s3_enabled() -> bool:
    return bool(os.getenv("S3_BUCKET"))


def s3_prefix() -> str:
    return os.getenv("S3_PREFIX", "jewelry-product-scraper").strip("/")


def s3_client():
    import boto3

    return boto3.client(
        "s3",
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL") or None,
    )


def _extra_args() -> dict[str, str]:
    encryption = os.getenv("S3_SERVER_SIDE_ENCRYPTION", "AES256").strip()
    return {"ServerSideEncryption": encryption} if encryption else {}


def _key(*parts: str) -> str:
    values = [s3_prefix(), *(str(part).strip("/") for part in parts)]
    return "/".join(value for value in values if value)


def download_checkpoint(output_dir: Path) -> bool:
    """Restore the latest normalized database before a restarted free-tier run."""

    if not s3_enabled() or not _truthy(os.getenv("S3_RESTORE_CHECKPOINT", "1")):
        return False
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        s3_client().download_file(
            os.environ["S3_BUCKET"],
            _key("checkpoints", "catalog.sqlite"),
            str(output_dir / "catalog.sqlite"),
        )
        return True
    except Exception as exc:
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if error_code not in {"404", "NoSuchKey", "NotFound"}:
            LOGGER.warning("Could not restore S3 checkpoint: %s", exc)
        return False


def presigned_artifact_url(expires_in: int = 3600) -> str | None:
    if not s3_enabled():
        return None
    try:
        return s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": os.environ["S3_BUCKET"],
                "Key": _key("latest", "catalog-export.zip"),
            },
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        LOGGER.warning("Could not create artifact URL: %s", exc)
        return None


def upload_final_artifacts(output_dir: Path) -> None:
    """Upload a completed writer output after SQLite and CSV handles are closed."""

    if not s3_enabled():
        return
    pipeline = S3ArtifactPipeline()
    pipeline.output_dir = Path(output_dir).resolve()
    try:
        pipeline._upload_checkpoint()
        pipeline._upload_final_artifacts()
    except Exception as exc:
        LOGGER.exception("Final S3 artifact upload failed")
        (pipeline.output_dir / "s3-upload-error.json").write_text(
            json.dumps({"error": str(exc)}, indent=2),
            encoding="utf-8",
        )
        raise


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class S3ArtifactPipeline:
    """Checkpoint normalized data and upload final WooCommerce-ready artifacts."""

    def __init__(self):
        self.crawler = None
        self.output_dir = Path("outputs/catalog")
        self.bucket = os.getenv("S3_BUCKET")
        self.upload_every = max(1, int(os.getenv("S3_UPLOAD_EVERY", "10")))
        self.products_seen = 0
        self.last_error: str | None = None

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        instance.crawler = crawler
        return instance

    def open_spider(self):
        spider = self.crawler.spider
        self.output_dir = Path(
            getattr(spider, "output_dir", "outputs/catalog")
        ).resolve()

    def process_item(self, item):
        if not self.bucket or item.get("_record_type") != "product":
            return item
        self.products_seen += 1
        if self.products_seen % self.upload_every == 0:
            self._safe_checkpoint()
        return item

    def _safe_checkpoint(self) -> None:
        try:
            self._upload_checkpoint()
        except Exception as exc:
            self.last_error = str(exc)
            LOGGER.warning("S3 checkpoint failed: %s", exc)

    def _database_snapshot(self, target: Path) -> bool:
        source_path = self.output_dir / "catalog.sqlite"
        if not source_path.exists():
            return False
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return True

    def _upload_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lgd-checkpoint-") as temp_dir:
            snapshot = Path(temp_dir) / "catalog.sqlite"
            if not self._database_snapshot(snapshot):
                return
            s3_client().upload_file(
                str(snapshot),
                self.bucket,
                _key("checkpoints", "catalog.sqlite"),
                ExtraArgs=_extra_args(),
            )

    def _artifact_files(self) -> list[Path]:
        allowed_suffixes = {".csv", ".json", ".jsonl", ".sqlite"}
        files: list[Path] = []
        for path in sorted(self.output_dir.iterdir()):
            if not path.is_file() or path.suffix not in allowed_suffixes:
                continue
            if path.name.endswith(("-wal", "-shm")):
                continue
            if path.name in {"s3-upload-error.json", "s3-upload.json"}:
                continue
            files.append(path)
        return files

    def _upload_final_artifacts(self) -> None:
        client = s3_client()
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        files = self._artifact_files()

        with tempfile.TemporaryDirectory(prefix="lgd-artifacts-") as temp_dir:
            temp_path = Path(temp_dir)
            snapshot = temp_path / "catalog.sqlite"
            snapshot_created = self._database_snapshot(snapshot)

            upload_files = [
                path for path in files if path.name != "catalog.sqlite"
            ]
            if snapshot_created:
                upload_files.append(snapshot)

            archive_path = temp_path / "catalog-export.zip"
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for path in upload_files:
                    archive.write(path, arcname=path.name)

            for path in upload_files:
                for branch in ("latest", f"runs/{run_id}"):
                    client.upload_file(
                        str(path),
                        self.bucket,
                        _key(branch, path.name),
                        ExtraArgs=_extra_args(),
                    )

            for branch in ("latest", f"runs/{run_id}"):
                client.upload_file(
                    str(archive_path),
                    self.bucket,
                    _key(branch, archive_path.name),
                    ExtraArgs=_extra_args(),
                )

            manifest = {
                "bucket": self.bucket,
                "prefix": s3_prefix(),
                "run_id": run_id,
                "artifact_key": _key("latest", archive_path.name),
                "files": [path.name for path in upload_files],
                "media_store": os.getenv("SCRAPER_MEDIA_STORE"),
                "media_public_base_url": os.getenv("S3_PUBLIC_BASE_URL"),
            }
            manifest_path = self.output_dir / "s3-upload.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            client.upload_file(
                str(manifest_path),
                self.bucket,
                _key("latest", manifest_path.name),
                ExtraArgs=_extra_args(),
            )
