import { useEffect, useState } from "react";

import { api } from "../api.js";
import { Icon } from "../icons.jsx";
import { Button, EmptyState, Field, StatusMark, formatDate, formatNumber } from "../components/Ui.jsx";

export function RunsPage({ status, settings, logs, onStart, onStop, onError }) {
  const [runs, setRuns] = useState([]);
  const [config, setConfig] = useState(settings?.crawl || {});
  const [selected, setSelected] = useState(status || null);
  useEffect(() => setConfig(settings?.crawl || {}), [settings]);
  useEffect(() => { api("/api/runs").then((value) => setRuns(value.items || [])).catch(onError); }, [status?.state, onError]);
  useEffect(() => { if (status?.run_id) setSelected(status); }, [status]);

  function update(key, value) { setConfig((current) => ({ ...current, [key]: value })); }
  const active = status?.state === "running" || status?.state === "stopping";

  return <main className="page">
    <header className="page-header"><div><h1>Crawl runs</h1><p>Configure, resume, and audit full or test catalog runs.</p></div><Button icon="play" tone="primary" disabled={active} onClick={() => onStart(config)}>Start new crawl</Button></header>
    <section className="run-config panel">
      <div className="run-config__grid">
        <Field label="Source URL"><input value={config.base_url || ""} onChange={(event) => update("base_url", event.target.value)} /></Field>
        <Field label="Crawl mode"><select value={config.mode || "test"} onChange={(event) => update("mode", event.target.value)}><option value="test">Test run</option><option value="full">Full catalog</option></select></Field>
        <Field label="Product limit" hint="0 means no limit"><input type="number" min="0" disabled={config.mode === "full"} value={config.mode === "full" ? 0 : config.max_products ?? 5} onChange={(event) => update("max_products", Number(event.target.value))} /></Field>
        <Field label="Concurrency"><input type="number" min="1" max="16" value={config.concurrency ?? 2} onChange={(event) => update("concurrency", Number(event.target.value))} /></Field>
        <Field label="Download delay (seconds)"><input type="number" min="0.1" step="0.1" value={config.download_delay ?? 1} onChange={(event) => update("download_delay", Number(event.target.value))} /></Field>
        <Field label="Request timeout"><input type="number" min="5" value={config.download_timeout ?? 45} onChange={(event) => update("download_timeout", Number(event.target.value))} /></Field>
      </div>
      <div className="toggle-row">
        {["obey_robots", "enrich_html", "download_media", "resume_checkpoint"].map((key) => <label key={key}><input type="checkbox" checked={Boolean(config[key])} onChange={(event) => update(key, event.target.checked)} /><span>{key.split("_").map((word) => word[0].toUpperCase() + word.slice(1)).join(" ")}</span></label>)}
      </div>
    </section>
    <div className="runs-layout">
      <section className="panel panel--table"><div className="panel-header"><h2>Run history</h2><span>{runs.length} recorded runs</span></div>{runs.length ? <div className="table-scroll"><table><thead><tr><th>Started</th><th>State</th><th>Products</th><th>Variations</th><th>Images</th><th>Diagnostics</th><th>Checkpoint</th><th /></tr></thead><tbody>{runs.map((run) => { const counts = run.summary?.database_counts || {}; return <tr key={run.run_id} onClick={() => setSelected(run)} className="clickable-row"><td>{formatDate(run.started_at)}</td><td><StatusMark state={run.state || "unknown"} /></td><td>{formatNumber(counts.products)}</td><td>{formatNumber(counts.variations)}</td><td>{formatNumber(counts.images)}</td><td>{formatNumber(counts.diagnostics)}</td><td>{run.checkpoint_restored ? "Restored" : "Fresh"}</td><td><Icon name="chevron" /></td></tr>; })}</tbody></table></div> : <EmptyState icon="runs" title="No run history" body="Completed and interrupted runs persisted to S3 appear here." />}</section>
      <RunInspector run={selected} current={status} logs={logs} onStop={onStop} />
    </div>
  </main>;
}

function RunInspector({ run, current, logs, onStop }) {
  if (!run) return <aside className="run-inspector"><EmptyState icon="runs" title="Select a run" body="Choose a row to inspect its coverage, settings, and logs." /></aside>;
  const isCurrent = run.run_id === current?.run_id;
  const counts = (isCurrent ? current?.catalog : run.summary?.database_counts) || {};
  const config = run.config || {};
  return <aside className="run-inspector"><header><div><h2>Run inspector</h2><small>{run.run_id}</small></div><StatusMark state={run.state || "unknown"} /></header><div className="run-inspector__body"><dl className="metadata-grid"><div><dt>Started</dt><dd>{formatDate(run.started_at)}</dd></div><div><dt>Finished</dt><dd>{formatDate(run.finished_at)}</dd></div><div><dt>Mode</dt><dd>{config.mode || "—"}</dd></div><div><dt>Limit</dt><dd>{config.max_products || "Full"}</dd></div></dl><section><h3>Data coverage</h3><div className="mini-coverage">{["products", "variations", "images", "diagnostics"].map((key) => <div key={key}><small>{key}</small><strong>{formatNumber(counts[key])}</strong></div>)}</div></section>{isCurrent && (run.state === "running" || run.state === "stopping") ? <Button icon="stop" tone="danger" onClick={onStop}>Stop run</Button> : null}<section><h3>Live log</h3><pre className="live-log">{isCurrent ? logs || "Waiting for crawler output…" : "Historical logs are included in the run export."}</pre></section></div></aside>;
}
