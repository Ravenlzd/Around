"""
Integration tests for: OTP-based signup email verification (see
app/models.py's PendingSignup), login error handling, nickname/bio
moderation, the curated interests catalog, friend-request
notifications, and friend-to-friend chat. Same real-Postgres
requirement as test_api_integration.py — see that file's module
docstring for how to run these (needs a live Postgres/PostGIS, not
available in this sandbox — see the session's own report for
confirmation these were compiled and reasoned through but not executed
here).

Fixtures (client/city_id/event_loop/_make_user) are duplicated from
test_api_integration.py rather than imported from it: there's no
tests/__init__.py, so "tests" isn't a guaranteed-importable package
across every pytest invocation style this project's README documents
(plain `pytest tests/test_api_integration.py -v`, which is also what
CI runs) — a cross-file import here could work locally and fail in CI
depending on rootdir detection. A few duplicated lines is a smaller
risk than a new, environment-sensitive way for the suite to break.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import async_session
from app.models import City, PendingSignup, User
from app.routers.auth import pwd_context, create_access_token


@pytest.fixture(scope="session")
def event_loop():
    """See test_api_integration.py's identical fixture for why this is session-scoped."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def city_id():
    from sqlalchemy import select
    from geoalchemy2.functions import ST_MakePoint, ST_SetSRID
    async with async_session() as db:
        existing = await db.scalar(select(City).where(City.name == "Vilnius"))
        if existing:
            return existing.id
        city = City(name="Vilnius", country_code="LT", center=ST_SetSRID(ST_MakePoint(25.28, 54.69), 4326))
        db.add(city)
        await db.commit()
        await db.refresh(city)
        return city.id


