"""
Discovery: the map/feed (spec §4) and search. `/discovery/nearby` powers
both the Map and Feed views on the home screen — same query, two
renderings. `/discovery/search` covers free-text + filters.

Both return the same card shape (`_card()`): capacity, occupancy,
access_mode, guest_policy, a location TEXT field that already respects
the reveal rule, and — as of this pass — reveal-aware latitude/
longitude so the frontend can actually plot a real map pin. Sending
coordinates for events the requester isn't authorized to see exactly
would defeat the whole point of the text-label reveal rule, so
`reveal_coordinates()` (app/location.py) is applied identically: exact
coordinates for public/authorized cases, a stable fuzzed point
otherwise. The full attendee list, chat, and host-only fields stay
behind `GET /events/{id}` since those aren't needed until someone opens
the event.

`private` events are excluded here unless the requester is already an
attendee or the host — matching "not publicly discoverable."
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID, ST_Distance, ST_X, ST_Y
from geoalchemy2 import Geography, Geometry
from sqlalchemy import select, func, or_, cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Event, EventParticipant, User, UserInterest, Friendship, Block
from app.ranking import EventCandidate, rank_events
from app.deps import get_current_user
from app.location import reveal_location, reveal_coordinates

router = APIRouter()

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


async def _occupancy_map(db: AsyncSession, event_ids: list) -> dict:
    if not event_ids:
        return {}
    rows = (await db.execute(
        select(EventParticipant.event_id, func.count())
        .where(EventParticipant.event_id.in_(event_ids), EventParticipant.status == "going")
        .group_by(EventParticipant.event_id)
    )).all()
    return {str(eid): count for eid, count in rows}


async def _host_name_map(db: AsyncSession, host_ids: list) -> dict:
    ids = [h for h in host_ids if h]
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.display_name).where(User.id.in_(ids)))).all()
    return {str(uid): name for uid, name in rows}


async def _user_interests(db: AsyncSession, user_id) -> set:
    """Real signal for ranking — was previously a hardcoded placeholder set."""
    rows = (await db.scalars(select(UserInterest.interest).where(UserInterest.user_id == user_id))).all()
    return set(rows)


async def _friend_ids(db: AsyncSession, user_id) -> set:
    rows = (await db.scalars(
        select(Friendship).where(
            or_(Friendship.user_id_a == user_id, Friendship.user_id_b == user_id),
            Friendship.status == "accepted",
        )
    )).all()
    return {(r.user_id_b if r.user_id_a == user_id else r.user_id_a) for r in rows}


async def _friends_attending_map(db: AsyncSession, event_ids: list, friend_ids: set) -> dict:
    """Real signal for ranking — was previously hardcoded to 0 for every event."""
    if not event_ids or not friend_ids:
        return {}
    rows = (await db.execute(
        select(EventParticipant.event_id, func.count())
        .where(
            EventParticipant.event_id.in_(event_ids),
            EventParticipant.status == "going",
            EventParticipant.user_id.in_(friend_ids),
        )
        .group_by(EventParticipant.event_id)
    )).all()
    return {str(eid): count for eid, count in rows}


def _card(ev: Event, lat: float, lng: float, distance_km, occupancy: int, user_id, is_attending: bool, host_name=None) -> dict:
    is_authorized = is_attending or ev.host_user_id == user_id
    location = reveal_location(ev, is_authorized=is_authorized, is_approved=is_authorized)
    pin_lat, pin_lng = reveal_coordinates(ev, lat=lat, lng=lng, is_authorized=is_authorized, is_approved=is_authorized)
    return {
        "id": str(ev.id),
        "title": ev.title,
        "category": ev.category,
        "description": ev.description,
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "starts_at": ev.starts_at.isoformat(),
        "ends_at": ev.ends_at.isoformat() if ev.ends_at else None,
        "capacity": ev.capacity,
        "occupancy": occupancy,
        "is_full": occupancy >= ev.capacity,
        "access_mode": ev.access_mode,
        "guest_policy": ev.guest_policy,
        "location_label": location,
        "latitude": pin_lat,
        "longitude": pin_lng,
        "cover_image_url": ev.cover_image_url,
        "host_name": host_name,
        "is_host": ev.host_user_id == user_id,
        "is_attending": is_attending,
    }


@router.get("/nearby")
async def nearby(
    lat: float = Query(...), lng: float = Query(...),
    radius_km: float = Query(10, le=50),
    category: str | None = None,
    limit: int = Query(DEFAULT_PAGE_SIZE, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_point = cast(ST_SetSRID(ST_MakePoint(lng, lat), 4326), Geography)
    distance_m = ST_Distance(Event.location, user_point)

    stmt = (
        select(Event, (distance_m / 1000).label("distance_km"), ST_Y(cast(Event.location, Geometry)).label("lat"), ST_X(cast(Event.location, Geometry)).label("lng"))
        .where(
            Event.status == "active",
            Event.starts_at > datetime.now(timezone.utc),
            distance_m <= radius_km * 1000,
            or_(Event.access_mode != "private", Event.host_user_id == user.id),
        )
    )
    if category:
        stmt = stmt.where(Event.category == category)

    all_rows = (await db.execute(stmt)).all()
    total = len(all_rows)
    event_ids = [ev.id for ev, *_ in all_rows]

    occ_map = await _occupancy_map(db, event_ids)
    host_names = await _host_name_map(db, [ev.host_user_id for ev, *_ in all_rows])
    user_interests = await _user_interests(db, user.id)
    friend_ids = await _friend_ids(db, user.id)
    friends_attending_map = await _friends_attending_map(db, event_ids, friend_ids)

    candidates = [
        EventCandidate(
            event_id=str(ev.id),
            distance_km=dist,
            minutes_until_start=(ev.starts_at - datetime.now(timezone.utc)).total_seconds() / 60,
            category=ev.category,
            spots_total=ev.capacity,
            spots_filled=occ_map.get(str(ev.id), 0),
            friends_attending=friends_attending_map.get(str(ev.id), 0),
            community_match=False,
        )
        for ev, dist, _lat, _lng in all_rows
    ]
    order = rank_events(candidates, user_interests)
    by_id = {str(ev.id): (ev, dist, lat_, lng_) for ev, dist, lat_, lng_ in all_rows}

    # Ranking happens over the FULL candidate set (so relevance isn't
    # skewed by pagination), then the page window is sliced off the
    # ranked order — this is what keeps "don't download every event in
    # the database" true without changing which page-1 events show up.
    page_ids = order[offset:offset + limit]

    result = []
    for eid in page_ids:
        ev, dist, ev_lat, ev_lng = by_id[eid]
        occ = occ_map.get(eid, 0)
        is_attending = await db.scalar(
            select(EventParticipant.id).where(
                EventParticipant.event_id == ev.id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
            )
        ) is not None
        result.append(_card(ev, ev_lat, ev_lng, dist, occ, user.id, is_attending, host_names.get(str(ev.host_user_id))))

    return {"results": result, "total": total, "limit": limit, "offset": offset, "has_more": offset + limit < total}


@router.get("/people")
async def people_nearby(
    limit: int = Query(20, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    "People around you" (spec: real functionality, privacy-respecting).
    Deliberately does NOT do GPS-proximity matching between two humans —
    that pattern (a live radar of nearby people) is exactly what turns a
    social-activity app into a dating app, which the product explicitly
    wants to avoid, and it would require tracking/broadcasting live
    personal location, which the rest of this app goes out of its way
    not to do. "Nearby" here means same city — a coarse, already-public
    signal — combined with real relationship signals: shared interests,
    events you've both actually attended, and friendship status.

    Excludes: yourself, anyone with hide_from_nearby set, and anyone
    with a block in either direction. Never returns coordinates,
    distance, or any location field for another user.
    """
    if user.hide_from_nearby:
        return []  # if you've hidden yourself, you don't get to browse others either — consistent, not punitive

    blocked_pairs = (await db.scalars(
        select(Block).where(or_(Block.blocker_id == user.id, Block.blocked_id == user.id))
    )).all()
    excluded_ids = {user.id}
    for b in blocked_pairs:
        excluded_ids.add(b.blocker_id)
        excluded_ids.add(b.blocked_id)

    candidates = (await db.scalars(
        select(User).where(
            User.city_id == user.city_id,
            User.hide_from_nearby.is_(False),
            User.id.notin_(excluded_ids),
        ).limit(200)  # coarse pre-filter before ranking; not "download every user"
    )).all()
    if not candidates:
        return []

    my_interests = await _user_interests(db, user.id)
    candidate_ids = [c.id for c in candidates]

    # shared interests per candidate
    interest_rows = (await db.execute(
        select(UserInterest.user_id, UserInterest.interest).where(UserInterest.user_id.in_(candidate_ids))
    )).all()
    interests_by_user: dict = {}
    for uid, interest in interest_rows:
        interests_by_user.setdefault(uid, set()).add(interest)

    # mutual events: events where BOTH me and the candidate have a 'going' row
    my_event_ids = set((await db.scalars(
        select(EventParticipant.event_id).where(EventParticipant.user_id == user.id, EventParticipant.status == "going")
    )).all())
    mutual_counts: dict = {}
    if my_event_ids:
        rows = (await db.execute(
            select(EventParticipant.user_id, func.count())
            .where(
                EventParticipant.user_id.in_(candidate_ids),
                EventParticipant.event_id.in_(my_event_ids),
                EventParticipant.status == "going",
            )
            .group_by(EventParticipant.user_id)
        )).all()
        mutual_counts = {uid: c for uid, c in rows}

    friend_rows = (await db.scalars(
        select(Friendship).where(
            or_(Friendship.user_id_a == user.id, Friendship.user_id_b == user.id),
            or_(Friendship.user_id_a.in_(candidate_ids), Friendship.user_id_b.in_(candidate_ids)),
        )
    )).all()
    friendship_by_user = {}
    for f in friend_rows:
        other = f.user_id_b if f.user_id_a == user.id else f.user_id_a
        friendship_by_user[other] = f.status

    results = []
    for c in candidates:
        shared = my_interests & interests_by_user.get(c.id, set())
        mutual = mutual_counts.get(c.id, 0)
        if not shared and not mutual:
            continue  # no signal to show them by — avoids a bare "random strangers" list
        results.append({
            "user_id": str(c.id),
            "display_name": c.display_name,
            "university_or_work": c.university_or_work,
            "avatar_url": c.avatar_url,
            "shared_interests": sorted(shared),
            "mutual_events": mutual,
            "friendship_status": friendship_by_user.get(c.id, "none"),
        })

    results.sort(key=lambda r: (len(r["shared_interests"]) + r["mutual_events"]), reverse=True)
    return results[:limit]
