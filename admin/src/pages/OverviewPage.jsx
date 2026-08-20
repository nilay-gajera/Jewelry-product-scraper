import { useEffect, useMemo, useState } from "react";

import { Icon } from "../icons.jsx";
import { Button, EmptyState, Field, StatusMark, formatDate, formatNumber } from "../components/Ui.jsx";
import { CurrentStep, RunTimeline, StorefrontPanel } from "../components/RunTimeline.jsx";

const counters = [
  ["products", "products"],
  ["products", "variations"],
  ["image", "images"],
  ["products", "categories"],
  ["tag", "attributes"],
  ["alert", "diagnostics"],
];

export function OverviewPage({ status, settings, products, logs, busy, onStart, onStop, onDownload, onNavigate }) {
  const [config, setConfig] = useState(settings?.crawl || {});
  useEffect(() => setConfig(settings?.crawl || {}), [settings]);

  const catalog = status?.catalog || {};
  const running = status?.state === "running" || status?.state === "stopping";
  const activeConfig = running && status?.config ? status.config : config;
  const currentProducts = status?.progress?.records_seen?.product ?? status?.summary?.records_seen_this_run?.product ?? 0;
  // The progress row describes the run, so it reads the run's own config --
  // the form above it may already show different, unsaved settings.
  const runConfig = status?.config || activeConfig;
  const limit = runConfig.mode === "full" ? 0 : Number(runConfig.max_products || 0);
  const progress = limit ? Math.min(100, Math.round((currentProducts / limit) * 100)) : 0;
  const logLines = useMemo(() => (logs || "").split("\n").filter(Boolean).slice(-7).reverse(), [logs]);
  const quality = catalog.quality || {};
  const enrichment = catalog.enrichment || {};
  const timeline = status?.timeline;
  const restore = status?.checkpoint_restore;
  const media = status?.progress || {};
  // Prefer the product limit when there is one, otherwise fall back to how far
  // through the run's named steps we are.
  const stepPercent = limit
    ? progress
    : timeline?.steps
      ? Math.round((timeline.completed / timeline.steps) * 100)
      : null;

  function update(key, value) {
    setConfig((current) => ({ ...current, [key]: value }));
  }

  function updateMode(mode) {
    setConfig((current) => ({
      ...current,
      mode,
      resume_checkpoint: mode === "enrich" ? true : current.resume_checkpoint,
    }));
  }

  return (
    <main className="page">
      <header className="page-header">
        <div><h1>Catalog crawl</h1><p>Control and audit the WooCommerce jewelry catalog scraper.</p></div>
        <div className="page-actions">
          <Button icon="play" tone="primary" disabled={running || busy} onClick={() => onStart(config)}>Start crawl</Button>
          <Button icon="stop" disabled={!running || busy} onClick={onStop}>Stop</Button>
          <Button icon="download" disabled={busy} onClick={onDownload}>Download export</Button>
        </div>
      </header>

      <section className="control-band">
        <div className="control-grid">
          <Field label="Source URL"><input value={activeConfig.base_url || ""} disabled={running} onChange={(event) => update("base_url", event.target.value)} /></Field>
          <Field label="Product limit" hint="0 means full catalog"><input type="number" min="0" value={activeConfig.mode === "full" ? 0 : activeConfig.max_products ?? 5} disabled={running || activeConfig.mode === "full"} onChange={(event) => update("max_products", Number(event.target.value))} /></Field>
          <Field label="Mode"><select value={activeConfig.mode || "test"} disabled={running} onChange={(event) => updateMode(event.target.value)}><option value="test">Test run</option><option value="full">Full WooCommerce catalog</option><option value="enrich">Enrich saved catalog</option></select></Field>
          <Field label="Resume from checkpoint"><select value={activeConfig.resume_checkpoint ? "yes" : "no"} disabled={running || activeConfig.mode === "enrich"} onChange={(event) => update("resume_checkpoint", event.target.value === "yes")}><option value="yes">Latest checkpoint</option><option value="no">Start fresh</option></select></Field>
          <div className="run-state">
            <StatusMark state={status?.state || "idle"} />
            <small>{status?.message || "Ready to start."}</small>
            <small>{status?.started_at ? `Started ${formatDate(status.started_at)}` : "No active run"}</small>
          </div>
        </div>
        <div className="progress-row">
          <strong>Progress</strong>
          <span className="progress-value">{stepPercent == null ? (running ? "Running" : "—") : `${stepPercent}%`}</span>
          <div className="progress-track"><span style={{ width: `${stepPercent ?? (running ? 16 : 0)}%` }} /></div>
          <span>{formatNumber(currentProducts)}{limit ? ` / ${formatNumber(limit)}` : " products"}</span>
          <button className="text-button" onClick={() => onNavigate("runs")}>View crawl runs <Icon name="chevron" size={15} /></button>
        </div>
      </section>

      {restore?.state === "running" || restore?.state === "failed" ? (
        <div className={restore.state === "failed" ? "security-notice security-notice--error" : "security-notice"}>
          <Icon name={restore.state === "failed" ? "alert" : "refresh"} />
          <span>
            {restore.state === "running" ? (
              <><strong>Restoring the saved catalog from S3.</strong> The service starts with an empty disk, so counts below fill in once the checkpoint finishes downloading.</>
            ) : (
              <><strong>The saved catalog could not be restored.</strong> {restore.error || "Check the S3 credentials and bucket in Settings."} Starting a crawl with Resume from checkpoint will try again.</>
            )}
          </span>
        </div>
      ) : null}

      <section className="run-detail">
        <section className="panel panel--timeline">
          <div className="panel-header">
            <h2>What the run is doing</h2>
            <CurrentStep status={status} />
          </div>
          <RunTimeline timeline={timeline} />
          {running ? (
            <div className="live-counters">
              <div><small>Products saved</small><strong>{formatNumber(media.records_seen?.product)}</strong></div>
              <div><small>Images stored</small><strong>{formatNumber(media.media_stored)}</strong></div>
              <div className={media.media_failed ? "metric-warning" : ""}><small>Images failed</small><strong>{formatNumber(media.media_failed)}</strong></div>
              <div><small>Diagnostics</small><strong>{formatNumber(media.records_seen?.diagnostic)}</strong></div>
            </div>
          ) : null}
        </section>
        <StorefrontPanel storefront={status?.storefront} catalog={catalog} onNavigate={onNavigate} />
      </section>

      <section className="coverage-strip" aria-label="Catalog data coverage">
        {counters.map(([icon, key]) => <div className="coverage-item" key={key}><Icon name={icon} size={22} /><span><small>{key[0].toUpperCase() + key.slice(1)}</small><strong>{catalog[key] == null ? "—" : formatNumber(catalog[key])}</strong></span></div>)}
      </section>

      <section className="enrichment-strip" aria-label="Source-page enrichment coverage">
        <div><small>Enrichment targets</small><strong>{formatNumber(enrichment.candidates)}</strong></div>
        <div><small>Completed</small><strong>{formatNumber(enrichment.completed)}</strong></div>
        <div><small>Remaining</small><strong>{formatNumber(enrichment.remaining)}</strong></div>
        <div><small>Variations with galleries</small><strong>{formatNumber(enrichment.variations_with_gallery)}</strong></div>
        <div><small>Variation gallery images</small><strong>{formatNumber(enrichment.variation_gallery_images)}</strong></div>
        <div className={enrichment.media_missing_storage ? "metric-warning" : ""}><small>Media not stored</small><strong>{formatNumber(enrichment.media_missing_storage)}</strong></div>
      </section>

      <section className="panel panel--table">
        <div className="panel-header"><h2>Products</h2><button className="text-button" onClick={() => onNavigate("products")}>View all products <Icon name="chevron" size={15} /></button></div>
        {products?.items?.length ? (
          <div className="table-scroll"><table><thead><tr><th>Image</th><th>Product</th><th>Type</th><th>Variations</th><th>Images</th><th>Categories</th><th>Quality</th><th>Updated</th></tr></thead><tbody>
            {products.items.slice(0, 5).map((product) => <tr key={product.id} onClick={() => onNavigate("products", product.id)} className="clickable-row"><td><ProductThumb product={product} /></td><td><strong>{product.name || product.id}</strong><small>{product.sku ? `SKU: ${product.sku}` : `ID: ${product.id}`}</small></td><td>{product.type || "—"}</td><td>{product.variation_count}</td><td>{product.image_count}</td><td>{product.categories?.join(", ") || "—"}</td><td><Quality score={product.quality?.score} /></td><td>{formatDate(product.updated)}</td></tr>)}
          </tbody></table></div>
        ) : <EmptyState title="No complete products yet" body="Run the corrected five-product test to populate this table." />}
      </section>

      <div className="overview-lower">
        <section className="panel"><div className="panel-header"><h2>Activity</h2><span>Latest crawler events</span></div><div className="activity-list">
          {logLines.length ? logLines.map((line, index) => <div className="activity-row" key={`${index}-${line}`}><Icon name={line.includes("ERROR") || line.includes("WARNING") ? "alert" : "runs"} /><code>{line}</code></div>) : <EmptyState icon="runs" title="No activity yet" body="Crawler events appear here while a run is active." />}
        </div></section>
        <section className="panel"><div className="panel-header"><h2>Data quality</h2><span>Missing catalog fields</span></div><div className="quality-list">
          {[["image", "Missing images", quality.missing_images], ["products", "Missing categories", quality.missing_categories], ["tag", "Missing attributes", quality.missing_attributes], ["alert", "Missing variations", quality.missing_variations]].map(([icon, label, value]) => <div key={label}><Icon name={icon} /><span>{label}</span><strong>{formatNumber(value)}</strong><button onClick={() => onNavigate("products")}>View</button></div>)}
        </div></section>
      </div>
    </main>
  );
}

export function ProductThumb({ product, large = false }) {
  return product.thumbnail ? <img className={large ? "product-thumb product-thumb--large" : "product-thumb"} src={product.thumbnail} alt="" loading="lazy" /> : <span className={large ? "product-thumb product-thumb--large product-thumb--empty" : "product-thumb product-thumb--empty"}><Icon name="image" /></span>;
}

function Quality({ score = 0 }) {
  const tone = score >= 90 ? "good" : score >= 70 ? "warn" : "bad";
  return <span className={`quality-score quality-score--${tone}`}>{score}%</span>;
}
