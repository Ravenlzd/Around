"""
Auth: email/password + OAuth (Google now, Apple structured for later).

Passwords are hashed with bcrypt via passlib. JWTs are short-lived
access tokens; for a real launch, pair with a refresh-token cookie —
omitted here to keep the reference implementation focused.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User, City
from app.schemas import SignupRequest, LoginRequest, TokenResponse, UserOut
from app.deps import get_current_user

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


@router.post("/signup", response_model=TokenResponse)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(User).where(User.email == payload.email))
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    city = await db.scalar(select(City).where(City.name == payload.city))
    if not city:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"City '{payload.city}' is not live yet")

    user = User(
        email=payload.email,
        password_hash=pwd_context.hash(payload.password),
        display_name=payload.display_name,
        city_id=city.id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == payload.email))
    if not user or not user.password_hash or not pwd_context.verify(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
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
