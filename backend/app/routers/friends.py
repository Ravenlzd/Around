"""
Friends. Deliberately thin: friend request/accept/reject, mutual list,
invite-to-event. No follower counts surfaced anywhere by design.

This replaces a previous version that didn't persist anything — every
endpoint returned a plausible-looking response without writing a row.
That silently broke `access_mode='friends'` events in events.py, which
had no real friendship graph to check against and defaulted to
"treat everyone as a friend" (a real access-control bug, not just an
unfinished feature — see README "Security fixes"). `is_friends_with()`
below is now the single source of truth events.py calls.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, Friendship
from app.deps import get_current_user

router = APIRouter()


def ordered_pair(a: uuid.UUID, b: uuid.UUID):
    return (a, b) if str(a) < str(b) else (b, a)


async def is_friends_with(db: AsyncSession, user_id: uuid.UUID, other_id: uuid.UUID) -> bool:
    """The only function events.py should call to answer 'are these two users friends?'."""
    if user_id == other_id:
        return True
    a, b = ordered_pair(user_id, other_id)
    row = await db.get(Friendship, {"user_id_a": a, "user_id_b": b})
    return bool(row and row.status == "accepted")


@router.post("/request/{target_user_id}", status_code=status.HTTP_201_CREATED)
async def send_request(target_user_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if target_user_id == user.id:
        raise HTTPException(400, "Can't friend yourself")
    target = await db.get(User, target_user_id)
    if not target:
        raise HTTPException(404, "User not found")

    a, b = ordered_pair(user.id, target_user_id)
    existing = await db.get(Friendship, {"user_id_a": a, "user_id_b": b})
    if existing:
        if existing.status == "accepted":
            return {"status": "already_friends"}
        if existing.status == "blocked":
            raise HTTPException(403, "Can't send a request to this user")
        return {"status": "already_pending"}

    db.add(Friendship(user_id_a=a, user_id_b=b, status="pending", requested_by=user.id))
    await db.commit()
    # notification: "X sent you a friend request" — see app/notify.py.
    # requester_id is what previously made this a dead end: the Activity
    # screen had no way to know WHO to accept/decline without it (only a
    # display name baked into a message string), so the only way to
    # respond was going back to the sender's profile. list_notifications()
    # in notifications.py uses this id to compute still_pending fresh on
    # every read, which is also what makes Accept/Decline disappear
    # correctly after being resolved from any entry point.
    from app.notify import notify
    await notify(db, target_user_id, "friend_request", {
        "message": f"{user.display_name} sent you a friend request.",
        "requester_id": str(user.id),
    })
    return {"status": "requested"}


@router.post("/accept/{requester_id}")
async def accept_request(requester_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    a, b = ordered_pair(user.id, requester_id)
    row = await db.get(Friendship, {"user_id_a": a, "user_id_b": b})
    if not row or row.status != "pending":
        raise HTTPException(404, "No pending request from this user")
    if row.requested_by == user.id:
        raise HTTPException(400, "Can't accept your own request")
    row.status = "accepted"
    await db.commit()
    from app.notify import notify
    await notify(db, requester_id, "friend_accepted", {"message": f"{user.display_name} accepted your friend request."})
    return {"status": "accepted"}


@router.post("/reject/{requester_id}")
async def reject_request(requester_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    a, b = ordered_pair(user.id, requester_id)
    row = await db.get(Friendship, {"user_id_a": a, "user_id_b": b})
    if row and row.status == "pending":
        await db.delete(row)
        await db.commit()
    return {"status": "rejected"}


@router.get("")
async def list_friends(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(
        select(Friendship).where(
            or_(Friendship.user_id_a == user.id, Friendship.user_id_b == user.id),
            Friendship.status == "accepted",
        )
    )).all()
    other_ids = [r.user_id_b if r.user_id_a == user.id else r.user_id_a for r in rows]
    if not other_ids:
        return []
    users = (await db.execute(select(User.id, User.display_name).where(User.id.in_(other_ids)))).all()
    return [{"user_id": str(uid), "display_name": name} for uid, name in users]


@router.get("/requests")
async def list_pending_requests(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Requests where someone else asked to be friends with the current user."""
    rows = (await db.scalars(
        select(Friendship).where(
            or_(Friendship.user_id_a == user.id, Friendship.user_id_b == user.id),
            Friendship.status == "pending",
            Friendship.requested_by != user.id,
        )
    )).all()
    out = []
    for r in rows:
        other_id = r.user_id_b if r.user_id_a == user.id else r.user_id_a
        other = await db.get(User, other_id)
        out.append({"user_id": str(other_id), "display_name": other.display_name if other else "Someone"})
    return out


@router.post("/invite")
async def invite_to_event(target_user_id: uuid.UUID, event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.models import Event
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    from app.notify import notify
    await notify(db, target_user_id, "invited", {"message": f"{user.display_name} invited you to {event.title}.", "event_id": str(event_id)})
    return {"status": "invited"}
