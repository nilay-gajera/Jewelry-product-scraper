from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from lgd_scraper.admin_data import (
    catalog_summary,
    diagnostics,
    effective_settings,
    list_products,
    persist_settings,
    product_detail,
    secret_presence,
    storage_settings,
)
from lgd_scraper.catalog_mutations import (
    copy_database,
    delete_product,
    rebuild_catalog_artifacts,
)
from lgd_scraper.discovery import CatalogDiscoveryError, discover_catalog
from lgd_scraper.s3sync import (
    delete_media_objects,
    download_checkpoint,
    load_json_object,
    list_admin_runs,
    presigned_artifact_url,
    save_json_object,
    upload_database_checkpoint,
    upload_latest_artifacts,
)


PROJECT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", PROJECT_DIR / "runtime")).resolve()
EXPORT_DIR = RUNTIME_DIR / "export"
DATABASE_PATH = EXPORT_DIR / "catalog.sqlite"
LOG_PATH = RUNTIME_DIR / "crawl.log"
STATUS_PATH = RUNTIME_DIR / "status.json"
SETTINGS_PATH = RUNTIME_DIR / "admin-settings.json"
DISCOVERY_PATH = RUNTIME_DIR / "catalog-discovery.json"
PROGRESS_PATH = EXPORT_DIR / "progress.json"
ADMIN_DIST = PROJECT_DIR / "admin_dist"

RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

state_lock = threading.Lock()
process: subprocess.Popen[str] | None = None


def _restore_runtime_checkpoint() -> bool:
    """Hydrate Render's ephemeral disk before the admin API serves data."""

    if DATABASE_PATH.exists():
        return False
    return download_checkpoint(EXPORT_DIR)


def _graceful_shutdown_crawl() -> None:
    """Give Scrapy time to close SQLite and upload its final S3 checkpoint."""

    current = process
    if current is None or current.poll() is not None:
        return
    current.terminate()
    try:
        current.wait(timeout=240)
    except subprocess.TimeoutExpired:
        current.kill()
        current.wait(timeout=10)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await asyncio.to_thread(_restore_runtime_checkpoint)
    try:
        yield
    finally:
        await asyncio.to_thread(_graceful_shutdown_crawl)


app = FastAPI(title="Jewelry Product Scraper", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)
if (ADMIN_DIST / "assets").exists():
    app.mount(
        "/assets", StaticFiles(directory=ADMIN_DIST / "assets"), name="admin-assets"
    )


class CrawlSettings(BaseModel):
    base_url: HttpUrl = "https://www.loosegrowndiamond.com/"
    mode: Literal["test", "full"] = "test"
    max_products: int = Field(default=5, ge=0, le=1_000_000)
    concurrency: int = Field(default=2, ge=1, le=16)
    download_delay: float = Field(default=1.0, ge=0.1, le=60)
    download_timeout: int = Field(default=45, ge=5, le=300)
    retry_times: int = Field(default=3, ge=0, le=10)
    enrich_html: bool = True
    download_media: bool = True
    resume_checkpoint: bool = True
    obey_robots: bool = True
    category_ids: list[str] = Field(default_factory=list, max_length=500)


class StartRequest(BaseModel):
    base_url: HttpUrl | None = None
    mode: Literal["test", "full"] | None = None
    max_products: int | None = Field(default=None, ge=0, le=1_000_000)
    concurrency: int | None = Field(default=None, ge=1, le=16)
    download_delay: float | None = Field(default=None, ge=0.1, le=60)
    download_timeout: int | None = Field(default=None, ge=5, le=300)
    retry_times: int | None = Field(default=None, ge=0, le=10)
    enrich_html: bool | None = None
    download_media: bool | None = None
    resume_checkpoint: bool | None = None
    obey_robots: bool | None = None
    category_ids: list[str] | None = Field(default=None, max_length=500)


class DiscoveryRequest(BaseModel):
    base_url: HttpUrl | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "run_id": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "config": None,
        "checkpoint_restored": False,
        "summary": None,
        "message": "Ready to start.",
    }


def _process_running() -> bool:
    return process is not None and process.poll() is None


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_state() -> dict[str, Any]:
    state = _read_json(STATUS_PATH)
    if not isinstance(state, dict):
        state = _default_state()
    if state.get("state") == "running" and not _process_running():
        state["state"] = "interrupted"
        state["message"] = "The process stopped. Start again to resume from S3."
    return state


def _write_state(state: dict[str, Any]) -> None:
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(STATUS_PATH)


