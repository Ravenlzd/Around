"""
Notifications (spec §12). Read model only here — writes happen from
inside other routers (join_event, imfree nearby match, etc.) and from
the scheduled "starting soon" job described in the README.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Friendship, Notification, User
from app.deps import get_current_user
from app.routers.friends import ordered_pair

router = APIRouter()


@router.get("")
async def list_notifications(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(50)
    rows = (await db.execute(stmt)).scalars().all()
    out = []
    for n in rows:
        payload = dict(n.payload or {})
        if n.type == "friend_request" and payload.get("requester_id"):
            # Computed fresh from the same Friendship rows friends.py and
            # events.py already treat as the single source of truth — not
            # a stored flag — so Accept/Decline on the Activity screen
            # correctly stop appearing once the request has been resolved
            # from ANY entry point (this notification, the sender's
            # profile sheet, or a stale second tab), and that survives a
            # refresh with no extra state to keep in sync.
            try:
                requester_id = uuid.UUID(payload["requester_id"])
            except (ValueError, TypeError):
                requester_id = None
            still_pending = False
            if requester_id:
                a, b = ordered_pair(user.id, requester_id)
                row = await db.get(Friendship, {"user_id_a": a, "user_id_b": b})
                still_pending = bool(row and row.status == "pending")
            payload["still_pending"] = still_pending
        out.append({"id": str(n.id), "type": n.type, "payload": payload, "read": n.read_at is not None, "created_at": n.created_at.isoformat()})
    return out


@router.post("/{notification_id}/read")
async def mark_read(notification_id, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    n = await db.get(Notification, notification_id)
    if n and n.user_id == user.id and not n.read_at:
        n.read_at = datetime.now(timezone.utc)
        await db.commit()
    return {"status": "ok"}


@router.post("/read-all")
async def mark_all_read(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    await db.execute(
        update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None)).values(read_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return {"status": "ok"}
