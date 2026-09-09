// frontend/api/posts.js
import { apiClient } from "./client.js";

/** @param {{body: string, lat: number, lng: number, expiresInMinutes?: number}} payload */
export async function createPost({ body, lat, lng, expiresInMinutes = 60 }) {
  return apiClient.post("/posts", { body, latitude: lat, longitude: lng, expires_in_minutes: expiresInMinutes });
}

export async function nearbyPosts(lat, lng, radiusKm = 5) {
  return apiClient.get("/posts/nearby", { query: { lat, lng, radius_km: radiusKm } });
}
