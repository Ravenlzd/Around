"""
Trust & reputation (spec §12–13).

Deliberately lightweight and social, not a star-rating system: after an
event finishes, eligible attendees (people whose event_participants row
had status='going', i.e. actually authorized to be there) can leave a
handful of positive tags — reliable_host, matched_description,
well_organized, would_join_again. There's no negative rating surface in
the MVP; absence of positive feedback is itself signal enough for now.

Trust state is DERIVED from UserStats at read time, never stored as a
mutable flag — a client can't set itself to "Trusted Host ✓" by writing
a field. The thresholds below are intentionally conservative: a single
hosted event should never earn the checkmark (spec explicitly calls
this out).
"""
from dataclasses import dataclass

from app.models import UserStats

ESTABLISHED_THRESHOLD = 3   # completed hosted events
TRUSTED_THRESHOLD = 8       # completed hosted events
TRUSTED_MIN_REPUTATION = 0.85  # positive_feedback / feedback_opportunities


@dataclass
class TrustState:
    key: str      # "new" | "established" | "trusted"
    label: str
    checkmark: bool


def derive_trust_state(stats: UserStats | None) -> TrustState:
    if not stats or stats.events_completed < ESTABLISHED_THRESHOLD:
        return TrustState("new", "New host", False)
    if stats.events_completed >= TRUSTED_THRESHOLD and float(stats.reputation_score) >= TRUSTED_MIN_REPUTATION:
        return TrustState("trusted", "Trusted Host", True)
    return TrustState("established", "Established host", False)


def record_event_completed(stats: UserStats) -> None:
    """Call when a hosted event transitions to status='completed' with >=1 attendee."""
    stats.events_completed += 1


def record_feedback(stats: UserStats, tag_count: int, feedback_opportunities: int) -> None:
    """
    Call after aggregating event_ratings for a completed event.
    `feedback_opportunities` = number of eligible attendees who could have
    left feedback; reputation is positive tags received / opportunities
    given, averaged over the host's history (a real implementation would
    store running sums rather than recompute from scratch each time).
    """
    stats.positive_feedback += tag_count
    if feedback_opportunities > 0:
        # simple exponential moving average so one bad event doesn't
        # permanently sink a long history, and one good one doesn't
        # instantly earn "Trusted"
        new_sample = tag_count / feedback_opportunities
        alpha = 0.3
        stats.reputation_score = (1 - alpha) * float(stats.reputation_score) + alpha * new_sample
