"""
Auth: email/password + OAuth (Google now, Apple structured for later).

Passwords are hashed with bcrypt via passlib. JWTs are short-lived
access tokens; for a real launch, pair with a refresh-token cookie —
omitted here to keep the reference implementation focused.

Email verification (rewritten this pass): signup no longer creates a
User row directly. It creates a PendingSignup — email, password hash,
profile fields, and a hashed 6-digit OTP — and emails the code. Only
once that code is verified does a real User row get created; there is
no window where an unverified, login-capable account exists. This
replaces the previous link-based EmailVerificationToken mechanism
entirely (see PendingSignup's docstring in app/models.py for why: one
verification system, not two, and why "verify before creating the
account" was chosen over "create it unverified").
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.email_sender import send_signup_otp_email, send_password_reset_otp_email
from app.deps import get_current_user
from app.models import City, PasswordReset, PendingSignup, User
from app.moderation import is_inappropriate
from app.schemas import (
    LoginRequest, RequestPasswordResetRequest, ResendSignupOtpRequest, ResetPasswordRequest,
    SignupOtpRequest, SignupRequest, TokenResponse, UserOut,
)

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

OTP_TTL = timedelta(minutes=10)
MAX_OTP_ATTEMPTS = 5
# Server-side floor on top of the per-IP window RateLimitMiddleware
# already enforces on this path — this one is per-EMAIL (so it also
# limits someone hammering resend for one address from many IPs) and is
# what the frontend's countdown timer reflects.
RESEND_COOLDOWN = timedelta(seconds=45)

# Password reset uses the exact same TTL/attempt-cap/cooldown shape as
# signup OTP above — same security bar, separate constants only because
# they're conceptually a different flow's knobs, not because the values
# actually differ today.
RESET_OTP_TTL = timedelta(minutes=10)
MAX_RESET_OTP_ATTEMPTS = 5
RESET_RESEND_COOLDOWN = timedelta(seconds=45)


def create_access_token(user_id: str, password_changed_at: datetime | None = None) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    claims = {"sub": user_id, "exp": expire}
    if password_changed_at is not None:
        # See app/deps.py::get_current_user for why this claim exists —
        # it's what lets a password reset invalidate previously-issued
        # tokens. Naive datetimes (as `default=datetime.utcnow` produces)
        # are treated as UTC here, matching how they're read back.
        pwt = password_changed_at if password_changed_at.tzinfo else password_changed_at.replace(tzinfo=timezone.utc)
        claims["pwt"] = pwt.timestamp()
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_otp() -> str:
    """
    secrets.randbelow() uses the OS CSPRNG (os.urandom under the hood)
    — never Python's default `random` module, which is a Mersenne
    Twister PRNG that's predictable given enough output and must never
    be used for anything security-sensitive.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


async def _issue_pending_signup(db: AsyncSession, *, email: str, password_hash: str, display_name: str, city_id) -> str:
    """
    Replaces any existing pending signup for this email (upsert-by-
    delete-then-insert — same idiom ImFreeStatus.activate() already
    uses for "one active thing per key") and returns the raw OTP. Only
    its hash is persisted.
    """
    await db.execute(delete(PendingSignup).where(PendingSignup.email == email))
    otp = _generate_otp()
    db.add(PendingSignup(
        email=email, password_hash=password_hash, display_name=display_name, city_id=city_id,
        otp_hash=_hash_secret(otp), otp_expires_at=datetime.now(timezone.utc) + OTP_TTL,
        attempt_count=0, last_sent_at=datetime.now(timezone.utc),
    ))
    return otp


@router.post("/signup")
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    email = normalize_email(payload.email)
    display_name = payload.display_name.strip()

    if is_inappropriate(display_name, despace=True):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please choose a different nickname")

    existing_user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if existing_user:
        # Deliberately different wording/status from login's generic
        # "wrong email or password" (409, not 401): at signup there's no
        # password attempt to protect against enumeration for — the user
        # just typed their own email into a form, and telling them to
        # log in instead is strictly more helpful than useful security.
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    city = await db.scalar(select(City).where(City.name == payload.city))
    if not city:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"City '{payload.city}' is not live yet")

    password_hash = pwd_context.hash(payload.password)

    if not settings.REQUIRE_SIGNUP_OTP:
        # OTP step temporarily disabled (see REQUIRE_SIGNUP_OTP's
        # docstring) — create the account directly, same as before the
        # OTP flow existed. email_verified=True here is honest, not a
        # workaround: nothing actually verified this email, but nothing
        # anywhere currently gates on that flag either (see
        # REQUIRE_EMAIL_VERIFICATION), so it correctly reflects "not a
        # known-bad state" rather than flagging every account created
        # while this is off as suspect.
        user = User(email=email, password_hash=password_hash, display_name=display_name, city_id=city.id, email_verified=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return TokenResponse(access_token=create_access_token(str(user.id), user.password_changed_at))

    otp = await _issue_pending_signup(db, email=email, password_hash=password_hash, display_name=display_name, city_id=city.id)
    await db.commit()

    send_signup_otp_email(email, otp)  # never logged/returned — see email_sender.py

    return {"status": "otp_sent", "email": email}


@router.post("/verify-signup-otp", response_model=TokenResponse)
async def verify_signup_otp(payload: SignupOtpRequest, db: AsyncSession = Depends(get_db)):
    email = normalize_email(payload.email)
    pending = await db.scalar(select(PendingSignup).where(PendingSignup.email == email))

    if not pending:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incorrect verification code.")
    if pending.otp_expires_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This code has expired. Please request a new one.")
    if pending.attempt_count >= MAX_OTP_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please request a new code.")

    if _hash_secret(payload.otp) != pending.otp_hash:
        pending.attempt_count += 1
        await db.commit()
        if pending.attempt_count >= MAX_OTP_ATTEMPTS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please request a new code.")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incorrect verification code.")

    # Correct code: create the real account now (already verified — no
    # window where an unverified User exists) and drop the pending row.
    # A duplicate-email race is caught by users.email's own unique
    # constraint; not otherwise special-cased given a single-use OTP
    # makes it exceptionally unlikely.
    user = User(
        email=pending.email, password_hash=pending.password_hash,
        display_name=pending.display_name, city_id=pending.city_id,
        email_verified=True,
    )
    db.add(user)
    await db.delete(pending)
    await db.commit()
    await db.refresh(user)

    return TokenResponse(access_token=create_access_token(str(user.id), user.password_changed_at))


