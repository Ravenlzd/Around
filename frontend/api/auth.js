// frontend/api/auth.js
import { apiClient } from "./client.js";

/**
 * @typedef {Object} TokenResponse
 * @property {string} access_token
 * @property {string} token_type
 */

/**
 * Starts signup. Backend behavior depends on the server's own
 * REQUIRE_SIGNUP_OTP setting (currently off — "disable 2FA for now"):
 * - OTP required: returns {status:"otp_sent", email} — no account, no
 *   token, yet. See verifySignupOtp() below for where it's created.
 * - OTP disabled: creates the account immediately and returns a real
 *   TokenResponse, same as signup worked before the OTP flow existed.
 * Either shape can come back; the caller (app.js's submitAuth) checks
 * for access_token to tell them apart rather than this file guessing.
 * @param {{email: string, password: string, display_name: string, city?: string}} payload
 * @returns {Promise<{status: string, email: string} | TokenResponse>}
 */
export async function register(payload) {
  const res = await apiClient.post("/auth/signup", { city: "Vilnius", ...payload }, { auth: false });
  if (res && res.access_token) await apiClient.tokens.set(res.access_token);
  return res;
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

/**
 * Always resolves with the same generic shape whether or not the email
 * has an account — the backend never reveals that (account-enumeration
 * guard), so there's nothing for the caller to branch on besides a
 * genuine network/rate-limit error.
 * @param {string} email
 */
export async function requestPasswordReset(email) {
  return apiClient.post("/auth/request-password-reset", { email }, { auth: false });
}

/**
 * Completes a password reset: verifies the OTP, sets the new password,
 * and logs the user in with a fresh token (this also invalidates every
 * previously-issued token server-side — see app/deps.py::get_current_user).
 * @param {string} email @param {string} otp @param {string} newPassword
 * @returns {Promise<TokenResponse>}
 */
export async function resetPassword(email, otp, newPassword) {
  const res = await apiClient.post("/auth/reset-password", { email, otp, new_password: newPassword }, { auth: false });
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
