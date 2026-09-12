"""
Profile (spec this-phase §16). Backs Around's Profile screen — the
authenticated user's own data only; there's no "view someone else's
full profile" endpoint here yet (that's a discovery/people-nearby
concern, already partly covered by app/routers/discovery.py's people
cards, and left for a future phase rather than expanded here per the
"no new major features" scope boundary for this integration pass).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Block, Event, EventParticipant, Friendship, Report, User, UserInterest, UserStats
from app.schemas import PublicUserOut, ReportProblemCreate, UserOut, ProfileUpdate
from app.deps import get_current_user
from app.trust import derive_trust_state
from app.routers.friends import ordered_pair

router = APIRouter()


@router.get("/me", response_model=UserOut)
async def get_my_profile(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
async def update_my_profile(payload: ProfileUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    data = payload.model_dump(exclude_unset=True, exclude={"interests"})
    for field, value in data.items():
        setattr(user, field, value)

    if payload.interests is not None:
        await db.execute(delete(UserInterest).where(UserInterest.user_id == user.id))
        for interest in payload.interests:
            db.add(UserInterest(user_id=user.id, interest=interest))

    await db.commit()
    await db.refresh(user)
    return user


@router.get("/me/trust")
async def get_my_trust_state(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stats = await db.get(UserStats, user.id)
    trust = derive_trust_state(stats)
    return {
        "state": trust.key, "label": trust.label, "checkmark": trust.checkmark,
        "events_hosted": stats.events_hosted if stats else 0,
        "events_completed": stats.events_completed if stats else 0,
        "reputation_score": float(stats.reputation_score) if stats else 0,
    }


@router.get("/me/interests")
async def get_my_interests(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(UserInterest.interest).where(UserInterest.user_id == user.id))).all()
    return {"interests": rows}


@router.get("/me/stats")
async def get_my_profile_stats(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Counts shown on the signed-in member's profile, derived from live rows."""
    now = datetime.now(timezone.utc)
    upcoming = await db.scalar(
        select(func.count()).select_from(EventParticipant).join(Event).where(
            EventParticipant.user_id == user.id,
            EventParticipant.status == "going",
            Event.status == "active",
            Event.starts_at >= now,
        )
    ) or 0
    hosting = await db.scalar(
        select(func.count()).select_from(Event).where(
            Event.host_user_id == user.id,
            Event.status == "active",
            Event.starts_at >= now,
        )
    ) or 0
    friends = await db.scalar(
        select(func.count()).select_from(Friendship).where(
            or_(Friendship.user_id_a == user.id, Friendship.user_id_b == user.id),
            Friendship.status == "accepted",
        )
    ) or 0
    return {"upcoming": upcoming, "hosting": hosting, "friends": friends}


@router.get("/{user_id}", response_model=PublicUserOut)
async def get_public_profile(
    user_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Read a limited member profile without exposing private account or location data."""
    try:
        profile_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    profile = await db.get(User, profile_id)
    if not profile or profile.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    blocked = await db.scalar(
        select(Block).where(
            or_(
                (Block.blocker_id == user.id) & (Block.blocked_id == profile_id),
                (Block.blocker_id == profile_id) & (Block.blocked_id == user.id),
            )
        )
    )
    if blocked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if profile_id == user.id:
        friendship_status = "self"
    else:
        a, b = ordered_pair(user.id, profile_id)
        row = await db.get(Friendship, {"user_id_a": a, "user_id_b": b})
        if not row:
            friendship_status = "none"
        elif row.status == "accepted":
            friendship_status = "accepted"
        elif row.status == "pending":
            friendship_status = "pending_sent" if row.requested_by == user.id else "pending_received"
        else:
            friendship_status = "none"

    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "university_or_work": profile.university_or_work,
        "bio": profile.bio,
        "avatar_url": profile.avatar_url,
        "friendship_status": friendship_status,
    }


@router.post("/{user_id}/block", status_code=status.HTTP_201_CREATED)
async def block_user(user_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    Was previously a visual-only "Blocked users" row with nothing behind
    it — the Block model and every read-side exclusion query (discovery
    people-nearby, I'm Free nearby, get_public_profile above) already
    existed and already filter on it, but nothing ever wrote a row.
    This is that missing write path, not a new blocking system.
    """
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Can't block yourself")
    target = await db.get(User, target_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    existing = await db.get(Block, {"blocker_id": user.id, "blocked_id": target_id})
    if not existing:
        db.add(Block(blocker_id=user.id, blocked_id=target_id))
        # A block always wins over a friendship — mirrors friends.py's
        # send_request() refusing new requests once a block exists, so a
        # block can't leave a stale pending/accepted friendship row a
        # user would otherwise see reflected as "Friends" or "Pending".
        a, b = ordered_pair(user.id, target_id)
        friendship = await db.get(Friendship, {"user_id_a": a, "user_id_b": b})
        if friendship:
            await db.delete(friendship)
        await db.commit()
    return {"status": "blocked"}


@router.delete("/{user_id}/block")
async def unblock_user(user_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    existing = await db.get(Block, {"blocker_id": user.id, "blocked_id": target_id})
    if existing:
        await db.delete(existing)
        await db.commit()
    return {"status": "unblocked"}


@router.post("/me/report-problem", status_code=status.HTTP_201_CREATED)
async def report_problem(payload: ReportProblemCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    Profile settings' "Report a problem" — previously a static toast
    with no backend behind it at all. Reuses the existing general-
    purpose Report model (already used by events.py's per-event report
    endpoint) rather than introducing a second reporting mechanism.
    target_type='app' with target_id=the reporter's own id is a
    sentinel (Report.target_id is NOT NULL and this report isn't about
    any specific user/event/post) rather than a claim the user reported
    themselves.
    """
    db.add(Report(reporter_id=user.id, target_type="app", target_id=user.id, reason="user_reported_problem", details=payload.message))
    await db.commit()
    return {"status": "submitted"}
