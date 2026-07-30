from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from lgd_scraper.s3sync import download_checkpoint, presigned_artifact_url


PROJECT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", PROJECT_DIR / "runtime")).resolve()
EXPORT_DIR = RUNTIME_DIR / "export"
LOG_PATH = RUNTIME_DIR / "crawl.log"
STATUS_PATH = RUNTIME_DIR / "status.json"

RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Jewelry Product Scraper", version="0.2.0")
state_lock = threading.Lock()
process: subprocess.Popen[str] | None = None


class StartRequest(BaseModel):
    max_products: int = Field(default=0, ge=0, le=100_000)


def _default_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "max_products": None,
        "checkpoint_restored": False,
        "summary": None,
        "message": "Ready to start.",
    }


def _read_state() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return _default_state()
    try:
        state = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_state()
    if state.get("state") == "running" and not _process_running():
        state["state"] = "interrupted"
        state["message"] = "The previous process stopped. Start again to resume from S3."
    return state


def _write_state(state: dict[str, Any]) -> None:
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(STATUS_PATH)


def _process_running() -> bool:
    return process is not None and process.poll() is None


def _require_control(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("CONTROL_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="CONTROL_TOKEN is not configured on this service.",
        )
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid control token.")


def _configure_crawl_environment() -> dict[str, str]:
    environment = dict(os.environ)
    bucket = environment.get("S3_BUCKET")
    if bucket and not environment.get("SCRAPER_MEDIA_STORE"):
        prefix = environment.get(
            "S3_PREFIX", "jewelry-product-scraper"
        ).strip("/")
        environment["SCRAPER_MEDIA_STORE"] = (
            f"s3://{bucket}/{prefix}/media/"
        )
    environment["SCRAPER_JOBDIR"] = str(
        RUNTIME_DIR
        / "jobs"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    environment["SCRAPER_LOG_LEVEL"] = environment.get(
        "SCRAPER_LOG_LEVEL", "INFO"
    )
    return environment


def _watch_process(current_process: subprocess.Popen[str]) -> None:
    exit_code = current_process.wait()
    summary_path = EXPORT_DIR / "crawl-summary.json"
    summary = None
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = None

    archive = EXPORT_DIR / "catalog-export.zip"
    archive.unlink(missing_ok=True)
    shutil.make_archive(
        str(archive.with_suffix("")),
        "zip",
        root_dir=EXPORT_DIR,
    )
    upload_error_path = EXPORT_DIR / "s3-upload-error.json"
    upload_error = None
    if upload_error_path.exists():
        try:
            upload_error = json.loads(
                upload_error_path.read_text(encoding="utf-8")
            ).get("error")
        except (json.JSONDecodeError, OSError):
            upload_error = "S3 upload failed."
    successful = exit_code == 0 and not upload_error

    with state_lock:
        state = _read_state()
        state.update(
            {
                "state": "completed" if successful else "failed",
                "finished_at": datetime.now(UTC).isoformat(),
                "exit_code": exit_code,
                "summary": summary,
                "message": (
                    "Crawl completed. Download the WooCommerce export."
                    if successful
                    else (
                        f"S3 upload failed: {upload_error}"
                        if upload_error
                        else "Crawl failed. Review the log."
                    )
                ),
            }
        )
        _write_state(state)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jewelry Product Scraper</title>
  <style>
    body { font: 16px/1.5 system-ui, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; color: #18212f; }
    input, button { font: inherit; padding: 10px 12px; margin: 4px; }
    input[type=password] { min-width: 320px; }
    button { cursor: pointer; }
    pre { background: #0b1220; color: #d8e5ff; padding: 16px; overflow: auto; border-radius: 8px; min-height: 160px; }
    .warning { background: #fff4d6; padding: 12px 16px; border-radius: 8px; }
  </style>
</head>
<body>
  <h1>Jewelry Product Scraper</h1>
  <p class="warning">Render Free can stop an idle service. Keep this page open while a crawl is running. S3 checkpoints preserve normalized product data.</p>
  <p>
    <input id="token" type="password" placeholder="CONTROL_TOKEN">
    <input id="limit" type="number" min="0" value="5" title="0 means full catalog">
  </p>
  <p>
    <button onclick="startCrawl()">Start crawl</button>
    <button onclick="stopCrawl()">Stop</button>
    <button onclick="downloadExport()">Download export</button>
  </p>
  <pre id="status">Enter the control token to view status.</pre>
  <pre id="logs"></pre>
  <script>
    const tokenInput = document.getElementById('token');
    tokenInput.value = sessionStorage.getItem('controlToken') || '';
    function headers() {
      const token = tokenInput.value.trim();
      sessionStorage.setItem('controlToken', token);
      return {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json'};
    }
    async function api(path, options={}) {
      options.headers = {...headers(), ...(options.headers || {})};
      const response = await fetch(path, options);
      if (!response.ok) throw new Error(await response.text());
      return response;
    }
    async function refresh() {
      if (!tokenInput.value.trim()) return;
      try {
        const status = await (await api('/api/status')).json();
        document.getElementById('status').textContent = JSON.stringify(status, null, 2);
        const logs = await (await api('/api/logs')).text();
        document.getElementById('logs').textContent = logs;
      } catch (error) {
        document.getElementById('status').textContent = String(error);
      }
    }
    async function startCrawl() {
      const max_products = Number(document.getElementById('limit').value || 0);
      await api('/api/start', {method: 'POST', body: JSON.stringify({max_products})});
      refresh();
    }
    async function stopCrawl() {
      await api('/api/stop', {method: 'POST'});
      refresh();
    }
    async function downloadExport() {
      const response = await api('/api/download');
      const data = await response.json();
      if (data.url.startsWith('/')) {
        const file = await api(data.url);
        const blobUrl = URL.createObjectURL(await file.blob());
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = 'catalog-export.zip';
        link.click();
        URL.revokeObjectURL(blobUrl);
      } else {
        window.location.href = data.url;
      }
    }
    tokenInput.addEventListener('change', refresh);
    setInterval(refresh, 10000);
    refresh();
  </script>
</body>
</html>
"""


@app.get("/api/status", dependencies=[Depends(_require_control)])
def status() -> dict[str, Any]:
    with state_lock:
        state = _read_state()
        state["process_running"] = _process_running()
        return state


@app.get(
    "/api/logs",
    dependencies=[Depends(_require_control)],
    response_class=PlainTextResponse,
)
def logs() -> str:
    if not LOG_PATH.exists():
        return ""
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-200:])


@app.post("/api/start", dependencies=[Depends(_require_control)])
def start_crawl(request: StartRequest) -> dict[str, Any]:
    global process
    with state_lock:
        if _process_running():
            raise HTTPException(status_code=409, detail="A crawl is already running.")

        checkpoint_restored = download_checkpoint(EXPORT_DIR)
        (EXPORT_DIR / "s3-upload-error.json").unlink(missing_ok=True)
        environment = _configure_crawl_environment()
        command = [
            sys.executable,
            "-m",
            "scrapy",
            "crawl",
            "catalog",
            "-a",
            f"output_dir={EXPORT_DIR}",
            "-a",
            f"max_products={request.max_products}",
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
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "exit_code": None,
            "max_products": request.max_products,
            "checkpoint_restored": checkpoint_restored,
            "summary": None,
            "message": "Crawl is running. Keep this page open on Render Free.",
        }
        _write_state(state)
        threading.Thread(
            target=_watch_process, args=(process,), daemon=True
        ).start()
        return state


@app.post("/api/stop", dependencies=[Depends(_require_control)])
def stop_crawl() -> dict[str, str]:
    if not _process_running():
        return {"status": "not-running"}
    assert process is not None
    process.terminate()
    return {"status": "stopping"}


@app.get("/api/download", dependencies=[Depends(_require_control)])
def download() -> dict[str, str]:
    url = presigned_artifact_url()
    if url:
        return {"url": url}
    archive = EXPORT_DIR / "catalog-export.zip"
    if not archive.exists():
        raise HTTPException(status_code=404, detail="No export is available.")
    return {"url": "/api/download-local"}


@app.get("/api/download-local", dependencies=[Depends(_require_control)])
def download_local():
    archive = EXPORT_DIR / "catalog-export.zip"
    if not archive.exists():
        raise HTTPException(status_code=404, detail="No export is available.")
    return FileResponse(archive, filename="catalog-export.zip")
