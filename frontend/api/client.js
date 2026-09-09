// frontend/api/client.js
//
// Centralized API client (spec §5 — "Do NOT scatter raw fetch() calls
// across every UI function"). Every other file under api/ goes through
// `request()` here. Typed via JSDoc rather than TypeScript so this can
// run directly in the browser with no build step, same as the rest of
// this prototype; the shapes below are the "strongly typed request/
// response interfaces" the spec asks for — see types.js for the shared
// typedefs referenced throughout.
//
// Token persistence: this file tries `window.storage` (Claude's
// artifact-persistence API) first, since this file is often previewed
// inside an artifact iframe where localStorage is explicitly unsupported
// and fails. If `window.storage` isn't present (e.g. you've downloaded
// this project and are running it as a normal static site against your
// own backend), it falls back to `localStorage`. If neither is
// available it falls back to an in-memory variable, which means the
// session will NOT survive a real page reload — a console warning is
// logged so this isn't a silent surprise. For a real production
// deployment outside of Claude's preview, localStorage (or better, an
// httpOnly refresh-token cookie issued by the backend) is the normal
// choice; adapt `TokenStore` below if you go that route.

export const API_BASE = window.AROUND_API_BASE || "http://localhost:8000";

const TOKEN_KEY = "around:auth_token";

const TokenStore = {
  _memory: null,
  _mode: null, // 'artifact-storage' | 'localStorage' | 'memory'

  async init() {
    if (this._mode) return;
    if (typeof window !== "undefined" && window.storage && typeof window.storage.get === "function") {
      this._mode = "artifact-storage";
      return;
    }
    try {
      if (typeof window !== "undefined" && window.localStorage) {
        window.localStorage.setItem("__around_probe__", "1");
        window.localStorage.removeItem("__around_probe__");
        this._mode = "localStorage";
        return;
      }
    } catch (_) { /* storage disabled/unavailable — fall through */ }
    this._mode = "memory";
    console.warn(
      "[Around] No persistent storage available — you'll be signed out on refresh. " +
      "This is expected inside some preview sandboxes; a normal deployment has localStorage available."
    );
  },

  async get() {
    await this.init();
    if (this._mode === "artifact-storage") {
      try {
        const result = await window.storage.get(TOKEN_KEY, false);
        return result ? result.value : null;
      } catch (_) {
        return null; // key doesn't exist yet
      }
    }
    if (this._mode === "localStorage") {
      return window.localStorage.getItem(TOKEN_KEY);
    }
    return this._memory;
  },

  async set(token) {
    await this.init();
    if (this._mode === "artifact-storage") {
      try { await window.storage.set(TOKEN_KEY, token, false); } catch (e) { console.error("[Around] token save failed", e); }
      return;
    }
    if (this._mode === "localStorage") {
      window.localStorage.setItem(TOKEN_KEY, token);
      return;
    }
    this._memory = token;
  },

  async clear() {
    await this.init();
    if (this._mode === "artifact-storage") {
      try { await window.storage.delete(TOKEN_KEY, false); } catch (_) { /* already gone */ }
      return;
    }
    if (this._mode === "localStorage") {
      window.localStorage.removeItem(TOKEN_KEY);
      return;
    }
    this._memory = null;
  },
};

/**
 * Normalized API error. `status` is the HTTP status code (0 for
 * network-level failures where the request never reached the server).
 * `code` is a short machine string when the backend supplied one
 * (attendance_rules.py's Decision.reason values surface here as the
 * `detail` string — see note in events.js about mapping those).
 */
export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
  get isNetworkError() { return this.status === 0; }
  get isAuthError() { return this.status === 401; }
}

/**
 * @param {string} path - e.g. "/events/123/join"
 * @param {{method?: string, body?: any, auth?: boolean, query?: Record<string,any>}} [opts]
 */
async function request(path, opts = {}) {
  const { method = "GET", body, auth = true, query } = opts;

  let url = API_BASE + path;
  if (query) {
    const qs = Object.entries(query)
      .filter(([, v]) => v !== undefined && v !== null)
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
      .join("&");
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
  }

  const headers = { "Content-Type": "application/json" };
  if (auth) {
    const token = await TokenStore.get();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  let res;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (networkErr) {
    // fetch() throws on DNS failure, connection refused, CORS block, etc.
    // — this is the "backend unavailable" case from spec §21.
    throw new ApiError("Can't reach Around's server. Check your connection and try again.", 0, null);
  }

  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = text; }
  }

  if (!res.ok) {
    const detail = (data && typeof data === "object" && "detail" in data) ? data.detail : null;
    const message = detail || `Request failed (${res.status})`;
    if (res.status === 401) {
      // expired/invalid token — clear it so the app doesn't keep retrying
      // with a dead token; the UI layer is responsible for redirecting to
      // the login screen when it sees an ApiError with isAuthError.
      await TokenStore.clear();
    }
    throw new ApiError(message, res.status, detail);
  }

  return data;
}

export const apiClient = {
  request,
  tokens: TokenStore,
  get: (path, opts) => request(path, { ...opts, method: "GET" }),
  post: (path, body, opts) => request(path, { ...opts, method: "POST", body }),
  patch: (path, body, opts) => request(path, { ...opts, method: "PATCH", body }),
  delete: (path, opts) => request(path, { ...opts, method: "DELETE" }),
};

/**
 * Quick reachability check with a short timeout, used once on boot to
 * decide between "real backend available" and "offline/demo mode" (see
 * app.js). Deliberately does NOT throw — returns a boolean — since a
 * failed health check is an expected, normal outcome (e.g. this file
 * opened standalone with no backend running), not an error to surface.
 */
export async function isBackendReachable(timeoutMs = 1500) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(API_BASE + "/health", { signal: controller.signal });
    return res.ok;
  } catch (_) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}
