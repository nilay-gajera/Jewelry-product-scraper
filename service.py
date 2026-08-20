from __future__ import annotations

import asyncio
import json
import logging
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
    product_filter_options,
    persist_settings,
    product_detail,
    secret_presence,
    storage_settings,
)
from lgd_scraper.catalog_mutations import (
    build_catalog_archive,
    copy_database,
    delete_products,
    rebuild_catalog_artifacts,
    unreferenced_media_paths,
)
from lgd_scraper.discovery import CatalogDiscoveryError, discover_catalog
from lgd_scraper.mysql_backend import (
    delete_products_mysql,
    mysql_enabled,
    sync_from_sqlite,
)
from lgd_scraper.run_progress import (
    SERVICE_PHASES,
    PhaseTracker,
    merge_timeline,
    write_json_atomic,
)
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
from lgd_scraper.woocommerce_csv import (
    MASTER_FILENAME,
    WooCommerceExportValidationError,
    export_woocommerce_csvs,
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
# ``state_lock`` is only ever held for in-memory state reads and writes.
# Catalog work (master export, deletion) can run for minutes, so it takes its
# own lock and must never hold ``state_lock`` while doing I/O -- otherwise the
# 5-second dashboard poll blocks behind it and the request threadpool fills.
catalog_lock = threading.Lock()
process: subprocess.Popen[str] | None = None
# Phases the service itself performs, around the child crawl. The crawl's own
# phases live in progress.json and are spliced into this timeline by
# ``_timeline``. Replaced at the start of every run.
SERVICE_PROGRESS_PATH = RUNTIME_DIR / "service-progress.json"
service_progress: PhaseTracker | None = None


def _new_service_progress(run_id: str, mode: str) -> PhaseTracker:
    global service_progress
    service_progress = PhaseTracker(
        SERVICE_PROGRESS_PATH, SERVICE_PHASES, run_id=run_id, mode=mode
    )
    return service_progress


# Startup restore, reported so the dashboard can say the catalog is on its way
# instead of showing an empty one.
restore_state: dict[str, Any] = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "restored": False,
    "error": None,
}


def _restore_runtime_checkpoint() -> bool:
    """Hydrate Render's ephemeral disk so the admin API has data to serve.

    Runs in the background: a container starts with an empty disk, so this
    downloads and verifies the whole catalog. Blocking startup on it meant an
    unreachable or merely large bucket kept the app from ever serving a
    request, which the platform reports as a 520.
    """

    if DATABASE_PATH.exists():
        restore_state.update({"state": "skipped", "finished_at": _now()})
        return False

    restore_state.update(
        {"state": "running", "started_at": _now(), "error": None, "restored": False}
    )
    try:
        with catalog_lock:
            # A crawl started in the meantime already owns the catalog.
            if DATABASE_PATH.exists():
                restore_state.update({"state": "skipped", "finished_at": _now()})
                return False
            restored = download_checkpoint(EXPORT_DIR)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Startup checkpoint restore failed: %s", exc
        )
        restore_state.update(
            {"state": "failed", "finished_at": _now(), "error": str(exc)}
        )
        return False
    restore_state.update(
        {
            "state": "completed" if restored else "unavailable",
            "finished_at": _now(),
            "restored": restored,
        }
    )
    return restored


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


def _force_stop_after_timeout(
    current_process: subprocess.Popen[str], timeout: int = 45
) -> None:
    """Force-kill a crawl that does not finish its graceful shutdown."""

    try:
        current_process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if current_process.poll() is None:
            current_process.kill()


def _startup_tasks() -> None:
    """Recover state and hydrate the catalog, off the startup path."""

    try:
        _recover_stale_process_state()
    except Exception as exc:
        logging.getLogger(__name__).warning("Startup state recovery failed: %s", exc)
    _restore_runtime_checkpoint()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Nothing that touches the network may run here: the port must start
    # serving immediately, because the platform health check will not wait on
    # S3. ``_read_state`` already normalises a stale "running" state on read,
    # so persisting that recovery can happen in the background too.
    threading.Thread(target=_startup_tasks, name="startup", daemon=True).start()
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
    mode: Literal["test", "full", "enrich"] = "test"
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
    mode: Literal["test", "full", "enrich"] | None = None
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


class BulkDeleteRequest(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=500)
    delete_media: bool = False


