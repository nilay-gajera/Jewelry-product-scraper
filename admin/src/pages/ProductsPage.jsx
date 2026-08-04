import { useDeferredValue, useEffect, useMemo, useState } from "react";

import { api, downloadExport } from "../api.js";
import { Icon } from "../icons.jsx";
import { Button, EmptyState, LoadingLine, formatDate } from "../components/Ui.jsx";
import { ProductThumb } from "./OverviewPage.jsx";

export function ProductsPage({ initialProductId = "", crawlActive = false, onError, onDeleted }) {
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [type, setType] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ items: [], total: 0, page: 1, page_size: 25 });
  const [selectedId, setSelectedId] = useState(initialProductId);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [confirmingBulkDelete, setConfirmingBulkDelete] = useState(false);
  const [bulkDeleteMedia, setBulkDeleteMedia] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    const params = new URLSearchParams({ q: deferredQuery, product_type: type, page: String(page), page_size: "25" });
    api(`/api/products?${params}`)
      .then((value) => { if (active) setData(value); })
      .catch(onError)
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [deferredQuery, type, page, onError, refreshKey]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let active = true;
    setDetail(null);
    api(`/api/products/${encodeURIComponent(selectedId)}`)
      .then((value) => { if (active) setDetail(value); })
      .catch((error) => { if (active) setDetail(null); onError(error); });
    return () => { active = false; };
  }, [selectedId, onError]);

  useEffect(() => {
    if (!crawlActive) return;
    setSelectedIds(new Set());
    setConfirmingBulkDelete(false);
  }, [crawlActive]);

  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  const visibleIds = useMemo(() => data.items.map((product) => String(product.id)), [data.items]);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));

  function toggleProduct(productId) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(productId)) next.delete(productId);
      else next.add(productId);
      return next;
    });
  }

  function toggleVisibleProducts() {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (allVisibleSelected) visibleIds.forEach((id) => next.delete(id));
      else visibleIds.forEach((id) => next.add(id));
      return next;
    });
  }

  async function deleteSelectedProduct(deleteMedia) {
    if (!detail?.id) return;
    const result = await api(`/api/products/${encodeURIComponent(detail.id)}?delete_media=${deleteMedia ? "true" : "false"}`, { method: "DELETE" });
    setSelectedIds((current) => {
      const next = new Set(current);
      next.delete(String(detail.id));
      return next;
    });
    setSelectedId("");
    setDetail(null);
    setRefreshKey((value) => value + 1);
    onDeleted?.(result);
  }

  async function deleteSelectedProducts() {
    const productIds = [...selectedIds];
    if (!productIds.length) return;
    setBulkDeleting(true);
    try {
      const result = await api("/api/products/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_ids: productIds, delete_media: bulkDeleteMedia }),
      });
      if (selectedId && productIds.includes(String(selectedId))) {
        setSelectedId("");
        setDetail(null);
      }
      const remainingTotal = Math.max(0, data.total - result.deleted_count);
      const remainingPages = Math.max(1, Math.ceil(remainingTotal / data.page_size));
      setPage((current) => Math.min(current, remainingPages));
      setSelectedIds(new Set());
      setConfirmingBulkDelete(false);
      setBulkDeleteMedia(false);
      setRefreshKey((value) => value + 1);
      onDeleted?.(result);
    } catch (error) {
      onError(error);
    } finally {
      setBulkDeleting(false);
    }
  }

  return (
    <main className="page page--products">
      <header className="page-header"><div><h1>Products</h1><p>Inspect normalized products, variations, media, attributes, and raw source records.</p></div><Button icon="download" tone="primary" onClick={() => downloadExport().catch(onError)}>Download export</Button></header>
      <div className={detail ? "product-layout product-layout--detail" : "product-layout"}>
        <section className="product-list-panel">
          <div className="product-toolbar">
            <label className="search-control"><Icon name="search" /><input placeholder="Search by name, SKU, or source ID…" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); setSelectedIds(new Set()); }} /></label>
            <label className="select-control"><Icon name="filter" /><select value={type} onChange={(event) => { setType(event.target.value); setPage(1); setSelectedIds(new Set()); }}><option value="">All types</option><option value="simple">Simple</option><option value="variable">Variable</option><option value="variation">Variation</option></select></label>
            {selectedIds.size && !crawlActive ? <div className="product-toolbar__bulk"><span><strong>{selectedIds.size}</strong> product{selectedIds.size === 1 ? "" : "s"} selected</span><button onClick={() => setSelectedIds(new Set())}>Clear selection</button><Button icon="trash" tone="danger" onClick={() => setConfirmingBulkDelete(true)}>Delete selected</Button></div> : null}
          </div>
          {crawlActive ? <div className="security-notice"><Icon name="alert" /><span><strong>Catalog crawl in progress.</strong> Product deletion and selection are disabled until the checkpoint is closed safely.</span></div> : null}
          {loading ? <LoadingLine /> : null}
          {data.items.length ? <div className="table-scroll"><table className="products-table"><thead><tr><th className="product-select"><input type="checkbox" disabled={crawlActive} checked={allVisibleSelected} onChange={toggleVisibleProducts} aria-label="Select all products on this page" /></th><th>Product</th><th>Type</th><th>Variations</th><th>Images</th><th>Categories</th><th>Quality</th><th>Updated</th><th /></tr></thead><tbody>
            {data.items.map((product) => { const productId = String(product.id); return <tr key={product.id} className={selectedId === product.id ? "clickable-row selected-row" : "clickable-row"} onClick={() => setSelectedId(product.id)}><td className="product-select" onClick={(event) => event.stopPropagation()}><input type="checkbox" disabled={crawlActive} checked={selectedIds.has(productId)} onChange={() => toggleProduct(productId)} aria-label={`Select ${product.name || product.id}`} /></td><td><div className="product-cell"><ProductThumb product={product} /><span><strong>{product.name || product.id}</strong><small>{product.sku ? `SKU: ${product.sku}` : `ID: ${product.id}`}</small></span></div></td><td>{product.type || "—"}</td><td>{product.variation_count}</td><td>{product.image_count}</td><td>{product.categories?.join(" › ") || "—"}</td><td><span className={`quality-score quality-score--${product.quality.score >= 90 ? "good" : product.quality.score >= 70 ? "warn" : "bad"}`}>{product.quality.score}%</span></td><td>{formatDate(product.updated)}</td><td><Icon name="chevron" /></td></tr>; })}
          </tbody></table></div> : <EmptyState title="No matching products" body="Change the filter or run the scraper to populate products." />}
          <footer className="pagination"><span>{data.total ? `${(page - 1) * data.page_size + 1}–${Math.min(page * data.page_size, data.total)} of ${data.total}` : "0 products"}</span><div><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)} aria-label="Previous page"><Icon name="chevron" className="icon-reverse" /></button><strong>{page}</strong><button disabled={page >= pages} onClick={() => setPage((value) => value + 1)} aria-label="Next page"><Icon name="chevron" /></button></div></footer>
        </section>
        {selectedId ? <ProductInspector product={detail} crawlActive={crawlActive} onClose={() => setSelectedId("")} onDelete={deleteSelectedProduct} onError={onError} /> : null}
      </div>
      {confirmingBulkDelete ? <DeleteDialog bulk count={selectedIds.size} deleteMedia={bulkDeleteMedia} setDeleteMedia={setBulkDeleteMedia} busy={bulkDeleting} onCancel={() => setConfirmingBulkDelete(false)} onConfirm={deleteSelectedProducts} /> : null}
    </main>
  );
}