def _persist_run(state: dict[str, Any]) -> None:
    run_id = state.get("run_id")
    if not run_id:
        return
    payload = {
        key: state.get(key)
        for key in (
            "run_id",
            "state",
            "started_at",
            "finished_at",
            "exit_code",
            "checkpoint_restored",
            "config",
            "summary",
            "message",
        )
    }
    save_json_object(payload, "admin", "runs", f"{run_id}.json")


def _require_control(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("CONTROL_TOKEN")
    if not expected:
        raise HTTPException(503, "CONTROL_TOKEN is not configured on this service.")
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "Invalid control token.")


def _merged_start_settings(request: StartRequest) -> dict[str, Any]:
    values = effective_settings(SETTINGS_PATH)
    provided = request.model_dump(exclude_none=True, mode="json")
    values.update(provided)
    if values["mode"] == "full":
        values["max_products"] = 0
    values["category_ids"] = list(
        dict.fromkeys(
            str(value).strip()
            for value in values.get("category_ids", [])
            if str(value).strip()
        )
    )
    return CrawlSettings.model_validate(values).model_dump(mode="json")


def _configure_crawl_environment(config: dict[str, Any], run_id: str) -> dict[str, str]:
    environment = dict(os.environ)
    bucket = environment.get("S3_BUCKET")
    if bucket and not environment.get("SCRAPER_MEDIA_STORE"):
        prefix = environment.get("S3_PREFIX", "jewelry-product-scraper").strip("/")
        environment["SCRAPER_MEDIA_STORE"] = f"s3://{bucket}/{prefix}/media/"
    environment.update(
        {
            "SCRAPER_RUN_ID": run_id,
            "SCRAPER_JOBDIR": str(RUNTIME_DIR / "jobs" / run_id),
            "SCRAPER_LOG_LEVEL": environment.get("SCRAPER_LOG_LEVEL", "INFO"),
            "SCRAPER_CONCURRENCY": str(config["concurrency"]),
            "SCRAPER_DOWNLOAD_DELAY": str(config["download_delay"]),
            "SCRAPER_DOWNLOAD_TIMEOUT": str(config["download_timeout"]),
            "SCRAPER_RETRY_TIMES": str(config["retry_times"]),
            "SCRAPER_DOWNLOAD_MEDIA": "1" if config["download_media"] else "0",
            "SCRAPER_OBEY_ROBOTS": "1" if config["obey_robots"] else "0",
        }
    )
    proxy = environment.get("SCRAPER_PROXY_URL")
    if proxy:
        environment["HTTPS_PROXY"] = proxy
        environment["HTTP_PROXY"] = proxy
    return environment


def _clear_local_catalog() -> None:
    for name in (
        "catalog.sqlite",
        "catalog.sqlite-wal",
        "catalog.sqlite-shm",
        "products.jsonl",
        "categories.jsonl",
        "attributes.jsonl",
        "diagnostics.jsonl",
        "progress.json",
    ):
        (EXPORT_DIR / name).unlink(missing_ok=True)


def _watch_process(current_process: subprocess.Popen[str], run_id: str) -> None:
    exit_code = current_process.wait()
    summary = _read_json(EXPORT_DIR / "crawl-summary.json")
    archive = EXPORT_DIR / "catalog-export.zip"
    archive.unlink(missing_ok=True)
    shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=EXPORT_DIR)
    upload_error = _read_json(EXPORT_DIR / "s3-upload-error.json")
    successful = exit_code == 0 and not upload_error
    access_warning = bool(
        isinstance(summary, dict) and summary.get("access_blocked")
    )

    with state_lock:
        state = _read_state()
        if state.get("run_id") != run_id:
            return
        state.update(
            {
                "state": (
                    "completed_with_warnings"
                    if successful and access_warning
                    else "completed" if successful else "failed"
                ),
                "finished_at": _now(),
                "exit_code": exit_code,
                "summary": summary,
                "message": (
                    "Crawl completed, but the source blocked one or more requests. Review diagnostics before importing."
                    if successful and access_warning
                    else "Crawl completed. Review products or download the export."
                    if successful
                    else (
                        f"S3 upload failed: {upload_error.get('error')}"
                        if isinstance(upload_error, dict)
                        else "Crawl failed. Review the live log."
                    )
                ),
            }
        )
        _write_state(state)
        _persist_run(state)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/session", dependencies=[Depends(_require_control)])
def session() -> dict[str, Any]:
    return {"authenticated": True, "service": "Jewelry Scraper", "version": "1.0.0"}


@app.get("/api/status", dependencies=[Depends(_require_control)])
def status() -> dict[str, Any]:
    with state_lock:
        state = _read_state()
        state["process_running"] = _process_running()
    progress = _read_json(PROGRESS_PATH)
    state["progress"] = progress if isinstance(progress, dict) else None
    state["catalog"] = catalog_summary(DATABASE_PATH)
    return state


