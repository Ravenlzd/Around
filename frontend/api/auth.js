// frontend/api/auth.js
import { apiClient } from "./client.js";

/**
 * @typedef {Object} TokenResponse
 * @property {string} access_token
 * @property {string} token_type
 */

/**
 * Starts signup — does NOT create an account or return a token. The
 * account only exists once verifySignupOtp() below succeeds; this just
 * gets a 6-digit code emailed. @returns {Promise<{status: string, email: string}>}
 * @param {{email: string, password: string, display_name: string, city?: string}} payload
 */
export async function register(payload) {
  return apiClient.post("/auth/signup", { city: "Vilnius", ...payload }, { auth: false });
}

/**
 * Completes signup: creates the account and logs in, in one step.
 * @param {string} email @param {string} otp @returns {Promise<TokenResponse>}
 */
export async function verifySignupOtp(email, otp) {
  const res = await apiClient.post("/auth/verify-signup-otp", { email, otp }, { auth: false });
  await apiClient.tokens.set(res.access_token);
  return res;
}

/** @param {string} email */
export async function resendSignupOtp(email) {
  return apiClient.post("/auth/resend-signup-otp", { email }, { auth: false });
}

/**
 * @param {{email: string, password: string}} payload
 * @returns {Promise<TokenResponse>}
 */
export async function login(payload) {
  const res = await apiClient.post("/auth/login", payload, { auth: false });
  await apiClient.tokens.set(res.access_token);
  return res;
}

/** Google OAuth is stubbed server-side (501) — see backend README. Not called from the UI yet. */
export async function loginWithGoogle() {
  throw new Error("Google sign-in isn't wired up yet — use email and password for now.");
}

/** @returns {Promise<import('./types.js').UserOut>} */
export async function me() {
  return apiClient.get("/auth/me");
}

export async function logout() {
  try { await apiClient.post("/auth/logout", {}); } catch (_) { /* stateless JWT — clearing locally is what matters */ }
  await apiClient.tokens.clear();
}

/** @returns {Promise<boolean>} whether a token is stored (does NOT verify it's still valid — call me() for that) */
export async function hasStoredSession() {
  const token = await apiClient.tokens.get();
  return !!token;
}
