import { useEffect, useState } from "react";

import { api } from "../api.js";
import { Icon } from "../icons.jsx";
import { Button, EmptyState, Field, StatusMark, formatDate, formatNumber } from "../components/Ui.jsx";

export function RunsPage({ status, settings, logs, onStart, onStop, onError }) {
  const [runs, setRuns] = useState([]);
  const [config, setConfig] = useState(settings?.crawl || {});
  const [selected, setSelected] = useState(status || null);
  const [discovery, setDiscovery] = useState({ total_products: 0, woocommerce_products: 0, categories: [] });
  const [discovering, setDiscovering] = useState(false);
  const active = status?.state === "running" || status?.state === "stopping";
  const displayedConfig = active && status?.config ? status.config : config;
  useEffect(() => setConfig(settings?.crawl || {}), [settings]);
  useEffect(() => { api("/api/runs").then((value) => setRuns(value.items || [])).catch(onError); }, [status?.state, onError]);
  useEffect(() => { api("/api/discovery").then(setDiscovery).catch(onError); }, [onError]);
  useEffect(() => { if (status?.run_id) setSelected(status); }, [status]);

  function update(key, value) { setConfig((current) => ({ ...current, [key]: value })); }
  function toggleCategory(categoryId) {
    setConfig((current) => {
      const selectedIds = new Set(current.category_ids || []);
      if (selectedIds.has(categoryId)) selectedIds.delete(categoryId);
      else selectedIds.add(categoryId);
      return { ...current, category_ids: [...selectedIds] };
    });
  }
  async function refreshDiscovery() {
    setDiscovering(true);
    try {
      const value = await api("/api/discovery/refresh", {
        method: "POST",
        body: JSON.stringify({ base_url: config.base_url }),
      });
      setDiscovery(value);
    } catch (error) { onError(error); } finally { setDiscovering(false); }
  }
  const selectedIds = new Set(displayedConfig.category_ids || []);
  const selectedCount = (discovery.categories || []).reduce((total, category) => total + (selectedIds.has(category.id) ? Number(category.count || 0) : 0), 0);

  return <main className="page">
    <header className="page-header"><div><h1>Crawl runs</h1><p>Build the catalog, then enrich its saved products with full page details and variation galleries.</p></div><Button icon="play" tone="primary" disabled={active} onClick={() => onStart(config)}>Start new crawl</Button></header>
    <section className="run-config panel">
      <div className="run-config__grid">
        <Field label="Source URL"><input disabled={active} value={displayedConfig.base_url || ""} onChange={(event) => update("base_url", event.target.value)} /></Field>
        <Field label="Crawl mode"><select disabled={active} value={displayedConfig.mode || "test"} onChange={(event) => update("mode", event.target.value)}><option value="test">Test run</option><option value="full">Full WooCommerce catalog</option><option value="enrich">Enrich saved catalog</option></select></Field>
        <Field label="Product limit" hint="0 means no limit"><input type="number" min="0" disabled={active || displayedConfig.mode === "full"} value={displayedConfig.mode === "full" ? 0 : displayedConfig.max_products ?? 5} onChange={(event) => update("max_products", Number(event.target.value))} /></Field>
        <Field label="Concurrency"><input disabled={active} type="number" min="1" max="16" value={displayedConfig.concurrency ?? 2} onChange={(event) => update("concurrency", Number(event.target.value))} /></Field>
        <Field label="Download delay (seconds)"><input disabled={active} type="number" min="0.1" step="0.1" value={displayedConfig.download_delay ?? 1} onChange={(event) => update("download_delay", Number(event.target.value))} /></Field>
        <Field label="Request timeout"><input disabled={active} type="number" min="5" value={displayedConfig.download_timeout ?? 45} onChange={(event) => update("download_timeout", Number(event.target.value))} /></Field>
      </div>
      {displayedConfig.mode === "enrich" ? <div className="security-notice"><Icon name="check" /><span><strong>Checkpoint enrichment.</strong> Reads the existing S3 catalog, adds dynamic diamond specifications and complete per-metal galleries, then updates the same master export. Keep Resume from checkpoint enabled.</span></div> : null}
      <div className="toggle-row">
        {["obey_robots", "enrich_html", "download_media", "resume_checkpoint"].map((key) => <label key={key}><input disabled={active} type="checkbox" checked={Boolean(displayedConfig[key])} onChange={(event) => update(key, event.target.checked)} /><span>{key.split("_").map((word) => word[0].toUpperCase() + word.slice(1)).join(" ")}</span></label>)}
      </div>
      {active ? <div className="security-notice"><Icon name="alert" /><span><strong>Showing the active run configuration.</strong> Stop or finish this crawl before preparing different settings.</span></div> : null}
    </section>
    <section className="catalog-discovery panel">
      <div className="panel-header catalog-discovery__header">
        <div><h2>Inventory discovery</h2><span>Separate WooCommerce products from the site's independently served loose-diamond inventory.</span></div>
        <Button disabled={active || discovering} onClick={refreshDiscovery}>{discovering ? "Counting…" : "Refresh inventory counts"}</Button>
      </div>
      {discovery.discovered_at ? <>
        <div className="catalog-discovery__summary">
          <div><small>WooCommerce catalog</small><strong>{formatNumber(discovery.woocommerce_products || discovery.total_products)}</strong><span>products</span></div>
          <div><small>Loose-diamond inventory</small><strong>{discovery.advertised_diamond_inventory_label || "Unavailable"}</strong><span>{discovery.advertised_diamond_inventory ? "advertised" : "separate feed"}</span></div>
          <div><small>Categories found</small><strong>{formatNumber(discovery.total_categories)}</strong><span>categories</span></div>
          <div><small>Selected Woo scope</small><strong>{selectedIds.size ? formatNumber(selectedCount) : formatNumber(discovery.woocommerce_products || discovery.total_products)}</strong><span>{selectedIds.size ? "category assignments" : "all Woo products"}</span></div>
          <button type="button" disabled={!selectedIds.size} onClick={() => update("category_ids", [])}>Clear selection</button>
        </div>
        <div className="category-picker" aria-label="Catalog categories">
          {(discovery.categories || []).map((category) => <label key={category.id}>
            <input type="checkbox" disabled={active} checked={selectedIds.has(category.id)} onChange={() => toggleCategory(category.id)} />
            <span><strong>{category.path || category.name}</strong><small>{formatNumber(category.count)} products</small></span>
          </label>)}
        </div>
        <p className="catalog-discovery__note">Category counts apply only to WooCommerce products and can overlap. Loose diamonds come from a separate dynamic inventory feed and are not included in this category crawl.</p>
      </> : <EmptyState icon="runs" title="Inventory not counted yet" body="Run discovery to count WooCommerce products, read the loose-diamond inventory claim, and load category-level counts without downloading product details or images." />}
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
  return <aside className="run-inspector"><header><div><h2>Run inspector</h2><small>{run.run_id}</small></div><StatusMark state={run.state || "unknown"} /></header><div className="run-inspector__body"><dl className="metadata-grid"><div><dt>Started</dt><dd>{formatDate(run.started_at)}</dd></div><div><dt>Finished</dt><dd>{formatDate(run.finished_at)}</dd></div><div><dt>Mode</dt><dd>{config.mode || "—"}</dd></div><div><dt>Limit</dt><dd>{config.max_products || "Full"}</dd></div><div><dt>Categories</dt><dd>{config.category_ids?.length ? `${config.category_ids.length} selected` : "All"}</dd></div></dl><section><h3>Data coverage</h3><div className="mini-coverage">{["products", "variations", "images", "diagnostics"].map((key) => <div key={key}><small>{key}</small><strong>{formatNumber(counts[key])}</strong></div>)}</div></section>{isCurrent && (run.state === "running" || run.state === "stopping") ? <Button icon="stop" tone="danger" onClick={onStop}>Stop run</Button> : null}<section><h3>Live log</h3><pre className="live-log">{isCurrent ? logs || "Waiting for crawler output…" : "Historical logs are included in the run export."}</pre></section></div></aside>;
}
