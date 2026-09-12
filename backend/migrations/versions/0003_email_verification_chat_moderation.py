"""email verification tokens

Revision ID: 0003_email_verification_chat
Revises: 0002_seed_default_city
Create Date: 2026-09-13

Adds users.email_verified (backfilled true for existing rows — see
below) and email_verification_tokens (single-use hashed tokens).

Uses IF NOT EXISTS guards rather than plain op.add_column/create_table.
Reason: migrations/0001_initial.py re-reads schema.sql at migration
RUN time (not frozen at authoring time — see its own docstring), and
schema.sql was updated alongside this migration to include both of
these for a fresh local database. That means a truly fresh database
(as CI's workflow creates on every run) gets email_verified and
email_verification_tokens from 0001 already, and this migration would
otherwise fail with "column/table already exists" immediately after.
An existing, already-migrated database (production) does NOT have
them from its already-applied 0001, so this migration is what actually
adds them there. IF NOT EXISTS makes this migration correct in both
cases without needing two different code paths.

Direct messaging (spec item 9/10) needed NO schema change at all: the
direct_messages table already existed in schema.sql/production from
0001_initial (sender_id/recipient_id/body/created_at/read_at, with
idx_dm_thread already built for the exact "messages between these two
users" lookup a conversation view needs) — it just never had an ORM
model or router built on top of it. Adding a second, differently-shaped
table (e.g. a conversations table) for the same concept would have been
exactly the "duplicate system" this pass was told not to create; see
app/models.py's DirectMessage and app/routers/chat.py instead.

users.email_verified backfill rationale: email verification is a
requirement added after real accounts already existed. Nothing about
an already-registered user's email became less trustworthy the moment
this migration ran, so every existing row is backfilled to true; only
accounts created after this migration start unverified. (Enforcement
of what unverified accounts can't do is separately gated off by
default — see app/config.py's REQUIRE_EMAIL_VERIFICATION — so this
backfill is about correctness of the data, not a functional necessity
today.)
"""
from alembic import op

revision = "0003_email_verification_chat"
down_revision = "0002_seed_default_city"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false")
    op.execute("UPDATE users SET email_verified = true")

    op.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID REFERENCES users(id) NOT NULL,
            token_hash  TEXT NOT NULL UNIQUE,
            expires_at  TIMESTAMPTZ NOT NULL,
            used_at     TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_email_verification_user ON email_verification_tokens(user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS email_verification_tokens")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email_verified")
