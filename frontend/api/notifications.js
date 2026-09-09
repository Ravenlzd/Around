// frontend/api/notifications.js
import { apiClient } from "./client.js";

/** @returns {Promise<import('./types.js').NotificationOut[]>} */
export async function listNotifications() {
  return apiClient.get("/notifications");
}

export async function markRead(notificationId) {
  return apiClient.post(`/notifications/${notificationId}/read`, {});
}

export async function markAllRead() {
  return apiClient.post("/notifications/read-all", {});
}
