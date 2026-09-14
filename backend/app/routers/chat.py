"""
Friend-to-friend direct messaging (spec items 9/10: a dedicated Chat
tab, "Message" from a friend's profile). Deliberately reuses rather
than reinvents:

  - Storage: the direct_messages table already existed in
    schema.sql/production (sender_id/recipient_id/body/created_at/
    read_at, with idx_dm_thread already built for exactly this lookup)
    — see app/models.py's DirectMessage docstring. There's no separate
    "conversations" table; a conversation between two users is just the
    set of rows between their two ids, the same way a Friendship is
    identified by its ordered pair rather than its own row elsewhere.
  - Real-time delivery: app/ws.py's ChatConnectionManager, already used
    for event chat and explicitly designed to be reused for exactly
    this (see its own docstring) — dm_ws_manager below is a second
    instance of that same class, keyed by a canonical "userA:userB"
    thread key instead of an event id.
  - Permission model: friends.py's is_friends_with(), the single source
    of truth events.py already uses for access_mode='friends' events.
    Messaging follows the same rule — only friends can message each
    other — rather than introducing a separate messaging-permission
    concept.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_verified_user
from app.models import DirectMessage, User
from app.moderation import is_inappropriate
from app.routers.friends import is_friends_with
from app.schemas import ChatMessageCreate
from app.ws import ChatConnectionManager

router = APIRouter()

dm_ws_manager = ChatConnectionManager()


def _thread_key(a: uuid.UUID, b: uuid.UUID) -> str:
    ids = sorted([str(a), str(b)])
    return f"{ids[0]}:{ids[1]}"


async def _require_friend(db: AsyncSession, me: User, friend_id: uuid.UUID) -> User:
    if friend_id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Can't message yourself")
    friend = await db.get(User, friend_id)
    if not friend or friend.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if not await is_friends_with(db, me.id, friend_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only message friends")
    return friend


@router.get("/conversations")
async def list_conversations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """
    One row per friend who has at least one message with the current
    user OR is simply a friend (so a brand-new friendship shows up as
    an open-able, empty conversation — matching "Message" always
    working from a friend's profile, not just once someone has already
    said something).
    """
    from app.models import Friendship
    from app.routers.friends import ordered_pair

    friend_rows = (await db.execute(
        select(Friendship).where(
            or_(Friendship.user_id_a == user.id, Friendship.user_id_b == user.id),
            Friendship.status == "accepted",
        )
    )).scalars().all()
    friend_ids = [r.user_id_b if r.user_id_a == user.id else r.user_id_a for r in friend_rows]
    if not friend_ids:
        return []

    friends = {
        u.id: u for u in (await db.scalars(select(User).where(User.id.in_(friend_ids)))).all()
    }

    least_expr = func.least(DirectMessage.sender_id, DirectMessage.recipient_id)
    greatest_expr = func.greatest(DirectMessage.sender_id, DirectMessage.recipient_id)
    last_message_stmt = (
        select(DirectMessage)
        .where(
            or_(
                DirectMessage.sender_id == user.id, DirectMessage.recipient_id == user.id,
            ),
            or_(DirectMessage.sender_id.in_(friend_ids), DirectMessage.recipient_id.in_(friend_ids)),
        )
        .distinct(least_expr, greatest_expr)
        .order_by(least_expr, greatest_expr, DirectMessage.created_at.desc())
    )
    last_messages = (await db.scalars(last_message_stmt)).all()
    last_by_friend = {}
    for m in last_messages:
        other = m.recipient_id if m.sender_id == user.id else m.sender_id
        last_by_friend[other] = m

    unread_rows = (await db.execute(
        select(DirectMessage.sender_id, func.count())
        .where(DirectMessage.recipient_id == user.id, DirectMessage.sender_id.in_(friend_ids), DirectMessage.read_at.is_(None))
        .group_by(DirectMessage.sender_id)
    )).all()
    unread_by_friend = {sender_id: count for sender_id, count in unread_rows}

    out = []
    for fid in friend_ids:
        friend = friends.get(fid)
        if not friend:
            continue
        last = last_by_friend.get(fid)
        out.append({
            "friend_user_id": str(fid),
            "display_name": friend.display_name,
            "avatar_url": friend.avatar_url,
            "last_message": last.body if last else None,
            "last_message_at": last.created_at.isoformat() if last else None,
            "unread_count": unread_by_friend.get(fid, 0),
        })
    # Most recently active conversations first; friends with no messages
    # yet (last_message_at=None) sort to the end.
    out.sort(key=lambda c: c["last_message_at"] or "", reverse=True)
    return out


@router.get("/{friend_user_id}/messages")
async def get_messages(friend_user_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    friend = await _require_friend(db, user, friend_user_id)

    stmt = (
        select(DirectMessage)
        .where(
            or_(
                (DirectMessage.sender_id == user.id) & (DirectMessage.recipient_id == friend_user_id),
                (DirectMessage.sender_id == friend_user_id) & (DirectMessage.recipient_id == user.id),
            )
        )
        .order_by(DirectMessage.created_at.asc())
        .limit(200)
    )
    rows = (await db.scalars(stmt)).all()

    # Opening the thread is what marks the other person's messages read
    # — mirrors the read-receipt semantics implied by direct_messages.
    # read_at existing in the schema from day one.
    await db.execute(
        update(DirectMessage)
        .where(DirectMessage.sender_id == friend_user_id, DirectMessage.recipient_id == user.id, DirectMessage.read_at.is_(None))
        .values(read_at=func.now())
    )
    await db.commit()

    return [
        {"id": str(m.id), "sender_id": str(m.sender_id), "body": m.body, "created_at": m.created_at.isoformat()}
        for m in rows
    ]


@router.post("/{friend_user_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(
    friend_user_id: uuid.UUID, payload: ChatMessageCreate,
    user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db),
):
    await _require_friend(db, user, friend_user_id)
    # This was previously the one free-text field strangers-turned-friends
    # actually exchange that had zero moderation on it at all — nickname
    # and bio were covered, DMs weren't. Free text, so no despace=True
    # (see moderation.py's docstring for why that's bio-like, not
    # nickname-like).
    if is_inappropriate(payload.body):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please remove inappropriate language from your message")

    msg = DirectMessage(sender_id=user.id, recipient_id=friend_user_id, body=payload.body)
    db.add(msg)
    await db.commit()

    out = {"id": str(msg.id), "sender_id": str(user.id), "body": msg.body, "created_at": msg.created_at.isoformat()}
    await dm_ws_manager.broadcast(_thread_key(user.id, friend_user_id), out)

    from app.notify import notify
    await notify(db, friend_user_id, "direct_message", {
        "message": f"{user.display_name} sent you a message.",
        "sender_id": str(user.id),
    })
    await db.commit()
    return out


@router.websocket("/{friend_user_id}/ws")
async def dm_socket(websocket: WebSocket, friend_user_id: uuid.UUID, token: str = ""):
    """Mirrors events.py's event_chat_socket exactly — see its docstring for the WS-auth-via-query-param rationale."""
    from jose import jwt, JWTError
    from app.config import settings
    from app.database import async_session

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        await websocket.close(code=4401)
        return

    async with async_session() as db:
        user = await db.get(User, user_id)
        if not user:
            await websocket.close(code=4401)
            return
        if not await is_friends_with(db, user.id, friend_user_id):
            await websocket.close(code=4403)
            return
        me_id = user.id

    thread_key = _thread_key(me_id, friend_user_id)
    await dm_ws_manager.connect(thread_key, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        dm_ws_manager.disconnect(thread_key, websocket)
