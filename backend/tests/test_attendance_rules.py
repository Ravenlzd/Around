"""
Unit tests for app/attendance_rules.py.

These use only the Python standard library (unittest) deliberately —
the project's other dependencies (FastAPI, SQLAlchemy, asyncpg) aren't
needed to verify the business rules themselves, only to verify the
concurrency guarantee (the row lock), which these tests don't cover —
see test_api_integration.py and README "Testing" for that half.

Run with:
    python3 -m unittest backend/tests/test_attendance_rules.py -v
"""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.attendance_rules import (
    decide_join, decide_join_with_code, decide_approve_request,
    decide_guest_invite, decide_waitlist_join, decide_waitlist_claim,
    next_waitlist_candidate, decide_remove_attendee, decide_check_in,
    decide_update_event, decide_cancel_event,
)


class TestCapacity(unittest.TestCase):
    def test_full_event_rejects_join(self):
        d = decide_join(access_mode="public", capacity=10, occupancy=10, is_banned=False, already_attending=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "event_full")

    def test_almost_full_event_allows_join(self):
        d = decide_join(access_mode="public", capacity=10, occupancy=9, is_banned=False, already_attending=False)
        self.assertTrue(d.allowed)

    def test_simultaneous_final_spot_only_one_should_be_allowed_at_a_time(self):
        # This models what the router does one request at a time under the
        # row lock: the SECOND call is evaluated against the occupancy
        # produced by the FIRST call's effect, not the stale pre-join value.
        capacity = 10
        occupancy = 9
        first = decide_join(access_mode="public", capacity=capacity, occupancy=occupancy, is_banned=False, already_attending=False)
        self.assertTrue(first.allowed)
        occupancy += 1  # the row-locked transaction would commit this before the next reader proceeds
        second = decide_join(access_mode="public", capacity=capacity, occupancy=occupancy, is_banned=False, already_attending=False)
        self.assertFalse(second.allowed)
        self.assertEqual(second.reason, "event_full")

    def test_already_attending_is_rejected_even_with_room(self):
        d = decide_join(access_mode="public", capacity=10, occupancy=3, is_banned=False, already_attending=True)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "already_attending")

    def test_banned_user_cannot_join_even_with_room(self):
        d = decide_join(access_mode="public", capacity=10, occupancy=0, is_banned=True, already_attending=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "banned")


class TestAccessModes(unittest.TestCase):
    def test_approval_mode_rejects_direct_join(self):
        d = decide_join(access_mode="approval", capacity=10, occupancy=0, is_banned=False, already_attending=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "requires_approval")

    def test_invite_only_rejects_direct_join(self):
        d = decide_join(access_mode="invite_only", capacity=10, occupancy=0, is_banned=False, already_attending=False)
        self.assertEqual(d.reason, "requires_invite")

    def test_private_rejects_join(self):
        d = decide_join(access_mode="private", capacity=10, occupancy=0, is_banned=False, already_attending=False)
        self.assertEqual(d.reason, "not_joinable")

    def test_friends_only_rejects_non_friend(self):
        d = decide_join(access_mode="friends", capacity=10, occupancy=0, is_banned=False, already_attending=False, is_friend_of_host=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "friends_only")

    def test_friends_only_allows_friend(self):
        d = decide_join(access_mode="friends", capacity=10, occupancy=0, is_banned=False, already_attending=False, is_friend_of_host=True)
        self.assertTrue(d.allowed)

    def test_invite_code_must_match(self):
        d = decide_join_with_code(access_mode="invite_only", capacity=10, occupancy=0, is_banned=False,
                                   already_attending=False, code="wrong", expected_code="TECHNO-VIP")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "invalid_code")

    def test_invite_code_case_insensitive_match_succeeds(self):
        d = decide_join_with_code(access_mode="invite_only", capacity=10, occupancy=0, is_banned=False,
                                   already_attending=False, code="techno-vip", expected_code="TECHNO-VIP")
        self.assertTrue(d.allowed)

    def test_invite_code_rejected_when_event_full(self):
        d = decide_join_with_code(access_mode="invite_only", capacity=5, occupancy=5, is_banned=False,
                                   already_attending=False, code="X", expected_code="X")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "event_full")


class TestApproval(unittest.TestCase):
    def test_non_host_cannot_approve(self):
        d = decide_approve_request(capacity=10, occupancy=5, is_host=False, request_status="pending")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "forbidden")

    def test_host_can_approve_pending_request_with_room(self):
        d = decide_approve_request(capacity=10, occupancy=5, is_host=True, request_status="pending")
        self.assertTrue(d.allowed)

    def test_cannot_approve_into_a_full_event(self):
        d = decide_approve_request(capacity=10, occupancy=10, is_host=True, request_status="pending")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "event_full")

    def test_cannot_approve_already_decided_request(self):
        d = decide_approve_request(capacity=10, occupancy=5, is_host=True, request_status="approved")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "already_decided")


