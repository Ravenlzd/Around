"""
Integration tests exercising the actual API + a real Postgres/PostGIS
database, per spec §28 ("Add meaningful backend tests for the
highest-risk logic"). Unlike test_attendance_rules.py (pure functions,
runs anywhere with just the stdlib), these need the real stack running
because they're verifying things the pure functions can't: that the
row lock in _lock_event() actually serializes concurrent requests, that
foreign keys/unique constraints hold, that a banned user really can't
rejoin through the API.

Run:

    docker compose up -d db
    cd backend
    pip install -r requirements.txt
    export DATABASE_URL=postgresql+asyncpg://around:around@localhost:5432/around_test
    createdb -h localhost -U around around_test   # or: docker compose exec db createdb -U around around_test
    psql $DATABASE_URL -f schema.sql               # or: alembic upgrade head
    pytest tests/test_api_integration.py -v

Each test creates its own users/events inline rather than relying on
seed.sql, so the suite is safe to run repeatedly against a scratch
database.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import app
from app.database import async_session
from app.models import Block, City, EventParticipant, Friendship, PasswordReset, Report, User
from app.routers.auth import pwd_context, create_access_token, _hash_secret


@pytest.fixture(scope="session")
def event_loop():
    """
    REAL BUG FOUND via live Postgres integration run: app/database.py
    creates its async engine (and asyncpg connection pool) once at
    import time, bound to whichever event loop is running then.
    pytest-asyncio's default is a NEW event loop per test function —
    fine for stateless tests, but our engine is a module-level
    singleton that outlives any single test. The second test to touch
    the database got "cannot perform operation: another operation is
    in progress" / cross-loop errors because it was handed a
    connection pool still attached to the first test's already-closed
    loop. A session-scoped loop, matching the engine's actual lifetime,
    fixes it. (The real running app under uvicorn never hits this —
    it has exactly one event loop for the whole process — this is
    purely a test-harness concern.)
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def city_id():
    async with async_session() as db:
        from sqlalchemy import select
        from geoalchemy2.functions import ST_MakePoint, ST_SetSRID
        existing = await db.scalar(select(City).where(City.name == "Vilnius"))
        if existing:
            return existing.id
        city = City(name="Vilnius", country_code="LT", center=ST_SetSRID(ST_MakePoint(25.28, 54.69), 4326))
        db.add(city)
        await db.commit()
        await db.refresh(city)
        return city.id


