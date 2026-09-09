"""
Notifications (spec §12). Read model only here — writes happen from
inside other routers (join_event, imfree nearby match, etc.) and from
the scheduled "starting soon" job described in the README.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Notification, User
from app.deps import get_current_user

router = APIRouter()


@router.get("")
async def list_notifications(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(50)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {"id": str(n.id), "type": n.type, "payload": n.payload, "read": n.read_at is not None, "created_at": n.created_at.isoformat()}
        for n in rows
    ]


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
