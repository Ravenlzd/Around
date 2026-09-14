"""
Shared blocking helper. Was previously duplicated inline in
app/routers/discovery.py (people_nearby) and NOT applied at all in
app/routers/events.py or app/routers/posts.py — this is the single
source of truth every caller should use, the same role
app/routers/friends.py::is_friends_with() plays for friendship checks.
"""
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Block


async def blocked_ids(db: AsyncSession, user_id: uuid.UUID) -> set:
    """Everyone `user_id` has a block relationship with, either direction (not including user_id itself)."""
    rows = (await db.scalars(
        select(Block).where(or_(Block.blocker_id == user_id, Block.blocked_id == user_id))
    )).all()
    excluded = set()
    for b in rows:
        excluded.add(b.blocker_id)
        excluded.add(b.blocked_id)
    excluded.discard(user_id)
    return excluded
