"""
Real-time spontaneous posts (spec §8) — the lightweight "real-time
social layer over the physical city." Always ephemeral: every post
carries an expires_at set at creation from the caller-chosen TTL.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from geoalchemy2 import Geography
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID, ST_Distance
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import SpontaneousPost, User
from app.schemas import SpontaneousPostCreate
from app.deps import get_current_user

router = APIRouter()


@router.post("", status_code=201)
async def create_post(payload: SpontaneousPostCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
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
async def nearby_posts(lat: float, lng: float, radius_km: float = 5, db: AsyncSession = Depends(get_db)):
    # SpontaneousPost.location is a geography column (schema.sql:
    # GEOGRAPHY(POINT, 4326)) — ST_DistanceSphere only has a
    # geometry-geometry signature, so calling it with a geography column
    # and a bare geometry point (as this did previously) fails in
    # production with `UndefinedFunctionError: function
    # st_distancesphere(geography, geometry) does not exist` (it never
    # worked; the analogous /discovery/nearby query already used the
    # correct pattern below). ST_Distance on two geography values returns
    # geodesic meters directly and needs no separate "sphere" variant.
    user_point = cast(ST_SetSRID(ST_MakePoint(lng, lat), 4326), Geography)
    distance_m = ST_Distance(SpontaneousPost.location, user_point)
    stmt = (
        select(SpontaneousPost, (distance_m / 1000).label("distance_km"))
        .where(
            SpontaneousPost.expires_at > datetime.now(timezone.utc),
            distance_m <= radius_km * 1000,
        )
        .order_by(SpontaneousPost.created_at.desc())
        .limit(50)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {"id": str(p.id), "body": p.body, "distance_km": round(d, 2), "expires_at": p.expires_at.isoformat()}
        for p, d in rows
    ]
