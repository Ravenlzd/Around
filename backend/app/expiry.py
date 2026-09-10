"""
The real implementation of the expiry job that README previously only
described as SQL a deployer would need to schedule themselves (spec
§6: "Do not leave the expiry logic as documentation-only SQL"). Runs as
an asyncio background task inside the FastAPI app's own lifespan — no
external scheduler process, no Celery, no Redis. Correct and sufficient
for a single-instance MVP; if this ever needs to run across multiple
backend replicas without doubling up, the natural next step is a
Postgres advisory lock around each sweep (`pg_try_advisory_lock`) so
only one replica's timer does the work per tick — not a different
technology, just one guard clause.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.database import async_session
from app.models import EventWaitlist, Event, SpontaneousPost, ImFreeStatus, GuestInvitation

logger = logging.getLogger("around.expiry")

SWEEP_INTERVAL_SECONDS = 60


async def _expire_waitlist_offers(db):
    """Flip timed-out 'offered' entries to 'expired', then offer the next person in line."""
    now = datetime.now(timezone.utc)
    expired = (await db.scalars(
        select(EventWaitlist).where(EventWaitlist.status == "offered", EventWaitlist.offer_expires_at < now)
    )).all()
    affected_event_ids = set()
    for entry in expired:
        entry.status = "expired"
        affected_event_ids.add(entry.event_id)
    if expired:
        await db.commit()

    # local import to avoid a circular import at module load time
    # (events.py imports things that eventually import this module's
    # sibling scheduler hook — keeping this import lazy sidesteps that)
    from app.routers.events import _offer_next_waitlist_spot
    for event_id in affected_event_ids:
        await _offer_next_waitlist_spot(db, event_id)


async def _expire_guest_invitations(db):
    now = datetime.now(timezone.utc)
    await db.execute(
        update(GuestInvitation)
        .where(GuestInvitation.status == "pending", GuestInvitation.expires_at.isnot(None), GuestInvitation.expires_at < now)
        .values(status="expired")
    )
    await db.commit()


async def _delete_expired_ephemeral_content(db):
    from sqlalchemy import delete
    now = datetime.now(timezone.utc)
    await db.execute(delete(SpontaneousPost).where(SpontaneousPost.expires_at < now))
    await db.execute(delete(ImFreeStatus).where(ImFreeStatus.expires_at < now))
    await db.commit()


async def _run_cleanup(label, cleanup):
    async with async_session() as db:
        try:
            await cleanup(db)
        except Exception:
            await db.rollback()
            logger.exception("%s failed", label)


async def run_sweep():
    # Each cleanup has independent semantics. A failed transaction must be
    # rolled back and discarded before another cleanup issues SQL.
    await _run_cleanup("waitlist offer expiry sweep", _expire_waitlist_offers)
    await _run_cleanup("guest invitation expiry sweep", _expire_guest_invitations)
    await _run_cleanup("spontaneous post / I'm Free expiry sweep", _delete_expired_ephemeral_content)


async def expiry_loop():
    """Runs forever, one sweep every SWEEP_INTERVAL_SECONDS, started from main.py's lifespan."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        await run_sweep()