@router.post("/resend-signup-otp")
async def resend_signup_otp(payload: ResendSignupOtpRequest, db: AsyncSession = Depends(get_db)):
    """Also rate-limited per-IP by app/rate_limit.py; RESEND_COOLDOWN above is the per-email floor this reports back to the frontend's countdown."""
    email = normalize_email(payload.email)
    pending = await db.scalar(select(PendingSignup).where(PendingSignup.email == email))
    if not pending:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No pending signup found for this email — please sign up again.")

    wait_seconds = int(((pending.last_sent_at + RESEND_COOLDOWN) - datetime.now(timezone.utc)).total_seconds())
    if wait_seconds > 0:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Please wait {wait_seconds}s before requesting another code.")

    otp = _generate_otp()
    pending.otp_hash = _hash_secret(otp)
    pending.otp_expires_at = datetime.now(timezone.utc) + OTP_TTL
    pending.attempt_count = 0
    pending.last_sent_at = datetime.now(timezone.utc)
    await db.commit()

    send_signup_otp_email(email, otp)
    return {"status": "otp_sent"}


# ---------- Password reset ----------
# Same security bar as signup OTP above: cryptographically-secure code
# (_generate_otp), hashed at rest (_hash_secret — never the raw code),
# short expiry, capped wrong-guess attempts, per-email resend cooldown,
# and per-IP rate limiting (app/rate_limit.py). Uses PasswordReset, a
# dedicated table — see its docstring in app/models.py for why this is
# NOT folded into PendingSignup despite the identical shape.

@router.post("/request-password-reset")
async def request_password_reset(payload: RequestPasswordResetRequest, db: AsyncSession = Depends(get_db)):
    """
    ALWAYS returns the same generic response, whether or not the email
    belongs to a real account — this is the account-enumeration guard
    (spec item: "Generic response that does NOT reveal whether an email
    exists"). An email is only actually sent when the account exists;
    the HTTP response gives no way to tell the two cases apart, and
    nothing about response timing is treated as meaningful here (no
    extra work is done in the "user doesn't exist" branch to fake a
    matching latency, but the query itself is a single indexed lookup
    either way, not something that produces an observable timing gap
    worth defending against separately at this MVP's threat level).
    """
    email = normalize_email(payload.email)
    generic = {"status": "if_account_exists_email_sent"}

    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if not user or not user.password_hash:
        # No account, or an OAuth-only account with no password to reset
        # — either way, silently do nothing rather than reveal which.
        return generic

    existing = await db.scalar(select(PasswordReset).where(PasswordReset.user_id == user.id))
    if existing:
        wait_seconds = int(((existing.last_sent_at + RESET_RESEND_COOLDOWN) - datetime.now(timezone.utc)).total_seconds())
        if wait_seconds > 0:
            # Still return the generic shape — a 429 here would itself
            # leak "an account exists and a reset was already requested
            # recently" to anyone probing an arbitrary email address.
            return generic
        await db.delete(existing)

    otp = _generate_otp()
    db.add(PasswordReset(
        user_id=user.id, otp_hash=_hash_secret(otp),
        otp_expires_at=datetime.now(timezone.utc) + RESET_OTP_TTL,
        attempt_count=0, last_sent_at=datetime.now(timezone.utc),
    ))
    await db.commit()

    send_password_reset_otp_email(email, otp)  # never logged/returned — see email_sender.py
    return generic


@router.post("/reset-password", response_model=TokenResponse)
async def reset_password(payload: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    email = normalize_email(payload.email)
    # Same generic wording as an invalid login/OTP — never confirm or
    # deny whether the email has an account or a pending reset.
    generic_error = "That code is invalid or has expired. Please request a new one."

    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, generic_error)

    reset = await db.scalar(select(PasswordReset).where(PasswordReset.user_id == user.id))
    if not reset:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, generic_error)
    if reset.otp_expires_at < datetime.now(timezone.utc):
        await db.delete(reset)
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, generic_error)
    if reset.attempt_count >= MAX_RESET_OTP_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please request a new code.")

    if _hash_secret(payload.otp) != reset.otp_hash:
        reset.attempt_count += 1
        await db.commit()
        if reset.attempt_count >= MAX_RESET_OTP_ATTEMPTS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please request a new code.")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, generic_error)

    # Correct code: set the new password (reusing the exact same bcrypt
    # context every other password path uses), bump password_changed_at
    # (invalidates every previously-issued token — see create_access_token
    # / get_current_user), delete the reset row (single-use — a reused or
    # replayed code fails the lookup above the same way an expired one
    # does), and log the user straight into a fresh, valid session.
    user.password_hash = pwd_context.hash(payload.new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    await db.delete(reset)
    await db.commit()
    await db.refresh(user)

    return TokenResponse(access_token=create_access_token(str(user.id), user.password_changed_at))


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
    return TokenResponse(access_token=create_access_token(str(user.id), user.password_changed_at))


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
