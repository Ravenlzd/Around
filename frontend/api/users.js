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

export async function blockUser(userId) {
  return apiClient.post(`/users/${encodeURIComponent(userId)}/block`, {});
}

export async function unblockUser(userId) {
  return apiClient.delete(`/users/${encodeURIComponent(userId)}/block`);
}

/**
 * Users the CURRENT account has blocked (never the reverse — see
 * backend/app/routers/users.py's list_blocked_users docstring).
 * @returns {Promise<Array<{user_id: string, display_name: string, avatar_url: string|null}>>}
 */
export async function listBlockedUsers() {
  return apiClient.get("/users/me/blocked");
}

/** @param {string} message */
export async function reportProblem(message) {
  return apiClient.post("/users/me/report-problem", { message });
}

/**
 * Report a specific member for moderation review (separate from
 * blocking — a report is a signal to Around, a block is a personal
 * "stop showing me this person" action; doing one doesn't imply doing
 * the other).
 * @param {string} userId @param {string} reason @param {string} [details]
 */
export async function reportUser(userId, reason, details) {
  return apiClient.post(`/users/${encodeURIComponent(userId)}/report`, { reason, details: details || null });
}

/** @returns {Promise<{groups: Record<string,string[]>}>} the curated interest catalog, grouped for a picker UI */
export async function getInterestCatalog() {
  return apiClient.get("/users/interests");
}

/** @returns {Promise<{interests: string[]}>} the current user's own selected interests */
export async function getMyInterests() {
  return apiClient.get("/users/me/interests");
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
