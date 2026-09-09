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

# path prefix -> (max requests, window in seconds)
LIMITS = {
    "/auth/login": (10, 60),
    "/auth/signup": (5, 60),
    # media uploads are authenticated (unlike the two above), so this
    # limits per-IP rather than the more useful per-user — good enough
    # to stop naive disk-filling abuse without adding a second limiter
    # keyed on user id for what's still a single-instance MVP.
    "/media/upload": (20, 60),
}

_hits: dict[str, deque] = defaultdict(deque)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        limit_cfg = None
        for prefix, cfg in LIMITS.items():
            if request.url.path.startswith(prefix):
                limit_cfg = cfg
                break
        if limit_cfg:
            max_requests, window = limit_cfg
            client_ip = request.client.host if request.client else "unknown"
            key = f"{client_ip}:{request.url.path}"
            now = time.monotonic()
            q = _hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= max_requests:
                return JSONResponse(status_code=429, content={"detail": "Too many attempts — try again in a minute."})
            q.append(now)
        return await call_next(request)
