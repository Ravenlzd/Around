"""
Around — FastAPI entrypoint.

Run locally with:
    uvicorn app.main:app --reload

This file wires together the routers, plus two things that previously
didn't exist anywhere in the app (see README "Security & reliability
fixes"):
  - A startup guard that refuses to boot with the default/placeholder
    JWT_SECRET, so a deployer can't accidentally ship an app anyone
    could forge tokens for.
  - A background asyncio task (app/expiry.py) that actually runs the
    waitlist-offer/guest-invitation/ephemeral-content expiry sweep on a
    timer, instead of leaving it as SQL a deployer has to remember to
    schedule themselves.
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.routers import auth, events, discovery, imfree, posts, friends, notifications, users, media
from app.config import settings
from app.expiry import expiry_loop
from app.rate_limit import RateLimitMiddleware

logger = logging.getLogger("around")

INSECURE_JWT_SECRETS = {"change-me", "replace-with-a-long-random-string", "", "secret", "dev-only-change-me"}


def _check_jwt_secret():
    if settings.JWT_SECRET in INSECURE_JWT_SECRETS or len(settings.JWT_SECRET) < 16:
        message = (
            "JWT_SECRET is missing, a known placeholder, or too short. "
            "Anyone could forge a valid session token. Set a real random "
            "value (e.g. `openssl rand -hex 32`) in your environment before "
            "starting the server."
        )
        if settings.ENV == "production":
            raise RuntimeError(message)
        logger.warning("INSECURE JWT_SECRET (allowed only because ENV != 'production'): %s", message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_jwt_secret()
    task = asyncio.create_task(expiry_loop())
    logger.info("Around API starting — expiry sweep running every %ss", 60)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Around API",
    description="The live social layer of your city.",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(discovery.router, prefix="/discovery", tags=["discovery"])
app.include_router(imfree.router, prefix="/im-free", tags=["im-free"])
app.include_router(posts.router, prefix="/posts", tags=["spontaneous-posts"])
app.include_router(friends.router, prefix="/friends", tags=["friends"])
app.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
app.include_router(media.router, prefix="/media", tags=["media"])

# Serves whatever media.py writes to MEDIA_ROOT back out as static files
# at /media/<filename> — fine for local dev / a single small instance;
# swap for a real object-storage bucket + CDN before serving meaningful
# traffic (see app/routers/media.py's docstring for the seam).
app.mount("/media", StaticFiles(directory=media.MEDIA_ROOT), name="media")


@app.get("/health")
def health():
    return {"status": "ok"}
