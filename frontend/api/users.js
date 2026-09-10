// frontend/api/users.js
import { apiClient } from "./client.js";

/** @returns {Promise<import('./types.js').UserOut>} */
export async function getMyProfile() {
  return apiClient.get("/users/me");
}

/** @param {import('./types.js').ProfileUpdate} payload */
export async function updateMyProfile(payload) {
  return apiClient.patch("/users/me", payload);
}

export async function getMyTrustState() {
  return apiClient.get("/users/me/trust");
}

export async function getMyProfileStats() {
  return apiClient.get("/users/me/stats");
}

export async function getPublicProfile(userId) {
  return apiClient.get(`/users/${encodeURIComponent(userId)}`);
}

// ---------- I'm Free ----------
// Preserves the expiry model from the backend README: statuses always
// carry expires_at server-side and reads filter on it, so a stale status
// can never linger in the UI just because a cleanup job hasn't run yet.

/**
 * @param {{whenWindow: 'now'|'tonight'|'tomorrow'|'this_weekend', lookingFor: string, radiusKm?: number, lat: number, lng: number}} payload
 */
export async function activateImFree({ whenWindow, lookingFor, radiusKm, lat, lng }) {
  return apiClient.post("/im-free", {
    when_window: whenWindow, looking_for: lookingFor, radius_km: radiusKm ?? null,
    latitude: lat, longitude: lng,
  });
}

export async function endImFree() {
  return apiClient.delete("/im-free");
}

export async function nearbyImFreePeople(lat, lng) {
  return apiClient.get("/im-free/nearby", { query: { lat, lng } });
}