_MODE_LABELS = {
    "test": "test run",
    "full": "full catalog crawl",
    "enrich": "saved-catalog enrichment",
}


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


def _tail(path: Path, lines: int, block_size: int = 64 * 1024) -> str:
    """Return the last ``lines`` lines without reading the whole file.

    The dashboard polls this every five seconds while a crawl runs, and a long
    crawl log does not fit comfortably in a 512 MB instance.
    """

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks: list[bytes] = []
            while position > 0 and sum(chunk.count(b"\n") for chunk in chunks) <= lines:
                read_size = min(block_size, position)
                position -= read_size
                handle.seek(position)
                chunks.append(handle.read(read_size))
            data = b"".join(reversed(chunks))
    except FileNotFoundError:
        return ""
    except OSError:
        return ""
    return "\n".join(
        data.decode("utf-8", errors="replace").splitlines()[-lines:]
    )


def _read_state() -> dict[str, Any]:
    state = _read_json(STATUS_PATH)
    if not isinstance(state, dict):
        state = _default_state()
    # A local watcher still owns a process object after the child exits and may
    # be packaging/uploading the final artifacts.  Leave the state alone during
    # that short window so the watcher can publish the real terminal state.
    # ``process is None`` identifies a genuinely stale state restored after a
    # service restart, where no watcher exists to finish it.
    if (
        state.get("state") == "running"
        and not _process_running()
        and process is None
    ):
        state["state"] = "interrupted"
        state["finished_at"] = state.get("finished_at") or _now()
        state["message"] = "The process stopped. Start again to resume from S3."
    elif (
        state.get("state") == "stopping"
        and not _process_running()
        and process is None
    ):
        state["state"] = "stopped"
        state["finished_at"] = state.get("finished_at") or _now()
        state["message"] = "Crawl stopped. The latest completed checkpoint remains available."
    return state


def _write_state(state: dict[str, Any]) -> None:
    write_json_atomic(STATUS_PATH, state)


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


def _recover_stale_process_state() -> dict[str, Any]:
    """Persist recovery from a Render restart or an orphaned stopping state."""

    with state_lock:
        original = _read_json(STATUS_PATH)
        state = _read_state()
        if isinstance(original, dict) and original.get("state") in {
            "running",
            "stopping",
        } and state.get("state") not in {"running", "stopping"}:
            _write_state(state)
            try:
                _persist_run(state)
            except Exception:
                # Local recovery must not prevent the service from starting if S3 is down.
                pass
        return state


def _require_control(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("CONTROL_TOKEN")
    if not expected:
        raise HTTPException(503, "CONTROL_TOKEN is not configured on this service.")
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
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
            # Diamond details and productVariations are server-rendered. Avoid
            # Chromium here so enrichment stays within small-instance memory.
            "SCRAPER_USE_PLAYWRIGHT": "0",
        }
    )
    if config["mode"] == "enrich":
        # A full catalog database is hundreds of MB before compression. Keep
        # enrichment restart-safe without uploading the whole snapshot after
        # every small batch.
        environment["S3_UPLOAD_EVERY"] = environment.get(
            "S3_ENRICH_UPLOAD_EVERY", "500"
        )
    proxy = environment.get("SCRAPER_PROXY_URL")
    if proxy:
        environment["HTTPS_PROXY"] = proxy
        environment["HTTP_PROXY"] = proxy
    return environment