async def search(
    q: str = Query(""), category: str | None = None,
    limit: int = Query(DEFAULT_PAGE_SIZE, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conditions = [Event.status == "active", or_(Event.access_mode != "private", Event.host_user_id == user.id)]
    if q:
        conditions.append(Event.search_vector.match(q))
    if category:
        conditions.append(Event.category == category)

    total = await db.scalar(select(func.count()).select_from(Event).where(*conditions))

    stmt = (
        select(Event, ST_Y(cast(Event.location, Geometry)).label("lat"), ST_X(cast(Event.location, Geometry)).label("lng"))
        .where(*conditions)
        .order_by(Event.starts_at.asc())
        .limit(limit).offset(offset)
    )
    rows = (await db.execute(stmt)).all()
    events = [ev for ev, _lat, _lng in rows]

    occ_map = await _occupancy_map(db, [e.id for e in events])
    host_names = await _host_name_map(db, [e.host_user_id for e in events])
    attending_ids = set((await db.scalars(
        select(EventParticipant.event_id).where(
            EventParticipant.user_id == user.id, EventParticipant.status == "going",
            EventParticipant.event_id.in_([e.id for e in events] or [None]),
        )
    )).all())

    results = [
        _card(ev, ev_lat, ev_lng, None, occ_map.get(str(ev.id), 0), user.id, ev.id in attending_ids, host_names.get(str(ev.host_user_id)))
        for ev, ev_lat, ev_lng in rows
    ]
    return {"results": results, "total": total or 0, "limit": limit, "offset": offset, "has_more": offset + limit < (total or 0)}