async def _make_user(city_id, name="Test User") -> tuple[uuid.UUID, str]:
    """Returns (user_id, bearer_token) for a fresh throwaway user — bypasses signup/OTP, matching test_api_integration.py's helper."""
    async with async_session() as db:
        user = User(
            email=f"{uuid.uuid4()}@test.around",
            password_hash=pwd_context.hash("testpass123"),
            display_name=name,
            city_id=city_id,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id, create_access_token(str(user.id))


async def _make_verified_user_with_email(city_id, email, name="Test User"):
    async with async_session() as db:
        user = User(email=email, password_hash=pwd_context.hash("testpass123"), display_name=name, city_id=city_id, email_verified=True)
        db.add(user)
        await db.commit()


async def _signup_and_capture_otp(client, email, password="testpass123", name="OTP User", city="Vilnius"):
    """
    Calls the REAL /auth/signup endpoint end-to-end; only the outbound
    email itself is mocked (send_signup_otp_email), so what's captured
    here is exactly the code the real endpoint generated and would have
    emailed — not a bypass of the endpoint's own logic.
    """
    captured = {}
    with patch("app.routers.auth.send_signup_otp_email", side_effect=lambda to, code: captured.update(to=to, code=code)):
        r = await client.post("/auth/signup", json={"email": email, "password": password, "display_name": name, "city": city})
    return r, captured.get("code")


class TestAuthSecurity:
    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_generic_message(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        await _make_verified_user_with_email(city_id, email, "Wrong Pw")
        r = await client.post("/auth/login", json={"email": email, "password": "wrongpassword"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Wrong email or password."

    @pytest.mark.asyncio
    async def test_login_nonexistent_email_returns_the_same_generic_message(self, client):
        r = await client.post("/auth/login", json={"email": f"{uuid.uuid4()}@nope.around", "password": "whatever123"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Wrong email or password."

    @pytest.mark.asyncio
    async def test_login_email_is_case_insensitive(self, client, city_id):
        email = f"{uuid.uuid4()}@Test.Around"
        await _make_verified_user_with_email(city_id, email.lower(), "Case Login")
        r = await client.post("/auth/login", json={"email": email.upper(), "password": "testpass123"})
        assert r.status_code == 200, r.text


class TestSignupOtp:
    """
    Signup now goes through app/models.py's PendingSignup (Option A —
    "verify before creating the account") rather than the previous
    link-based EmailVerificationToken flow: no User row exists at all
    until the OTP is verified, so there is no window where an
    unverified, login-capable account exists.
    """

    @pytest.mark.asyncio
    async def test_signup_creates_a_pending_signup_not_a_user(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        r, code = await _signup_and_capture_otp(client, email)
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "otp_sent", "email": email}
        assert code is not None and len(code) == 6 and code.isdigit()

        async with async_session() as db:
            from sqlalchemy import select
            assert await db.scalar(select(User).where(User.email == email)) is None
            pending = await db.scalar(select(PendingSignup).where(PendingSignup.email == email))
            assert pending is not None
            assert pending.otp_hash != code  # raw code is never persisted
            assert pending.attempt_count == 0

    @pytest.mark.asyncio
    async def test_account_is_not_usable_before_verification(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        await _signup_and_capture_otp(client, email, password="correctpass1")
        login = await client.post("/auth/login", json={"email": email, "password": "correctpass1"})
        assert login.status_code == 401  # no User row exists yet — not "unverified", genuinely absent

    @pytest.mark.asyncio
    async def test_correct_otp_completes_signup_and_the_account_can_then_log_in(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        _, code = await _signup_and_capture_otp(client, email)
        r = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": code})
        assert r.status_code == 200, r.text
        assert r.json()["token_type"] == "bearer" and r.json()["access_token"]

        async with async_session() as db:
            from sqlalchemy import select
            user = await db.scalar(select(User).where(User.email == email))
            assert user is not None and user.email_verified is True
            assert await db.scalar(select(PendingSignup).where(PendingSignup.email == email)) is None

        login = await client.post("/auth/login", json={"email": email, "password": "testpass123"})
        assert login.status_code == 200, login.text

    @pytest.mark.asyncio
    async def test_wrong_otp_is_rejected_and_counts_as_an_attempt(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        _, code = await _signup_and_capture_otp(client, email)
        wrong = "000000" if code != "000000" else "111111"
        r = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": wrong})
        assert r.status_code == 400
        assert r.json()["detail"] == "Incorrect verification code."
        async with async_session() as db:
            from sqlalchemy import select
            pending = await db.scalar(select(PendingSignup).where(PendingSignup.email == email))
            assert pending.attempt_count == 1

    @pytest.mark.asyncio
    async def test_otp_is_single_use(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        _, code = await _signup_and_capture_otp(client, email)
        first = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": code})
        assert first.status_code == 200, first.text
        second = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": code})
        assert second.status_code == 400  # the PendingSignup row is gone — the account already exists

    @pytest.mark.asyncio
    async def test_expired_otp_is_rejected(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        _, code = await _signup_and_capture_otp(client, email)
        async with async_session() as db:
            from sqlalchemy import select
            pending = await db.scalar(select(PendingSignup).where(PendingSignup.email == email))
            pending.otp_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        r = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": code})
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_too_many_attempts_blocks_even_the_correct_code(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        _, code = await _signup_and_capture_otp(client, email)
        wrong = "000000" if code != "000000" else "111111"
        last = None
        for _ in range(5):
            last = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": wrong})
        assert last.status_code == 429
        assert "Too many attempts" in last.json()["detail"]
        r = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": code})
        assert r.status_code == 429

    @pytest.mark.asyncio
    async def test_resend_issues_a_new_code_and_invalidates_the_old_one(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        _, first_code = await _signup_and_capture_otp(client, email)

        async with async_session() as db:
            from sqlalchemy import select
            pending = await db.scalar(select(PendingSignup).where(PendingSignup.email == email))
            pending.last_sent_at = datetime.now(timezone.utc) - timedelta(seconds=60)  # clear the resend cooldown for this test
            await db.commit()

        captured = {}
        with patch("app.routers.auth.send_signup_otp_email", side_effect=lambda to, code: captured.update(code=code)):
            r = await client.post("/auth/resend-signup-otp", json={"email": email})
        assert r.status_code == 200, r.text
        new_code = captured["code"]

        stale = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": first_code})
        assert stale.status_code == 400  # old code no longer matches the (now different) stored hash

        fresh = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": new_code})
        assert fresh.status_code == 200, fresh.text

    @pytest.mark.asyncio
    async def test_resend_is_rate_limited_by_cooldown(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        await _signup_and_capture_otp(client, email)
        r = await client.post("/auth/resend-signup-otp", json={"email": email})
        assert r.status_code == 429  # immediate resend, inside RESEND_COOLDOWN
        assert "wait" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_signup_duplicate_of_a_verified_account_is_rejected(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        await _make_verified_user_with_email(city_id, email)
        r, _ = await _signup_and_capture_otp(client, email)
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_email_is_normalized_for_duplicate_detection(self, client, city_id):
        base = f"{uuid.uuid4()}@Test.Around"
        await _make_verified_user_with_email(city_id, base.lower())
        r, _ = await _signup_and_capture_otp(client, base.upper())
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_resignup_with_a_still_pending_email_replaces_it_rather_than_erroring(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        r1, code1 = await _signup_and_capture_otp(client, email, name="First Try")
        assert r1.status_code == 200, r1.text
        r2, code2 = await _signup_and_capture_otp(client, email, name="Second Try")
        assert r2.status_code == 200, r2.text  # not a 409 — a pending (unverified) signup isn't a real account yet

        stale = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": code1})
        assert stale.status_code == 400

        fresh = await client.post("/auth/verify-signup-otp", json={"email": email, "otp": code2})
        assert fresh.status_code == 200, fresh.text

        async with async_session() as db:
            from sqlalchemy import select
            user = await db.scalar(select(User).where(User.email == email))
            assert user.display_name == "Second Try"

    @pytest.mark.asyncio
    async def test_moderated_nickname_is_rejected_before_any_pending_signup_is_created(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        r, _ = await _signup_and_capture_otp(client, email, name="fuck")
        assert r.status_code == 400
        async with async_session() as db:
            from sqlalchemy import select
            assert await db.scalar(select(PendingSignup).where(PendingSignup.email == email)) is None


class TestSignupOtpDisabled:
    """
    REQUIRE_SIGNUP_OTP currently defaults to False in production (user
    request: "disable 2FA for now i will activate it later" — SMTP
    isn't configured yet, so no one could otherwise complete signup at
    all). CI forces it back on for the class above so the real OTP path
    keeps getting exercised regardless of production's current default
    (see .github/workflows/ci.yml) — this class covers the *other*
    branch directly, with the setting patched off just for these tests,
    so both codepaths in app/routers/auth.py's signup() have real
    coverage rather than only whichever one CI's env happens to select.
    """

    @pytest.mark.asyncio
    async def test_signup_creates_the_account_immediately_when_otp_is_disabled(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        with patch("app.routers.auth.settings.REQUIRE_SIGNUP_OTP", False):
            r = await client.post("/auth/signup", json={"email": email, "password": "testpass123", "display_name": "No OTP", "city": "Vilnius"})
        assert r.status_code == 200, r.text
        assert r.json()["token_type"] == "bearer" and r.json()["access_token"]

        async with async_session() as db:
            from sqlalchemy import select
            user = await db.scalar(select(User).where(User.email == email))
            assert user is not None and user.email_verified is True
            assert await db.scalar(select(PendingSignup).where(PendingSignup.email == email)) is None

    @pytest.mark.asyncio
    async def test_created_account_can_log_in_immediately(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        with patch("app.routers.auth.settings.REQUIRE_SIGNUP_OTP", False):
            await client.post("/auth/signup", json={"email": email, "password": "testpass123", "display_name": "No OTP 2", "city": "Vilnius"})
        login = await client.post("/auth/login", json={"email": email, "password": "testpass123"})
        assert login.status_code == 200, login.text

    @pytest.mark.asyncio
    async def test_duplicate_email_still_rejected_with_otp_disabled(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        await _make_verified_user_with_email(city_id, email)
        with patch("app.routers.auth.settings.REQUIRE_SIGNUP_OTP", False):
            r = await client.post("/auth/signup", json={"email": email, "password": "testpass123", "display_name": "Dup", "city": "Vilnius"})
        assert r.status_code == 409


class TestModeration:
    @pytest.mark.asyncio
    async def test_signup_rejects_an_offensive_nickname(self, client, city_id):
        r = await client.post("/auth/signup", json={
            "email": f"{uuid.uuid4()}@test.around", "password": "testpass123",
            "display_name": "fuck", "city": "Vilnius",
        })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_profile_update_rejects_an_offensive_bio(self, client, city_id):
        _, token = await _make_user(city_id, "ModTest")
        r = await client.patch("/users/me", json={"bio": "you are a fucking idiot"}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_profile_update_allows_an_ordinary_bio(self, client, city_id):
        _, token = await _make_user(city_id, "ModTest2")
        r = await client.patch("/users/me", json={"bio": "I love hiking and coffee"}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text


class TestInterests:
    @pytest.mark.asyncio
    async def test_interest_catalog_is_exposed(self, client):
        r = await client.get("/users/interests")
        assert r.status_code == 200
        assert "Sports" in r.json()["groups"]

    @pytest.mark.asyncio
    async def test_profile_update_rejects_an_unknown_interest(self, client, city_id):
        _, token = await _make_user(city_id, "IntTest")
        r = await client.patch("/users/me", json={"interests": ["NotARealInterest"]}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_profile_update_accepts_curated_interests(self, client, city_id):
        _, token = await _make_user(city_id, "IntTest2")
        r = await client.patch("/users/me", json={"interests": ["Basketball", "Coffee"]}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        me = await client.get("/users/me/interests", headers={"Authorization": f"Bearer {token}"})
        assert sorted(me.json()["interests"]) == ["Basketball", "Coffee"]


class TestFriendRequestNotifications:
    @pytest.mark.asyncio
    async def test_friend_request_creates_an_actionable_notification_for_the_recipient(self, client, city_id):
        a_id, a_token = await _make_user(city_id, "FrA")
        b_id, b_token = await _make_user(city_id, "FrB")
        r = await client.post(f"/friends/request/{b_id}", headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code == 201

        notifs = (await client.get("/notifications", headers={"Authorization": f"Bearer {b_token}"})).json()
        friend_notifs = [n for n in notifs if n["type"] == "friend_request"]
        assert len(friend_notifs) == 1
        assert friend_notifs[0]["payload"]["requester_id"] == str(a_id)
        assert friend_notifs[0]["payload"]["still_pending"] is True

    @pytest.mark.asyncio
    async def test_accepting_clears_still_pending_and_both_sides_see_the_friendship(self, client, city_id):
        a_id, a_token = await _make_user(city_id, "FrC")
        b_id, b_token = await _make_user(city_id, "FrD")
        await client.post(f"/friends/request/{b_id}", headers={"Authorization": f"Bearer {a_token}"})
        accept = await client.post(f"/friends/accept/{a_id}", headers={"Authorization": f"Bearer {b_token}"})
        assert accept.status_code == 200, accept.text

        notifs = (await client.get("/notifications", headers={"Authorization": f"Bearer {b_token}"})).json()
        friend_notifs = [n for n in notifs if n["type"] == "friend_request"]
        assert friend_notifs[0]["payload"]["still_pending"] is False

        a_friends = (await client.get("/friends", headers={"Authorization": f"Bearer {a_token}"})).json()
        assert any(f["user_id"] == str(b_id) for f in a_friends)
        b_friends = (await client.get("/friends", headers={"Authorization": f"Bearer {b_token}"})).json()
        assert any(f["user_id"] == str(a_id) for f in b_friends)

    @pytest.mark.asyncio
    async def test_declining_notifies_the_requester(self, client, city_id):
        a_id, a_token = await _make_user(city_id, "FrE")
        b_id, b_token = await _make_user(city_id, "FrF")
        await client.post(f"/friends/request/{b_id}", headers={"Authorization": f"Bearer {a_token}"})
        reject = await client.post(f"/friends/reject/{a_id}", headers={"Authorization": f"Bearer {b_token}"})
        assert reject.status_code == 200
        notifs = (await client.get("/notifications", headers={"Authorization": f"Bearer {a_token}"})).json()
        assert any(n["type"] == "friend_declined" for n in notifs)

    @pytest.mark.asyncio
    async def test_duplicate_friend_request_does_not_create_a_second_notification(self, client, city_id):
        a_id, a_token = await _make_user(city_id, "FrG")
        b_id, b_token = await _make_user(city_id, "FrH")
        await client.post(f"/friends/request/{b_id}", headers={"Authorization": f"Bearer {a_token}"})
        second = await client.post(f"/friends/request/{b_id}", headers={"Authorization": f"Bearer {a_token}"})
        assert second.status_code == 201
        assert second.json()["status"] == "already_pending"
        notifs = (await client.get("/notifications", headers={"Authorization": f"Bearer {b_token}"})).json()
        friend_notifs = [n for n in notifs if n["type"] == "friend_request"]
        assert len(friend_notifs) == 1


class TestChat:
    @pytest.mark.asyncio
    async def test_non_friend_cannot_message(self, client, city_id):
        _, a_token = await _make_user(city_id, "ChA")
        b_id, _ = await _make_user(city_id, "ChB")
        r = await client.post(f"/chat/{b_id}/messages", json={"body": "hi"}, headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_friends_can_message_and_it_persists_and_appears_in_the_conversation_list(self, client, city_id):
        a_id, a_token = await _make_user(city_id, "ChC")
        b_id, b_token = await _make_user(city_id, "ChD")
        await client.post(f"/friends/request/{b_id}", headers={"Authorization": f"Bearer {a_token}"})
        await client.post(f"/friends/accept/{a_id}", headers={"Authorization": f"Bearer {b_token}"})

        send = await client.post(f"/chat/{b_id}/messages", json={"body": "hey there"}, headers={"Authorization": f"Bearer {a_token}"})
        assert send.status_code == 201, send.text

        messages = await client.get(f"/chat/{a_id}/messages", headers={"Authorization": f"Bearer {b_token}"})
        assert messages.status_code == 200
        assert any(m["body"] == "hey there" for m in messages.json())

        convos = (await client.get("/chat/conversations", headers={"Authorization": f"Bearer {b_token}"})).json()
        assert any(c["friend_user_id"] == str(a_id) and c["last_message"] == "hey there" for c in convos)

    @pytest.mark.asyncio
    async def test_reading_a_conversation_marks_it_read(self, client, city_id):
        a_id, a_token = await _make_user(city_id, "ChE")
        b_id, b_token = await _make_user(city_id, "ChF")
        await client.post(f"/friends/request/{b_id}", headers={"Authorization": f"Bearer {a_token}"})
        await client.post(f"/friends/accept/{a_id}", headers={"Authorization": f"Bearer {b_token}"})
        await client.post(f"/chat/{b_id}/messages", json={"body": "read me"}, headers={"Authorization": f"Bearer {a_token}"})

        before = (await client.get("/chat/conversations", headers={"Authorization": f"Bearer {b_token}"})).json()
        assert next(c for c in before if c["friend_user_id"] == str(a_id))["unread_count"] == 1

        await client.get(f"/chat/{a_id}/messages", headers={"Authorization": f"Bearer {b_token}"})  # opening the thread marks it read

        after = (await client.get("/chat/conversations", headers={"Authorization": f"Bearer {b_token}"})).json()
        assert next(c for c in after if c["friend_user_id"] == str(a_id))["unread_count"] == 0