def _prune_old_jobdirs(current_run_id: str, keep: int = 3) -> None:
    """Drop Scrapy job directories from earlier runs.

    One directory is created per run and nothing removed them, which slowly
    fills the container disk on long-lived deployments.
    """

    jobs_dir = RUNTIME_DIR / "jobs"
    if not jobs_dir.is_dir():
        return
    directories = sorted(
        (path for path in jobs_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    for path in directories[keep:]:
        if path.name == current_run_id:
            continue
        shutil.rmtree(path, ignore_errors=True)


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
    global process
    exit_code = current_process.wait()
    progress = service_progress
    summary = _read_json(EXPORT_DIR / "crawl-summary.json")
    if progress is not None:
        progress.finish(
            "crawl",
            "Crawler finished."
            if exit_code == 0
            else f"Crawler exited with code {exit_code}.",
        )
        progress.start("archive", "Packaging the downloadable catalog archive")
    archive_error = None
    try:
        build_catalog_archive(EXPORT_DIR)
    except Exception as exc:
        archive_error = str(exc)
    if progress is not None:
        if archive_error:
            progress.fail("archive", archive_error)
        else:
            progress.finish("archive", "catalog-export.zip is ready to download.")
    upload_error = _read_json(EXPORT_DIR / "s3-upload-error.json")
    successful = exit_code == 0 and not upload_error
    access_warning = bool(
        isinstance(summary, dict) and summary.get("access_blocked")
    )

    with state_lock:
        state = _read_state()
        if state.get("run_id") != run_id:
            if process is current_process:
                process = None
            return
        requested_stop = state.get("state") in {"stopping", "stopped"}
        final_state = (
            "failed"
            if upload_error
            else "stopped"
            if requested_stop
            else "completed_with_warnings"
            if successful and (access_warning or archive_error)
            else "completed"
            if successful
            else "failed"
        )
        state.update(
            {
                "state": final_state,
                "finished_at": _now(),
                "exit_code": exit_code,
                "summary": summary,
                "message": (
                    "Crawl stopped. Partial catalog data was checkpointed."
                    if final_state == "stopped"
                    else f"Crawl completed, but the local archive could not be rebuilt: {archive_error}"
                    if successful and archive_error
                    else "Crawl completed, but the source blocked one or more requests. Review diagnostics before importing."
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
        if process is current_process:
            process = None

    # Refresh the MySQL read replica. A stopped or warned run still produced
    # real rows and a real checkpoint, so anything that reached a terminal
    # state with a database on disk is worth syncing -- otherwise the dashboard
    # keeps serving the previous run's catalog.
    if final_state != "failed" and mysql_enabled() and DATABASE_PATH.exists():
        if progress is not None:
            progress.start("replica", "Copying the catalog into the dashboard database")
        with catalog_lock:
            try:
                counts = sync_from_sqlite(DATABASE_PATH)
                if progress is not None:
                    progress.finish(
                        "replica",
                        f"{counts.get('products', 0):,} products available to the dashboard.",
                    )
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Post-crawl MySQL sync failed: %s", exc
                )
                if progress is not None:
                    progress.fail("replica", f"Dashboard database not refreshed: {exc}")
    elif progress is not None:
        progress.skip(
            "replica",
            "Not configured."
            if not mysql_enabled()
            else "Skipped because the run failed.",
        )

    if progress is not None:
        progress.close_open_phases()
        progress.set_extra(state=final_state, message=state.get("message"))
        progress.flush()


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
        running = _process_running()
        state["process_running"] = running
    crawl_progress = _read_json(PROGRESS_PATH)
    crawl_progress = crawl_progress if isinstance(crawl_progress, dict) else None
    state["progress"] = crawl_progress
    state["timeline"] = _timeline(crawl_progress, state)
    state["checkpoint_restore"] = dict(restore_state)
    state["storefront"] = _storefront_state(crawl_progress, state)
    state["catalog"] = catalog_summary(DATABASE_PATH, prefer_sqlite=running)
    return state


def _service_snapshot() -> dict[str, Any] | None:
    """The service's own phases, from memory or from the last run on disk."""

    if service_progress is not None:
        return service_progress.snapshot()
    stored = _read_json(SERVICE_PROGRESS_PATH)
    return stored if isinstance(stored, dict) else None


def _timeline(
    crawl_progress: dict[str, Any] | None, state: dict[str, Any]
) -> dict[str, Any]:
    """One ordered list of every step, service and crawler alike."""

    service_snapshot = _service_snapshot()
    # Only splice in the crawler's phases when they belong to this run;
    # a stale progress.json from a previous run must not be shown as current.
    run_id = state.get("run_id")
    if (
        crawl_progress
        and run_id
        and crawl_progress.get("run_id")
        and crawl_progress.get("run_id") != run_id
    ):
        crawl_progress = None
    phases = merge_timeline(service_snapshot, crawl_progress)
    active = next(
        (phase for phase in phases if phase.get("state") == "active"), None
    )
    finished = [
        phase
        for phase in phases
        if phase.get("state") in {"done", "skipped", "failed"}
    ]
    return {
        "phases": phases,
        "current": active,
        "current_label": (active or {}).get("label"),
        "current_detail": (active or {}).get("detail") or "",
        "step": len(finished) + (1 if active else 0),
        "completed": len(finished),
        "steps": len(phases),
        "failed": [phase for phase in phases if phase.get("state") == "failed"],
    }


def _storefront_state(
    crawl_progress: dict[str, Any] | None, state: dict[str, Any]
) -> dict[str, Any] | None:
    """Latest storefront availability scan result, live or from the last run."""

    if isinstance(crawl_progress, dict) and crawl_progress.get("storefront"):
        return crawl_progress["storefront"]
    summary = state.get("summary")
    if isinstance(summary, dict) and summary.get("storefront"):
        return summary["storefront"]
    return None


@app.get(
    "/api/logs",
    dependencies=[Depends(_require_control)],
    response_class=PlainTextResponse,
)
def logs(lines: int = Query(default=250, ge=20, le=2000)) -> str:
    return _tail(LOG_PATH, lines)


@app.post("/api/start", dependencies=[Depends(_require_control)])
def start_crawl(request: StartRequest) -> dict[str, Any]:
    global process
    # Restoring the S3 checkpoint downloads and verifies the whole database.
    # ``catalog_lock`` serializes starts against each other and against catalog
    # mutations; ``state_lock`` is taken only for the short state updates.
    with catalog_lock:
        with state_lock:
            if _process_running():
                raise HTTPException(409, "A crawl is already running.")

        config = _merged_start_settings(request)
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        progress = _new_service_progress(run_id, config["mode"])
        progress.start("prepare", f"Preparing a {_MODE_LABELS[config['mode']]}")

        if config["mode"] == "enrich" and not config["resume_checkpoint"]:
            progress.fail(
                "prepare", "Enrichment needs the saved checkpoint to stay enabled."
            )
            raise HTTPException(
                409,
                "Enrichment requires Resume from checkpoint to remain enabled.",
            )
        progress.finish("prepare", "Settings accepted.")

        if config["resume_checkpoint"]:
            progress.start(
                "restore", "Downloading the saved catalog checkpoint from S3"
            )
            try:
                checkpoint_restored = download_checkpoint(EXPORT_DIR, strict=True)
            except RuntimeError as exc:
                progress.fail("restore", str(exc))
                raise HTTPException(502, str(exc)) from exc
            progress.finish(
                "restore",
                "Checkpoint restored."
                if checkpoint_restored
                else "No stored checkpoint yet; starting from the local catalog.",
            )
        else:
            progress.start("restore", "Clearing the local catalog for a fresh run")
            _clear_local_catalog()
            checkpoint_restored = False
            progress.finish("restore", "Local catalog cleared.")

        if config["mode"] == "enrich" and not DATABASE_PATH.exists():
            progress.fail("restore", "No saved catalog is available to enrich.")
            raise HTTPException(
                409,
                "Enrichment requires the existing catalog checkpoint. Enable Resume from checkpoint first.",
            )
        progress.start("launch", "Starting the crawler process")
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
            f"resume_existing={'true' if config['mode'] != 'enrich' and config['resume_checkpoint'] and DATABASE_PATH.exists() else 'false'}",
            "-a",
            f"enrichment_mode={'true' if config['mode'] == 'enrich' else 'false'}",
            "-a",
            "use_playwright=false",
        ]
        _prune_old_jobdirs(run_id)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_handle = LOG_PATH.open("w", encoding="utf-8")
        with state_lock:
            if _process_running():
                raise HTTPException(409, "A crawl is already running.")
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
            "message": (
                "Catalog enrichment is running against the saved checkpoint."
                if config["mode"] == "enrich"
                else "Crawl is running. Checkpoints are saved to S3."
            ),
        }
        progress.finish("launch", f"Crawler started (run {run_id}).")
        progress.start(
            "crawl",
            "Crawling"
            if config["mode"] != "enrich"
            else "Refreshing saved products from the live storefront",
        )
        with state_lock:
            _write_state(state)
        _persist_run(state)
        threading.Thread(
            target=_watch_process, args=(process, run_id), daemon=True
        ).start()
        return state


@app.post("/api/stop", dependencies=[Depends(_require_control)])
def stop_crawl() -> dict[str, str]:
    with state_lock:
        if not _process_running():
            state = _read_state()
            if state.get("state") in {"stopped", "interrupted"}:
                _write_state(state)
                try:
                    _persist_run(state)
                except Exception:
                    pass
                return {"status": str(state["state"])}
            return {"status": "not-running"}
        assert process is not None
        current_process = process
        state = _read_state()
        state["state"] = "stopping"
        state["message"] = "Stopping gracefully; partial data will be checkpointed."
        _write_state(state)
        _persist_run(state)
    current_process.terminate()
    threading.Thread(
        target=_force_stop_after_timeout, args=(current_process,), daemon=True
    ).start()
    return {"status": "stopping"}


@app.get("/api/catalog/summary", dependencies=[Depends(_require_control)])
def get_catalog_summary() -> dict[str, Any]:
    return catalog_summary(DATABASE_PATH, prefer_sqlite=_process_running())


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
    write_json_atomic(DISCOVERY_PATH, value)
    save_json_object(value, "admin", "catalog-discovery.json")
    return value


@app.get("/api/products", dependencies=[Depends(_require_control)])
def get_products(
    q: str = Query(default="", max_length=200),
    product_type: str = Query(default="", max_length=30),
    category_id: str = Query(default="", max_length=100),
    stock_status: str = Query(default="", max_length=30),
    coverage: str = Query(default="", max_length=40),
    storefront: str = Query(default="", max_length=20),
    sort: str = Query(default="name_asc", max_length=40),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    return list_products(
        DATABASE_PATH,
        query=q.strip(),
        product_type=product_type.strip(),
        category_id=category_id.strip(),
        stock_status=stock_status.strip(),
        coverage=coverage.strip(),
        storefront=storefront.strip(),
        sort=sort.strip(),
        page=page,
        page_size=page_size,
        prefer_sqlite=_process_running(),
    )


@app.get("/api/products/options", dependencies=[Depends(_require_control)])
def get_product_filter_options() -> dict[str, Any]:
    return product_filter_options(DATABASE_PATH, prefer_sqlite=_process_running())


def _remove_products(product_ids: list[str], delete_media: bool) -> dict[str, Any]:
    """Durably remove products from the active catalog and mutable S3 objects."""

    requested_ids = list(
        dict.fromkeys(
            str(product_id).strip()
            for product_id in product_ids
            if str(product_id).strip()
        )
    )
    if not requested_ids:
        raise HTTPException(422, "Select at least one product to delete.")
    if not DATABASE_PATH.exists():
        raise HTTPException(404, "No catalog database is available.")

    # Render mounts /tmp separately from /app/runtime, so the candidate must be
    # created beside the active database for the final atomic replace to work.
    with tempfile.TemporaryDirectory(
        prefix=".catalog-delete-", dir=DATABASE_PATH.parent
    ) as temp_dir:
        candidate = Path(temp_dir) / "catalog.sqlite"
        copy_database(DATABASE_PATH, candidate)
        deleted_products = delete_products(candidate, requested_ids)
        if not deleted_products:
            raise HTTPException(404, "None of the selected products were found.")
        try:
            checkpoint_updated = upload_database_checkpoint(candidate)
        except Exception as exc:
            raise HTTPException(
                502,
                "The products were not deleted because the S3 checkpoint could not be updated.",
            ) from exc

        DATABASE_PATH.with_name(f"{DATABASE_PATH.name}-wal").unlink(missing_ok=True)
        DATABASE_PATH.with_name(f"{DATABASE_PATH.name}-shm").unlink(missing_ok=True)
        candidate.replace(DATABASE_PATH)

    artifact_error = None
    try:
        artifact_summary = rebuild_catalog_artifacts(DATABASE_PATH, EXPORT_DIR)
    except Exception as exc:
        artifact_error = f"Catalog exports could not be rebuilt: {exc}"
        artifact_summary = {"database_counts": catalog_summary(DATABASE_PATH)}
    latest_artifacts_updated = False
    if artifact_error is None:
        try:
            latest_artifacts_updated = upload_latest_artifacts(EXPORT_DIR)
        except Exception as exc:
            artifact_error = str(exc)

    media_deleted = 0
    media_error = None
    media_paths = list(
        dict.fromkeys(
            path
            for deleted in deleted_products
            for path in deleted["media_paths"]
        )
    )
    if delete_media:
        try:
            media_deleted = delete_media_objects(
                unreferenced_media_paths(DATABASE_PATH, media_paths)
            )
        except Exception as exc:
            media_error = str(exc)

    deleted_ids = [deleted["id"] for deleted in deleted_products]
    deleted_id_set = set(deleted_ids)

    # The dashboard reads MySQL whenever it is configured, so a delete that
    # only touched SQLite would leave the products visible and counted.
    replica_error = None
    if mysql_enabled():
        try:
            delete_products_mysql(deleted_ids)
        except Exception as exc:
            replica_error = str(exc)

    return {
        "deleted": True,
        "deleted_count": len(deleted_products),
        "product_ids": deleted_ids,
        "product_names": [deleted["name"] for deleted in deleted_products],
        "not_found_ids": [
            product_id for product_id in requested_ids if product_id not in deleted_id_set
        ],
        "checkpoint_updated": checkpoint_updated,
        "latest_artifacts_updated": latest_artifacts_updated,
        "media_requested": delete_media,
        "media_deleted": media_deleted,
        "media_error": media_error,
        "artifact_error": artifact_error,
        "replica_error": replica_error,
        "catalog": artifact_summary["database_counts"],
        "historical_runs_preserved": True,
    }


@app.post(
    "/api/products/bulk-delete", dependencies=[Depends(_require_control)]
)
def remove_products(request: BulkDeleteRequest) -> dict[str, Any]:
    """Delete up to 500 selected products with one durable checkpoint update."""

    with catalog_lock:
        with state_lock:
            if _process_running():
                raise HTTPException(
                    409, "Stop the active crawl before deleting products."
                )
        return _remove_products(request.product_ids, request.delete_media)


@app.get("/api/products/{product_id}", dependencies=[Depends(_require_control)])
def get_product(product_id: str) -> dict[str, Any]:
    value = product_detail(
        DATABASE_PATH, product_id, prefer_sqlite=_process_running()
    )
    if value is None:
        raise HTTPException(404, "Product not found.")
    return value


@app.delete("/api/products/{product_id}", dependencies=[Depends(_require_control)])
def remove_product(
    product_id: str,
    delete_media: bool = Query(default=False),
) -> dict[str, Any]:
    """Delete one active product and durably replace mutable S3 artifacts."""

    with catalog_lock:
        with state_lock:
            if _process_running():
                raise HTTPException(
                    409, "Stop the active crawl before deleting products."
                )
        result = _remove_products([product_id], delete_media)
    result["product_id"] = result["product_ids"][0]
    result["product_name"] = result["product_names"][0]
    return result


@app.get("/api/diagnostics", dependencies=[Depends(_require_control)])
def get_diagnostics(limit: int = Query(default=100, ge=1, le=1000)):
    return {
        "items": diagnostics(
            DATABASE_PATH, limit=limit, prefer_sqlite=_process_running()
        )
    }


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


@app.get("/api/download-master", dependencies=[Depends(_require_control)])
def download_master():
    """Build one current WooCommerce CSV from the restored SQLite checkpoint."""

    # Building the master CSV walks every product three times. Serialize it
    # against other catalog mutations, but never behind ``state_lock`` -- that
    # would stall the dashboard status poll for the whole export.
    with catalog_lock:
        with state_lock:
            if _process_running():
                raise HTTPException(
                    409,
                    "Wait for the active crawl to stop before building the master export.",
                )
        if not DATABASE_PATH.exists():
            raise HTTPException(404, "No catalog checkpoint is available.")

        connection = sqlite3.connect(
            f"file:{DATABASE_PATH}?mode=ro", uri=True
        )
        try:
            try:
                counts = export_woocommerce_csvs(
                    connection,
                    EXPORT_DIR,
                    public_base_url=os.getenv("S3_PUBLIC_BASE_URL"),
                )
            except WooCommerceExportValidationError as exc:
                raise HTTPException(
                    422,
                    {
                        "message": "Master CSV failed WooCommerce preflight.",
                        "errors": exc.errors[:100],
                        "error_count": len(exc.errors),
                    },
                ) from exc
        finally:
            connection.close()

    target = EXPORT_DIR / MASTER_FILENAME
    return FileResponse(
        target,
        filename=MASTER_FILENAME,
        media_type="text/csv",
        headers={
            "X-WooCommerce-Parent-Rows": str(counts["parent_rows"]),
            "X-WooCommerce-Variation-Rows": str(counts["variation_rows"]),
            "X-WooCommerce-Master-Rows": str(counts["master_rows"]),
        },
    )


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
