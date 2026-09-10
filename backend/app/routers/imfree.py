"""
"I'm Free" (spec §7). Upsert-one-active-status-per-user, always with an
expiry so the feature can't accumulate stale statuses. A scheduled job
(see README "Expiry job") hard-deletes expired rows every few minutes;
queries also always filter `expires_at > now()` as a second guard.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID, ST_DistanceSphere
from sqlalchemy import select, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Block, ImFreeStatus, User
from app.schemas import ImFreeCreate
from app.deps import get_current_user

router = APIRouter()

WINDOW_TTL = {
    "now": timedelta(hours=3),
    "tonight": timedelta(hours=8),
    "tomorrow": timedelta(hours=30),
    "this_weekend": timedelta(days=3),
}


@router.post("")
async def activate(payload: ImFreeCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ImFreeStatus).where(ImFreeStatus.user_id == user.id))
    status_row = ImFreeStatus(
        user_id=user.id,
        when_window=payload.when_window,
        looking_for=payload.looking_for,
        radius_km=payload.radius_km,
        location=ST_SetSRID(ST_MakePoint(payload.longitude, payload.latitude), 4326),
        expires_at=datetime.now(timezone.utc) + WINDOW_TTL[payload.when_window],
    )
    db.add(status_row)
    await db.commit()
    return {"status": "active", "expires_at": status_row.expires_at.isoformat()}


@router.delete("")
async def end_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ImFreeStatus).where(ImFreeStatus.user_id == user.id))
    await db.commit()
    return {"status": "ended"}


@router.get("/nearby")
async def nearby_free_people(
    lat: float, lng: float, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if user.hide_from_nearby:
        return []

    blocked_rows = (await db.scalars(
        select(Block).where(or_(Block.blocker_id == user.id, Block.blocked_id == user.id))
    )).all()
    excluded_ids = {user.id}
    for block in blocked_rows:
        excluded_ids.add(block.blocker_id)
        excluded_ids.add(block.blocked_id)

    user_point = ST_SetSRID(ST_MakePoint(lng, lat), 4326)
    distance_expr = (ST_DistanceSphere(ImFreeStatus.location, user_point) / 1000).label("distance_km")
    stmt = (
        select(ImFreeStatus, distance_expr)
        .join(User, User.id == ImFreeStatus.user_id)
        .where(
            ImFreeStatus.expires_at > datetime.now(timezone.utc),
            ImFreeStatus.user_id.notin_(excluded_ids),
            ImFreeStatus.location.is_not(None),
            User.city_id == user.city_id,
            User.hide_from_nearby.is_(False),
            or_(ImFreeStatus.radius_km.is_(None), distance_expr <= ImFreeStatus.radius_km),
        )
        .order_by(distance_expr.asc())
        .limit(30)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {"user_id": str(s.user_id), "when": s.when_window, "looking_for": s.looking_for, "distance_km": round(d, 1)}
        for s, d in rows
    ]
