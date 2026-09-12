"""
Auth: email/password + OAuth (Google now, Apple structured for later).

Passwords are hashed with bcrypt via passlib. JWTs are short-lived
access tokens; for a real launch, pair with a refresh-token cookie —
omitted here to keep the reference implementation focused.

Email verification (new this pass): signup creates a single-use,
expiring, hashed token (EmailVerificationToken) and emails a link
through app/email_sender.py. Whether unverified accounts are actually
restricted from anything is a separate decision — see
app/deps.py:require_verified_user and app/config.py's
REQUIRE_EMAIL_VERIFICATION.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.email_sender import send_verification_email
from app.deps import get_current_user
from app.models import City, EmailVerificationToken, User
from app.moderation import is_inappropriate
from app.schemas import LoginRequest, SignupRequest, TokenResponse, UserOut, VerifyEmailRequest

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

VERIFICATION_TOKEN_TTL = timedelta(hours=24)


def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _issue_verification_token(db: AsyncSession, user: User) -> str:
    """
    Invalidates any still-usable tokens this user already has (so
    resending doesn't leave multiple simultaneously-valid links, and an
    old, possibly-forwarded email stops working the moment a fresh one
    is requested), then creates and returns a new raw token. Only the
    hash is persisted — see EmailVerificationToken's docstring.
    """
    await db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user.id, EmailVerificationToken.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc))
    )
    raw_token = secrets.token_urlsafe(32)
    db.add(EmailVerificationToken(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + VERIFICATION_TOKEN_TTL,
    ))
    return raw_token


@router.post("/signup", response_model=TokenResponse)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    email = normalize_email(payload.email)
    display_name = payload.display_name.strip()

    if is_inappropriate(display_name, despace=True):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please choose a different nickname")

    existing = await db.scalar(select(User).where(func.lower(User.email) == email))
    if existing:
        # Spec: "An account with this email already exists." — deliberately
        # different wording/status from login's generic "wrong email or
        # password" (409, not 401): at signup there's no password attempt
        # to protect against enumeration for — the user just typed their
        # own email into a form, and telling them to log in instead is
        # strictly more helpful than useful security. Login's endpoint
        # below is where the enumeration-resistant generic error applies.
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    city = await db.scalar(select(City).where(City.name == payload.city))
    if not city:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"City '{payload.city}' is not live yet")

    user = User(
        email=email,
        password_hash=pwd_context.hash(payload.password),
        display_name=display_name,
        city_id=city.id,
    )
    db.add(user)
    await db.flush()  # assigns user.id without ending the transaction — the verification token needs it below
    raw_token = await _issue_verification_token(db, user)
    await db.commit()
    await db.refresh(user)

    send_verification_email(user.email, user.display_name, raw_token)

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    email = normalize_email(payload.email)
    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    # Same generic message whether the email doesn't exist, exists with a
    # different password, or is an OAuth-only account with no
    # password_hash at all — never reveal which case it was (spec: no
    # account enumeration).
    if not user or not user.password_hash or not pwd_context.verify(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")
    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is no longer active.")
    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/oauth/google", response_model=TokenResponse)
async def oauth_google(id_token: str, db: AsyncSession = Depends(get_db)):
    """
    Verify `id_token` against Google's public keys (google-auth library),
    then find-or-create the user by (oauth_provider='google', oauth_subject).
    Left as a stub — wire up `google.oauth2.id_token.verify_oauth2_token`
    with settings.GOOGLE_OAUTH_CLIENT_ID before shipping. NOT functional
    yet; the frontend hides the "Continue with Google" button behind a
    feature flag for exactly this reason — see frontend/api/auth.js.
    """
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Google OAuth is not wired up yet — use email/password")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.post("/logout")
async def logout():
    """
    JWTs here are stateless (no server-side session row), so there's
    nothing to invalidate server-side — logout is the frontend discarding
    its stored token. This endpoint exists so the frontend has a single
    consistent call to make either way, and so a future move to a
    server-tracked session/refresh-token model doesn't require a new
    endpoint, just a new implementation of this one (e.g. deleting a
    refresh-token row or adding the token's jti to a revocation list).
    """
    return {"status": "logged_out"}


@router.post("/verify-email")
async def verify_email(payload: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    token_hash = _hash_token(payload.token)
    row = await db.scalar(select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash))

    # Same 400 for "no such token" / "already used" / "expired" — the
    # distinct copy the frontend shows for each case is derived from
    # what it already knows locally (e.g. "you're already verified"
    # when /auth/me says so) rather than from this response, so there's
    # no reason to hand back which specific reason it was.
    if not row:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This verification link is invalid.")
    if row.used_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This verification link has already been used.")
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This verification link has expired.")

    row.used_at = datetime.now(timezone.utc)
    user = await db.get(User, row.user_id)
    if user:
        user.email_verified = True
    await db.commit()
    return {"status": "verified"}


@router.post("/resend-verification")
async def resend_verification(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Rate-limited (see app/rate_limit.py) — this is the endpoint a spam-triggered resend loop would hit."""
    if user.email_verified:
        return {"status": "already_verified"}
    raw_token = await _issue_verification_token(db, user)
    await db.commit()
    send_verification_email(user.email, user.display_name, raw_token)
    return {"status": "sent"}
