import { downloadExport } from "../api.js";
import { Icon } from "../icons.jsx";
import { Button } from "../components/Ui.jsx";

const files = [
  ["woocommerce-products.csv", "Combined parent and variation import sheet"],
  ["woocommerce-parents.csv", "Parent products for first-pass import"],
  ["woocommerce-variations.csv", "Variations linked by generated or source SKU"],
  ["catalog.sqlite", "Normalized product, taxonomy, variation, and media database"],
  ["products.jsonl", "Complete nested source records"],
  ["crawl-summary.json", "Coverage counts and diagnostic status"],
];

export function ExportsPage({ status, settings, onError }) {
  const privateMedia = settings?.storage?.media_delivery === "private_s3_signed";
  return <main className="page"><header className="page-header"><div><h1>Exports</h1><p>Download the latest private S3 archive and import-ready WooCommerce sheets.</p></div><Button icon="download" tone="primary" onClick={() => downloadExport().catch(onError)}>Download latest archive</Button></header><section className="export-hero"><Icon name="exports" size={32} /><div><h2>Latest catalog export</h2><p>{status?.summary ? `Run ${status.run_id} completed with ${status.summary.database_counts?.products || 0} products.` : "Complete a crawl to create the first export."}</p></div></section>{privateMedia ? <div className="security-notice"><Icon name="alert" /><span><strong>WooCommerce sheets use original source image URLs.</strong> Your S3 media is private, so configure a durable CDN in S3_PUBLIC_BASE_URL before a large import if source URLs are blocked or unstable.</span></div> : null}<section className="panel"><div className="panel-header"><h2>Archive contents</h2><span>Private, presigned download</span></div><div className="file-list">{files.map(([name, description]) => <div key={name}><Icon name="exports" /><span><strong>{name}</strong><small>{description}</small></span></div>)}</div></section><section className="import-order"><h2>WooCommerce import order</h2><ol><li>Import <strong>woocommerce-parents.csv</strong>.</li><li>Import <strong>woocommerce-variations.csv</strong>.</li><li>Verify image reachability and variation assignments on staging.</li></ol></section></main>;
}
