// frontend/api/events.js
//
// Every attendance action here returns whatever the server decided —
// nothing in this file (or the UI code calling it) assumes success and
// patches local state first. See README "Optimistic UI" for which
// actions in the app ARE allowed to update instantly (none of the
// capacity-sensitive ones below) vs which wait on this promise to
// resolve.
import { apiClient, ApiError, getWebSocketBase } from "./client.js";

/**
 * @param {{lat: number, lng: number, radiusKm?: number, category?: string, limit?: number, offset?: number}} params
 * @returns {Promise<{results: import('./types.js').EventCard[], total: number, limit: number, offset: number, has_more: boolean}>}
 */
export async function nearby({ lat, lng, radiusKm = 10, category, limit, offset } = {}) {
  return apiClient.get("/discovery/nearby", { query: { lat, lng, radius_km: radiusKm, category, limit, offset } });
}

/**
 * @param {{q?: string, category?: string, limit?: number, offset?: number}} params
 * @returns {Promise<{results: import('./types.js').EventCard[], total: number, limit: number, offset: number, has_more: boolean}>}
 */
export async function search({ q = "", category, limit, offset } = {}) {
  return apiClient.get("/discovery/search", { query: { q, category, limit, offset } });
}

/**
 * @returns {Promise<Array<{user_id:string, display_name:string, university_or_work:string|null, avatar_url:string|null, shared_interests:string[], mutual_events:number, friendship_status:string}>>}
 */
export async function peopleNearby() {
  return apiClient.get("/discovery/people");
}

/** @returns {Promise<import('./types.js').EventDetail>} */
export async function getEvent(eventId) {
  return apiClient.get(`/events/${eventId}`);
}

/**
 * @param {import('./types.js').EventCreatePayload} payload
 * @returns {Promise<import('./types.js').EventCard>}
 */
export async function createEvent(payload) {
  return apiClient.post("/events", payload);
}

/** Host-only. Only send fields that actually changed — PATCH semantics, matches the backend's EventUpdate schema (all-optional). */
export async function updateEvent(eventId, payload) {
  return apiClient.patch(`/events/${eventId}`, payload);
}

/** Host-only. Marks the event cancelled — does not delete it or its history. */
export async function cancelEvent(eventId, reason) {
  return apiClient.post(`/events/${eventId}/cancel`, {}, { query: { reason: reason || undefined } });
}

// ---------- join / leave ----------
// Each of these throws ApiError on failure — callers should catch and
// read `.detail` (the server's human-readable reason, e.g. "Event is
// full — join the waitlist instead") to show directly in the UI rather
// than inventing a generic message. `.status` is also available for
// branching (401 -> bounce to login, 409 -> capacity/state conflict, etc).

export async function joinEvent(eventId) {
  return apiClient.post(`/events/${eventId}/join`, {});
}

export async function joinEventWithCode(eventId, code) {
  return apiClient.post(`/events/${eventId}/join-with-code`, {}, { query: { code } });
}

export async function leaveEvent(eventId) {
  return apiClient.post(`/events/${eventId}/leave`, {});
}

// ---------- approval ----------

export async function requestToJoin(eventId, guestCountRequested = 0) {
  return apiClient.post(`/events/${eventId}/join-requests`, { guest_count_requested: guestCountRequested });
}

export async function cancelMyJoinRequest(eventId) {
  return apiClient.delete(`/events/${eventId}/join-requests/mine`);
}

export async function approveRequest(eventId, requestId) {
  return apiClient.post(`/events/${eventId}/join-requests/${requestId}/approve`, {});
}

export async function rejectRequest(eventId, requestId) {
  return apiClient.post(`/events/${eventId}/join-requests/${requestId}/reject`, {});
}

// ---------- waitlist ----------
// Queue position always comes from the server response / event detail
// payload's `my_status.waitlist_position` — never computed client-side.

export async function joinWaitlist(eventId) {
  return apiClient.post(`/events/${eventId}/waitlist`, {});
}

export async function leaveWaitlist(eventId) {
  return apiClient.delete(`/events/${eventId}/waitlist`);
}

export async function claimWaitlistSpot(eventId) {
  return apiClient.post(`/events/${eventId}/waitlist/claim`, {});
}

// ---------- guests ----------
// The guest "token" shown to the user (e.g. "RAVAN-7F2K") is generated
// and validated entirely server-side — the frontend only ever displays
// it, it never constructs or checks one itself (spec §10: "The guest
// token should never be trusted by the frontend alone").

export async function inviteGuest(eventId, guestName) {
  return apiClient.post(`/events/${eventId}/guests`, { guest_name: guestName || null });
}

export async function cancelGuest(eventId, guestParticipantId) {
  return apiClient.delete(`/events/${eventId}/guests/${guestParticipantId}`);
}