@app.get(
    "/api/logs",
    dependencies=[Depends(_require_control)],
    response_class=PlainTextResponse,
)
def logs(lines: int = Query(default=250, ge=20, le=2000)) -> str:
    if not LOG_PATH.exists():
        return ""
    values = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(values[-lines:])


@app.post("/api/start", dependencies=[Depends(_require_control)])
def start_crawl(request: StartRequest) -> dict[str, Any]:
    global process
    with state_lock:
        if _process_running():
            raise HTTPException(409, "A crawl is already running.")

        config = _merged_start_settings(request)
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        if config["resume_checkpoint"]:
            checkpoint_restored = download_checkpoint(EXPORT_DIR)
        else:
            _clear_local_catalog()
            checkpoint_restored = False
        PROGRESS_PATH.unlink(missing_ok=True)
        (EXPORT_DIR / "s3-upload-error.json").unlink(missing_ok=True)
        environment = _configure_crawl_environment(config, run_id)
        command = [
            sys.executable,
            "-m",
            "scrapy",
            "crawl",
            "catalog",
            "-a",
            f"base_url={config['base_url']}",
            "-a",
            f"output_dir={EXPORT_DIR}",
            "-a",
            f"max_products={config['max_products']}",
            "-a",
            f"category_ids={','.join(config['category_ids'])}",
            "-a",
            f"enrich_html={'true' if config['enrich_html'] else 'false'}",
            "-a",
            "use_playwright=false",
        ]
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_handle = LOG_PATH.open("w", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log_handle.close()
        state = {
            "state": "running",
            "run_id": run_id,
            "started_at": _now(),
            "finished_at": None,
            "exit_code": None,
            "config": config,
            "checkpoint_restored": checkpoint_restored,
            "summary": None,
            "message": "Crawl is running. Checkpoints are saved to S3.",
        }
        _write_state(state)
        _persist_run(state)
        threading.Thread(
            target=_watch_process, args=(process, run_id), daemon=True
        ).start()
        return state


@app.post("/api/stop", dependencies=[Depends(_require_control)])
def stop_crawl() -> dict[str, str]:
    if not _process_running():
        return {"status": "not-running"}
    assert process is not None
    process.terminate()
    with state_lock:
        state = _read_state()
        state["state"] = "stopping"
        state["message"] = "Stopping gracefully; partial data will be checkpointed."
        _write_state(state)
        _persist_run(state)
    return {"status": "stopping"}


@app.get("/api/catalog/summary", dependencies=[Depends(_require_control)])
def get_catalog_summary() -> dict[str, Any]:
    return catalog_summary(DATABASE_PATH)


@app.get("/api/discovery", dependencies=[Depends(_require_control)])
def get_discovery() -> dict[str, Any]:
    value = _read_json(DISCOVERY_PATH)
    if not isinstance(value, dict):
        value = load_json_object("admin", "catalog-discovery.json")
    return value if isinstance(value, dict) else {
        "total_products": 0,
        "woocommerce_products": 0,
        "advertised_diamond_inventory": None,
        "advertised_diamond_inventory_label": None,
        "total_categories": 0,
        "categories": [],
        "discovered_at": None,
    }


@app.post("/api/discovery/refresh", dependencies=[Depends(_require_control)])
def refresh_discovery(request: DiscoveryRequest) -> dict[str, Any]:
    settings = effective_settings(SETTINGS_PATH)
    base_url = str(request.base_url or settings["base_url"])
    try:
        value = discover_catalog(
            base_url,
            consumer_key=os.getenv("WC_CONSUMER_KEY"),
            consumer_secret=os.getenv("WC_CONSUMER_SECRET"),
        )
    except CatalogDiscoveryError as exc:
        raise HTTPException(502, str(exc)) from exc
    DISCOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DISCOVERY_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(DISCOVERY_PATH)
    save_json_object(value, "admin", "catalog-discovery.json")
    return value


@app.get("/api/products", dependencies=[Depends(_require_control)])
def get_products(
    q: str = Query(default="", max_length=200),
    product_type: str = Query(default="", max_length=30),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    return list_products(
        DATABASE_PATH,
        query=q.strip(),
        product_type=product_type.strip(),
        page=page,
        page_size=page_size,
    )


@app.get("/api/products/{product_id}", dependencies=[Depends(_require_control)])
def get_product(product_id: str) -> dict[str, Any]:
    value = product_detail(DATABASE_PATH, product_id)
    if value is None:
        raise HTTPException(404, "Product not found.")
    return value


@app.delete("/api/products/{product_id}", dependencies=[Depends(_require_control)])
def remove_product(
    product_id: str,
    delete_media: bool = Query(default=False),
) -> dict[str, Any]:
    """Delete one active product and durably replace mutable S3 artifacts."""

    with state_lock:
        if _process_running():
            raise HTTPException(409, "Stop the active crawl before deleting products.")
        if not DATABASE_PATH.exists():
            raise HTTPException(404, "No catalog database is available.")

        with tempfile.TemporaryDirectory(prefix="catalog-delete-") as temp_dir:
            candidate = Path(temp_dir) / "catalog.sqlite"
            copy_database(DATABASE_PATH, candidate)
            deleted = delete_product(candidate, product_id)
            if deleted is None:
                raise HTTPException(404, "Product not found.")
            try:
                checkpoint_updated = upload_database_checkpoint(candidate)
            except Exception as exc:
                raise HTTPException(
                    502,
                    "The product was not deleted because the S3 checkpoint could not be updated.",
                ) from exc

            DATABASE_PATH.with_name(f"{DATABASE_PATH.name}-wal").unlink(missing_ok=True)
            DATABASE_PATH.with_name(f"{DATABASE_PATH.name}-shm").unlink(missing_ok=True)
            candidate.replace(DATABASE_PATH)

        artifact_summary = rebuild_catalog_artifacts(DATABASE_PATH, EXPORT_DIR)
        latest_artifacts_updated = False
        artifact_error = None
        try:
            latest_artifacts_updated = upload_latest_artifacts(EXPORT_DIR)
        except Exception as exc:
            artifact_error = str(exc)

        media_deleted = 0
        media_error = None
        if delete_media:
            try:
                media_deleted = delete_media_objects(deleted["media_paths"])
            except Exception as exc:
                media_error = str(exc)

    return {
        "deleted": True,
        "product_id": deleted["id"],
        "product_name": deleted["name"],
        "checkpoint_updated": checkpoint_updated,
        "latest_artifacts_updated": latest_artifacts_updated,
        "media_requested": delete_media,
        "media_deleted": media_deleted,
        "media_error": media_error,
        "artifact_error": artifact_error,
        "catalog": artifact_summary["database_counts"],
        "historical_runs_preserved": True,
    }


@app.get("/api/diagnostics", dependencies=[Depends(_require_control)])
def get_diagnostics(limit: int = Query(default=100, ge=1, le=1000)):
    return {"items": diagnostics(DATABASE_PATH, limit=limit)}


@app.get("/api/runs", dependencies=[Depends(_require_control)])
def get_runs(limit: int = Query(default=25, ge=1, le=100)) -> dict[str, Any]:
    records = list_admin_runs(limit)
    current = _read_state()
    if current.get("run_id") and not any(
        item.get("run_id") == current.get("run_id") for item in records
    ):
        records.insert(0, current)
    return {"items": records[:limit]}


@app.get("/api/settings", dependencies=[Depends(_require_control)])
def get_settings() -> dict[str, Any]:
    return {
        "crawl": effective_settings(SETTINGS_PATH),
        "storage": storage_settings(),
        "secrets": secret_presence(),
        "notice": "Secrets are stored in Render environment and are never returned by the API.",
    }


@app.put("/api/settings", dependencies=[Depends(_require_control)])
def update_settings(settings: CrawlSettings) -> dict[str, Any]:
    values = settings.model_dump(mode="json")
    if values["mode"] == "full":
        values["max_products"] = 0
    persist_settings(SETTINGS_PATH, values)
    return get_settings()


@app.get("/api/download", dependencies=[Depends(_require_control)])
def download() -> dict[str, str]:
    url = presigned_artifact_url()
    if url:
        return {"url": url}
    archive = EXPORT_DIR / "catalog-export.zip"
    if not archive.exists():
        raise HTTPException(404, "No export is available.")
    return {"url": "/api/download-local"}


@app.get("/api/download-local", dependencies=[Depends(_require_control)])
def download_local():
    archive = EXPORT_DIR / "catalog-export.zip"
    if not archive.exists():
        raise HTTPException(404, "No export is available.")
    return FileResponse(archive, filename="catalog-export.zip")


def _admin_index() -> FileResponse | HTMLResponse:
    index_path = ADMIN_DIST / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        "<h1>Jewelry Scraper</h1><p>Admin assets are not built. Run npm build in admin/.</p>",
        status_code=503,
    )


@app.get("/", include_in_schema=False)
def index():
    return _admin_index()


@app.get("/{path:path}", include_in_schema=False)
def admin_route(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return _admin_index()
