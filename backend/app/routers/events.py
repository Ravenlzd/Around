"""
Events: capacity, guests, approval, waitlist, check-in (spec §1-11, §18).

=== Capacity & concurrency strategy (see also README) ===
Every operation that can change occupancy (join, approve a request,
claim a waitlist offer, accept a guest invite) runs inside a single
transaction that starts with:

    SELECT * FROM events WHERE id = :event_id FOR UPDATE

This takes a row lock on the event itself. Two concurrent join attempts
against the same event serialize on that lock: the second transaction
blocks until the first commits (or rolls back), and by the time it
proceeds it sees the first transaction's inserted event_participants
row. Occupancy is then recomputed fresh from event_participants inside
the same transaction — never read from a cached counter — so the
capacity check is always against the true current state. This is the
"appropriate row lock" called for in spec §7: it doesn't need a
separate advisory lock or SERIALIZABLE isolation, because the event row
itself is the natural contention point and every writer takes the same
lock before writing.

Pending join requests and pending waitlist offers do NOT hold a lock or
reserve a spot ahead of time — they're just rows with status='pending'/
'offered'. The capacity check happens again, under the same lock, at
the moment of approval/claim. That means an approval can fail if the
event filled up while the request was pending; the API surfaces that
as a 409 so the host UI can show "can't approve, event is full."
"""
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import WebSocket, WebSocketDisconnect
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID, ST_X, ST_Y
from geoalchemy2 import Geometry
from sqlalchemy import cast as sa_cast
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    Event, EventParticipant, EventMessage, EventJoinRequest, EventWaitlist,
    GuestInvitation, EventCheckIn, EventBan, User, GUEST_POLICY_MAX,
)
from app.schemas import (
    EventCreate, EventUpdate, EventOut, ChatMessageCreate, JoinRequestCreate, GuestInviteCreate,
    CheckInCreate, CheckInTokenOut, AttendanceOut, BanCreate,
)
from app.deps import get_current_user
from app import attendance_rules as rules
from app.location import reveal_location, reveal_coordinates
from app.ws import chat_ws_manager
from app.notify import notify
from app.routers import friends

router = APIRouter()

# HTTP status per decision "reason" code — keeps the mapping in one place
# rather than repeating status picks at every call site.
_REASON_STATUS = {
    "banned": status.HTTP_403_FORBIDDEN,
    "already_attending": status.HTTP_409_CONFLICT,
    "requires_approval": status.HTTP_409_CONFLICT,
    "requires_invite": status.HTTP_409_CONFLICT,
    "not_joinable": status.HTTP_403_FORBIDDEN,
    "friends_only": status.HTTP_403_FORBIDDEN,
    "event_full": status.HTTP_409_CONFLICT,
    "wrong_mode": status.HTTP_400_BAD_REQUEST,
    "invalid_code": status.HTTP_403_FORBIDDEN,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "already_decided": status.HTTP_409_CONFLICT,
    "guests_not_allowed": status.HTTP_400_BAD_REQUEST,
    "guest_limit_reached": status.HTTP_409_CONFLICT,
    "already_waitlisted": status.HTTP_409_CONFLICT,
    "no_active_offer": status.HTTP_404_NOT_FOUND,
    "offer_expired": status.HTTP_410_GONE,
    "cannot_remove_host": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "not_going": status.HTTP_409_CONFLICT,
    "not_active": status.HTTP_409_CONFLICT,
    "capacity_below_occupancy": status.HTTP_409_CONFLICT,
    "already_cancelled": status.HTTP_409_CONFLICT,
}


def _enforce(decision: rules.Decision):
    """Raise HTTPException if a pure-function Decision disallows the action."""
    if not decision.allowed:
        raise HTTPException(_REASON_STATUS.get(decision.reason, status.HTTP_400_BAD_REQUEST), decision.message)


# ---------- shared helpers ----------

async def _lock_event(db: AsyncSession, event_id: uuid.UUID) -> Event:
    event = await db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if event.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Event is not active")
    if event.starts_at < datetime.now(timezone.utc) and event.ends_at and event.ends_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_409_CONFLICT, "Event has already ended")
    return event


async def _occupancy(db: AsyncSession, event_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count()).select_from(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.status == "going"
        )
    ) or 0


async def _assert_not_banned(db: AsyncSession, event_id: uuid.UUID, user_id: uuid.UUID):
    banned = await db.get(EventBan, {"event_id": event_id, "user_id": user_id})
    if banned:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You've been removed from this event by the host")


async def _assert_not_already_attending(db: AsyncSession, event_id: uuid.UUID, user_id: uuid.UUID):
    existing = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user_id,
            EventParticipant.status == "going",
        )
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Already attending this event")


def _to_out(event: Event, occupancy: int, distance_km: float | None = None, host_name: str | None = None) -> EventOut:
    return EventOut(
        id=event.id, title=event.title, category=event.category, description=event.description,
        distance_km=distance_km, starts_at=event.starts_at, capacity=event.capacity, occupancy=occupancy,
        access_mode=event.access_mode, guest_policy=event.guest_policy, is_full=occupancy >= event.capacity,
        host_name=host_name, cover_image_url=event.cover_image_url, status=event.status,
    )


