"""
The write side of notifications. Previously every attendance-changing
endpoint in events.py had a `# NOTE: notify X` comment and nothing else
— the notifications table only ever got read from, never written to.
This module is the fix: one small function, called from the exact
points those comments marked, so the Activity screen actually reflects
real events instead of staying permanently empty for a real user.

Deliberately NOT a queue/task system — for MVP scale, writing a row
inline in the same request that caused the notification is simple,
reliable, and fast enough (a single INSERT). If notification volume or
fan-out ever becomes a bottleneck, this is the seam to introduce a
background queue at — the call sites don't need to change, just this
function's internals.
"""
from app.models import Notification


async def notify(db, user_id, type_: str, payload: dict):
    """
    Writes one notification row. Does NOT commit — callers already have
    an open transaction from the action that triggered this (join,
    approve, etc.) and should commit once, after this call, so the
    notification and the state change it describes land atomically.
    """
    if not user_id:
        return
    db.add(Notification(user_id=user_id, type=type_, payload=payload))
