from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from lgd_scraper.woocommerce_csv import (
    MASTER_FILENAME,
    export_woocommerce_csvs,
    validate_woocommerce_csv,
)


REQUIRED_TABLES = {"products", "categories", "product_categories"}
COUNT_TABLES = (
    "products",
    "variations",
    "categories",
    "attributes",
    "product_categories",
    "product_attributes",
    "images",
    "diagnostics",
)


def inspect_checkpoint(database_path: Path) -> dict[str, int]:
    """Validate a catalog checkpoint and return its normalized row counts."""

    database_path = Path(database_path).expanduser().resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Catalog checkpoint does not exist: {database_path}")

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if not quick_check or quick_check[0] != "ok":
            raise sqlite3.DatabaseError("SQLite quick_check did not return ok")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise sqlite3.DatabaseError(
                f"Catalog checkpoint is missing table(s): {', '.join(missing)}"
            )
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in COUNT_TABLES
            if table in tables
        }
    finally:
        connection.close()


def build_local_master(
    database_path: Path,
    output_dir: Path,
    *,
    public_base_url: str | None = None,
) -> dict[str, Any]:
    """Build and preflight one master CSV from a local SQLite checkpoint."""

    database_path = Path(database_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    database_counts = inspect_checkpoint(database_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        export_counts = export_woocommerce_csvs(
            connection,
            output_dir,
            public_base_url=public_base_url,
        )
    finally:
        connection.close()

    target = output_dir / MASTER_FILENAME
    validation = validate_woocommerce_csv(target)
    return {
        "source_database": str(database_path),
        "master_csv": str(target),
        "master_bytes": target.stat().st_size,
        "database_counts": database_counts,
        "woocommerce_export": export_counts,
        "preflight": validation,
        "images_use_public_base_url": bool(public_base_url),
    }


def download_s3_checkpoint(
    target: Path,
    *,
    bucket: str,
    prefix: str,
    region: str | None = None,
) -> Path:
    """Download the durable scraper checkpoint without exposing credentials."""

    import boto3

    normalized_prefix = prefix.strip("/")
    key = "/".join(
        value for value in (normalized_prefix, "checkpoints", "catalog.sqlite") if value
    )
    target = Path(target).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    candidate: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".catalog-checkpoint-",
            suffix=".sqlite",
            dir=target.parent,
            delete=False,
        ) as temporary:
            candidate = Path(temporary.name)
        boto3.client("s3", region_name=region).download_file(
            bucket,
            key,
            str(candidate),
        )
        inspect_checkpoint(candidate)
        candidate.replace(target)
        return target
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a validated WooCommerce master CSV locally from a downloaded "
            "catalog.sqlite checkpoint or directly from S3."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Path to a catalog.sqlite file already downloaded from S3.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/local-woocommerce-export"),
    )
    parser.add_argument("--s3-bucket", default=os.getenv("S3_BUCKET"))
    parser.add_argument(
        "--s3-prefix",
        default=os.getenv("S3_PREFIX", "jewelry-product-scraper"),
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
    )
    parser.add_argument(
        "--public-base-url",
        default=os.getenv("S3_PUBLIC_BASE_URL"),
        help="Public S3/CloudFront media base used for the WooCommerce Images column.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    output_dir = args.output_dir.expanduser().resolve()

    if args.database:
        report = build_local_master(
            args.database,
            output_dir,
            public_base_url=args.public_base_url,
        )
    else:
        if not args.s3_bucket:
            raise SystemExit(
                "Provide --database or configure S3_BUCKET and AWS credentials."
            )
        with tempfile.TemporaryDirectory(prefix="lgd-local-export-") as temp_dir:
            checkpoint = download_s3_checkpoint(
                Path(temp_dir) / "catalog.sqlite",
                bucket=args.s3_bucket,
                prefix=args.s3_prefix,
                region=args.region,
            )
            report = build_local_master(
                checkpoint,
                output_dir,
                public_base_url=args.public_base_url,
            )
            report["source_database"] = (
                f"s3://{args.s3_bucket}/{args.s3_prefix.strip('/')}/"
                "checkpoints/catalog.sqlite"
            )

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
