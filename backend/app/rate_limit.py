"""
Minimal rate limiting for auth endpoints (spec §17: "No rate limiting
anywhere" was a real gap — /auth/login and /auth/signup were previously
unlimited-attempt, which is a brute-force/spam risk on the two
endpoints that don't even require a prior valid token).

Deliberately NOT Redis-backed — a single-process in-memory sliding
window is correct and sufficient for a single backend instance, which
is exactly this MVP's deployment target (see README "Do not
overengineer"). If the app ever runs multiple replicas, swap the
in-memory dict for a Redis INCR+EXPIRE pair; the middleware's shape
doesn't need to change, just where the counter lives.
"""
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

# path prefix -> (max requests, window in seconds, method or None for any)
#
# The method field exists because /users/me is shared by frequent reads
# (GET, on every app boot) and the one write worth limiting (PATCH,
# profile edits — spam-editing a nickname/bio is the actual abuse case,
# per spec §5's moderation section). Without it, limiting "/users/me"
# would also throttle normal profile-loading GETs, which was never the
# intent.
LIMITS = {
    "/auth/login": (10, 60, None),
    "/auth/signup": (5, 60, None),
    "/auth/resend-signup-otp": (3, 300, None),
    # Defense in depth alongside PendingSignup.attempt_count (which
    # caps *wrong-guess* attempts per pending signup regardless of IP);
    # this caps request volume per IP regardless of correctness.
    "/auth/verify-signup-otp": (10, 60, None),
    # Password reset: same per-IP request-volume floor as signup's
    # equivalents above, alongside PasswordReset.attempt_count (wrong-
    # guess cap) and RESET_RESEND_COOLDOWN (per-email cooldown) enforced
    # in app/routers/auth.py itself.
    "/auth/request-password-reset": (5, 300, None),
    "/auth/reset-password": (10, 60, None),
    # media uploads are authenticated (unlike the two above), so this
    # limits per-IP rather than the more useful per-user — good enough
    # to stop naive disk-filling abuse without adding a second limiter
    # keyed on user id for what's still a single-instance MVP.
    "/media/upload": (20, 60, None),
    "/users/me": (15, 60, "PATCH"),
}

# Path-SUFFIX limits, for endpoints identified by a variable id in the
# middle of the path — /users/<uuid>/report and /events/<uuid>/report
# both end in "/report" but no fixed PREFIX isolates them without also
# throttling every other /users/* or /events/* call (which would catch
# ordinary profile/event browsing). Checked only when no prefix rule
# already matched. See dispatch() below for why the bucket key for a
# suffix match is the suffix itself, not the literal request path —
# using the literal path would let a client spam reports against many
# different target ids, each getting its own fresh bucket.
SUFFIX_LIMITS = {
    "/report": (10, 300, "POST"),
}

_hits: dict[str, deque] = defaultdict(deque)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # This is a per-process, in-memory, IP-keyed counter (see module
        # docstring) that persists for the life of the process — including
        # a whole pytest run. httpx's ASGITransport test client generally
        # doesn't populate a distinct request.client per test, so every
        # rate-limited call across an entire test session shares one
        # bucket; enough integration tests calling e.g. /auth/signup would
        # eventually 429 *unrelated* later tests, not just the ones
        # actually testing rate limiting. The business-logic limits that
        # matter for correctness (PendingSignup.attempt_count,
        # RESEND_COOLDOWN) are enforced in app/routers/auth.py regardless
        # of this bypass and are what the OTP tests actually exercise.
        if settings.ENV == "testing":
            return await call_next(request)
        limit_cfg = None
        limit_key = request.url.path
        for prefix, cfg in LIMITS.items():
            if request.url.path.startswith(prefix) and (cfg[2] is None or cfg[2] == request.method):
                limit_cfg = cfg
                break
        if not limit_cfg:
            for suffix, cfg in SUFFIX_LIMITS.items():
                if request.url.path.endswith(suffix) and (cfg[2] is None or cfg[2] == request.method):
                    limit_cfg = cfg
                    limit_key = suffix  # shared bucket across every target id, not one per id
                    break
        if limit_cfg:
            max_requests, window, _method = limit_cfg
            client_ip = request.client.host if request.client else "unknown"
            key = f"{client_ip}:{limit_key}"
            now = time.monotonic()
            q = _hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= max_requests:
                return JSONResponse(status_code=429, content={"detail": "Too many attempts — try again in a minute."})
            q.append(now)
        return await call_next(request)