function ProductInspector({ product, crawlActive, onClose, onDelete, onError }) {
  const [tab, setTab] = useState("overview");
  const [selectedVariation, setSelectedVariation] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteMedia, setDeleteMedia] = useState(false);
  const [deleting, setDeleting] = useState(false);
  useEffect(() => { setTab("overview"); setSelectedVariation(product?.variations?.[0]?.id || ""); setConfirmingDelete(false); setDeleteMedia(false); }, [product?.id]);
  const media = product?.media || [];
  const productMedia = useMemo(() => uniqueMediaByUrl(media), [media]);
  const variations = product?.variations || [];
  const variation = variations.find((item) => String(item.id) === String(selectedVariation)) || variations[0];
  const variationMedia = useMemo(() => {
    if (!variation) return [];
    return uniqueMediaByUrl(media.filter((item) => String(item.variation_id || "") === String(variation.id)));
  }, [media, variation]);
  const activeMedia = tab === "variations" && variationMedia.length ? variationMedia : productMedia;
  const featured = activeMedia.find((item) => item.role === "variation") || activeMedia.find((item) => item.role === "featured") || activeMedia[0];

  async function confirmDelete() {
    setDeleting(true);
    try {
      await onDelete(deleteMedia);
    } catch (error) {
      onError(error);
      setDeleting(false);
    }
  }

  return (
    <aside className="product-inspector">
      <header><div><h2>{product ? product.name || product.id || "Unnamed product" : "Loading product…"}</h2>{product ? <small>{product.sku ? `SKU: ${product.sku}` : `Source ID: ${product.id}`}</small> : null}</div><div className="inspector-actions">{product && !crawlActive ? <button className="icon-button icon-button--danger" onClick={() => setConfirmingDelete(true)} aria-label="Delete product"><Icon name="trash" /></button> : null}<button className="icon-button" onClick={onClose} aria-label="Close product"><Icon name="close" /></button></div></header>
      {!product ? <LoadingLine /> : <>
        <div className="inspector-hero">{featured ? <img src={featured.display_url || featured.source_url} alt={featured.alt || product.name || "Product"} /> : <span><Icon name="image" size={38} /></span>}</div>
        <div className="media-rail">{activeMedia.slice(0, 8).map((item) => <img key={item.source_url} src={item.display_url || item.source_url} alt={item.alt || ""} loading="lazy" />)}{activeMedia.length > 8 ? <span>+{activeMedia.length - 8}</span> : null}</div>
        <nav className="tabs" aria-label="Product detail tabs">{["overview", "variations", "media", "raw"].map((name) => <button key={name} className={tab === name ? "selected" : ""} onClick={() => setTab(name)}>{name === "raw" ? "Raw data" : name[0].toUpperCase() + name.slice(1)}</button>)}</nav>
        {tab === "overview" ? <Overview product={product} /> : null}
        {tab === "variations" ? <Variations variations={variations} selected={variation} onSelect={setSelectedVariation} media={variationMedia} /> : null}
        {tab === "media" ? <MediaGrid media={productMedia} /> : null}
        {tab === "raw" ? <pre className="raw-data">{JSON.stringify(product, null, 2)}</pre> : null}
      </>}
      {confirmingDelete ? <DeleteDialog productName={product?.name || product?.id} count={1} deleteMedia={deleteMedia} setDeleteMedia={setDeleteMedia} busy={deleting} onCancel={() => setConfirmingDelete(false)} onConfirm={confirmDelete} /> : null}
    </aside>
  );
}

