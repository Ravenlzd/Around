"""
Feed / map ranking.

This is intentionally a transparent, hand-tuned heuristic rather than a
learned model — it's the MVP version referenced in the product spec
("explain the ranking concept in code so it can later be replaced by a
proper recommendation system").

Score is a weighted sum of normalized signals in [0, 1]. To swap in a
real recommender later: keep `rank_events()`'s signature (list of
EventCandidate -> sorted list), and replace `score()` with a model call
that consumes the same signal dict (`build_signals`). Nothing in the
API layer needs to change.
"""
from dataclasses import dataclass
from datetime import datetime
from math import exp


@dataclass
class EventCandidate:
    event_id: str
    distance_km: float
    minutes_until_start: float
    category: str
    spots_total: int
    spots_filled: int
    friends_attending: int
    community_match: bool


WEIGHTS = {
    "proximity": 0.28,
    "urgency": 0.22,
    "interest_match": 0.20,
    "friends": 0.15,
    "popularity": 0.10,
    "availability": 0.05,
}


def build_signals(candidate: EventCandidate, user_interests: set[str]) -> dict:
    # Proximity: exponential decay, ~half weight lost every 2.5km
    proximity = exp(-candidate.distance_km / 2.5)

    # Urgency: things starting soon score higher; events far in the future
    # (or already started/ended, negative) decay toward zero.
    if candidate.minutes_until_start < 0:
        urgency = 0.0
    else:
        urgency = exp(-candidate.minutes_until_start / 180)  # ~3hr half-life

    interest_match = 1.0 if candidate.category in user_interests else (
        0.5 if candidate.community_match else 0.0
    )

    friends = min(1.0, candidate.friends_attending / 5)

    fill_ratio = candidate.spots_filled / max(1, candidate.spots_total)
    # Popularity peaks at high-but-not-full fill ratio (social proof),
    # dips right at 100% since it signals "too late to join."
    popularity = fill_ratio if fill_ratio < 1 else 0.6

    spots_left = candidate.spots_total - candidate.spots_filled
    availability = 1.0 if 0 < spots_left <= 3 else (0.6 if spots_left > 3 else 0.0)

    return {
        "proximity": proximity,
        "urgency": urgency,
        "interest_match": interest_match,
        "friends": friends,
        "popularity": popularity,
        "availability": availability,
    }


def score(candidate: EventCandidate, user_interests: set[str]) -> float:
    signals = build_signals(candidate, user_interests)
    return sum(WEIGHTS[k] * v for k, v in signals.items())


def rank_events(candidates: list[EventCandidate], user_interests: set[str]) -> list[str]:
    """Returns event_ids sorted best-first."""
    scored = [(c.event_id, score(c, user_interests)) for c in candidates]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [event_id for event_id, _ in scored]