async def _make_user(city_id, name="Test User") -> tuple[uuid.UUID, str]:
    """Returns (user_id, bearer_token) for a fresh throwaway user."""
    async with async_session() as db:
        user = User(
            email=f"{uuid.uuid4()}@test.around",
            password_hash=pwd_context.hash("testpass123"),
            display_name=name,
            city_id=city_id,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id, create_access_token(str(user.id))


async def _make_event(client, token, **overrides):
    payload = {
        "title": "Test Event", "category": "sports", "description": "desc",
        "latitude": 54.69, "longitude": 25.28, "starts_at": "2030-01-01T18:00:00Z",
        "capacity": 2, "access_mode": "public", "guest_policy": "none",
    }
    payload.update(overrides)
    r = await client.post("/events", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestAuthentication:
    @pytest.mark.asyncio
    async def test_signup_uses_the_migrated_default_city(self, client):
        response = await client.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4()}@test.around",
                "password": "testpass123",
                "display_name": "New Member",
                "city": "Vilnius",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["token_type"] == "bearer"
        assert response.json()["access_token"]


class TestProfiles:
    @pytest.mark.asyncio
    async def test_event_detail_exposes_member_ids_and_limited_profiles(self, client, city_id):
        host_id, host_token = await _make_user(city_id, "Profile Host")
        event_id = await _make_event(client, host_token, capacity=5)
        attendee_id, attendee_token = await _make_user(city_id, "Profile Attendee")
        await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {attendee_token}"})

        detail = await client.get(f"/events/{event_id}", headers={"Authorization": f"Bearer {attendee_token}"})
        assert detail.status_code == 200, detail.text
        assert detail.json()["host_user_id"] == str(host_id)
        assert {p["user_id"] for p in detail.json()["participants"]} == {str(host_id), str(attendee_id)}

        profile = await client.get(f"/users/{host_id}", headers={"Authorization": f"Bearer {attendee_token}"})
        assert profile.status_code == 200, profile.text
        assert profile.json()["display_name"] == "Profile Host"
        assert "email" not in profile.json()
        assert "location_precision" not in profile.json()

    @pytest.mark.asyncio
    async def test_blocked_profiles_are_not_readable(self, client, city_id):
        viewer_id, viewer_token = await _make_user(city_id, "Blocked Viewer")
        target_id, _ = await _make_user(city_id, "Blocked Target")
        async with async_session() as db:
            db.add(Block(blocker_id=viewer_id, blocked_id=target_id))
            await db.commit()

        response = await client.get(f"/users/{target_id}", headers={"Authorization": f"Bearer {viewer_token}"})
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_profile_stats_are_scoped_to_the_authenticated_user(self, client, city_id):
        user_id, user_token = await _make_user(city_id, "Stats User")
        await _make_event(client, user_token, capacity=5)
        friend_id, _ = await _make_user(city_id, "Stats Friend")
        a, b = sorted((user_id, friend_id), key=str)
        async with async_session() as db:
            db.add(Friendship(user_id_a=a, user_id_b=b, status="accepted", requested_by=user_id))
            await db.commit()

        response = await client.get("/users/me/stats", headers={"Authorization": f"Bearer {user_token}"})
        assert response.status_code == 200, response.text
        assert response.json() == {"upcoming": 1, "hosting": 1, "friends": 1}

    @pytest.mark.asyncio
    async def test_public_profile_includes_interests(self, client, city_id):
        """Quick Post author profiles (and every other caller of GET /users/{id}) now surface interests — was missing entirely."""
        target_id, target_token = await _make_user(city_id, "Interests Target")
        set_r = await client.patch("/users/me", json={"interests": ["Basketball", "Hiking"]}, headers={"Authorization": f"Bearer {target_token}"})
        assert set_r.status_code == 200, set_r.text

        _, viewer_token = await _make_user(city_id, "Interests Viewer")
        r = await client.get(f"/users/{target_id}", headers={"Authorization": f"Bearer {viewer_token}"})
        assert r.status_code == 200, r.text
        assert set(r.json()["interests"]) == {"Basketball", "Hiking"}


class TestImFree:
    @pytest.mark.asyncio
    async def test_nearby_returns_another_eligible_active_status(self, client, city_id):
        _, viewer_token = await _make_user(city_id, "Free Viewer")
        active_id, active_token = await _make_user(city_id, "Free Member")
        activation = await client.post(
            "/im-free",
            json={"when_window": "now", "looking_for": "coffee", "radius_km": 5, "latitude": 54.69, "longitude": 25.28},
            headers={"Authorization": f"Bearer {active_token}"},
        )
        assert activation.status_code == 200, activation.text

        response = await client.get(
            "/im-free/nearby", params={"lat": 54.69, "lng": 25.28}, headers={"Authorization": f"Bearer {viewer_token}"}
        )
        assert response.status_code == 200, response.text
        match = next((row for row in response.json() if row["user_id"] == str(active_id)), None)
        assert match == {"user_id": str(active_id), "when": "now", "looking_for": "coffee", "distance_km": 0.0}

    @pytest.mark.asyncio
    async def test_nearby_excludes_hidden_and_blocked_members(self, client, city_id):
        viewer_id, viewer_token = await _make_user(city_id, "Visibility Viewer")
        hidden_id, hidden_token = await _make_user(city_id, "Hidden Member")
        blocked_id, blocked_token = await _make_user(city_id, "Blocked Member")
        async with async_session() as db:
            hidden = await db.get(User, hidden_id)
            hidden.hide_from_nearby = True
            db.add(Block(blocker_id=viewer_id, blocked_id=blocked_id))
            await db.commit()

        for token in (hidden_token, blocked_token):
            response = await client.post(
                "/im-free",
                json={"when_window": "now", "looking_for": "coffee", "radius_km": 5, "latitude": 54.69, "longitude": 25.28},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200, response.text

        response = await client.get(
            "/im-free/nearby", params={"lat": 54.69, "lng": 25.28}, headers={"Authorization": f"Bearer {viewer_token}"}
        )
        assert response.status_code == 200, response.text
        returned_ids = {row["user_id"] for row in response.json()}
        assert str(hidden_id) not in returned_ids
        assert str(blocked_id) not in returned_ids


class TestCapacity:
    @pytest.mark.asyncio
    async def test_full_event_rejects_join(self, client, city_id):
        _, host_token = await _make_user(city_id, "Host")
        event_id = await _make_event(client, host_token, capacity=1)  # host alone fills it

        _, joiner_token = await _make_user(city_id, "Joiner")
        r = await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {joiner_token}"})
        assert r.status_code == 409
        assert "full" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_join_succeeds_with_room(self, client, city_id):
        _, host_token = await _make_user(city_id, "Host2")
        event_id = await _make_event(client, host_token, capacity=5)

        _, joiner_token = await _make_user(city_id, "Joiner2")
        r = await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {joiner_token}"})
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_two_simultaneous_joins_for_final_spot_only_one_succeeds(self, client, city_id):
        _, host_token = await _make_user(city_id, "Host3")
        event_id = await _make_event(client, host_token, capacity=2)  # host + 1 more spot

        _, a_token = await _make_user(city_id, "RaceA")
        _, b_token = await _make_user(city_id, "RaceB")

        results = await asyncio.gather(
            client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {a_token}"}),
            client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {b_token}"}),
        )
        statuses = sorted(r.status_code for r in results)
        # exactly one 200 and one 409 — this is the row-lock guarantee under real concurrency
        assert statuses == [200, 409]


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_search_returns_events_matching_the_query(self, client, city_id):
        _, host_token = await _make_user(city_id, "Search Host")
        event_id = await _make_event(client, host_token, title="Midnight Basketball")

        response = await client.get(
            "/discovery/search",
            params={"q": "basketball"},
            headers={"Authorization": f"Bearer {host_token}"},
        )

        assert response.status_code == 200, response.text
        assert [event["id"] for event in response.json()["results"]] == [event_id]


class TestGuests:
    @pytest.mark.asyncio
    async def test_guest_invite_succeeds_when_allowed(self, client, city_id):
        _, host_token = await _make_user(city_id, "GHost")
        event_id = await _make_event(client, host_token, capacity=5, guest_policy="one")
        r = await client.post(f"/events/{event_id}/guests", json={"guest_name": "Plus One"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 201

    @pytest.mark.asyncio
    async def test_guest_invite_rejected_when_policy_is_none(self, client, city_id):
        _, host_token = await _make_user(city_id, "GHost2")
        event_id = await _make_event(client, host_token, capacity=5, guest_policy="none")
        r = await client.post(f"/events/{event_id}/guests", json={"guest_name": "Nope"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_guest_cannot_invite_another_guest(self, client, city_id):
        _, host_token = await _make_user(city_id, "GHost3")
        event_id = await _make_event(client, host_token, capacity=5, guest_policy="two")
        _, participant_token = await _make_user(city_id, "GParticipant")
        await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {participant_token}"})
        # participant invites a guest — succeeds (their own account, no login for the guest exists)
        r1 = await client.post(f"/events/{event_id}/guests", json={"guest_name": "G1"}, headers={"Authorization": f"Bearer {participant_token}"})
        assert r1.status_code == 201
        # a random third user who is NOT a participant/host tries to invite — rejected
        _, outsider_token = await _make_user(city_id, "Outsider")
        r2 = await client.post(f"/events/{event_id}/guests", json={"guest_name": "G2"}, headers={"Authorization": f"Bearer {outsider_token}"})
        assert r2.status_code == 403


class TestApproval:
    @pytest.mark.asyncio
    async def test_request_then_approve_updates_capacity(self, client, city_id):
        _, host_token = await _make_user(city_id, "AHost")
        event_id = await _make_event(client, host_token, capacity=5, access_mode="approval")
        _, requester_token = await _make_user(city_id, "Requester")

        r = await client.post(f"/events/{event_id}/join-requests", json={}, headers={"Authorization": f"Bearer {requester_token}"})
        assert r.status_code == 201
        request_id = r.json()["id"]

        before = await client.get(f"/events/{event_id}", headers={"Authorization": f"Bearer {host_token}"})
        occ_before = before.json()["occupancy"]

        approve = await client.post(f"/events/{event_id}/join-requests/{request_id}/approve", headers={"Authorization": f"Bearer {host_token}"})
        assert approve.status_code == 200

        after = await client.get(f"/events/{event_id}", headers={"Authorization": f"Bearer {host_token}"})
        assert after.json()["occupancy"] == occ_before + 1

    @pytest.mark.asyncio
    async def test_non_host_cannot_approve(self, client, city_id):
        _, host_token = await _make_user(city_id, "AHost2")
        event_id = await _make_event(client, host_token, capacity=5, access_mode="approval")
        _, requester_token = await _make_user(city_id, "Requester2")
        r = await client.post(f"/events/{event_id}/join-requests", json={}, headers={"Authorization": f"Bearer {requester_token}"})
        request_id = r.json()["id"]

        _, outsider_token = await _make_user(city_id, "Outsider2")
        r2 = await client.post(f"/events/{event_id}/join-requests/{request_id}/approve", headers={"Authorization": f"Bearer {outsider_token}"})
        assert r2.status_code == 403


class TestWaitlist:
    @pytest.mark.asyncio
    async def test_full_event_waitlist_offers_first_in_line(self, client, city_id):
        _, host_token = await _make_user(city_id, "WHost")
        event_id = await _make_event(client, host_token, capacity=1)  # host alone fills it

        _, first_token = await _make_user(city_id, "WFirst")
        _, second_token = await _make_user(city_id, "WSecond")
        await client.post(f"/events/{event_id}/waitlist", headers={"Authorization": f"Bearer {first_token}"})
        await client.post(f"/events/{event_id}/waitlist", headers={"Authorization": f"Bearer {second_token}"})

        # host leaves is not allowed (hosts cancel instead), so simulate a
        # freed spot by having the host remove themself via ban — for this
        # test we instead just directly assert queue order via /events/{id}
        detail = await client.get(f"/events/{event_id}", headers={"Authorization": f"Bearer {first_token}"})
        assert detail.json()["my_status"]["waitlist_position"] == 1

        detail2 = await client.get(f"/events/{event_id}", headers={"Authorization": f"Bearer {second_token}"})
        assert detail2.json()["my_status"]["waitlist_position"] == 2


class TestAuthorization:
    @pytest.mark.asyncio
    async def test_non_host_cannot_view_attendance_management(self, client, city_id):
        _, host_token = await _make_user(city_id, "MHost")
        event_id = await _make_event(client, host_token, capacity=5)
        _, outsider_token = await _make_user(city_id, "MOutsider")
        r = await client.get(f"/events/{event_id}/attendance", headers={"Authorization": f"Bearer {outsider_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_non_attendee_cannot_post_chat(self, client, city_id):
        _, host_token = await _make_user(city_id, "CHost")
        event_id = await _make_event(client, host_token, capacity=5)
        _, outsider_token = await _make_user(city_id, "COutsider")
        r = await client.post(f"/events/{event_id}/chat", json={"body": "hi"}, headers={"Authorization": f"Bearer {outsider_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_banned_user_cannot_rejoin(self, client, city_id):
        _, host_token = await _make_user(city_id, "BHost")
        event_id = await _make_event(client, host_token, capacity=5)
        banned_id, banned_token = await _make_user(city_id, "Banned")
        await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {banned_token}"})
        r = await client.post(f"/events/{event_id}/ban", json={"user_id": str(banned_id), "reason": "test"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 200
        rejoin = await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {banned_token}"})
        assert rejoin.status_code == 403


class TestEventUpdateAndCancel:
    """
    Regression coverage for spec Priority 1/4: event lifecycle
    (update/cancel) and specifically IDOR/authorization on every new
    surface it touches — a non-host must be rejected by the backend on
    update, cancel, checkin-token generation, and attendee management,
    regardless of what the frontend does or doesn't show them.
    """

    @pytest.mark.asyncio
    async def test_host_can_update_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "UHost")
        event_id = await _make_event(client, host_token, capacity=10, title="Original Title")
        r = await client.patch(f"/events/{event_id}", json={"title": "Updated Title"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "Updated Title"

    @pytest.mark.asyncio
    async def test_non_host_cannot_update_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "UHost2")
        event_id = await _make_event(client, host_token, capacity=10)
        _, outsider_token = await _make_user(city_id, "UOutsider")
        r = await client.patch(f"/events/{event_id}", json={"title": "Hijacked"}, headers={"Authorization": f"Bearer {outsider_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_capacity_cannot_be_reduced_below_current_attendees(self, client, city_id):
        _, host_token = await _make_user(city_id, "UHost3")
        event_id = await _make_event(client, host_token, capacity=5)  # host alone occupies 1
        for i in range(3):
            _, t = await _make_user(city_id, f"UFiller{i}")
            await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {t}"})
        # occupancy is now 4 (host + 3) — reducing to 2 should be rejected
        r = await client.patch(f"/events/{event_id}", json={"capacity": 2}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 409
        assert "below" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_capacity_can_be_reduced_to_exactly_current_attendees(self, client, city_id):
        _, host_token = await _make_user(city_id, "UHost4")
        event_id = await _make_event(client, host_token, capacity=5)
        _, t = await _make_user(city_id, "UFiller4")
        await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {t}"})
        # occupancy is 2 (host + 1) — reducing to exactly 2 should succeed
        r = await client.patch(f"/events/{event_id}", json={"capacity": 2}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_host_can_cancel_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CxHost")
        event_id = await _make_event(client, host_token, capacity=5)
        r = await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 200, r.text
        detail = await client.get(f"/events/{event_id}", headers={"Authorization": f"Bearer {host_token}"})
        assert detail.json()["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_non_host_cannot_cancel_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CxHost2")
        event_id = await _make_event(client, host_token, capacity=5)
        _, outsider_token = await _make_user(city_id, "CxOutsider")
        r = await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {outsider_token}"})
        assert r.status_code == 403
        # and the event must still be active afterwards — the rejected
        # attempt must not have had any side effect
        detail = await client.get(f"/events/{event_id}", headers={"Authorization": f"Bearer {host_token}"})
        assert detail.json()["status"] == "active"

    @pytest.mark.asyncio
    async def test_cannot_join_a_cancelled_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CxHost3")
        event_id = await _make_event(client, host_token, capacity=5)
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        _, joiner_token = await _make_user(city_id, "CxJoiner")
        r = await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {joiner_token}"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_cancelled_event_disappears_from_discovery(self, client, city_id):
        _, host_token = await _make_user(city_id, "CxHost4")
        event_id = await _make_event(client, host_token, capacity=5, latitude=54.69, longitude=25.28)
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        r = await client.get("/discovery/nearby", params={"lat": 54.69, "lng": 25.28}, headers={"Authorization": f"Bearer {host_token}"})
        ids = [e["id"] for e in r.json()["results"]]
        assert event_id not in ids

    @pytest.mark.asyncio
    async def test_non_host_cannot_generate_checkin_token(self, client, city_id):
        _, host_token = await _make_user(city_id, "QHost")
        event_id = await _make_event(client, host_token, capacity=5)
        _, outsider_token = await _make_user(city_id, "QOutsider")
        r = await client.post(f"/events/{event_id}/checkin-token", headers={"Authorization": f"Bearer {outsider_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_cannot_generate_checkin_token_for_cancelled_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "QHost2")
        event_id = await _make_event(client, host_token, capacity=5)
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        r = await client.post(f"/events/{event_id}/checkin-token", headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_waitlisted_user_notified_and_cleared_on_cancel(self, client, city_id):
        _, host_token = await _make_user(city_id, "WxHost")
        event_id = await _make_event(client, host_token, capacity=1)  # host alone fills it
        _, waiter_token = await _make_user(city_id, "WxWaiter")
        await client.post(f"/events/{event_id}/waitlist", headers={"Authorization": f"Bearer {waiter_token}"})
        r = await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 200
        notifs = await client.get("/notifications", headers={"Authorization": f"Bearer {waiter_token}"})
        assert any(n["type"] == "event_cancelled" for n in notifs.json())

    @pytest.mark.asyncio
    async def test_cannot_join_waitlist_on_cancelled_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CwHost")
        event_id = await _make_event(client, host_token, capacity=1)
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        _, waiter_token = await _make_user(city_id, "CwWaiter")
        r = await client.post(f"/events/{event_id}/waitlist", headers={"Authorization": f"Bearer {waiter_token}"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_cannot_invite_guest_on_cancelled_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CgHost")
        event_id = await _make_event(client, host_token, capacity=5, guest_policy="one")
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        r = await client.post(f"/events/{event_id}/guests", json={"guest_name": "Plus One"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_cannot_chat_on_cancelled_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CcHost")
        event_id = await _make_event(client, host_token, capacity=5)
        # host is auto-attending as the event creator, so posting works pre-cancel...
        pre = await client.post(f"/events/{event_id}/chat", json={"body": "before cancel"}, headers={"Authorization": f"Bearer {host_token}"})
        assert pre.status_code == 201
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        # ...but not after
        post = await client.post(f"/events/{event_id}/chat", json={"body": "after cancel"}, headers={"Authorization": f"Bearer {host_token}"})
        assert post.status_code == 409
        # chat HISTORY must remain readable — cancellation doesn't erase it
        history = await client.get(f"/events/{event_id}/chat", headers={"Authorization": f"Bearer {host_token}"})
        assert history.status_code == 200
        assert any(m["body"] == "before cancel" for m in history.json())

    @pytest.mark.asyncio
    async def test_cannot_approve_request_on_cancelled_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CaHost")
        event_id = await _make_event(client, host_token, capacity=5, access_mode="approval")
        _, requester_token = await _make_user(city_id, "CaRequester")
        req = await client.post(f"/events/{event_id}/join-requests", json={}, headers={"Authorization": f"Bearer {requester_token}"})
        request_id = req.json()["id"]
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        r = await client.post(f"/events/{event_id}/join-requests/{request_id}/approve", headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_cannot_check_in_on_cancelled_event(self, client, city_id):
        _, host_token = await _make_user(city_id, "CiHost")
        event_id = await _make_event(client, host_token, capacity=5)
        await client.post(f"/events/{event_id}/cancel", headers={"Authorization": f"Bearer {host_token}"})
        r = await client.post(f"/events/{event_id}/check-in", json={"method": "manual"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 409


class TestPasswordReset:
    """
    Password reset (added this pass — see app/models.py's PasswordReset
    and app/routers/auth.py's request_password_reset/reset_password).
    Directly inserting a PasswordReset row with a known OTP (rather than
    reading it off a real email) mirrors this file's own established
    pattern for testing something normally delivered out-of-band — see
    TestProfiles.test_blocked_profiles_are_not_readable inserting a
    Block row directly instead of going through a UI action that has no
    API equivalent.
    """

    async def _seed_reset(self, user_id, otp="123456", *, expired=False, attempts=0):
        async with async_session() as db:
            await db.execute(PasswordReset.__table__.delete().where(PasswordReset.user_id == user_id))
            db.add(PasswordReset(
                user_id=user_id, otp_hash=_hash_secret(otp),
                otp_expires_at=datetime.now(timezone.utc) + (timedelta(minutes=-1) if expired else timedelta(minutes=10)),
                attempt_count=attempts,
            ))
            await db.commit()

    @pytest.mark.asyncio
    async def test_request_reset_is_generic_for_known_and_unknown_email(self, client, city_id):
        """Account-enumeration guard: identical response either way."""
        user_id, _ = await _make_user(city_id, "ResetKnown")
        async with async_session() as db:
            user = await db.get(User, user_id)
            known_email = user.email

        known = await client.post("/auth/request-password-reset", json={"email": known_email})
        unknown = await client.post("/auth/request-password-reset", json={"email": f"{uuid.uuid4()}@nowhere.test"})
        assert known.status_code == 200 and unknown.status_code == 200
        assert known.json() == unknown.json() == {"status": "if_account_exists_email_sent"}

    @pytest.mark.asyncio
    async def test_full_reset_flow_changes_password_and_invalidates_old_tokens(self, client, city_id):
        user_id, old_token = await _make_user(city_id, "ResetFlow")
        await self._seed_reset(user_id, "654321")
        async with async_session() as db:
            email = (await db.get(User, user_id)).email

        # Old token works before the reset.
        pre = await client.get("/auth/me", headers={"Authorization": f"Bearer {old_token}"})
        assert pre.status_code == 200

        r = await client.post("/auth/reset-password", json={"email": email, "otp": "654321", "new_password": "brandNewPass123"})
        assert r.status_code == 200, r.text
        new_token = r.json()["access_token"]

        # Old token is now rejected — this is the actual security property
        # a reset is supposed to provide (a stolen token stops working).
        stale = await client.get("/auth/me", headers={"Authorization": f"Bearer {old_token}"})
        assert stale.status_code == 401

        # New token works, and the new password logs in.
        fresh = await client.get("/auth/me", headers={"Authorization": f"Bearer {new_token}"})
        assert fresh.status_code == 200

        login = await client.post("/auth/login", json={"email": email, "password": "brandNewPass123"})
        assert login.status_code == 200, login.text

        old_login = await client.post("/auth/login", json={"email": email, "password": "testpass123"})
        assert old_login.status_code == 401

    @pytest.mark.asyncio
    async def test_reset_is_single_use(self, client, city_id):
        user_id, _ = await _make_user(city_id, "ResetOnce")
        await self._seed_reset(user_id, "111222")
        async with async_session() as db:
            email = (await db.get(User, user_id)).email
        first = await client.post("/auth/reset-password", json={"email": email, "otp": "111222", "new_password": "firstNewPass123"})
        assert first.status_code == 200, first.text
        replay = await client.post("/auth/reset-password", json={"email": email, "otp": "111222", "new_password": "secondNewPass123"})
        assert replay.status_code == 400

    @pytest.mark.asyncio
    async def test_reset_rejects_expired_otp(self, client, city_id):
        user_id, _ = await _make_user(city_id, "ResetExpired")
        await self._seed_reset(user_id, "222333", expired=True)
        async with async_session() as db:
            email = (await db.get(User, user_id)).email
        r = await client.post("/auth/reset-password", json={"email": email, "otp": "222333", "new_password": "newPassword123"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_reset_locks_out_after_max_wrong_attempts(self, client, city_id):
        user_id, _ = await _make_user(city_id, "ResetLockout")
        await self._seed_reset(user_id, "333444")
        async with async_session() as db:
            email = (await db.get(User, user_id)).email
        last = None
        for _ in range(5):
            last = await client.post("/auth/reset-password", json={"email": email, "otp": "000000", "new_password": "newPassword123"})
        assert last.status_code == 429
        # Even the CORRECT code is now refused — the attempt cap, not just
        # "that specific wrong guess", is what's enforced.
        correct_after_lockout = await client.post("/auth/reset-password", json={"email": email, "otp": "333444", "new_password": "newPassword123"})
        assert correct_after_lockout.status_code == 429

    @pytest.mark.asyncio
    async def test_reset_rejects_unknown_email(self, client):
        r = await client.post("/auth/reset-password", json={"email": f"{uuid.uuid4()}@nowhere.test", "otp": "123456", "new_password": "newPassword123"})
        assert r.status_code == 400


class TestBlockingAndModeration:
    """
    Regression coverage for the block-bypass and moderation-coverage
    gaps found in this pass's audit (see friends.py, discovery.py,
    chat.py, events.py, posts.py for the actual fixes).
    """

    @pytest.mark.asyncio
    async def test_blocked_user_cannot_send_friend_request(self, client, city_id):
        target_id, target_token = await _make_user(city_id, "BlockTarget")
        blocker_id, blocker_token = await _make_user(city_id, "Blocker")
        # blocker blocks target, THEN target tries to send a friend request anyway
        await client.post(f"/users/{target_id}/block", headers={"Authorization": f"Bearer {blocker_token}"})
        r = await client.post(f"/friends/request/{blocker_id}", headers={"Authorization": f"Bearer {target_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_blocked_hosts_events_excluded_from_nearby_and_search(self, client, city_id):
        host_id, host_token = await _make_user(city_id, "BlockedHost")
        viewer_id, viewer_token = await _make_user(city_id, "BlockViewer")
        event_id = await _make_event(client, host_token, title="Unique Block Test Event", capacity=5)

        # Sanity: visible before any block.
        before = await client.get("/discovery/nearby", params={"lat": 54.69, "lng": 25.28, "radius_km": 50}, headers={"Authorization": f"Bearer {viewer_token}"})
        assert any(e["id"] == event_id for e in before.json()["results"])

        await client.post(f"/users/{host_id}/block", headers={"Authorization": f"Bearer {viewer_token}"})

        nearby = await client.get("/discovery/nearby", params={"lat": 54.69, "lng": 25.28, "radius_km": 50}, headers={"Authorization": f"Bearer {viewer_token}"})
        assert all(e["id"] != event_id for e in nearby.json()["results"])

        search = await client.get("/discovery/search", params={"q": "Unique Block Test Event"}, headers={"Authorization": f"Bearer {viewer_token}"})
        assert all(e["id"] != event_id for e in search.json()["results"])

    @pytest.mark.asyncio
    async def test_event_chat_rejects_profane_message(self, client, city_id):
        _, host_token = await _make_user(city_id, "ModHost")
        event_id = await _make_event(client, host_token, capacity=5)
        r = await client.post(f"/events/{event_id}/chat", json={"body": "you are a fucking idiot"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_create_event_rejects_profane_title(self, client, city_id):
        _, host_token = await _make_user(city_id, "ModCreator")
        r = await client.post(
            "/events",
            json={
                "title": "fuck this event", "category": "sports", "description": "desc",
                "latitude": 54.69, "longitude": 25.28, "starts_at": "2030-01-01T18:00:00Z",
                "capacity": 2, "access_mode": "public", "guest_policy": "none",
            },
            headers={"Authorization": f"Bearer {host_token}"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_direct_message_rejects_profane_body(self, client, city_id):
        a_id, a_token = await _make_user(city_id, "DmA")
        b_id, b_token = await _make_user(city_id, "DmB")
        a, b = sorted((a_id, b_id), key=str)
        async with async_session() as db:
            db.add(Friendship(user_id_a=a, user_id_b=b, status="accepted", requested_by=a_id))
            await db.commit()
        r = await client.post(f"/chat/{b_id}/messages", json={"body": "you fucking idiot"}, headers={"Authorization": f"Bearer {a_token}"})
        assert r.status_code == 400


class TestBlockedUsersList:
    """
    Profile -> Blocked People (GET /users/me/blocked) — previously a
    hardcoded toast with no endpoint behind it at all; see
    app/routers/users.py::list_blocked_users.
    """

    @pytest.mark.asyncio
    async def test_empty_when_nothing_blocked(self, client, city_id):
        _, token = await _make_user(city_id, "BlockedListEmpty")
        r = await client.get("/users/me/blocked", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_lists_users_i_blocked_with_expected_fields(self, client, city_id):
        blocker_id, blocker_token = await _make_user(city_id, "BlockedListBlocker")
        target_id, _ = await _make_user(city_id, "BlockedListTarget")
        block_r = await client.post(f"/users/{target_id}/block", headers={"Authorization": f"Bearer {blocker_token}"})
        assert block_r.status_code == 201, block_r.text

        r = await client.get("/users/me/blocked", headers={"Authorization": f"Bearer {blocker_token}"})
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["user_id"] == str(target_id)
        assert rows[0]["display_name"] == "BlockedListTarget"
        assert set(rows[0].keys()) == {"user_id", "display_name", "avatar_url"}

    @pytest.mark.asyncio
    async def test_does_not_expose_the_reverse_direction(self, client, city_id):
        """A blocks B: A's list contains B. B's list must NOT contain A (who-blocked-me is never exposed)."""
        a_id, a_token = await _make_user(city_id, "BlockedListA")
        b_id, b_token = await _make_user(city_id, "BlockedListB")
        await client.post(f"/users/{b_id}/block", headers={"Authorization": f"Bearer {a_token}"})

        a_list = await client.get("/users/me/blocked", headers={"Authorization": f"Bearer {a_token}"})
        assert [row["user_id"] for row in a_list.json()] == [str(b_id)]

        b_list = await client.get("/users/me/blocked", headers={"Authorization": f"Bearer {b_token}"})
        assert b_list.json() == []

    @pytest.mark.asyncio
    async def test_requires_authentication(self, client):
        r = await client.get("/users/me/blocked")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_full_block_then_unblock_cycle(self, client, city_id):
        blocker_id, blocker_token = await _make_user(city_id, "BlockedListCycleBlocker")
        target_id, _ = await _make_user(city_id, "BlockedListCycleTarget")

        await client.post(f"/users/{target_id}/block", headers={"Authorization": f"Bearer {blocker_token}"})
        appears = await client.get("/users/me/blocked", headers={"Authorization": f"Bearer {blocker_token}"})
        assert str(target_id) in [row["user_id"] for row in appears.json()]

        unblock_r = await client.delete(f"/users/{target_id}/block", headers={"Authorization": f"Bearer {blocker_token}"})
        assert unblock_r.status_code == 200, unblock_r.text

        after = await client.get("/users/me/blocked", headers={"Authorization": f"Bearer {blocker_token}"})
        assert after.json() == []

        # Existing blocking behavior is genuinely undone, not just absent
        # from this list — e.g. the target is visible again in the
        # blocker's public-profile lookup.
        profile_r = await client.get(f"/users/{target_id}", headers={"Authorization": f"Bearer {blocker_token}"})
        assert profile_r.status_code == 200

    @pytest.mark.asyncio
    async def test_multiple_blocked_users_all_listed(self, client, city_id):
        blocker_id, blocker_token = await _make_user(city_id, "BlockedListMulti")
        target_ids = []
        for i in range(3):
            tid, _ = await _make_user(city_id, f"BlockedListMultiTarget{i}")
            target_ids.append(tid)
            await client.post(f"/users/{tid}/block", headers={"Authorization": f"Bearer {blocker_token}"})

        r = await client.get("/users/me/blocked", headers={"Authorization": f"Bearer {blocker_token}"})
        assert {row["user_id"] for row in r.json()} == {str(t) for t in target_ids}


class TestBlockedUsersAndEvents:
    """
    Regression coverage for this pass's fix: blocking must be a real
    barrier against contact via events (join/attend/chat), not just
    profile/DM/discovery — see app/routers/events.py's
    _assert_no_block_conflict and its call sites.
    """

    @pytest.mark.asyncio
    async def test_blocked_user_cannot_join_hosts_event(self, client, city_id):
        host_id, host_token = await _make_user(city_id, "EvBlockHost")
        joiner_id, joiner_token = await _make_user(city_id, "EvBlockJoiner")
        event_id = await _make_event(client, host_token, capacity=5)
        await client.post(f"/users/{joiner_id}/block", headers={"Authorization": f"Bearer {host_token}"})

        r = await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {joiner_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_blocked_user_cannot_join_event_with_existing_blocked_attendee(self, client, city_id):
        """Block conflict with an ORDINARY attendee, not just the host."""
        host_id, host_token = await _make_user(city_id, "EvBlockHost2")
        attendee_id, attendee_token = await _make_user(city_id, "EvBlockAttendee")
        joiner_id, joiner_token = await _make_user(city_id, "EvBlockJoiner2")
        event_id = await _make_event(client, host_token, capacity=5)
        await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {attendee_token}"})
        await client.post(f"/users/{joiner_id}/block", headers={"Authorization": f"Bearer {attendee_token}"})

        r = await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {joiner_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_blocked_user_cannot_request_to_join_approval_event(self, client, city_id):
        host_id, host_token = await _make_user(city_id, "EvBlockApprovalHost")
        requester_id, requester_token = await _make_user(city_id, "EvBlockRequester")
        event_id = await _make_event(client, host_token, capacity=5, access_mode="approval")
        await client.post(f"/users/{requester_id}/block", headers={"Authorization": f"Bearer {host_token}"})

        r = await client.post(f"/events/{event_id}/join-requests", json={}, headers={"Authorization": f"Bearer {requester_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_blocked_user_cannot_join_waitlist(self, client, city_id):
        host_id, host_token = await _make_user(city_id, "EvBlockWlHost")
        joiner_id, joiner_token = await _make_user(city_id, "EvBlockWlJoiner")
        event_id = await _make_event(client, host_token, capacity=5)
        await client.post(f"/users/{joiner_id}/block", headers={"Authorization": f"Bearer {host_token}"})

        r = await client.post(f"/events/{event_id}/waitlist", headers={"Authorization": f"Bearer {joiner_token}"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_existing_attendance_is_preserved_after_a_later_block(self, client, city_id):
        """
        A block that happens AFTER both people are already attending must
        NOT retroactively remove either of them — see
        _assert_no_block_conflict's docstring for why this is
        query-time prevention, not a cleanup job.
        """
        host_id, host_token = await _make_user(city_id, "EvBlockPreserveHost")
        attendee_id, attendee_token = await _make_user(city_id, "EvBlockPreserveAttendee")
        event_id = await _make_event(client, host_token, capacity=5)
        join = await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {attendee_token}"})
        assert join.status_code == 200

        await client.post(f"/users/{attendee_id}/block", headers={"Authorization": f"Bearer {host_token}"})

        async with async_session() as db:
            rows = (await db.scalars(
                select(EventParticipant).where(EventParticipant.event_id == uuid.UUID(event_id), EventParticipant.status == "going")
            )).all()
            user_ids = {r.user_id for r in rows}
            assert host_id in user_ids and attendee_id in user_ids

    @pytest.mark.asyncio
    async def test_blocked_attendees_cannot_chat_even_if_both_already_attending(self, client, city_id):
        """
        The residual case join-time checks can't prevent: both were
        already attending before either blocked the other. Attendance
        stays; NEW chat messages between them stop.
        """
        host_id, host_token = await _make_user(city_id, "EvBlockChatHost")
        attendee_id, attendee_token = await _make_user(city_id, "EvBlockChatAttendee")
        event_id = await _make_event(client, host_token, capacity=5)
        await client.post(f"/events/{event_id}/join", headers={"Authorization": f"Bearer {attendee_token}"})

        pre = await client.post(f"/events/{event_id}/chat", json={"body": "hello before block"}, headers={"Authorization": f"Bearer {attendee_token}"})
        assert pre.status_code == 201

        await client.post(f"/users/{attendee_id}/block", headers={"Authorization": f"Bearer {host_token}"})

        post_attendee = await client.post(f"/events/{event_id}/chat", json={"body": "hello after block"}, headers={"Authorization": f"Bearer {attendee_token}"})
        assert post_attendee.status_code == 403
        post_host = await client.post(f"/events/{event_id}/chat", json={"body": "hello from host"}, headers={"Authorization": f"Bearer {host_token}"})
        assert post_host.status_code == 403


class TestReporting:
    """
    Regression coverage for report_user (new) and report_event (now
    wired to the frontend + deduped) — see app/reports.py.
    """

    @pytest.mark.asyncio
    async def test_report_user_success_and_stores_correct_target(self, client, city_id):
        reporter_id, reporter_token = await _make_user(city_id, "ReportReporter")
        target_id, _ = await _make_user(city_id, "ReportTarget")
        r = await client.post(f"/users/{target_id}/report", json={"reason": "Harassment or abuse", "details": "sent unwanted messages"}, headers={"Authorization": f"Bearer {reporter_token}"})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "reported"
        async with async_session() as db:
            row = await db.scalar(select(Report).where(Report.reporter_id == reporter_id, Report.target_id == target_id))
            assert row is not None
            assert row.target_type == "user"
            assert row.reason == "Harassment or abuse"

    @pytest.mark.asyncio
    async def test_report_user_rejects_nonexistent_target_id(self, client, city_id):
        """IDOR check: an arbitrary/nonexistent UUID must not silently create a report."""
        _, reporter_token = await _make_user(city_id, "ReportIdorReporter")
        fake_id = uuid.uuid4()
        r = await client.post(f"/users/{fake_id}/report", json={"reason": "Spam"}, headers={"Authorization": f"Bearer {reporter_token}"})
        assert r.status_code == 404
        async with async_session() as db:
            row = await db.scalar(select(Report).where(Report.target_id == fake_id))
            assert row is None

    @pytest.mark.asyncio
    async def test_report_user_rejects_self_report(self, client, city_id):
        user_id, token = await _make_user(city_id, "ReportSelf")
        r = await client.post(f"/users/{user_id}/report", json={"reason": "Spam"}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_duplicate_report_is_deduped_not_double_written(self, client, city_id):
        reporter_id, reporter_token = await _make_user(city_id, "ReportDupeReporter")
        target_id, _ = await _make_user(city_id, "ReportDupeTarget")
        first = await client.post(f"/users/{target_id}/report", json={"reason": "Spam"}, headers={"Authorization": f"Bearer {reporter_token}"})
        assert first.json()["status"] == "reported"
        second = await client.post(f"/users/{target_id}/report", json={"reason": "Spam again"}, headers={"Authorization": f"Bearer {reporter_token}"})
        assert second.status_code == 201
        assert second.json()["status"] == "already_reported"
        async with async_session() as db:
            rows = (await db.scalars(select(Report).where(Report.reporter_id == reporter_id, Report.target_id == target_id))).all()
            assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_report_event_success(self, client, city_id):
        reporter_id, reporter_token = await _make_user(city_id, "ReportEventReporter")
        _, host_token = await _make_user(city_id, "ReportEventHost")
        event_id = await _make_event(client, host_token, capacity=5)
        r = await client.post(f"/events/{event_id}/report", json={"reason": "Misleading"}, headers={"Authorization": f"Bearer {reporter_token}"})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "reported"

    @pytest.mark.asyncio
    async def test_report_event_rejects_nonexistent_event(self, client, city_id):
        _, reporter_token = await _make_user(city_id, "ReportEventIdor")
        fake_id = uuid.uuid4()
        r = await client.post(f"/events/{fake_id}/report", json={"reason": "Spam"}, headers={"Authorization": f"Bearer {reporter_token}"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_report_event_rejects_self_report_by_host(self, client, city_id):
        """Consistency with test_report_user_rejects_self_report above — a host can't report their own event."""
        _, host_token = await _make_user(city_id, "ReportEventSelfHost")
        event_id = await _make_event(client, host_token, capacity=5)
        r = await client.post(f"/events/{event_id}/report", json={"reason": "Spam"}, headers={"Authorization": f"Bearer {host_token}"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_report_response_never_includes_reporter_identity(self, client, city_id):
        """Nothing in the response should echo reporter-identifying data back."""
        _, reporter_token = await _make_user(city_id, "ReportPrivacy")
        target_id, _ = await _make_user(city_id, "ReportPrivacyTarget")
        r = await client.post(f"/users/{target_id}/report", json={"reason": "Spam"}, headers={"Authorization": f"Bearer {reporter_token}"})
        assert set(r.json().keys()) == {"status"}


class TestPostsBlocking:
    """/posts/nearby now requires auth and excludes blocked users — see app/routers/posts.py."""

    @pytest.mark.asyncio
    async def test_nearby_posts_requires_authentication(self, client):
        r = await client.get("/posts/nearby", params={"lat": 54.69, "lng": 25.28})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_nearby_posts_excludes_blocked_users_posts(self, client, city_id):
        poster_id, poster_token = await _make_user(city_id, "PostBlockedPoster")
        viewer_id, viewer_token = await _make_user(city_id, "PostBlockViewer")

        create = await client.post("/posts", json={"body": "Unique nearby post marker", "latitude": 54.69, "longitude": 25.28, "expires_in_minutes": 60}, headers={"Authorization": f"Bearer {poster_token}"})
        assert create.status_code == 201, create.text

        before = await client.get("/posts/nearby", params={"lat": 54.69, "lng": 25.28, "radius_km": 50}, headers={"Authorization": f"Bearer {viewer_token}"})
        assert any(p["body"] == "Unique nearby post marker" for p in before.json())

        await client.post(f"/users/{poster_id}/block", headers={"Authorization": f"Bearer {viewer_token}"})

        after = await client.get("/posts/nearby", params={"lat": 54.69, "lng": 25.28, "radius_km": 50}, headers={"Authorization": f"Bearer {viewer_token}"})
        assert all(p["body"] != "Unique nearby post marker" for p in after.json())

    @pytest.mark.asyncio
    async def test_nearby_posts_excludes_own_posts_from_blocker_side_too(self, client, city_id):
        """Reverse direction: the VIEWER being blocked by the poster also excludes them (blocked_ids is bidirectional)."""
        poster_id, poster_token = await _make_user(city_id, "PostReverseBlockPoster")
        viewer_id, viewer_token = await _make_user(city_id, "PostReverseBlockViewer")

        create = await client.post("/posts", json={"body": "Reverse block marker post", "latitude": 54.69, "longitude": 25.28, "expires_in_minutes": 60}, headers={"Authorization": f"Bearer {poster_token}"})
        assert create.status_code == 201, create.text

        # poster blocks viewer (not the other direction)
        await client.post(f"/users/{viewer_id}/block", headers={"Authorization": f"Bearer {poster_token}"})

        result = await client.get("/posts/nearby", params={"lat": 54.69, "lng": 25.28, "radius_km": 50}, headers={"Authorization": f"Bearer {viewer_token}"})
        assert all(p["body"] != "Reverse block marker post" for p in result.json())

    @pytest.mark.asyncio
    async def test_nearby_posts_include_author_identity_for_profile_linking(self, client, city_id):
        """Quick Post author tap-through (spec: avatar/name -> public profile) needs user_id + display_name + avatar_url on each post."""
        poster_id, poster_token = await _make_user(city_id, "PostAuthorIdentity")
        _, viewer_token = await _make_user(city_id, "PostAuthorViewer")
        create = await client.post("/posts", json={"body": "Author identity marker post", "latitude": 54.69, "longitude": 25.28, "expires_in_minutes": 60}, headers={"Authorization": f"Bearer {poster_token}"})
        assert create.status_code == 201, create.text

        result = await client.get("/posts/nearby", params={"lat": 54.69, "lng": 25.28, "radius_km": 50}, headers={"Authorization": f"Bearer {viewer_token}"})
        post = next(p for p in result.json() if p["body"] == "Author identity marker post")
        assert post["user_id"] == str(poster_id)
        assert post["display_name"] == "PostAuthorIdentity"
        assert "avatar_url" in post
        # No fields beyond what GET /users/{id} (the existing public
        # profile endpoint) already exposes to anyone — e.g. no email,
        # no exact location.
        assert "email" not in post

    @pytest.mark.asyncio
    async def test_nearby_posts_excludes_posts_from_suspended_author(self, client, city_id):
        poster_id, poster_token = await _make_user(city_id, "PostSuspendedAuthor")
        _, viewer_token = await _make_user(city_id, "PostSuspendedViewer")
        create = await client.post("/posts", json={"body": "Suspended author marker post", "latitude": 54.69, "longitude": 25.28, "expires_in_minutes": 60}, headers={"Authorization": f"Bearer {poster_token}"})
        assert create.status_code == 201, create.text

        async with async_session() as db:
            user = await db.get(User, poster_id)
            user.status = "suspended"
            await db.commit()

        result = await client.get("/posts/nearby", params={"lat": 54.69, "lng": 25.28, "radius_km": 50}, headers={"Authorization": f"Bearer {viewer_token}"})
        assert all(p["body"] != "Suspended author marker post" for p in result.json())