function DeleteDialog({ productName, bulk = false, count, deleteMedia, setDeleteMedia, busy, onCancel, onConfirm }) {
  const bulkLabel = `${count} product${count === 1 ? "" : "s"}`;
  return <div className="dialog-backdrop"><section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-product-title"><div className="confirm-dialog__icon"><Icon name="trash" size={22} /></div><h2 id="delete-product-title">Delete {bulk ? bulkLabel : "this product"}?</h2><p>{bulk ? <><strong>{bulkLabel} selected</strong> and all of their variations and assignments</> : <><strong>{productName}</strong> and all of its variations and assignments</>} will be removed from the active catalog and S3 checkpoint.</p><label className="delete-media-option"><input type="checkbox" checked={deleteMedia} onChange={(event) => setDeleteMedia(event.target.checked)} /><span><strong>Also delete downloaded media from S3</strong><small>Historical run archives are kept for recovery.</small></span></label><footer><Button disabled={busy} onClick={onCancel}>Cancel</Button><Button icon="trash" tone="danger" disabled={busy} onClick={onConfirm}>{busy ? "Deleting…" : bulk ? `Delete ${bulkLabel}` : "Delete product"}</Button></footer></section></div>;
}

function Overview({ product }) {
  const attributes = product.attributes || [];
  const tags = product.tags || [];
  const brands = product.brands || [];
  const dimensions = product.dimensions || {};
  return <div className="inspector-content">
    <dl className="metadata-grid"><div><dt>Status</dt><dd>{product.status || "—"}</dd></div><div><dt>Type</dt><dd>{product.type || "—"}</dd></div><div><dt>Price</dt><dd>{product.currency || ""} {product.price || "—"}</dd></div><div><dt>Stock</dt><dd>{product.stock_quantity ?? product.stock_status ?? "—"}</dd></div><div><dt>Source</dt><dd>{product.source || "—"}</dd></div><div><dt>Quality</dt><dd>{product.quality?.score || 0}%</dd></div><div><dt>Weight</dt><dd>{product.weight || "—"}</dd></div><div><dt>Dimensions</dt><dd>{[dimensions.length, dimensions.width, dimensions.height].filter(Boolean).join(" × ") || "—"}</dd></div></dl>
    <section><h3>Description</h3><p className="description-text">{product.description || product.short_description || "No description captured."}</p></section>
    <section><h3>Categories</h3><p>{(product.categories || []).map((item) => item.name).join(" › ") || "No categories captured."}</p></section>
    {tags.length ? <section><h3>Tags</h3><p>{tags.map((item) => item.name || item.slug).filter(Boolean).join(", ")}</p></section> : null}
    {brands.length ? <section><h3>Brands</h3><p>{brands.map((item) => item.name || item.slug).filter(Boolean).join(", ")}</p></section> : null}
    <section><h3>Attributes</h3>{attributes.length ? <dl className="attribute-list">{attributes.map((attribute, index) => <div key={attribute.id || attribute.name || index}><dt>{attribute.name || attribute.slug || "Attribute"}</dt><dd>{(attribute.options || attribute.terms || []).map((item) => typeof item === "object" ? item.name : item).join(", ") || "—"}</dd></div>)}</dl> : <p>No attributes captured.</p>}</section>
    {product.seo ? <section><h3>SEO metadata</h3><dl className="metadata-grid"><div><dt>Title</dt><dd>{product.seo.title || "—"}</dd></div><div><dt>Canonical</dt><dd>{product.seo.canonical_url || "—"}</dd></div><div><dt>Meta description</dt><dd>{product.seo.meta_description || "—"}</dd></div></dl></section> : null}
    {product.quality?.missing?.length ? <section className="quality-warning"><Icon name="alert" /><span><strong>Missing fields</strong>{product.quality.missing.join(", ")}</span></section> : null}
  </div>;
}

