// frontend/api/media.js
//
// Multipart upload doesn't go through the shared JSON request() helper
// in client.js (different Content-Type, no JSON body) — this is the
// one deliberate exception to "everything goes through the central
// client," kept here rather than complicating client.js's single
// request path for one endpoint.
import { apiClient, ApiError, API_BASE } from "./client.js";

const MAX_BYTES = 5 * 1024 * 1024;
const ALLOWED_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];

/**
 * @param {File} file
 * @returns {Promise<{url: string, content_type: string, size_bytes: number}>}
 */
export async function uploadImage(file) {
  if (!ALLOWED_TYPES.includes(file.type)) {
    // Client-side check is a UX nicety only — the backend re-validates
    // by sniffing actual file bytes regardless, since a MIME type the
    // browser reports is not something the server can trust.
    throw new ApiError("Please choose a JPEG, PNG, WEBP, or GIF image.", 0, "invalid_type");
  }
  if (file.size > MAX_BYTES) {
    throw new ApiError("Image must be under 5MB.", 0, "too_large");
  }

  const token = await apiClient.tokens.get();
  const form = new FormData();
  form.append("file", file);

  let res;
  try {
    res = await fetch(API_BASE + "/media/upload", {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
  } catch (_) {
    throw new ApiError("Can't reach the server to upload that image.", 0, null);
  }

  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiError((data && data.detail) || "Upload failed", res.status, data && data.detail);
  }
  return data;
}

/** Resolves a possibly-relative /media/... URL returned by the backend into an absolute one the <img> tag can load. */
export function absoluteMediaUrl(url) {
  if (!url) return null;
  if (url.startsWith("http")) return url;
  return API_BASE + url;
}
