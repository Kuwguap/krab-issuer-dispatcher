const DEFAULT_TIMEOUT_MS = 20000;

function apiBase() {
  const raw = import.meta.env.VITE_KRAB_API_BASE_URL;
  if (!raw || raw === "SAME_ORIGIN") {
    return window.location.origin.replace(/\/$/, "");
  }
  return String(raw).replace(/\/$/, "");
}

export async function api(path, opts = {}) {
  const url = apiBase() + path;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);

  try {
    const res = await fetch(url, {
      credentials: "include",
      ...opts,
      signal: controller.signal,
      headers: {
        ...(opts.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...opts.headers,
      },
    });
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = {
        detail: res.ok
          ? "Invalid response"
          : `API error ${res.status}${text ? `: ${text.slice(0, 160)}` : ""}`,
      };
    }
    if (!res.ok) {
      const err = new Error(
        typeof data.detail === "string"
          ? data.detail
          : data.message || res.statusText || `HTTP ${res.status}`
      );
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  } catch (e) {
    if (e.name === "AbortError") {
      const err = new Error("Request timed out — check API connection.");
      err.status = 408;
      throw err;
    }
    throw e;
  } finally {
    clearTimeout(timeout);
  }
}

export function brandName() {
  return import.meta.env.VITE_BRAND_NAME || "Driver Interview Call";
}
