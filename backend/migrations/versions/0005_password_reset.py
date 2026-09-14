"""password reset (OTP) + session invalidation on password change

Revision ID: 0005_password_reset
Revises: 0004_signup_otp
Create Date: 2026-09-14

Adds password_resets (see app/models.py's PasswordReset for the full
rationale — a dedicated table, not reused from pending_signups, since
the two flows have opposite existence preconditions) and
users.password_changed_at, which every freshly-issued JWT now embeds
and every request re-checks (app/deps.py's get_current_user) so a
password reset actually invalidates tokens issued before it, not just
ones a client chooses to discard.

Backfill: existing users get password_changed_at = created_at, a
reasonable "as far as we know, unchanged since account creation" value
that does NOT invalidate any already-issued token (those tokens predate
this migration entirely and carry no "pwt" claim at all — get_current_user
only enforces the check when the claim is present, so pre-migration
sessions keep working until they naturally expire).

IF NOT EXISTS guards for the same reason migrations/0003 and 0004
needed them: migrations/0001_initial.py re-reads schema.sql at
migration run time, and schema.sql was updated alongside this migration
— a truly fresh database gets both from 0001 already; an
already-migrated database (production) does not, so this migration is
what actually adds them there.
"""
from alembic import op

revision = "0005_password_reset"
down_revision = "0004_signup_otp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Added nullable-with-no-default FIRST, deliberately — adding it as
    # `NOT NULL DEFAULT now()` in one step was tried and is a real bug:
    # now() is a VOLATILE default, so Postgres backfills every existing
    # row to the migration's run time before the next statement ever
    # runs, which makes a follow-up `WHERE password_changed_at IS NULL`
    # match zero rows — the intended "existing users get
    # password_changed_at = created_at" backfill silently never happens
    # (harmless in practice, since pre-migration tokens carry no "pwt"
    # claim and skip the check entirely either way — see
    # app/deps.py::get_current_user — but the column's actual value
    # would have quietly been wrong). This three-step order (add
    # nullable -> backfill -> attach default + NOT NULL) is what
    # actually gets every existing row set to its own created_at.
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ")
    op.execute("UPDATE users SET password_changed_at = created_at WHERE password_changed_at IS NULL")
    op.execute("ALTER TABLE users ALTER COLUMN password_changed_at SET DEFAULT now()")
    op.execute("ALTER TABLE users ALTER COLUMN password_changed_at SET NOT NULL")

    op.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            otp_hash        TEXT NOT NULL,
            otp_expires_at  TIMESTAMPTZ NOT NULL,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            last_sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS password_resets")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS password_changed_at")
