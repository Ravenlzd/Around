"""
"I'm Free" (spec §7). Upsert-one-active-status-per-user, always with an
expiry so the feature can't accumulate stale statuses. A scheduled job
(see README "Expiry job") hard-deletes expired rows every few minutes;
queries also always filter `expires_at > now()` as a second guard.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from geoalchemy2 import Geography
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID, ST_Distance
from sqlalchemy import cast, select, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Block, ImFreeStatus, User
from app.schemas import ImFreeCreate
from app.deps import get_current_user, require_verified_user

router = APIRouter()

WINDOW_TTL = {
    "now": timedelta(hours=3),
    "tonight": timedelta(hours=8),
    "tomorrow": timedelta(hours=30),
    "this_weekend": timedelta(days=3),
}


@router.post("")
async def activate(payload: ImFreeCreate, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
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

    # ImFreeStatus.location is a geography column (schema.sql:
    # GEOGRAPHY(POINT, 4326)) — ST_DistanceSphere only has a
    # geometry-geometry signature, so calling it with a geography column
    # and a bare geometry point (as this did previously) fails in
    # production with `UndefinedFunctionError: function
    # st_distancesphere(geography, geometry) does not exist` (confirmed
    # from Render logs; it never worked). ST_Distance on two geography
    # values returns geodesic meters directly — see the identical,
    # already-working pattern in discovery.py's nearby().
    user_point = cast(ST_SetSRID(ST_MakePoint(lng, lat), 4326), Geography)
    distance_expr = (ST_Distance(ImFreeStatus.location, user_point) / 1000).label("distance_km")
    stmt = (
        select(ImFreeStatus, distance_expr, User.display_name, User.avatar_url)
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
    # display_name/avatar_url added so the frontend can show who this is
    # and open their profile (the existing Add Friend / Block actions
    # live there) — there's no separate direct-message feature in this
    # app to wire up instead; the only messaging surface that exists at
    # all is per-event chat (app/ws.py), which doesn't apply to two
    # people who aren't sharing an event.
    return [
        {
            "user_id": str(s.user_id), "display_name": name, "avatar_url": avatar_url,
            "when": s.when_window, "looking_for": s.looking_for, "distance_km": round(d, 1),
        }
        for s, d, name, avatar_url in rows
    ]
