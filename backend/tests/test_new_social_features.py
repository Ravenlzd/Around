"""
Integration tests for this pass's new features: email verification,
signup/login error handling, nickname/bio moderation, the curated
interests catalog, friend-request notifications, and friend-to-friend
chat. Same real-Postgres requirement as test_api_integration.py — see
that file's module docstring for how to run these (needs a live
Postgres/PostGIS, not available in this sandbox — see the session's
own report for confirmation these were compiled and reasoned through
but not executed here).

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

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import async_session
from app.models import City, EmailVerificationToken, User
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
    """Returns (user_id, bearer_token) for a fresh throwaway user — bypasses signup/moderation, matching test_api_integration.py's helper."""
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


class TestAuthSecurity:
    @pytest.mark.asyncio
    async def test_signup_duplicate_email_returns_409_with_clear_message(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        payload = {"email": email, "password": "testpass123", "display_name": "Dup User", "city": "Vilnius"}
        first = await client.post("/auth/signup", json=payload)
        assert first.status_code == 200, first.text
        second = await client.post("/auth/signup", json=payload)
        assert second.status_code == 409
        assert "already exists" in second.json()["detail"]

    @pytest.mark.asyncio
    async def test_signup_email_normalized_case_insensitively_for_duplicates(self, client, city_id):
        base = f"{uuid.uuid4()}@Test.Around"
        payload = {"email": base, "password": "testpass123", "display_name": "Case User", "city": "Vilnius"}
        first = await client.post("/auth/signup", json=payload)
        assert first.status_code == 200, first.text
        dup = dict(payload, email=base.upper())
        second = await client.post("/auth/signup", json=dup)
        assert second.status_code == 409

    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_generic_message(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        await client.post("/auth/signup", json={"email": email, "password": "correctpass1", "display_name": "Wrong Pw", "city": "Vilnius"})
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
        await client.post("/auth/signup", json={"email": email, "password": "testpass123", "display_name": "Case Login", "city": "Vilnius"})
        r = await client.post("/auth/login", json={"email": email.upper(), "password": "testpass123"})
        assert r.status_code == 200, r.text


class TestEmailVerification:
    @pytest.mark.asyncio
    async def test_signup_creates_an_unverified_user_and_a_token(self, client, city_id):
        email = f"{uuid.uuid4()}@test.around"
        r = await client.post("/auth/signup", json={"email": email, "password": "testpass123", "display_name": "Verify Me", "city": "Vilnius"})
        assert r.status_code == 200, r.text
        async with async_session() as db:
            from sqlalchemy import select
            user = await db.scalar(select(User).where(User.email == email))
            assert user is not None and user.email_verified is False
            token_row = await db.scalar(select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id))
            assert token_row is not None and token_row.used_at is None

    @pytest.mark.asyncio
    async def test_verify_email_rejects_an_invalid_token(self, client):
        r = await client.post("/auth/verify-email", json={"token": "not-a-real-token"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_resend_verification_requires_auth(self, client):
        r = await client.post("/auth/resend-verification")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_full_verification_round_trip_is_single_use(self, client, city_id):
        from app.routers.auth import _issue_verification_token
        user_id, _token = await _make_user(city_id, "RoundTrip")
        async with async_session() as db:
            user = await db.get(User, user_id)
            raw_token = await _issue_verification_token(db, user)
            await db.commit()

        first = await client.post("/auth/verify-email", json={"token": raw_token})
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "verified"

        second = await client.post("/auth/verify-email", json={"token": raw_token})
        assert second.status_code == 400  # already used

        async with async_session() as db:
            refreshed = await db.get(User, user_id)
            assert refreshed.email_verified is True


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