// ---------- host attendee management ----------

export async function getAttendance(eventId) {
  return apiClient.get(`/events/${eventId}/attendance`);
}

export async function removeAttendee(eventId, participantId, { ban = false } = {}) {
  return apiClient.delete(`/events/${eventId}/attendees/${participantId}`, { query: { ban } });
}

export async function banUser(eventId, userId, reason) {
  return apiClient.post(`/events/${eventId}/ban`, { user_id: userId, reason });
}

/**
 * Report an event for moderation review. Existed backend-only until
 * this pass — nothing in the UI ever called it.
 * @param {string} eventId @param {string} reason @param {string} [details]
 */
export async function reportEvent(eventId, reason, details) {
  return apiClient.post(`/events/${eventId}/report`, { reason, details: details || null });
}

// ---------- check-in ----------
// Real, signed, time-limited tokens as of this pass (see backend
// app/routers/events.py's module docstring for why the old
// no-token self-checkin was a real security gap). Camera-based QR
// *scanning* is still not implemented — the host displays the raw
// token as a QR-shaped image and an attendee would need to type/paste
// it (or a future phase adds an actual camera scanner); the token
// itself, its expiry, and its verification are all real.

/** Host-only: generates a fresh check-in token for this event, valid for `validHours` (default 6, capped at 12 server-side). */
export async function createCheckinToken(eventId, validHours = 6) {
  return apiClient.post(`/events/${eventId}/checkin-token`, {}, { query: { valid_hours: validHours } });
}

/** Self check-in with a scanned/entered token. */
export async function checkInWithToken(eventId, token) {
  return apiClient.post(`/events/${eventId}/check-in`, { token, method: "qr" });
}

/** Host-only manual override (e.g. attendee's phone died) — unchanged from before. */
export async function checkIn(eventId, { participantId = null, method = "manual" } = {}) {
  return apiClient.post(`/events/${eventId}/check-in`, { participant_id: participantId, method });
}

// ---------- chat ----------

export async function getChatMessages(eventId, after) {
  return apiClient.get(`/events/${eventId}/chat`, { query: { after } });
}

export async function sendChatMessage(eventId, body) {
  return apiClient.post(`/events/${eventId}/chat`, { body });
}

/**
 * Opens a WebSocket for real-time chat on one event. Returns a handle
 * with `.close()`; `onMessage(msg)` fires for each new message pushed
 * by the server (see backend app/ws.py). Falls back gracefully — if the
 * socket fails to connect, `onError` fires once and the caller is
 * expected to keep using getChatMessages() polling instead; this file
 * doesn't retry/reconnect on its own to keep the behavior predictable.
 */
/**
 * Opens a WebSocket for real-time chat on one event, with automatic
 * reconnect (spec: "frontend reconnects if the socket disconnects").
 * Uses capped exponential backoff (1s, 2s, 4s... up to 15s) rather than
 * hammering the server after a network blip or a backend restart.
 * `onMessage(msg)` fires for each new message; `onStatus(status)` fires
 * with 'connected' | 'reconnecting' | 'closed' so the UI can show a
 * subtle "reconnecting…" indicator instead of silently going stale.
 * Returns a handle with `.close()` that stops reconnect attempts too —
 * always call it when the event sheet closes.
 */
export async function openChatSocket(eventId, { onMessage, onError, onOpen, onStatus } = {}) {
  const wsBase = getWebSocketBase();
  let ws = null;
  let closedByCaller = false;
  let attempt = 0;
  let reconnectTimer = null;

  async function connect() {
    if (closedByCaller) return;
    const token = await apiClient.tokens.get();
    ws = new WebSocket(`${wsBase}/events/${eventId}/ws?token=${encodeURIComponent(token || "")}`);
    ws.onopen = () => {
      attempt = 0;
      onStatus && onStatus("connected");
      onOpen && onOpen();
    };
    ws.onmessage = (evt) => {
      try { onMessage && onMessage(JSON.parse(evt.data)); } catch (_) { /* ignore malformed frame */ }
    };
    ws.onerror = () => { onError && onError(); };
    ws.onclose = (evt) => {
      if (closedByCaller) return;
      // 4401/4403 are our own auth/authorization close codes (see
      // backend app/routers/events.py's WS endpoint) — retrying won't
      // help if the token is invalid or the user isn't an attendee.
      if (evt.code === 4401 || evt.code === 4403) {
        onStatus && onStatus("closed");
        return;
      }
      onStatus && onStatus("reconnecting");
      const delay = Math.min(15000, 1000 * Math.pow(2, attempt));
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };
  }

  await connect();

  return {
    close: () => {
      closedByCaller = true;
      clearTimeout(reconnectTimer);
      if (ws) ws.close();
    },
  };
}

export { ApiError };