function Variations({ variations, selected, onSelect, media }) {
  if (!variations.length) return <EmptyState icon="products" title="No variations captured" body="This may be a simple product, or the variation endpoint still needs attention." />;
  return <div className="variation-layout"><div className="variation-table"><div className="variation-table__head"><span>Variation</span><span>Price</span><span>Stock</span></div>{variations.map((item) => <button key={item.id} className={String(selected?.id) === String(item.id) ? "selected" : ""} onClick={() => onSelect(String(item.id))}><span><strong>{item.name || item.id}</strong><small>{item.sku || Object.entries(item.attributes || {}).map(([key, value]) => `${key}: ${value}`).join(", ")}</small></span><span>{item.price || item.regular_price || "—"}</span><span>{item.stock_quantity ?? item.stock_status ?? "—"}</span></button>)}</div>{selected ? <section className="selected-variation"><h3>{selected.name}</h3><dl className="metadata-grid"><div><dt>SKU</dt><dd>{selected.sku || "—"}</dd></div><div><dt>Price</dt><dd>{selected.price || "—"}</dd></div><div><dt>Stock</dt><dd>{selected.stock_quantity ?? selected.stock_status ?? "—"}</dd></div><div><dt>Gallery URLs</dt><dd>{media.length}</dd></div><div><dt>Gallery references</dt><dd>{selected.gallery_image_ids?.length || 0}</dd></div><div><dt>Source</dt><dd>{selected.source || "—"}</dd></div></dl><p>{Object.entries(selected.attributes || {}).map(([key, value]) => `${key}: ${value}`).join(" · ")}</p><div className="media-grid media-grid--compact">{media.map((item, index) => <img key={`${item.source_url}-${index}`} src={item.display_url || item.source_url} alt={item.alt || ""} loading="lazy" />)}</div></section> : null}</div>;
}

function MediaGrid({ media }) {
  return media.length ? <div className="media-grid">{media.map((item, index) => <figure key={`${item.source_url}-${index}`}><img src={item.display_url || item.source_url} alt={item.alt || ""} loading="lazy" /><figcaption>{item.role}{item.variation_id ? ` · variation ${item.variation_id}` : ""}</figcaption></figure>)}</div> : <EmptyState icon="image" title="No media captured" body="Product and variation images appear here after a successful media run." />;
}

function uniqueMediaByUrl(media) {
  const seen = new Set();
  return media.filter((item) => {
    const url = item.source_url || item.local_path || item.display_url;
    if (!url || seen.has(url)) return false;
    seen.add(url);
    return true;
  });
}
