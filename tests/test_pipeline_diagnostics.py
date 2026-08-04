from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from lgd_scraper.pipelines import CatalogWriterPipeline


def _pipeline(output_dir):
    pipeline = CatalogWriterPipeline()
    pipeline.crawler = SimpleNamespace(
        spider=SimpleNamespace(output_dir=str(output_dir), products_scheduled=0)
    )
    return pipeline


def _close_pipeline(pipeline):
    assert pipeline.connection is not None
    pipeline.connection.close()
    for handle in pipeline.handles.values():
        handle.close()


def test_opening_new_run_clears_historical_diagnostics(tmp_path):
    database = tmp_path / "catalog.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE diagnostics (created_at TEXT, kind TEXT, url TEXT, "
        "status INTEGER, message TEXT, raw_json TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO diagnostics VALUES (?, ?, ?, ?, ?, ?)",
        ("yesterday", "old", "https://old.test", 404, "Old run", "{}"),
    )
    connection.commit()
    connection.close()
    (tmp_path / "diagnostics.jsonl").write_text(
        json.dumps({"kind": "old"}) + "\n", encoding="utf-8"
    )

    pipeline = _pipeline(tmp_path)
    pipeline.open_spider()

    assert pipeline.connection.execute(
        "SELECT COUNT(*) FROM diagnostics"
    ).fetchone()[0] == 0
    assert (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8") == ""
    _close_pipeline(pipeline)


def test_identical_diagnostics_are_written_once_per_run(tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.open_spider()
    diagnostic = {
        "_record_type": "diagnostic",
        "kind": "request_failed",
        "url": "https://example.test/product/ring/",
        "status": 500,
        "message": "Temporary failure",
    }

    pipeline.process_item(dict(diagnostic))
    pipeline.process_item(dict(diagnostic))

    assert pipeline.counts["diagnostic"] == 1
    assert pipeline.connection.execute(
        "SELECT COUNT(*) FROM diagnostics"
    ).fetchone()[0] == 1
    assert len(
        (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8").splitlines()
    ) == 1
    _close_pipeline(pipeline)
