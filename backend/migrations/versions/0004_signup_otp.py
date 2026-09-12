"""OTP-based signup verification, replacing link-based email verification

Revision ID: 0004_signup_otp
Revises: 0003_email_verification_chat
Create Date: 2026-09-13

Adds pending_signups (see app/models.py's PendingSignup for the full
rationale) and drops email_verification_tokens, which this replaces
entirely — the previous pass's link-based /auth/verify-email and
/auth/resend-verification are gone, superseded by
/auth/verify-signup-otp and /auth/resend-signup-otp. One verification
mechanism, not two.

users.email_verified is untouched: every row created through the new
flow already has it True at creation (verification happens before the
User row exists at all), and rows from before this migration keep
whatever migrations/0003 already backfilled.

IF NOT EXISTS / IF EXISTS guards for the same reason migrations/0003
needed them: migrations/0001_initial.py re-reads schema.sql at
migration run time, and schema.sql was updated alongside this
migration (pending_signups added, email_verification_tokens removed).
A truly fresh database (CI) gets pending_signups from 0001 already and
never had email_verification_tokens in the first place; an
already-migrated database (production) has the reverse. These guards
make the migration correct in both cases.
"""
from alembic import op

revision = "0004_signup_otp"
down_revision = "0003_email_verification_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS pending_signups (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email           TEXT NOT NULL UNIQUE,
            password_hash   TEXT NOT NULL,
            display_name    TEXT NOT NULL,
            city_id         UUID REFERENCES cities(id) NOT NULL,
            otp_hash        TEXT NOT NULL,
            otp_expires_at  TIMESTAMPTZ NOT NULL,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            last_sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("DROP TABLE IF EXISTS email_verification_tokens")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pending_signups")
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
