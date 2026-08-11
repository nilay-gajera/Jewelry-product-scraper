import { downloadMasterExport } from "../api.js";
import { Icon } from "../icons.jsx";
import { Button } from "../components/Ui.jsx";

const files = [
  ["woocommerce-master.csv", "Simple products, variable parents, variations, categories, attributes, diamond metadata, and image URLs"],
];

export function ExportsPage({ status, settings, onError }) {
  const privateMedia = settings?.storage?.media_delivery === "private_s3_signed";
  const products = status?.catalog?.products ?? status?.summary?.database_counts?.products ?? 0;
  const variations = status?.catalog?.variations ?? status?.summary?.database_counts?.variations ?? 0;
  return <main className="page"><header className="page-header"><div><h1>Exports</h1><p>Build one import-ready WooCommerce master sheet from the current restored checkpoint.</p></div><Button icon="download" tone="primary" onClick={() => downloadMasterExport().catch(onError)}>Download master CSV</Button></header><section className="export-hero"><Icon name="exports" size={32} /><div><h2>Current catalog checkpoint</h2><p>{`${products.toLocaleString()} parent/simple product rows + ${variations.toLocaleString()} variation rows = ${(products + variations).toLocaleString()} expected master rows.`}</p></div></section>{privateMedia ? <div className="security-notice"><Icon name="alert" /><span><strong>S3 media is private, so expiring signed URLs are not written into the import.</strong> Configure a durable CloudFront or public media base in S3_PUBLIC_BASE_URL; until then, the master sheet uses original source image URLs and retains S3 object paths in metadata.</span></div> : null}<section className="panel"><div className="panel-header"><h2>Master file</h2><span>Generated from the current SQLite checkpoint</span></div><div className="file-list">{files.map(([name, description]) => <div key={name}><Icon name="exports" /><span><strong>{name}</strong><small>{description}</small></span></div>)}</div></section><section className="import-order"><h2>WooCommerce import</h2><ol><li>Download <strong>woocommerce-master.csv</strong> after the crawl has stopped.</li><li>Import this single file with WooCommerce’s built-in product CSV importer.</li><li>Verify image reachability and variation assignments on staging before the production import.</li></ol></section></main>;
}
