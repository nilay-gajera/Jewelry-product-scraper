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


def load_json_object(*parts: str) -> dict[str, Any] | list[Any] | None:
    """Load a small JSON control-plane object from the configured bucket."""

    if not s3_enabled():
        return None
    try:
        response = s3_client().get_object(
            Bucket=os.environ["S3_BUCKET"], Key=_key(*parts)
        )
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as exc:
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if error_code not in {"404", "NoSuchKey", "NotFound"}:
            LOGGER.warning("Could not load S3 JSON object %s: %s", _key(*parts), exc)
        return None


def save_json_object(payload: Any, *parts: str) -> bool:
    """Persist a small non-secret JSON object used by the admin service."""

    if not s3_enabled():
        return False
    try:
        s3_client().put_object(
            Bucket=os.environ["S3_BUCKET"],
            Key=_key(*parts),
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
            **_extra_args(),
        )
        return True
    except Exception as exc:
        LOGGER.warning("Could not save S3 JSON object %s: %s", _key(*parts), exc)
        return False


def list_admin_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Return the newest persisted run records without exposing credentials."""

    if not s3_enabled():
        return []
    client = s3_client()
    prefix = _key("admin", "runs") + "/"
    try:
        response = client.list_objects_v2(
            Bucket=os.environ["S3_BUCKET"], Prefix=prefix, MaxKeys=max(1, limit)
        )
    except Exception as exc:
        LOGGER.warning("Could not list S3 run records: %s", exc)
        return []

    records: list[dict[str, Any]] = []
    objects = sorted(
        response.get("Contents", []),
        key=lambda item: item.get("LastModified") or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    for item in objects[:limit]:
        key = str(item.get("Key") or "")
        if not key.endswith(".json"):
            continue
        try:
            payload = client.get_object(
                Bucket=os.environ["S3_BUCKET"], Key=key
            )
            value = json.loads(payload["Body"].read().decode("utf-8"))
        except Exception as exc:
            LOGGER.warning("Could not read S3 run record %s: %s", key, exc)
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def download_checkpoint(output_dir: Path, *, strict: bool = False) -> bool:
    """Restore the latest normalized database before a restarted free-tier run."""

    if not s3_enabled() or not _truthy(os.getenv("S3_RESTORE_CHECKPOINT", "1")):
        return False
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".catalog-checkpoint-",
            suffix=".sqlite",
            dir=output_dir,
            delete=False,
        ) as temporary:
            candidate = Path(temporary.name)
        s3_client().download_file(
            os.environ["S3_BUCKET"],
            _key("checkpoints", "catalog.sqlite"),
            str(candidate),
        )
        connection = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not result or result[0] != "ok" or "products" not in tables:
                raise sqlite3.DatabaseError("checkpoint is not a valid catalog database")
        finally:
            connection.close()

        target = output_dir / "catalog.sqlite"
        target.with_name(f"{target.name}-wal").unlink(missing_ok=True)
        target.with_name(f"{target.name}-shm").unlink(missing_ok=True)
        candidate.replace(target)
        return True
    except Exception as exc:
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if error_code not in {"404", "NoSuchKey", "NotFound"}:
            LOGGER.warning("Could not restore S3 checkpoint: %s", exc)
            if strict:
                raise RuntimeError(
                    "The existing S3 catalog checkpoint could not be restored. "
                    "The crawl was not started, to avoid overwriting recoverable data."
                ) from exc
        return False
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


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


def presigned_media_url(local_path: str, expires_in: int = 3600) -> str | None:
    """Return a temporary URL for a private media object stored by Scrapy."""

    normalized = str(local_path or "").replace("\\", "/").lstrip("/")
    if (
        not s3_enabled()
        or not normalized
        or normalized.startswith(("http://", "https://"))
        or ".." in normalized.split("/")
    ):
        return None
    try:
        return s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": os.environ["S3_BUCKET"],
                "Key": _key("media", normalized),
            },
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        LOGGER.warning("Could not create media URL for %s: %s", normalized, exc)
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


def upload_database_checkpoint(database_path: Path) -> bool:
    """Replace the active S3 checkpoint with a snapshot of one database."""

    if not s3_enabled():
        return False
    database_path = Path(database_path).resolve()
    pipeline = S3ArtifactPipeline()
    pipeline.output_dir = database_path.parent
    pipeline.bucket = os.environ["S3_BUCKET"]
    pipeline._upload_checkpoint()
    return True


def upload_latest_artifacts(output_dir: Path) -> bool:
    """Refresh mutable latest artifacts without rewriting historical runs."""

    if not s3_enabled():
        return False
    pipeline = S3ArtifactPipeline()
    pipeline.output_dir = Path(output_dir).resolve()
    pipeline.bucket = os.environ["S3_BUCKET"]
    pipeline._upload_checkpoint()
    pipeline._upload_final_artifacts(include_run_archive=False)
    return True


def delete_media_objects(local_paths: list[str]) -> int:
    """Delete selected downloaded media keys while preserving run archives."""

    if not s3_enabled():
        return 0
    normalized_paths = list(
        dict.fromkeys(
            str(path).replace("\\", "/").lstrip("/")
            for path in local_paths
            if path
            and not str(path).startswith(("http://", "https://"))
            and ".." not in str(path).replace("\\", "/").split("/")
        )
    )
    if not normalized_paths:
        return 0

    client = s3_client()
    deleted = 0
    for index in range(0, len(normalized_paths), 1000):
        batch = normalized_paths[index : index + 1000]
        response = client.delete_objects(
            Bucket=os.environ["S3_BUCKET"],
            Delete={
                "Objects": [{"Key": _key("media", path)} for path in batch],
                "Quiet": True,
            },
        )
        errors = response.get("Errors") or []
        if errors:
            failures = ", ".join(
                f"{item.get('Code') or 'Error'}: "
                f"{item.get('Message') or 'delete failed'} ({item.get('Key')})"
                for item in errors[:5]
            )
            raise RuntimeError(f"S3 could not delete media objects: {failures}")
        deleted += len(batch)
    return deleted


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class S3ArtifactPipeline:
    """Checkpoint normalized data and upload final WooCommerce-ready artifacts."""

    def __init__(self):
        self.crawler = None
        self.output_dir = Path("outputs/catalog")
        self.bucket = os.getenv("S3_BUCKET")
        self.upload_every = max(1, int(os.getenv("S3_UPLOAD_EVERY", "100")))
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

    def _upload_final_artifacts(self, include_run_archive: bool = True) -> None:
        client = s3_client()
        run_id = os.getenv("SCRAPER_RUN_ID") or datetime.now(UTC).strftime(
            "%Y%m%dT%H%M%SZ"
        )
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

            branches = (
                ("latest", f"runs/{run_id}")
                if include_run_archive
                else ("latest",)
            )
            for path in upload_files:
                for branch in branches:
                    client.upload_file(
                        str(path),
                        self.bucket,
                        _key(branch, path.name),
                        ExtraArgs=_extra_args(),
                    )

            for branch in branches:
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
            if include_run_archive:
                client.upload_file(
                    str(manifest_path),
                    self.bucket,
                    _key("runs", run_id, manifest_path.name),
                    ExtraArgs=_extra_args(),
                )
