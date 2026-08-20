"""Named, ordered phases for a run, written to disk for the dashboard.

The admin service and the Scrapy child process each own one tracker file:
``runtime/status.json`` for the service, ``runtime/export/progress.json`` for
the crawl.  ``/api/status`` merges them into one timeline, so the dashboard can
say what is happening right now instead of only "running".

Two processes never write the same file, so no locking is involved.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


PENDING = "pending"
ACTIVE = "active"
DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"


# Phase catalogues. ``key`` is stable and machine-readable; ``label`` is what
# the dashboard shows.
SERVICE_PHASES: list[tuple[str, str]] = [
    ("prepare", "Validating run settings"),
    ("restore", "Restoring catalog checkpoint"),
    ("launch", "Starting the crawler"),
    ("crawl", "Crawling"),
    ("archive", "Building the downloadable archive"),
    ("replica", "Refreshing the dashboard database"),
]

# Image downloads run concurrently with product collection rather than after
# it, so they are reported as live counters instead of a sequential phase.
CRAWL_PHASES: list[tuple[str, str]] = [
    ("discover", "Reading WooCommerce catalog APIs"),
    ("products", "Collecting products, categories and images"),
    ("export", "Writing catalog exports"),
    ("upload", "Uploading artifacts to S3"),
]

ENRICH_PHASES: list[tuple[str, str]] = [
    ("storefront_scan", "Checking which products are live on the storefront"),
    ("enrich", "Reading live product pages"),
    ("export", "Writing catalog exports"),
    ("upload", "Uploading artifacts to S3"),
]


def phases_for_mode(mode: str) -> list[tuple[str, str]]:
    return ENRICH_PHASES if mode == "enrich" else CRAWL_PHASES


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PhaseTracker:
    """An ordered list of phases persisted as JSON after every change.

    Writes are throttled so a per-item ``update`` call stays cheap; ``start``,
    ``finish``, ``skip`` and ``fail`` always flush immediately because they are
    the transitions a watching dashboard must not miss.
    """

    def __init__(
        self,
        path: Path | str,
        phases: Iterable[tuple[str, str]],
        *,
        run_id: str | None = None,
        mode: str | None = None,
        min_write_interval: float = 1.0,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.mode = mode
        self.min_write_interval = min_write_interval
        self.extra: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._phases: list[dict[str, Any]] = [
            {
                "key": key,
                "label": label,
                "state": PENDING,
                "detail": "",
                "processed": None,
                "total": None,
                "started_at": None,
                "finished_at": None,
            }
            for key, label in phases
        ]

    # -- mutation ---------------------------------------------------------

    def _find(self, key: str) -> dict[str, Any] | None:
        return next((phase for phase in self._phases if phase["key"] == key), None)

    def start(self, key: str, detail: str = "", total: int | None = None) -> None:
        with self._lock:
            phase = self._find(key)
            if phase is None:
                return
            # Anything still pending before an started phase was not reached.
            for earlier in self._phases:
                if earlier is phase:
                    break
                if earlier["state"] == ACTIVE:
                    earlier["state"] = DONE
                    earlier["finished_at"] = earlier["finished_at"] or _now()
            phase["state"] = ACTIVE
            phase["started_at"] = phase["started_at"] or _now()
            if detail:
                phase["detail"] = detail
            if total is not None:
                phase["total"] = total
        self._write(force=True)

    def update(
        self,
        key: str,
        *,
        detail: str | None = None,
        processed: int | None = None,
        total: int | None = None,
    ) -> None:
        with self._lock:
            phase = self._find(key)
            if phase is None:
                return
            if phase["state"] == PENDING:
                phase["state"] = ACTIVE
                phase["started_at"] = phase["started_at"] or _now()
            if detail is not None:
                phase["detail"] = detail
            if processed is not None:
                phase["processed"] = processed
            if total is not None:
                phase["total"] = total
        self._write()

    def finish(self, key: str, detail: str = "") -> None:
        with self._lock:
            phase = self._find(key)
            if phase is None:
                return
            phase["state"] = DONE
            phase["finished_at"] = _now()
            phase["started_at"] = phase["started_at"] or phase["finished_at"]
            if detail:
                phase["detail"] = detail
            if phase["total"] is not None and phase["processed"] is None:
                phase["processed"] = phase["total"]
        self._write(force=True)

    def skip(self, key: str, detail: str = "") -> None:
        with self._lock:
            phase = self._find(key)
            if phase is None:
                return
            phase["state"] = SKIPPED
            phase["detail"] = detail or phase["detail"]
            phase["finished_at"] = _now()
        self._write(force=True)

    def fail(self, key: str, detail: str = "") -> None:
        with self._lock:
            phase = self._find(key)
            if phase is None:
                return
            phase["state"] = FAILED
            phase["detail"] = detail or phase["detail"]
            phase["finished_at"] = _now()
        self._write(force=True)

    def set_extra(self, **values: Any) -> None:
        """Merge extra fields into the snapshot. Throttled: call ``flush`` for
        anything a reader must see immediately, such as final counts."""

        with self._lock:
            self.extra.update(values)
        self._write()

    def flush(self) -> None:
        """Write the current snapshot regardless of the throttle."""

        self._write(force=True)

    def close_open_phases(self) -> None:
        """Mark whatever is still running as done. Called when a run ends."""

        with self._lock:
            for phase in self._phases:
                if phase["state"] == ACTIVE:
                    phase["state"] = DONE
                    phase["finished_at"] = _now()
        self._write(force=True)

    # -- output -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        active = next(
            (phase for phase in self._phases if phase["state"] == ACTIVE), None
        )
        completed = sum(
            1 for phase in self._phases if phase["state"] in {DONE, SKIPPED, FAILED}
        )
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "updated_at": _now(),
            "phase": active["key"] if active else None,
            "phase_label": active["label"] if active else None,
            "phase_detail": active["detail"] if active else "",
            "phase_index": completed + (1 if active else 0),
            "phase_total": len(self._phases),
            "phases": [dict(phase) for phase in self._phases],
            **self.extra,
        }

    def _write(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_write < self.min_write_interval:
                return
            self._last_write = now
            payload = self._snapshot_locked()
        write_json_atomic(self.path, payload)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Replace ``path`` in one step so a reader never sees a partial file."""

    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError:
        # Progress reporting must never break the run that is reporting.
        pass


def merge_timeline(
    service_snapshot: dict[str, Any] | None,
    crawl_snapshot: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Splice the crawl's phases into the service's ``crawl`` placeholder."""

    service_phases = list((service_snapshot or {}).get("phases") or [])
    crawl_phases = list((crawl_snapshot or {}).get("phases") or [])
    if not service_phases:
        return crawl_phases
    if not crawl_phases:
        return service_phases

    timeline: list[dict[str, Any]] = []
    for phase in service_phases:
        if phase.get("key") != "crawl":
            timeline.append(phase)
            continue
        # The crawl placeholder is replaced by the child's real phases, but only
        # once the child has actually started reporting them.
        timeline.extend(crawl_phases)
    return timeline