class TestGuests(unittest.TestCase):
    def test_guest_allowed_when_policy_permits_and_room_exists(self):
        d = decide_guest_invite(inviter_is_participant_or_host=True, guest_policy="one",
                                 existing_guest_count_for_inviter=0, capacity=10, occupancy=5)
        self.assertTrue(d.allowed)

    def test_guest_not_allowed_when_policy_is_none(self):
        d = decide_guest_invite(inviter_is_participant_or_host=True, guest_policy="none",
                                 existing_guest_count_for_inviter=0, capacity=10, occupancy=5)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "guests_not_allowed")

    def test_guest_exceeding_capacity_is_rejected(self):
        d = decide_guest_invite(inviter_is_participant_or_host=True, guest_policy="two",
                                 existing_guest_count_for_inviter=0, capacity=10, occupancy=10)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "event_full")

    def test_guest_exceeding_per_participant_limit_is_rejected(self):
        d = decide_guest_invite(inviter_is_participant_or_host=True, guest_policy="one",
                                 existing_guest_count_for_inviter=1, capacity=10, occupancy=5)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "guest_limit_reached")

    def test_a_guest_cannot_invite_another_guest(self):
        # The caller here has type='guest', so inviter_is_participant_or_host is False —
        # this is what stops the Ravan -> Alex -> Maria cascade (spec §3).
        d = decide_guest_invite(inviter_is_participant_or_host=False, guest_policy="two",
                                 existing_guest_count_for_inviter=0, capacity=10, occupancy=5)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "forbidden")


class TestWaitlist(unittest.TestCase):
    def test_join_waitlist_when_not_already_on_it(self):
        d = decide_waitlist_join(is_banned=False, already_on_waitlist=False, already_attending=False)
        self.assertTrue(d.allowed)

    def test_cannot_join_waitlist_twice(self):
        d = decide_waitlist_join(is_banned=False, already_on_waitlist=True, already_attending=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "already_waitlisted")

    def test_cannot_join_waitlist_if_already_attending(self):
        d = decide_waitlist_join(is_banned=False, already_on_waitlist=False, already_attending=True)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "already_attending")

    def test_claim_succeeds_when_offered_and_room_exists(self):
        d = decide_waitlist_claim(entry_status="offered", offer_expired=False, capacity=10, occupancy=9)
        self.assertTrue(d.allowed)

    def test_claim_fails_when_offer_expired(self):
        d = decide_waitlist_claim(entry_status="offered", offer_expired=True, capacity=10, occupancy=9)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "offer_expired")

    def test_claim_fails_when_not_offered(self):
        d = decide_waitlist_claim(entry_status="waiting", offer_expired=False, capacity=10, occupancy=9)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "no_active_offer")

    def test_first_waiting_entry_receives_the_offer(self):
        entries = [
            {"user_id": "a", "status": "waiting", "created_at": 1},
            {"user_id": "b", "status": "waiting", "created_at": 2},
        ]
        candidate = next_waitlist_candidate(entries, capacity=10, occupancy=9)
        self.assertEqual(candidate["user_id"], "a")

    def test_no_candidate_when_event_still_full(self):
        entries = [{"user_id": "a", "status": "waiting", "created_at": 1}]
        candidate = next_waitlist_candidate(entries, capacity=10, occupancy=10)
        self.assertIsNone(candidate)

    def test_no_new_offer_while_one_is_already_pending(self):
        entries = [
            {"user_id": "a", "status": "offered", "created_at": 1},
            {"user_id": "b", "status": "waiting", "created_at": 2},
        ]
        candidate = next_waitlist_candidate(entries, capacity=10, occupancy=9)
        self.assertIsNone(candidate)


class TestHostManagement(unittest.TestCase):
    def test_non_host_cannot_remove_attendee(self):
        d = decide_remove_attendee(is_host=False, target_type="participant")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "forbidden")

    def test_host_cannot_remove_self_as_host(self):
        d = decide_remove_attendee(is_host=True, target_type="host")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "cannot_remove_host")

    def test_host_can_remove_a_participant(self):
        d = decide_remove_attendee(is_host=True, target_type="participant")
        self.assertTrue(d.allowed)

    def test_check_in_requires_going_status(self):
        d = decide_check_in(participant_status="left", participant_exists=True)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "not_going")

    def test_check_in_succeeds_for_going_attendee(self):
        d = decide_check_in(participant_status="going", participant_exists=True)
        self.assertTrue(d.allowed)


class TestEventUpdateAndCancel(unittest.TestCase):
    def test_non_host_cannot_edit(self):
        d = decide_update_event(is_host=False, event_status="active", new_capacity=None, current_occupancy=3)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "forbidden")

    def test_host_can_edit_active_event(self):
        d = decide_update_event(is_host=True, event_status="active", new_capacity=None, current_occupancy=3)
        self.assertTrue(d.allowed)

    def test_cannot_edit_cancelled_event(self):
        d = decide_update_event(is_host=True, event_status="cancelled", new_capacity=None, current_occupancy=3)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "not_active")

    def test_capacity_cannot_drop_below_current_occupancy(self):
        d = decide_update_event(is_host=True, event_status="active", new_capacity=5, current_occupancy=8)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "capacity_below_occupancy")

    def test_capacity_reduction_to_exactly_occupancy_is_allowed(self):
        d = decide_update_event(is_host=True, event_status="active", new_capacity=8, current_occupancy=8)
        self.assertTrue(d.allowed)

    def test_capacity_increase_always_allowed(self):
        d = decide_update_event(is_host=True, event_status="active", new_capacity=50, current_occupancy=8)
        self.assertTrue(d.allowed)

    def test_non_host_cannot_cancel(self):
        d = decide_cancel_event(is_host=False, event_status="active")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "forbidden")

    def test_host_can_cancel_active_event(self):
        d = decide_cancel_event(is_host=True, event_status="active")
        self.assertTrue(d.allowed)

    def test_cannot_cancel_already_cancelled_event(self):
        d = decide_cancel_event(is_host=True, event_status="cancelled")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "already_cancelled")

    def test_cannot_cancel_completed_event(self):
        d = decide_cancel_event(is_host=True, event_status="completed")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "not_active")


if __name__ == "__main__":
    unittest.main()
