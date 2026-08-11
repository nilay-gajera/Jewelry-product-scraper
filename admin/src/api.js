const TOKEN_KEY = "jewelry-scraper:control-token:v1";

let controlToken = "";
try {
  controlToken = sessionStorage.getItem(TOKEN_KEY) || "";
} catch {
  controlToken = "";
}

export function getToken() {
  return controlToken;
}

export function setToken(value) {
  controlToken = value.trim();
  try {
    if (controlToken) sessionStorage.setItem(TOKEN_KEY, controlToken);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Private browsing can disable storage; the in-memory token still works.
  }
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${controlToken}`);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : response.text();
}

export async function downloadExport() {
  const result = await api("/api/download");
  if (!result.url.startsWith("/")) {
    window.location.assign(result.url);
    return;
  }
  const response = await fetch(result.url, {
    headers: { Authorization: `Bearer ${controlToken}` },
  });
  if (!response.ok) throw new Error(await response.text());
  await saveResponseAsFile(response, "catalog-export.zip");
}

export async function downloadMasterExport() {
  const response = await fetch("/api/download-master", {
    headers: { Authorization: `Bearer ${controlToken}` },
  });
  if (!response.ok) throw new Error(await response.text());
  await saveResponseAsFile(response, "woocommerce-master.csv");
}

async function saveResponseAsFile(response, fallbackName) {
  const blobUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = fallbackName;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();

  // Some browsers begin reading the object URL after the click handler exits.
  // Revoking it immediately can silently cancel a large download.
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
}
