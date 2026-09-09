"""
Profile (spec this-phase §16). Backs Around's Profile screen — the
authenticated user's own data only; there's no "view someone else's
full profile" endpoint here yet (that's a discovery/people-nearby
concern, already partly covered by app/routers/discovery.py's people
cards, and left for a future phase rather than expanded here per the
"no new major features" scope boundary for this integration pass).
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, UserInterest, UserStats
from app.schemas import UserOut, ProfileUpdate
from app.deps import get_current_user
from app.trust import derive_trust_state

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
