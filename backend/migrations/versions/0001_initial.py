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
"""
import os

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "schema.sql")


def upgrade() -> None:
    with open(_SCHEMA_PATH) as f:
        op.execute(f.read())


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
