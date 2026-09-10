from contextlib import asynccontextmanager

import pytest
from sqlalchemy.dialects import postgresql

from app import expiry


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.statements = []

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, statement):
        self.statements.append(statement)


class FakeSessionFactory:
    def __init__(self):
        self.sessions = []

    @asynccontextmanager
    async def __call__(self):
        session = FakeSession()
        self.sessions.append(session)
        yield session


@pytest.mark.asyncio
async def test_run_sweep_completes_all_cleanups_when_they_succeed(monkeypatch):
    session_factory = FakeSessionFactory()
    calls = []

    def succeeds(name):
        async def cleanup(db):
            calls.append(name)
            await db.commit()
        return cleanup

    monkeypatch.setattr(expiry, "async_session", session_factory)
    monkeypatch.setattr(expiry, "_expire_waitlist_offers", succeeds("waitlist"))
    monkeypatch.setattr(expiry, "_expire_guest_invitations", succeeds("guests"))
    monkeypatch.setattr(expiry, "_delete_expired_ephemeral_content", succeeds("ephemeral"))

    await expiry.run_sweep()

    assert calls == ["waitlist", "guests", "ephemeral"]
    assert [session.commits for session in session_factory.sessions] == [1, 1, 1]
    assert [session.rollbacks for session in session_factory.sessions] == [0, 0, 0]


@pytest.mark.asyncio
async def test_failed_cleanup_rolls_back_without_blocking_later_cleanups(monkeypatch):
    session_factory = FakeSessionFactory()
    calls = []

    async def fails(db):
        calls.append("waitlist")
        raise RuntimeError("database statement failed")

    def succeeds(name):
        async def cleanup(db):
            calls.append(name)
            await db.commit()
        return cleanup

    monkeypatch.setattr(expiry, "async_session", session_factory)
    monkeypatch.setattr(expiry, "_expire_waitlist_offers", fails)
    monkeypatch.setattr(expiry, "_expire_guest_invitations", succeeds("guests"))
    monkeypatch.setattr(expiry, "_delete_expired_ephemeral_content", succeeds("ephemeral"))

    await expiry.run_sweep()

    assert calls == ["waitlist", "guests", "ephemeral"]
    assert session_factory.sessions[0].rollbacks == 1
    assert [session.commits for session in session_factory.sessions[1:]] == [1, 1]


@pytest.mark.asyncio
async def test_expired_ephemeral_content_deletes_posts_and_statuses():
    db = FakeSession()

    await expiry._delete_expired_ephemeral_content(db)

    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in db.statements]
    assert "DELETE FROM spontaneous_posts" in sql[0]
    assert "spontaneous_posts.expires_at <" in sql[0]
    assert "DELETE FROM im_free_status" in sql[1]
    assert db.commits == 1
