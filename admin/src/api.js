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
  const blobUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = "catalog-export.zip";
  link.click();
  URL.revokeObjectURL(blobUrl);
}
