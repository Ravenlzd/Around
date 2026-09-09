"""
Pure attendance decision functions — no DB session, no I/O.

Everything here answers "is this action allowed, given this state?" as a
plain function of plain inputs. The router (app/routers/events.py) is
responsible for gathering that state under the row lock described in
README "Capacity & concurrency strategy", calling these functions, and
then writing the result. Keeping the decision logic pure means it can be
unit-tested directly (see backend/tests/test_attendance_rules.py) without
spinning up Postgres/PostGIS — the actual concurrency guarantee still
depends on the router's row lock, but the business rules themselves
(capacity math, guest limits, cascade prevention, banned users) are
verified here in isolation.
"""
from dataclasses import dataclass

GUEST_POLICY_MAX = {"none": 0, "one": 1, "two": 2, "host_approval": 2}


@dataclass
class Decision:
    allowed: bool
    reason: str = ""          # short machine-readable code, e.g. "event_full"
    message: str = ""         # human-readable, safe to show in the UI


def decide_join(
    *,
    access_mode: str,
    capacity: int,
    occupancy: int,
    is_banned: bool,
    already_attending: bool,
    is_friend_of_host: bool = False,
) -> Decision:
    if is_banned:
        return Decision(False, "banned", "You've been removed from this event by the host")
    if already_attending:
        return Decision(False, "already_attending", "You're already going")
    if access_mode == "approval":
        return Decision(False, "requires_approval", "This event requires approval — send a join request instead")
    if access_mode == "invite_only":
        return Decision(False, "requires_invite", "This event requires an invite code")
    if access_mode == "private":
        return Decision(False, "not_joinable", "This event is not publicly joinable")
    if access_mode == "friends" and not is_friend_of_host:
        return Decision(False, "friends_only", "Only the host's friends can join this event")
    if occupancy >= capacity:
        return Decision(False, "event_full", "Event is full — join the waitlist instead")
    return Decision(True, "joined", "Joined")


def decide_join_with_code(
    *, access_mode: str, capacity: int, occupancy: int, is_banned: bool,
    already_attending: bool, code: str, expected_code: str | None,
) -> Decision:
    if access_mode != "invite_only":
        return Decision(False, "wrong_mode", "This event doesn't use invite codes")
    if is_banned:
        return Decision(False, "banned", "You've been removed from this event by the host")
    if already_attending:
        return Decision(False, "already_attending", "You're already going")
    if not expected_code or code.strip().upper() != expected_code.upper():
        return Decision(False, "invalid_code", "Invalid invite code")
    if occupancy >= capacity:
        return Decision(False, "event_full", "Event is full — join the waitlist instead")
    return Decision(True, "joined", "Joined")


def decide_approve_request(*, capacity: int, occupancy: int, is_host: bool, request_status: str) -> Decision:
    if not is_host:
        return Decision(False, "forbidden", "Only the host can approve requests")
    if request_status != "pending":
        return Decision(False, "already_decided", "This request was already decided")
    if occupancy >= capacity:
        return Decision(False, "event_full", "Can't approve — event is now full")
    return Decision(True, "approved", "Approved")


def decide_guest_invite(
    *,
    inviter_is_participant_or_host: bool,
    guest_policy: str,
    existing_guest_count_for_inviter: int,
    capacity: int,
    occupancy: int,
) -> Decision:
    if not inviter_is_participant_or_host:
        # Guest permissions never cascade: a guest's own event_participants
        # row has type='guest', so they never satisfy this check and can
        # never invite a second-order guest.
        return Decision(False, "forbidden", "Only confirmed participants can invite a guest")
    max_guests = GUEST_POLICY_MAX.get(guest_policy, 0)
    if max_guests == 0:
        return Decision(False, "guests_not_allowed", "This event doesn't allow guests")
    if existing_guest_count_for_inviter >= max_guests:
        return Decision(False, "guest_limit_reached", "You've reached your guest limit for this event")
    if occupancy >= capacity:
        return Decision(False, "event_full", "No spots left for a guest")
    return Decision(True, "invited", "Guest invited")


def decide_waitlist_join(*, is_banned: bool, already_on_waitlist: bool, already_attending: bool) -> Decision:
    if is_banned:
        return Decision(False, "banned", "You've been removed from this event by the host")
    if already_attending:
        return Decision(False, "already_attending", "You're already going")
    if already_on_waitlist:
        return Decision(False, "already_waitlisted", "You're already on the waitlist")
    return Decision(True, "waitlisted", "Added to the waitlist")


def decide_waitlist_claim(*, entry_status: str, offer_expired: bool, capacity: int, occupancy: int) -> Decision:
    if entry_status != "offered":
        return Decision(False, "no_active_offer", "No active offer for you on this event")
    if offer_expired:
        return Decision(False, "offer_expired", "Your claim window expired")
    if occupancy >= capacity:
        return Decision(False, "event_full", "Spot no longer available")
    return Decision(True, "claimed", "Spot claimed")


def next_waitlist_candidate(waitlist_entries: list[dict], capacity: int, occupancy: int) -> dict | None:
    """
    `waitlist_entries`: list of {"user_id":..., "status":..., "created_at":...}
    ordered ascending by created_at. Returns the entry that should be
    offered the next open spot, or None. Only ever surfaces one 'offered'
    candidate at a time — if one is already offered, returns None (the
    router/job should wait for it to resolve or expire first).
    """
    if occupancy >= capacity:
        return None
    if any(e["status"] == "offered" for e in waitlist_entries):
        return None
    waiting = [e for e in waitlist_entries if e["status"] == "waiting"]
    return waiting[0] if waiting else None


def decide_remove_attendee(*, is_host: bool, target_type: str) -> Decision:
    if not is_host:
        return Decision(False, "forbidden", "Only the host can remove attendees")
    if target_type == "host":
        return Decision(False, "cannot_remove_host", "Can't remove the host")
    return Decision(True, "removed", "Removed")


def decide_check_in(*, participant_status: str, participant_exists: bool) -> Decision:
    if not participant_exists:
        return Decision(False, "not_found", "Attendee not found")
    if participant_status != "going":
        return Decision(False, "not_going", "This attendee is not currently going")
    return Decision(True, "checked_in", "Checked in")


def decide_update_event(*, is_host: bool, event_status: str, new_capacity: int | None, current_occupancy: int) -> Decision:
    """
    Governs PATCH /events/{id}. The capacity-reduction rule is the one
    with real consequences: silently dropping attendees to fit a
    smaller number would violate the "going participants <= capacity"
    invariant everywhere else in this codebase, or worse, would pick
    who gets removed with no clear rule. Rejecting the edit outright
    keeps the invariant intact and puts the decision back in the
    host's hands (they can message people and ask someone to drop out).
    """
    if not is_host:
        return Decision(False, "forbidden", "Only the host can edit this event")
    if event_status != "active":
        return Decision(False, "not_active", "This event can't be edited anymore")
    if new_capacity is not None and new_capacity < current_occupancy:
        return Decision(False, "capacity_below_occupancy", "Capacity can't be reduced below the current number of attendees")
    return Decision(True, "updated", "Updated")


def decide_cancel_event(*, is_host: bool, event_status: str) -> Decision:
    if not is_host:
        return Decision(False, "forbidden", "Only the host can cancel this event")
    if event_status == "cancelled":
        return Decision(False, "already_cancelled", "This event is already cancelled")
    if event_status != "active":
        return Decision(False, "not_active", "This event can't be cancelled")
    return Decision(True, "cancelled", "Event cancelled")
