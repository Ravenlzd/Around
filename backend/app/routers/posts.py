"""
Real-time spontaneous posts (spec §8) — the lightweight "real-time
social layer over the physical city." Always ephemeral: every post
carries an expires_at set at creation from the caller-chosen TTL.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from geoalchemy2 import Geography
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID, ST_Distance
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blocking import blocked_ids
from app.database import get_db
from app.models import SpontaneousPost, User
from app.moderation import is_inappropriate
from app.schemas import SpontaneousPostCreate
from app.deps import get_current_user

router = APIRouter()


@router.post("", status_code=201)
async def create_post(payload: SpontaneousPostCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Broadcast to anyone nearby, city-wide — same stranger-visibility
    # level as an event description, previously unmoderated.
    if is_inappropriate(payload.body):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please remove inappropriate language from your post")
    post = SpontaneousPost(
        user_id=user.id,
        city_id=user.city_id,
        body=payload.body,
        location=ST_SetSRID(ST_MakePoint(payload.longitude, payload.latitude), 4326),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=payload.expires_in_minutes),
    )
    db.add(post)
    await db.commit()
    return {"id": str(post.id), "expires_at": post.expires_at.isoformat()}


@router.get("/nearby")
async def nearby_posts(
    lat: float, lng: float, radius_km: float = 5,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    # SpontaneousPost.location is a geography column (schema.sql:
    # GEOGRAPHY(POINT, 4326)) — ST_DistanceSphere only has a
    # geometry-geometry signature, so calling it with a geography column
    # and a bare geometry point (as this did previously) fails in
    # production with `UndefinedFunctionError: function
    # st_distancesphere(geography, geometry) does not exist` (it never
    # worked; the analogous /discovery/nearby query already used the
    # correct pattern below). ST_Distance on two geography values returns
    # geodesic meters directly and needs no separate "sphere" variant.
    #
    # SECURITY FIX (this pass): this endpoint had no auth dependency at
    # all — anyone, logged in or not, could read real-time,
    # location-tagged posts for any city. Excluding a blocked user's
    # posts requires knowing WHO's asking in the first place, so this
    # now requires login, matching every other discovery-style endpoint
    # in the app (/discovery/nearby, /discovery/people, etc. all already
    # require get_current_user).
    user_point = cast(ST_SetSRID(ST_MakePoint(lng, lat), 4326), Geography)
    distance_m = ST_Distance(SpontaneousPost.location, user_point)
    excluded_ids = await blocked_ids(db, user.id)
    conditions = [
        SpontaneousPost.expires_at > datetime.now(timezone.utc),
        distance_m <= radius_km * 1000,
        # A post's author row is never hard-deleted (soft-delete via
        # User.status only), so this can't silently drop a row via the
        # join below going unmatched — it deliberately excludes posts
        # from a suspended/deleted account, the same "author unavailable"
        # rule get_public_profile() already enforces for /users/{id}.
        User.status == "active",
    ]
    if excluded_ids:
        conditions.append(SpontaneousPost.user_id.notin_(excluded_ids))
    # Author identity, added this pass (spec: tapping a Quick Post's
    # author should open their profile — previously there was nothing
    # here to link to; the frontend fell back to a hardcoded "Someone").
    # Only display_name/avatar_url/user_id — the exact subset
    # GET /users/{id} (the existing public-profile endpoint) already
    # shows to any signed-in viewer regardless of relationship; nothing
    # more sensitive (bio, university, email, location) is added here,
    # and that endpoint remains the only place those live.
    stmt = (
        select(SpontaneousPost, (distance_m / 1000).label("distance_km"), User.display_name, User.avatar_url)
        .join(User, User.id == SpontaneousPost.user_id)
        .where(*conditions)
        .order_by(SpontaneousPost.created_at.desc())
        .limit(50)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": str(p.id), "body": p.body, "distance_km": round(d, 2), "expires_at": p.expires_at.isoformat(),
            "user_id": str(p.user_id), "display_name": display_name, "avatar_url": avatar_url,
        }
        for p, d, display_name, avatar_url in rows
    ]
