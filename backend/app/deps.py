from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
        pwt = payload.get("pwt")
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = await db.get(User, user_id)
    if not user or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    # "pwt" (password-changed-at, as a unix timestamp) is embedded in
    # every token issued since migrations/0005 — see
    # app/routers/auth.py::create_access_token. Comparing it against the
    # user's CURRENT password_changed_at is what makes a password reset
    # actually invalidate every token issued before it, not just ones a
    # client chooses to discard — otherwise a stolen token (the exact
    # scenario a reset is meant to respond to) would keep working for its
    # full 14-day lifetime regardless of the password change. A small
    # epsilon absorbs float/DB round-trip precision differences, not a
    # meaningful grace window. Tokens issued before this migration carry
    # no "pwt" claim at all — those are left alone (None skips the check)
    # so existing sessions aren't force-logged-out by the deploy itself.
    if pwt is not None and user.password_changed_at is not None:
        if abs(float(pwt) - user.password_changed_at.timestamp()) > 1:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your password was changed — please log in again")
    return user


async def require_verified_user(user: User = Depends(get_current_user)) -> User:
    """
    Gate for the handful of actions the product spec calls out as
    requiring a confirmed email (creating an event, sending a friend
    request, activating I'm Free, sending a chat message) — NOT applied
    globally to every authenticated endpoint, since most of the app
    (browsing, viewing your own profile, etc.) has no real reason to
    block an unverified user. Only actually enforces anything when
    settings.REQUIRE_EMAIL_VERIFICATION is true — see that setting's
    docstring for why it defaults to false (no mail provider configured
    yet would otherwise lock every new signup out of these actions).
    """
    if settings.REQUIRE_EMAIL_VERIFICATION and not user.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Please verify your email to continue")
    return user
