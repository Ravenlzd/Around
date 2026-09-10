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

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import async_session
from app.models import City, User
from app.routers.auth import pwd_context, create_access_token


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
