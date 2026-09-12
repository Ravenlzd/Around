// frontend/api/chat.js
//
// Friend-to-friend direct messaging (backend/app/routers/chat.py).
// openDmSocket mirrors events.js's openChatSocket almost exactly —
// same reconnect-with-backoff behavior, same 4401/4403 close-code
// handling, same WS-auth-via-query-token approach — because it's the
// same underlying pattern (app/ws.py's ChatConnectionManager) applied
// to a friend pair instead of an event id, not a separate design.
import { apiClient, getWebSocketBase } from "./client.js";

export async function listConversations() {
  return apiClient.get("/chat/conversations");
}

/** @param {string} friendUserId */
export async function getMessages(friendUserId) {
  return apiClient.get(`/chat/${encodeURIComponent(friendUserId)}/messages`);
}

/** @param {string} friendUserId @param {string} body */
export async function sendMessage(friendUserId, body) {
  return apiClient.post(`/chat/${encodeURIComponent(friendUserId)}/messages`, { body });
}

/**
 * @param {string} friendUserId
 * @param {{onMessage?: Function, onError?: Function, onOpen?: Function, onStatus?: Function}} handlers
 */
export async function openDmSocket(friendUserId, { onMessage, onError, onOpen, onStatus } = {}) {
  const wsBase = getWebSocketBase();
  let ws = null;
  let closedByCaller = false;
  let attempt = 0;
  let reconnectTimer = null;

  async function connect() {
    if (closedByCaller) return;
    const token = await apiClient.tokens.get();
    ws = new WebSocket(`${wsBase}/chat/${encodeURIComponent(friendUserId)}/ws?token=${encodeURIComponent(token || "")}`);
    ws.onopen = () => { attempt = 0; onStatus && onStatus("connected"); onOpen && onOpen(); };
    ws.onmessage = (evt) => {
      try { onMessage && onMessage(JSON.parse(evt.data)); } catch (_) { /* ignore malformed frame */ }
    };
    ws.onerror = () => { onError && onError(); };
    ws.onclose = (evt) => {
      if (closedByCaller) return;
      if (evt.code === 4401 || evt.code === 4403) { onStatus && onStatus("closed"); return; }
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
