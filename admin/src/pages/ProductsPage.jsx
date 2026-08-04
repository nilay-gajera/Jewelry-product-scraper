import { useDeferredValue, useEffect, useMemo, useState } from "react";

import { api, downloadExport } from "../api.js";
import { Icon } from "../icons.jsx";
import { Button, EmptyState, LoadingLine, formatDate } from "../components/Ui.jsx";
import { ProductThumb } from "./OverviewPage.jsx";

export function ProductsPage({ initialProductId = "", onError }) {
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [type, setType] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ items: [], total: 0, page: 1, page_size: 25 });
  const [selectedId, setSelectedId] = useState(initialProductId);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    const params = new URLSearchParams({ q: deferredQuery, product_type: type, page: String(page), page_size: "25" });
    api(`/api/products?${params}`)
      .then((value) => { if (active) setData(value); })
      .catch(onError)
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [deferredQuery, type, page, onError]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let active = true;
    api(`/api/products/${encodeURIComponent(selectedId)}`)
      .then((value) => { if (active) setDetail(value); })
      .catch(onError);
    return () => { active = false; };
  }, [selectedId, onError]);

  const pages = Math.max(1, Math.ceil(data.total / data.page_size));

  return (
    <main className="page page--products">
      <header className="page-header"><div><h1>Products</h1><p>Inspect normalized products, variations, media, attributes, and raw source records.</p></div><Button icon="download" tone="primary" onClick={() => downloadExport().catch(onError)}>Download export</Button></header>
      <div className={detail ? "product-layout product-layout--detail" : "product-layout"}>
        <section className="product-list-panel">
          <div className="product-toolbar">
            <label className="search-control"><Icon name="search" /><input placeholder="Search by name, SKU, or source ID…" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} /></label>
            <label className="select-control"><Icon name="filter" /><select value={type} onChange={(event) => { setType(event.target.value); setPage(1); }}><option value="">All types</option><option value="simple">Simple</option><option value="variable">Variable</option><option value="variation">Variation</option></select></label>
          </div>
          {loading ? <LoadingLine /> : null}
          {data.items.length ? <div className="table-scroll"><table className="products-table"><thead><tr><th>Product</th><th>Type</th><th>Variations</th><th>Images</th><th>Categories</th><th>Quality</th><th>Updated</th><th /></tr></thead><tbody>
            {data.items.map((product) => <tr key={product.id} className={selectedId === product.id ? "clickable-row selected-row" : "clickable-row"} onClick={() => setSelectedId(product.id)}><td><div className="product-cell"><ProductThumb product={product} /><span><strong>{product.name || product.id}</strong><small>{product.sku ? `SKU: ${product.sku}` : `ID: ${product.id}`}</small></span></div></td><td>{product.type || "—"}</td><td>{product.variation_count}</td><td>{product.image_count}</td><td>{product.categories?.join(" › ") || "—"}</td><td><span className={`quality-score quality-score--${product.quality.score >= 90 ? "good" : product.quality.score >= 70 ? "warn" : "bad"}`}>{product.quality.score}%</span></td><td>{formatDate(product.updated)}</td><td><Icon name="chevron" /></td></tr>)}
          </tbody></table></div> : <EmptyState title="No matching products" body="Change the filter or run the scraper to populate products." />}
          <footer className="pagination"><span>{data.total ? `${(page - 1) * data.page_size + 1}–${Math.min(page * data.page_size, data.total)} of ${data.total}` : "0 products"}</span><div><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)} aria-label="Previous page"><Icon name="chevron" className="icon-reverse" /></button><strong>{page}</strong><button disabled={page >= pages} onClick={() => setPage((value) => value + 1)} aria-label="Next page"><Icon name="chevron" /></button></div></footer>
        </section>
        {selectedId ? <ProductInspector product={detail} onClose={() => setSelectedId("")} /> : null}
      </div>
    </main>
  );
}

