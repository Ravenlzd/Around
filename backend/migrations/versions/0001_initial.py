"""initial schema — mirrors schema.sql

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-20

This migration applies the same DDL as schema.sql by executing that
file directly. Rationale: the schema uses PostGIS geography columns,
generated tsvector columns, and partial unique indexes that are more
reliably expressed as hand-written SQL (which is already carefully
commented in schema.sql) than re-derived through SQLAlchemy's op.*
builders. Keeping ONE authored copy of the DDL (schema.sql) avoids the
two ever drifting apart. Future schema changes should still be added as
new Alembic revisions (using op.execute with plain SQL, or op.* helpers
— either is fine) rather than by editing this file or schema.sql alone.

REAL BUG FOUND running `alembic upgrade head` against a genuinely fresh
database (previous sessions only ever applied schema.sql via `psql -f`,
which uses libpq's simple-query protocol and happily runs a
multi-statement script — nobody had actually exercised this code path
through Alembic before): passing the WHOLE file as one `op.execute()`
call sends it to asyncpg as a single prepared statement, and asyncpg's
extended-query protocol does not support multiple SQL statements in one
prepare/execute — it raises `PostgresSyntaxError: cannot insert
multiple commands into a prepared statement`. `op.execute()` on a
sync-style Alembic migration still goes through the async engine
configured in env.py, so it hits this the same way the app itself would
if it ever tried to send a semicolon-joined batch over asyncpg.
Fixed by splitting schema.sql into individual statements and executing
each one separately. schema.sql itself is unchanged — this is purely
how the migration replays it.
"""
import os
import re

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "schema.sql")


def _split_sql_statements(sql_text: str) -> list[str]:
    """
    schema.sql is plain DDL — no functions/triggers/dollar-quoted
    bodies, no semicolons inside string literals or comments (verified
    by inspection, not assumed) — so a straightforward "strip line
    comments, then split on ';'" is safe here. This is deliberately NOT
    a general-purpose SQL parser; if a future schema change introduces
    a PL/pgSQL function body with embedded semicolons, this will need
    to special-case $$-quoted blocks.
    """
    no_comments = re.sub(r"--[^\n]*", "", sql_text)
    statements = [s.strip() for s in no_comments.split(";")]
    return [s for s in statements if s]


def upgrade() -> None:
    with open(_SCHEMA_PATH) as f:
        schema_sql = f.read()
    for statement in _split_sql_statements(schema_sql):
        op.execute(statement)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS
            event_ratings, event_bans, event_check_ins, event_waitlist,
            event_join_requests, guest_invitations, event_messages,
            event_participants, event_tags, events, businesses,
            community_members, communities, user_stats, notifications,
            blocks, reports, direct_messages, friendships, user_interests,
            users, cities
        CASCADE;
    """)
