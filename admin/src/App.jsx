import { useCallback, useEffect, useState } from "react";

import { api, downloadExport, getToken, setToken } from "./api.js";
import { AppShell } from "./components/AppShell.jsx";
import { Login } from "./components/Login.jsx";
import { ExportsPage } from "./pages/ExportsPage.jsx";
import { OverviewPage } from "./pages/OverviewPage.jsx";
import { ProductsPage } from "./pages/ProductsPage.jsx";
import { RunsPage } from "./pages/RunsPage.jsx";
import { SettingsPage } from "./pages/SettingsPage.jsx";

const validPages = new Set(["overview", "products", "runs", "settings", "exports"]);

function pageFromHash() {
  const value = window.location.hash.replace(/^#\/?/, "").split("/")[0];
  return validPages.has(value) ? value : "overview";
}

export function App() {
  const [authenticated, setAuthenticated] = useState(false);
  const [checking, setChecking] = useState(Boolean(getToken()));
  const [page, setPage] = useState(pageFromHash);
  const [initialProductId, setInitialProductId] = useState("");
  const [status, setStatus] = useState(null);
  const [settings, setSettings] = useState(null);
  const [products, setProducts] = useState({ items: [] });
  const [logs, setLogs] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);

  const showError = useCallback((error) => {
    const message = error instanceof Error ? error.message : String(error);
    setNotice({ tone: "error", message: message.replace(/^\{"detail":"?|"?\}$/g, "") });
  }, []);

  const refreshStatus = useCallback(async () => {
    const [nextStatus, nextLogs, nextProducts] = await Promise.all([
      api("/api/status"),
      api("/api/logs"),
      api("/api/products?page_size=5"),
    ]);
    setStatus(nextStatus);
    setLogs(nextLogs);
    setProducts(nextProducts);
  }, []);

  const loadInitial = useCallback(async () => {
    const [nextStatus, nextSettings, nextProducts, nextLogs] = await Promise.all([
      api("/api/status"),
      api("/api/settings"),
      api("/api/products?page_size=5"),
      api("/api/logs"),
    ]);
    setStatus(nextStatus);
    setSettings(nextSettings);
    setProducts(nextProducts);
    setLogs(nextLogs);
  }, []);

  useEffect(() => {
    if (!getToken()) { setChecking(false); return; }
    api("/api/session")
      .then(async () => { setAuthenticated(true); await loadInitial(); })
      .catch(() => setToken(""))
      .finally(() => setChecking(false));
  }, [loadInitial]);

  useEffect(() => {
    if (!authenticated) return undefined;
    const interval = window.setInterval(() => refreshStatus().catch(showError), 5000);
    return () => window.clearInterval(interval);
  }, [authenticated, refreshStatus, showError]);

  useEffect(() => {
    const handler = () => setPage(pageFromHash());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);

  function navigate(nextPage, productId = "") {
    setInitialProductId(productId);
    setPage(nextPage);
    window.location.hash = nextPage;
  }

  async function start(config) {
    setBusy(true);
    try {
      const value = await api("/api/start", { method: "POST", body: JSON.stringify(config) });
      setStatus(value);
      setNotice({ tone: "success", message: "Crawl started. The dashboard will update automatically." });
    } catch (error) { showError(error); } finally { setBusy(false); }
  }

  async function stop() {
    setBusy(true);
    try { await api("/api/stop", { method: "POST" }); await refreshStatus(); } catch (error) { showError(error); } finally { setBusy(false); }
  }

  async function download() {
    setBusy(true);
    try { await downloadExport(); } catch (error) { showError(error); } finally { setBusy(false); }
  }

  async function authenticatedLogin() {
    setAuthenticated(true);
    setChecking(true);
    try { await loadInitial(); } catch (error) { showError(error); setAuthenticated(false); } finally { setChecking(false); }
  }

  function logout() {
    setToken("");
    setAuthenticated(false);
    setStatus(null);
  }

  function handleProductsDeleted(result) {
    const warning = result.media_error || result.artifact_error;
    const count = result.deleted_count || 1;
    const subject = count === 1 ? result.product_name || "Product" : `${count} products`;
    setNotice({
      tone: warning ? "error" : "success",
      message: warning
        ? `${subject} deleted, but cleanup needs attention: ${warning}`
        : `${subject} ${count === 1 ? "was" : "were"} deleted${result.media_requested ? ` with ${result.media_deleted} S3 media file${result.media_deleted === 1 ? "" : "s"}` : ""}.`,
    });
    refreshStatus().catch(showError);
  }

  if (checking) return <div className="boot-screen">Loading Jewelry Scraper…</div>;
  if (!authenticated) return <Login onAuthenticated={authenticatedLogin} />;

  return <AppShell page={page} onNavigate={navigate} onLogout={logout}>
    {notice ? <div className={`toast toast--${notice.tone}`} role="status"><span>{notice.message}</span><button onClick={() => setNotice(null)}>Dismiss</button></div> : null}
    {page === "overview" ? <OverviewPage status={status} settings={settings} products={products} logs={logs} busy={busy} onStart={start} onStop={stop} onDownload={download} onNavigate={navigate} /> : null}
    {page === "products" ? <ProductsPage initialProductId={initialProductId} onError={showError} onDeleted={handleProductsDeleted} /> : null}
    {page === "runs" ? <RunsPage status={status} settings={settings} logs={logs} onStart={start} onStop={stop} onError={showError} /> : null}
    {page === "settings" ? <SettingsPage settings={settings} onSaved={(value) => { setSettings(value); setNotice({ tone: "success", message: "Settings saved to local runtime and S3." }); }} onError={showError} /> : null}
    {page === "exports" ? <ExportsPage status={status} onError={showError} /> : null}
  </AppShell>;
}