function ProductInspector({ product, onClose }) {
  const [tab, setTab] = useState("overview");
  const [selectedVariation, setSelectedVariation] = useState("");
  useEffect(() => { setTab("overview"); setSelectedVariation(product?.variations?.[0]?.id || ""); }, [product?.id]);
  const media = product?.media || [];
  const featured = media.find((item) => item.role === "featured") || media[0];
  const variations = product?.variations || [];
  const variation = variations.find((item) => String(item.id) === String(selectedVariation)) || variations[0];
  const variationMedia = useMemo(() => {
    if (!variation) return [];
    return media.filter((item) => String(item.variation_id || "") === String(variation.id));
  }, [media, variation]);

  return (
    <aside className="product-inspector">
      <header><div><h2>{product?.name || "Loading product…"}</h2>{product ? <small>{product.sku ? `SKU: ${product.sku}` : `Source ID: ${product.id}`}</small> : null}</div><button className="icon-button" onClick={onClose} aria-label="Close product"><Icon name="close" /></button></header>
      {!product ? <LoadingLine /> : <>
        <div className="inspector-hero">{featured ? <img src={featured.display_url || featured.source_url} alt={featured.alt || product.name || "Product"} /> : <span><Icon name="image" size={38} /></span>}</div>
        <div className="media-rail">{media.slice(0, 8).map((item, index) => <img key={`${item.source_url}-${index}`} src={item.display_url || item.source_url} alt={item.alt || ""} loading="lazy" />)}{media.length > 8 ? <span>+{media.length - 8}</span> : null}</div>
        <nav className="tabs" aria-label="Product detail tabs">{["overview", "variations", "media", "raw"].map((name) => <button key={name} className={tab === name ? "selected" : ""} onClick={() => setTab(name)}>{name === "raw" ? "Raw data" : name[0].toUpperCase() + name.slice(1)}</button>)}</nav>
        {tab === "overview" ? <Overview product={product} /> : null}
        {tab === "variations" ? <Variations variations={variations} selected={variation} onSelect={setSelectedVariation} media={variationMedia} /> : null}
        {tab === "media" ? <MediaGrid media={media} /> : null}
        {tab === "raw" ? <pre className="raw-data">{JSON.stringify(product, null, 2)}</pre> : null}
      </>}
    </aside>
  );
}

function Overview({ product }) {
  const attributes = product.attributes || [];
  return <div className="inspector-content">
    <dl className="metadata-grid"><div><dt>Status</dt><dd>{product.status || "—"}</dd></div><div><dt>Type</dt><dd>{product.type || "—"}</dd></div><div><dt>Price</dt><dd>{product.currency || ""} {product.price || "—"}</dd></div><div><dt>Stock</dt><dd>{product.stock_status || "—"}</dd></div><div><dt>Source</dt><dd>{product.source || "—"}</dd></div><div><dt>Quality</dt><dd>{product.quality?.score || 0}%</dd></div></dl>
    <section><h3>Description</h3><p className="description-text">{product.description || product.short_description || "No description captured."}</p></section>
    <section><h3>Categories</h3><p>{(product.categories || []).map((item) => item.name).join(" › ") || "No categories captured."}</p></section>
    <section><h3>Attributes</h3>{attributes.length ? <dl className="attribute-list">{attributes.map((attribute, index) => <div key={attribute.id || attribute.name || index}><dt>{attribute.name || attribute.slug || "Attribute"}</dt><dd>{(attribute.options || attribute.terms || []).map((item) => typeof item === "object" ? item.name : item).join(", ") || "—"}</dd></div>)}</dl> : <p>No attributes captured.</p>}</section>
    {product.quality?.missing?.length ? <section className="quality-warning"><Icon name="alert" /><span><strong>Missing fields</strong>{product.quality.missing.join(", ")}</span></section> : null}
  </div>;
}

function Variations({ variations, selected, onSelect, media }) {
  if (!variations.length) return <EmptyState icon="products" title="No variations captured" body="This may be a simple product, or the variation endpoint still needs attention." />;
  return <div className="variation-layout"><div className="variation-table"><div className="variation-table__head"><span>Variation</span><span>Price</span><span>Stock</span></div>{variations.map((item) => <button key={item.id} className={String(selected?.id) === String(item.id) ? "selected" : ""} onClick={() => onSelect(String(item.id))}><span><strong>{item.name || item.id}</strong><small>{item.sku || Object.entries(item.attributes || {}).map(([key, value]) => `${key}: ${value}`).join(", ")}</small></span><span>{item.price || item.regular_price || "—"}</span><span>{item.stock_quantity ?? item.stock_status ?? "—"}</span></button>)}</div>{selected ? <section className="selected-variation"><h3>{selected.name}</h3><dl className="metadata-grid"><div><dt>SKU</dt><dd>{selected.sku || "—"}</dd></div><div><dt>Price</dt><dd>{selected.price || "—"}</dd></div><div><dt>Stock</dt><dd>{selected.stock_quantity ?? selected.stock_status ?? "—"}</dd></div><div><dt>Gallery</dt><dd>{media.length}</dd></div></dl><p>{Object.entries(selected.attributes || {}).map(([key, value]) => `${key}: ${value}`).join(" · ")}</p><div className="media-grid media-grid--compact">{media.map((item, index) => <img key={`${item.source_url}-${index}`} src={item.display_url || item.source_url} alt={item.alt || ""} loading="lazy" />)}</div></section> : null}</div>;
}

function MediaGrid({ media }) {
  return media.length ? <div className="media-grid">{media.map((item, index) => <figure key={`${item.source_url}-${index}`}><img src={item.display_url || item.source_url} alt={item.alt || ""} loading="lazy" /><figcaption>{item.role}{item.variation_id ? ` · variation ${item.variation_id}` : ""}</figcaption></figure>)}</div> : <EmptyState icon="image" title="No media captured" body="Product and variation images appear here after a successful media run." />;
}
