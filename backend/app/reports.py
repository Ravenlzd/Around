"""
Shared report-creation logic. Previously duplicated ad hoc between
app/routers/events.py's report_event() and app/routers/users.py's
report_problem() — neither had any duplicate/spam control, and there
was no report-a-user endpoint at all. One function now, reused by
every report-creation call site (report_user, report_event, and
report_problem's "app" sentinel target), so dedupe/rate-limit/storage
behavior can't drift between them.

No schema change needed — app.models.Report already carries everything
a reviewer needs (reporter_id, target_type, target_id, reason, details,
status, created_at); this module only adds the write-path logic.

=== On review/admin access (checked before writing this) ===
There is NO platform admin/superuser concept anywhere in this codebase
today. The only "role" column that exists at all is
community_members.role ('member'|'moderator'|'admin'), and it's scoped
to a single not-yet-built community — it has no bearing on the
platform-wide question "can this user review reports." Per instruction,
this pass deliberately does NOT invent a role system to answer that.
Reports are written cleanly (this module) and read by NOBODY yet — no
endpoint anywhere returns a Report row to any caller, ordinary or
otherwise. That is intentional: better an inert-but-safe queue than a
review endpoint bolted onto a nonexistent permission model.

The smallest safe next step, when you're ready to build real review:
  1. Add `users.is_admin BOOLEAN NOT NULL DEFAULT false` (a migration +
     a matching schema.sql line — one flag is enough at this scale; a
     full role enum is unnecessary until there's more than one tier of
     staff access).
  2. Add `require_admin` to app/deps.py, the same shape as
     `require_verified_user` — depends on get_current_user, raises 403
     if not user.is_admin.
  3. Add app/routers/admin.py with GET /admin/reports (list, filterable
     by status/target_type, paginated — same shape discovery.py's
     pagination already uses) and PATCH /admin/reports/{id} (status
     transitions: open -> reviewing -> resolved/dismissed) — both
     gated on require_admin.
  4. Grant the flag by hand (`UPDATE users SET is_admin = true WHERE
     email = ...`) for the first admin; a self-service "become admin"
     path should never exist.
That's it — no separate admin auth system, no new token type, reuses
the exact same JWT/get_current_user path every other endpoint already
goes through, just with one extra boolean check.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Report

# A reporter re-submitting against the same target while an earlier
# report of theirs is still unresolved doesn't create a second row —
# it's the same complaint, not a new one, and this is what stops
# spam-clicking "Report" from flooding the queue with duplicates a
# reviewer would just have to dedupe by hand anyway. A NEW report from
# the same reporter against the same target IS allowed once the
# previous one has been reviewed (status moved to resolved/dismissed —
# see app/routers/admin.py) — that's a new incident, not a duplicate.
OPEN_STATUSES = ("open", "reviewing")


async def create_report(
    db: AsyncSession, *, reporter_id: uuid.UUID, target_type: str, target_id: uuid.UUID,
    reason: str, details: str | None = None,
) -> str:
    """
    Returns "reported" (a new row was written) or "already_reported"
    (deduped against an existing open report — not an error; the
    caller should treat both as success from the reporter's point of
    view). Callers are responsible for validating the target actually
    exists and is of the claimed type BEFORE calling this — this
    function trusts target_type/target_id are already correct, which is
    what makes "can the reporter manipulate target_id to report an
    unrelated/nonexistent object" a question answered at each call site
    (see report_user/report_event), not here.
    """
    existing = await db.scalar(
        select(Report).where(
            Report.reporter_id == reporter_id,
            Report.target_type == target_type,
            Report.target_id == target_id,
            Report.status.in_(OPEN_STATUSES),
        )
    )
    if existing:
        return "already_reported"

    db.add(Report(reporter_id=reporter_id, target_type=target_type, target_id=target_id, reason=reason, details=details))
    await db.commit()
    return "reported"