# ---------- create / read ----------

@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
async def create_event(payload: EventCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    event = Event(
        host_user_id=user.id,
        city_id=user.city_id,
        title=payload.title,
        category=payload.category,
        description=payload.description,
        location=ST_SetSRID(ST_MakePoint(payload.longitude, payload.latitude), 4326),
        location_label=payload.location_label,
        approx_location_label=payload.approx_location_label,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        capacity=payload.capacity,
        access_mode=payload.access_mode,
        guest_policy=payload.guest_policy,
        location_reveal=payload.location_reveal,
        chat_enabled=payload.chat_enabled,
        cover_image_url=payload.cover_image_url,
    )
    db.add(event)
    await db.flush()
    db.add(EventParticipant(event_id=event.id, user_id=user.id, type="host", status="going"))
    await db.commit()
    return _to_out(event, occupancy=1, host_name=user.display_name)


@router.get("/{event_id}")
async def get_event(event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    The full event-detail payload — everything the frontend's event
    sheet needs in one request: attendee list (with guest nesting),
    the requester's own relationship to the event (attending/requested/
    waitlisted), host-only pending requests, and a location string
    already shaped by the reveal rule for this specific requester.
    """
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    host = await db.get(User, event.host_user_id) if event.host_user_id else None
    is_host = event.host_user_id == user.id

    going = (await db.scalars(
        select(EventParticipant).where(EventParticipant.event_id == event_id, EventParticipant.status == "going")
    )).all()
    occ = len(going)
    is_attending = is_host or any(p.user_id == user.id and p.type != "guest" for p in going)

    my_request = await db.scalar(
        select(EventJoinRequest).where(
            EventJoinRequest.event_id == event_id, EventJoinRequest.user_id == user.id, EventJoinRequest.status == "pending"
        )
    )
    my_waitlist = await db.scalar(
        select(EventWaitlist).where(
            EventWaitlist.event_id == event_id, EventWaitlist.user_id == user.id,
            EventWaitlist.status.in_(["waiting", "offered"]),
        )
    )
    my_waitlist_position = None
    if my_waitlist:
        ahead = await db.scalar(
            select(func.count()).select_from(EventWaitlist).where(
                EventWaitlist.event_id == event_id, EventWaitlist.status.in_(["waiting", "offered"]),
                EventWaitlist.created_at < my_waitlist.created_at,
            )
        )
        my_waitlist_position = (ahead or 0) + 1

    # attendee names: batch-load the users referenced by `going` rows
    user_ids = [p.user_id for p in going if p.user_id]
    names = {}
    if user_ids:
        rows = (await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids)))).all()
        names = {str(uid): name for uid, name in rows}

    participants_out = [
        {
            "participant_id": str(p.id),
            "name": names.get(str(p.user_id), p.guest_name or "Guest"),
            "type": p.type,
            "invited_by": names.get(str(p.invited_by_user_id)) if p.invited_by_user_id else None,
            "checked_in": p.checked_in_at is not None,
        }
        for p in going
    ]

    pending_requests_out = []
    if is_host:
        pending = (await db.scalars(
            select(EventJoinRequest).where(EventJoinRequest.event_id == event_id, EventJoinRequest.status == "pending")
        )).all()
        req_names_rows = (await db.execute(
            select(User.id, User.display_name).where(User.id.in_([r.user_id for r in pending] or [uuid.uuid4()]))
        )).all()
        req_names = {str(uid): name for uid, name in req_names_rows}
        pending_requests_out = [
            {"id": str(r.id), "user_id": str(r.user_id), "name": req_names.get(str(r.user_id), "Someone")}
            for r in pending
        ]

    location = reveal_location(event, is_authorized=is_attending, is_approved=bool(my_request and my_request.status == "approved"))
    ev_lat, ev_lng = (await db.execute(select(ST_Y(sa_cast(Event.location, Geometry)), ST_X(sa_cast(Event.location, Geometry))).where(Event.id == event_id))).one()
    pin_lat, pin_lng = reveal_coordinates(
        event, lat=ev_lat, lng=ev_lng, is_authorized=is_attending,
        is_approved=bool(my_request and my_request.status == "approved"),
    )

    return {
        "id": str(event.id), "title": event.title, "category": event.category, "description": event.description,
        "starts_at": event.starts_at.isoformat(), "ends_at": event.ends_at.isoformat() if event.ends_at else None,
        "capacity": event.capacity, "occupancy": occ, "is_full": occ >= event.capacity,
        "access_mode": event.access_mode, "guest_policy": event.guest_policy, "invite_required": event.access_mode == "invite_only",
        "location_label": location, "location_reveal": event.location_reveal,
        "latitude": pin_lat, "longitude": pin_lng, "cover_image_url": event.cover_image_url,
        "host_name": host.display_name if host else None, "is_host": is_host,
        "chat_enabled": event.chat_enabled, "status": event.status,
        "participants": participants_out,
        "pending_requests": pending_requests_out,   # only populated for the host
        "my_status": {
            "is_attending": is_attending,
            "has_pending_request": my_request is not None,
            "waitlist_position": my_waitlist_position,
            "waitlist_status": my_waitlist.status if my_waitlist else None,
        },
    }


# ---------- update / cancel (host-only) ----------

@router.patch("/{event_id}", response_model=EventOut)
async def update_event(
    event_id: uuid.UUID, payload: EventUpdate,
    host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """
    Host-only edit. Locks the event row (same as every capacity-
    sensitive write in this file) specifically because the capacity
    check below needs a consistent occupancy read — an edit racing a
    concurrent join could otherwise approve a capacity reduction based
    on a stale attendee count.
    """
    event = await db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    occ = await _occupancy(db, event_id)
    decision = rules.decide_update_event(
        is_host=(event.host_user_id == host.id), event_status=event.status,
        new_capacity=payload.capacity, current_occupancy=occ,
    )
    _enforce(decision)

    updates = payload.model_dump(exclude_unset=True, exclude={"latitude", "longitude"})
    for field, value in updates.items():
        setattr(event, field, value)
    if payload.latitude is not None and payload.longitude is not None:
        event.location = ST_SetSRID(ST_MakePoint(payload.longitude, payload.latitude), 4326)

    # Notify attendees once per edit (not once per changed field) — a
    # generic "event was updated" is enough for them to go re-check
    # what changed, without guessing which fields matter enough to spam
    # about individually.
    if updates or (payload.latitude is not None):
        going_user_ids = (await db.scalars(
            select(EventParticipant.user_id).where(
                EventParticipant.event_id == event_id, EventParticipant.status == "going",
                EventParticipant.user_id.isnot(None), EventParticipant.user_id != host.id,
            )
        )).all()
        for uid in going_user_ids:
            await notify(db, uid, "event_updated", {"message": f"{event.title} was updated by the host.", "event_id": str(event_id)})

    await db.commit()
    await db.refresh(event)
    return _to_out(event, occ, host_name=host.display_name)


@router.post("/{event_id}/cancel", status_code=status.HTTP_200_OK)
async def cancel_event(
    event_id: uuid.UUID, reason: str | None = None,
    host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """
    Marks the event cancelled rather than deleting it — historical
    attendance (who went, who was checked in, chat history) stays
    intact for everyone who already has it in their Activity/Profile.
    Everything downstream that matters is already gated on
    `status == 'active'`:
      - join/leave/approve/claim/guest-invite all go through
        `_lock_event()`, which already rejects a non-active event.
      - `/discovery/*` already filters `status == 'active'`, so a
        cancelled event silently drops out of Map/Feed/Search.
      - check-in and check-in-token generation are additionally gated
        explicitly below, since they don't route through `_lock_event`.
    What THIS endpoint is responsible for: flipping the status (under
    the same row lock as every other capacity-relevant write, so it
    can't race a concurrent join), closing out anything left pending
    (waitlist entries, join requests, guest invitations) rather than
    leaving them dangling forever, and notifying everyone affected.
    """
    event = await db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    decision = rules.decide_cancel_event(is_host=(event.host_user_id == host.id), event_status=event.status)
    _enforce(decision)

    event.status = "cancelled"

    affected_user_ids = set()

    going = (await db.scalars(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.status == "going",
            EventParticipant.user_id.isnot(None), EventParticipant.user_id != host.id,
        )
    )).all()
    affected_user_ids.update(p.user_id for p in going)

    waitlisted = (await db.scalars(
        select(EventWaitlist).where(EventWaitlist.event_id == event_id, EventWaitlist.status.in_(["waiting", "offered"]))
    )).all()
    for w in waitlisted:
        w.status = "cancelled"
        affected_user_ids.add(w.user_id)

    pending_requests = (await db.scalars(
        select(EventJoinRequest).where(EventJoinRequest.event_id == event_id, EventJoinRequest.status == "pending")
    )).all()
    for r in pending_requests:
        r.status = "cancelled"
        affected_user_ids.add(r.user_id)

    pending_guest_invites = (await db.scalars(
        select(GuestInvitation).where(GuestInvitation.event_id == event_id, GuestInvitation.status == "pending")
    )).all()
    for g in pending_guest_invites:
        g.status = "cancelled"

    message = f"{event.title} was cancelled by the host." + (f" \u201c{reason}\u201d" if reason else "")
    for uid in affected_user_ids:
        await notify(db, uid, "event_cancelled", {"message": message, "event_id": str(event_id)})

    await db.commit()
    return {"status": "cancelled", "notified": len(affected_user_ids)}


# ---------- join / leave ----------

@router.post("/{event_id}/join", status_code=status.HTTP_200_OK)
async def join_event(event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    event = await _lock_event(db, event_id)
    is_banned = await db.get(EventBan, {"event_id": event_id, "user_id": user.id}) is not None
    already = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
        )
    ) is not None
    occ = await _occupancy(db, event_id)
    # SECURITY FIX: this used to hardcode is_friend_of_host=True, which
    # meant `access_mode='friends'` events were joinable by anyone — a
    # real access-control bypass, not just an unfinished feature. Now
    # backed by the real friendship graph in app/routers/friends.py.
    is_friend = await friends.is_friends_with(db, user.id, event.host_user_id) if event.host_user_id else False
    decision = rules.decide_join(
        access_mode=event.access_mode, capacity=event.capacity, occupancy=occ,
        is_banned=is_banned, already_attending=already, is_friend_of_host=is_friend,
    )
    _enforce(decision)

    db.add(EventParticipant(event_id=event_id, user_id=user.id, type="participant", status="going"))
    if event.host_user_id:
        await notify(db, event.host_user_id, "joined", {"message": f"{user.display_name} joined your event: {event.title}."})
        left = event.capacity - (occ + 1)
        if 0 < left <= 2:
            others = (await db.scalars(
                select(EventParticipant.user_id).where(
                    EventParticipant.event_id == event_id, EventParticipant.status == "going", EventParticipant.user_id.isnot(None)
                )
            )).all()
            for other_id in others:
                if other_id != user.id:
                    await notify(db, other_id, "spots_low", {"message": f"Only {left} spot{'s' if left>1 else ''} left in {event.title}."})
    await db.commit()
    return {"status": "joined"}


@router.post("/{event_id}/join-with-code", status_code=status.HTTP_200_OK)
async def join_with_invite_code(
    event_id: uuid.UUID, code: str,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await _lock_event(db, event_id)
    is_banned = await db.get(EventBan, {"event_id": event_id, "user_id": user.id}) is not None
    already = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
        )
    ) is not None
    occ = await _occupancy(db, event_id)
    decision = rules.decide_join_with_code(
        access_mode=event.access_mode, capacity=event.capacity, occupancy=occ,
        is_banned=is_banned, already_attending=already, code=code, expected_code=event.invite_code,
    )
    _enforce(decision)

    db.add(EventParticipant(event_id=event_id, user_id=user.id, type="participant", status="going"))
    await db.commit()
    return {"status": "joined"}


@router.post("/{event_id}/leave", status_code=status.HTTP_200_OK)
async def leave_event(event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    event = await _lock_event(db, event_id)
    participant = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
        )
    )
    if not participant:
        return {"status": "not_attending"}
    if participant.type == "host":
        raise HTTPException(status.HTTP_409_CONFLICT, "Hosts cancel the event instead of leaving it")

    participant.status = "left"
    # your guests leave with you — see GuestInvitation docstring: guest
    # authorization is scoped to the inviting participant, not the event.
    guests = (await db.scalars(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.invited_by_user_id == user.id,
            EventParticipant.status == "going",
        )
    )).all()
    for g in guests:
        g.status = "left"
    await db.commit()
    await _offer_next_waitlist_spot(db, event_id)
    return {"status": "left"}


# ---------- join requests (approval mode) ----------

@router.post("/{event_id}/join-requests", status_code=status.HTTP_201_CREATED)
async def create_join_request(
    event_id: uuid.UUID, payload: JoinRequestCreate,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await db.get(Event, event_id)
    if not event or event.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if event.access_mode != "approval":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This event doesn't require approval")
    await _assert_not_banned(db, event_id, user.id)
    await _assert_not_already_attending(db, event_id, user.id)

    existing = await db.scalar(
        select(EventJoinRequest).where(
            EventJoinRequest.event_id == event_id, EventJoinRequest.user_id == user.id, EventJoinRequest.status == "pending"
        )
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Request already pending")

    # NOTE: pending requests do NOT check/reserve capacity — see module docstring.
    req = EventJoinRequest(event_id=event_id, user_id=user.id, guest_count_requested=payload.guest_count_requested)
    db.add(req)
    if event.host_user_id:
        await notify(db, event.host_user_id, "join_request", {"message": f"{user.display_name} requested to join {event.title}.", "event_id": str(event_id), "request_id": str(req.id)})
    await db.commit()
    return {"status": "requested", "id": str(req.id)}


@router.post("/{event_id}/join-requests/{request_id}/approve", status_code=status.HTTP_200_OK)
async def approve_join_request(
    event_id: uuid.UUID, request_id: uuid.UUID,
    host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await _lock_event(db, event_id)  # lock BEFORE re-checking capacity
    if event.host_user_id != host.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can approve requests")

    req = await db.get(EventJoinRequest, request_id)
    if not req or req.event_id != event_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")

    occ = await _occupancy(db, event_id)
    decision = rules.decide_approve_request(
        capacity=event.capacity, occupancy=occ, is_host=(event.host_user_id == host.id), request_status=req.status
    )
    _enforce(decision)

    req.status = "approved"
    req.decided_at = datetime.now(timezone.utc)
    req.decided_by = host.id
    db.add(EventParticipant(event_id=event_id, user_id=req.user_id, type="participant", status="going"))
    await notify(db, req.user_id, "approved", {"message": f"Your request to join {event.title} was approved.", "event_id": str(event_id)})
    await db.commit()
    return {"status": "approved"}


@router.post("/{event_id}/join-requests/{request_id}/reject", status_code=status.HTTP_200_OK)
async def reject_join_request(
    event_id: uuid.UUID, request_id: uuid.UUID,
    host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await db.get(Event, event_id)
    if not event or event.host_user_id != host.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can reject requests")
    req = await db.get(EventJoinRequest, request_id)
    if not req or req.event_id != event_id or req.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found or already decided")
    req.status = "rejected"
    req.decided_at = datetime.now(timezone.utc)
    req.decided_by = host.id
    await notify(db, req.user_id, "rejected", {"message": f"Your request to join {event.title} wasn't approved this time.", "event_id": str(event_id)})
    await db.commit()
    return {"status": "rejected"}


@router.delete("/{event_id}/join-requests/mine", status_code=status.HTTP_200_OK)
async def cancel_my_join_request(event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    Lets a requester cancel their own pending request (was previously a
    known gap — the frontend's "tap to cancel" told the user to ask the
    host instead, since this endpoint didn't exist).
    """
    req = await db.scalar(
        select(EventJoinRequest).where(
            EventJoinRequest.event_id == event_id, EventJoinRequest.user_id == user.id, EventJoinRequest.status == "pending"
        )
    )
    if req:
        req.status = "cancelled"
        req.decided_at = datetime.now(timezone.utc)
        await db.commit()
    return {"status": "cancelled"}


# ---------- waitlist ----------

@router.post("/{event_id}/waitlist", status_code=status.HTTP_201_CREATED)
async def join_waitlist(event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if not event or event.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    is_banned = await db.get(EventBan, {"event_id": event_id, "user_id": user.id}) is not None
    already_attending = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
        )
    ) is not None
    existing = await db.scalar(
        select(EventWaitlist).where(
            EventWaitlist.event_id == event_id, EventWaitlist.user_id == user.id,
            EventWaitlist.status.in_(["waiting", "offered"]),
        )
    )
    decision = rules.decide_waitlist_join(is_banned=is_banned, already_on_waitlist=existing is not None, already_attending=already_attending)
    _enforce(decision)

    db.add(EventWaitlist(event_id=event_id, user_id=user.id))
    await db.commit()

    position = await db.scalar(
        select(func.count()).select_from(EventWaitlist).where(
            EventWaitlist.event_id == event_id, EventWaitlist.status.in_(["waiting", "offered"])
        )
    )
    return {"status": "waitlisted", "position": position}


@router.delete("/{event_id}/waitlist", status_code=status.HTTP_200_OK)
async def leave_waitlist(event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    entry = await db.scalar(
        select(EventWaitlist).where(
            EventWaitlist.event_id == event_id, EventWaitlist.user_id == user.id,
            EventWaitlist.status.in_(["waiting", "offered"]),
        )
    )
    if entry:
        entry.status = "cancelled"
        await db.commit()
        await _offer_next_waitlist_spot(db, event_id)
    return {"status": "left_waitlist"}


@router.post("/{event_id}/waitlist/claim", status_code=status.HTTP_200_OK)
async def claim_waitlist_offer(event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    event = await _lock_event(db, event_id)
    entry = await db.scalar(
        select(EventWaitlist).where(
            EventWaitlist.event_id == event_id, EventWaitlist.user_id == user.id, EventWaitlist.status == "offered"
        )
    )
    entry_status = entry.status if entry else "none"
    is_expired = bool(entry and entry.offer_expires_at and entry.offer_expires_at < datetime.now(timezone.utc))
    if entry and is_expired:
        entry.status = "expired"
        await db.commit()
        await _offer_next_waitlist_spot(db, event_id)

    occ = await _occupancy(db, event_id)
    decision = rules.decide_waitlist_claim(entry_status=entry_status, offer_expired=is_expired, capacity=event.capacity, occupancy=occ)
    _enforce(decision)

    entry.status = "claimed"
    db.add(EventParticipant(event_id=event_id, user_id=user.id, type="participant", status="going"))
    await db.commit()
    return {"status": "claimed"}


async def _offer_next_waitlist_spot(db: AsyncSession, event_id: uuid.UUID):
    """
    Called after any operation that frees a spot. Runs under its own
    lock so it composes safely with whatever just committed. In
    production this also gets called from the periodic expiry job
    (see README) so an unclaimed offer cascades to the next person
    without waiting for another attendance-changing event.
    """
    event = await _lock_event(db, event_id)
    occ = await _occupancy(db, event_id)
    if occ >= event.capacity:
        return
    already_offered = await db.scalar(
        select(EventWaitlist).where(EventWaitlist.event_id == event_id, EventWaitlist.status == "offered")
    )
    if already_offered:
        return
    next_entry = await db.scalar(
        select(EventWaitlist)
        .where(EventWaitlist.event_id == event_id, EventWaitlist.status == "waiting")
        .order_by(EventWaitlist.created_at.asc())
    )
    if not next_entry:
        return
    next_entry.status = "offered"
    next_entry.offered_at = datetime.now(timezone.utc)
    from datetime import timedelta
    next_entry.offer_expires_at = datetime.now(timezone.utc) + timedelta(minutes=event.waitlist_claim_minutes)
    await notify(db, next_entry.user_id, "waitlist_offer", {
        "message": f"A spot opened for {event.title} — claim it within {event.waitlist_claim_minutes} minutes.",
        "event_id": str(event_id),
    })
    await db.commit()


# ---------- guests ----------

@router.post("/{event_id}/guests", status_code=status.HTTP_201_CREATED)
async def invite_guest(
    event_id: uuid.UUID, payload: GuestInviteCreate,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await _lock_event(db, event_id)

    inviter = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user.id,
            EventParticipant.status == "going", EventParticipant.type.in_(["host", "participant"]),
        )
    )
    existing_guest_count = await db.scalar(
        select(func.count()).select_from(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.invited_by_user_id == user.id,
            EventParticipant.status == "going",
        )
    )
    occ = await _occupancy(db, event_id)
    decision = rules.decide_guest_invite(
        inviter_is_participant_or_host=inviter is not None, guest_policy=event.guest_policy,
        existing_guest_count_for_inviter=existing_guest_count, capacity=event.capacity, occupancy=occ,
    )
    _enforce(decision)

    token = f"{user.display_name.upper().replace(' ', '')}-{uuid.uuid4().hex[:4].upper()}"
    invite = GuestInvitation(event_id=event_id, invited_by_user_id=user.id, token=token, guest_name=payload.guest_name)
    db.add(invite)
    await db.flush()

    if event.guest_policy == "host_approval":
        # host_approval: the invite is created but the guest isn't seated
        # until the host approves it (mirrors the join_requests pattern —
        # capacity isn't consumed until approval).
        await db.commit()
        return {"status": "pending_host_approval", "token": token}

    # none/one/two: auto-seats the guest immediately, same as the
    # frontend prototype (guest still consumes capacity right away).
    participant = EventParticipant(
        event_id=event_id, guest_name=payload.guest_name, type="guest",
        invited_by_user_id=user.id, status="going",
    )
    db.add(participant)
    await db.flush()
    invite.status = "claimed"
    invite.claimed_by_participant_id = participant.id
    if event.host_user_id and event.host_user_id != user.id:
        await notify(db, event.host_user_id, "guest_invited", {
            "message": f"{user.display_name} brought a guest to {event.title}.", "event_id": str(event_id),
        })
    await db.commit()
    return {"status": "seated", "token": token}


@router.delete("/{event_id}/guests/{guest_id}", status_code=status.HTTP_200_OK)
async def cancel_guest(
    event_id: uuid.UUID, guest_id: uuid.UUID,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    guest = await db.get(EventParticipant, guest_id)
    if not guest or guest.event_id != event_id or guest.type != "guest":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Guest not found")

    event = await db.get(Event, event_id)
    is_host = event and event.host_user_id == user.id
    is_inviter = guest.invited_by_user_id == user.id
    if not (is_host or is_inviter):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host or the inviting participant can remove this guest")

    guest.status = "removed"
    # notify whichever side of the invite didn't just take this action —
    # the host if the inviter cancelled, the inviter if the host removed it.
    if is_inviter and event and event.host_user_id and event.host_user_id != user.id:
        await notify(db, event.host_user_id, "guest_cancelled", {"message": f"{user.display_name}'s guest invitation for {event.title} was cancelled.", "event_id": str(event_id)})
    elif is_host and guest.invited_by_user_id and guest.invited_by_user_id != user.id:
        await notify(db, guest.invited_by_user_id, "guest_cancelled", {"message": f"Your guest invitation for {event.title} was cancelled by the host.", "event_id": str(event_id)})
    await db.commit()
    await _offer_next_waitlist_spot(db, event_id)
    return {"status": "removed"}


# ---------- host attendee management ----------

@router.get("/{event_id}/attendance", response_model=AttendanceOut)
async def get_attendance(event_id: uuid.UUID, host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if not event or event.host_user_id != host.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can view attendee management")

    going = (await db.scalars(
        select(EventParticipant).where(EventParticipant.event_id == event_id, EventParticipant.status == "going")
    )).all()
    participants = [p for p in going if p.type in ("host", "participant")]
    guests = [p for p in going if p.type == "guest"]
    checked_in = sum(1 for p in going if p.checked_in_at)

    pending = (await db.scalars(
        select(EventJoinRequest).where(EventJoinRequest.event_id == event_id, EventJoinRequest.status == "pending")
    )).all()
    waitlist = (await db.scalars(
        select(EventWaitlist).where(EventWaitlist.event_id == event_id, EventWaitlist.status.in_(["waiting", "offered"]))
        .order_by(EventWaitlist.created_at.asc())
    )).all()

    return AttendanceOut(
        capacity=event.capacity, occupancy=len(going), checked_in=checked_in, not_checked_in=len(going) - checked_in,
        participants=[{"id": str(p.id), "user_id": str(p.user_id), "type": p.type, "checked_in": bool(p.checked_in_at)} for p in participants],
        guests=[{"id": str(g.id), "name": g.guest_name, "invited_by": str(g.invited_by_user_id), "checked_in": bool(g.checked_in_at)} for g in guests],
        pending_requests=[{"id": str(r.id), "user_id": str(r.user_id), "requested_at": r.requested_at.isoformat()} for r in pending],
        waitlist=[{"id": str(w.id), "user_id": str(w.user_id), "status": w.status} for w in waitlist],
    )


@router.delete("/{event_id}/attendees/{participant_id}", status_code=status.HTTP_200_OK)
async def remove_attendee(
    event_id: uuid.UUID, participant_id: uuid.UUID, ban: bool = False,
    host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    participant = await db.get(EventParticipant, participant_id)
    if not participant or participant.event_id != event_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attendee not found")

    decision = rules.decide_remove_attendee(is_host=(event.host_user_id == host.id), target_type=participant.type)
    _enforce(decision)

    participant.status = "banned" if ban else "removed"
    # Removing/banning a participant invalidates any guests they brought —
    # their authorization was scoped to the inviter (spec §14).
    if participant.user_id:
        guests = (await db.scalars(
            select(EventParticipant).where(
                EventParticipant.event_id == event_id,
                EventParticipant.invited_by_user_id == participant.user_id,
                EventParticipant.status == "going",
            )
        )).all()
        for g in guests:
            g.status = "removed"
        if ban:
            db.add(EventBan(event_id=event_id, user_id=participant.user_id, banned_by=host.id))
    await db.commit()
    await _offer_next_waitlist_spot(db, event_id)
    return {"status": "banned" if ban else "removed"}


# ---------- check-in ----------
#
# The check-in token is a short-lived JWT scoped to (event_id,
# purpose='checkin'), signed with the same JWT_SECRET as login tokens.
# The host generates it once (POST .../checkin-token) and displays it
# as a QR code at the door; anyone with a 'going' row scans it and
# POSTs the token back to check themselves in. Previously, self
# check-in required NO token at all — any attendee could call this
# endpoint and mark themselves present with zero verification, which
# defeated the entire point of a check-in system. This is the fix.
#
# The frontend renders this token as a real, scannable QR code (see
# frontend/app.js::renderRealQr, using the qrcode-generator library) —
# there's no camera-based scanning wired up yet, so an attendee scans
# it with their phone's own camera app or types the code manually; the
# token itself is real, signed, time-limited, and actually required.

def _generate_checkin_token(event_id: uuid.UUID, expires_at: datetime) -> str:
    from jose import jwt
    from app.config import settings
    return jwt.encode(
        {"event_id": str(event_id), "purpose": "checkin", "exp": expires_at},
        settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
    )


def _verify_checkin_token(token: str, event_id: uuid.UUID) -> bool:
    from jose import jwt, JWTError
    from app.config import settings
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return False
    return payload.get("purpose") == "checkin" and payload.get("event_id") == str(event_id)


@router.post("/{event_id}/checkin-token", response_model=CheckInTokenOut)
async def create_checkin_token(
    event_id: uuid.UUID, valid_hours: int = 6,
    host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await db.get(Event, event_id)
    if not event or event.host_user_id != host.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can generate a check-in code")
    if event.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Can't generate a check-in code for a cancelled event")
    expires_at = datetime.now(timezone.utc) + timedelta(hours=min(max(valid_hours, 1), 12))
    if event.ends_at:
        expires_at = min(expires_at, event.ends_at + timedelta(hours=1))
    token = _generate_checkin_token(event_id, expires_at)
    return CheckInTokenOut(token=token, expires_at=expires_at)


@router.post("/{event_id}/check-in", status_code=status.HTTP_200_OK)
async def check_in(
    event_id: uuid.UUID, payload: CheckInCreate,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if event.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "This event was cancelled — check-in isn't available")

    if payload.participant_id:
        # host manual override — fallback for someone without a working
        # phone/camera at the door; still gated on being the host.
        if event.host_user_id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can check in another attendee")
        participant = await db.get(EventParticipant, payload.participant_id)
    else:
        # self check-in — REQUIRES a valid, unexpired, event-scoped token.
        if not payload.token or not _verify_checkin_token(payload.token, event_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired check-in code — ask the host for the current QR code")
        participant = await db.scalar(
            select(EventParticipant).where(
                EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
            )
        )
    if not participant or participant.event_id != event_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attendee not found")
    decision = rules.decide_check_in(participant_status=participant.status, participant_exists=True)
    _enforce(decision)

    if participant.checked_in_at:
        return {"status": "already_checked_in", "checked_in_at": participant.checked_in_at.isoformat()}

    participant.checked_in_at = datetime.now(timezone.utc)
    participant.checked_in_by = user.id
    db.add(EventCheckIn(event_id=event_id, participant_id=participant.id, checked_in_by=user.id, method=payload.method))
    # only notify the host when someone OTHER than the host self-checked-in
    # via participant_id (host manually checking people in shouldn't ping itself)
    if event.host_user_id and event.host_user_id != participant.user_id and not payload.participant_id:
        who = await db.get(User, participant.user_id) if participant.user_id else None
        await notify(db, event.host_user_id, "checked_in", {"message": f"{who.display_name if who else 'Someone'} checked in to {event.title}.", "event_id": str(event_id)})
    await db.commit()
    return {"status": "checked_in"}


# ---------- chat ----------

@router.get("/{event_id}/chat")
async def get_chat_messages(
    event_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
    after: str | None = None,
):
    """
    `after` is an ISO timestamp for incremental polling fallback (see
    README "Real-time chat" — the WebSocket endpoint below is the
    primary path; this GET exists for initial load and for clients that
    can't hold a socket open). Access rule matches post_chat_message:
    any 'going' attendee (host, participant, or guest) can read.
    """
    participant = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
        )
    )
    if not participant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join the event to view chat")
    stmt = select(EventMessage).where(EventMessage.event_id == event_id)
    if after:
        stmt = stmt.where(EventMessage.created_at > after)
    stmt = stmt.order_by(EventMessage.created_at.asc()).limit(200)
    rows = (await db.scalars(stmt)).all()
    sender_ids = {m.user_id for m in rows}
    names = {}
    if sender_ids:
        name_rows = (await db.execute(select(User.id, User.display_name).where(User.id.in_(sender_ids)))).all()
        names = {str(uid): name for uid, name in name_rows}
    return [
        {"id": str(m.id), "user_id": str(m.user_id), "user_name": names.get(str(m.user_id), "Someone"),
         "body": m.body, "created_at": m.created_at.isoformat()}
        for m in rows
    ]


@router.post("/{event_id}/chat", status_code=status.HTTP_201_CREATED)
async def post_chat_message(
    event_id: uuid.UUID, payload: ChatMessageCreate,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    participant = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
        )
    )
    if not participant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join the event to chat")
    event = await db.get(Event, event_id)
    if event and event.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "This event was cancelled — chat is read-only now")
    msg = EventMessage(event_id=event_id, user_id=user.id, body=payload.body)
    db.add(msg)
    await db.commit()
    out = {"id": str(msg.id), "user_id": str(user.id), "user_name": user.display_name,
           "body": msg.body, "created_at": msg.created_at.isoformat()}
    await chat_ws_manager.broadcast(str(event_id), out)
    return out


# ---------- reports / bans ----------

@router.post("/{event_id}/report", status_code=status.HTTP_201_CREATED)
async def report_event(event_id: uuid.UUID, reason: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.models import Report  # local import: Report is a general-purpose table, not event-specific
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    db.add(Report(reporter_id=user.id, target_type="event", target_id=event_id, reason=reason))
    await db.commit()
    return {"status": "reported"}


@router.post("/{event_id}/ban", status_code=status.HTTP_200_OK)
async def ban_attendee(event_id: uuid.UUID, payload: BanCreate, host: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if not event or event.host_user_id != host.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can ban attendees")
    participant = await db.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == payload.user_id, EventParticipant.status == "going"
        )
    )
    if participant:
        participant.status = "banned"
    db.add(EventBan(event_id=event_id, user_id=payload.user_id, banned_by=host.id, reason=payload.reason))
    await db.commit()
    await _offer_next_waitlist_spot(db, event_id)
    return {"status": "banned"}


# ---------- real-time chat ----------

@router.websocket("/{event_id}/ws")
async def event_chat_socket(websocket: WebSocket, event_id: uuid.UUID, token: str = ""):
    """
    Real-time fan-out for new chat messages (spec §13). The frontend
    still POSTs new messages over plain HTTP (`POST /{event_id}/chat`,
    above) — that's what actually persists the message and is what
    validates the sender is an authorized attendee. This socket only
    pushes the resulting message to everyone else already viewing the
    event, so message B doesn't need to refresh to see message A.

    Browsers can't set custom headers on a WebSocket handshake, so the
    JWT is passed as a query parameter (`?token=...`) instead of the
    `Authorization` header used everywhere else. This is standard
    practice for WS auth but means the token can end up in server access
    logs — acceptable for a short-lived access token in this MVP, worth
    revisiting (e.g. a short-lived one-time WS ticket) before a real launch.
    """
    from jose import jwt, JWTError
    from app.config import settings
    from app.database import async_session
    from app.models import User as UserModel

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        await websocket.close(code=4401)
        return

    async with async_session() as db:
        user = await db.get(UserModel, user_id)
        if not user:
            await websocket.close(code=4401)
            return
        participant = await db.scalar(
            select(EventParticipant).where(
                EventParticipant.event_id == event_id, EventParticipant.user_id == user.id, EventParticipant.status == "going"
            )
        )
        if not participant:
            await websocket.close(code=4403)
            return

    await chat_ws_manager.connect(str(event_id), websocket)
    try:
        while True:
            # This socket is receive-only from the client's perspective —
            # messages are sent via the HTTP POST above so they're
            # persisted and validated the same way regardless of
            # transport. We still need to await something to detect
            # disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        chat_ws_manager.disconnect(str(event_id), websocket)
