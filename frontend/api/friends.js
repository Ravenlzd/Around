// frontend/api/friends.js
//
// Wraps backend/app/routers/friends.py (prefix /friends). Previously
// nothing in the frontend called any of these endpoints — the backend
// friendship graph (request/accept/reject/list) was fully implemented
// and already the single source of truth `events.py` checks for
// access_mode='friends' events, but the UI never exposed a way to
// actually create a friendship. This is the missing wiring, not a new
// friendship system.
import { apiClient } from "./client.js";

/** @param {string} targetUserId */
export async function sendRequest(targetUserId) {
  return apiClient.post(`/friends/request/${encodeURIComponent(targetUserId)}`, {});
}

/** @param {string} requesterId */
export async function acceptRequest(requesterId) {
  return apiClient.post(`/friends/accept/${encodeURIComponent(requesterId)}`, {});
}

/** @param {string} requesterId */
export async function rejectRequest(requesterId) {
  return apiClient.post(`/friends/reject/${encodeURIComponent(requesterId)}`, {});
}

export async function listFriends() {
  return apiClient.get("/friends");
}

/** Requests where someone else asked to be friends with the current user. */
export async function listPendingRequests() {
  return apiClient.get("/friends/requests");
}

/** @param {string} targetUserId @param {string} eventId */
export async function inviteToEvent(targetUserId, eventId) {
  return apiClient.post("/friends/invite", null, { query: { target_user_id: targetUserId, event_id: eventId } });
}
