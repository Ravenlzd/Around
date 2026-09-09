"""
Location reveal (spec §11, prior-phase README "Privacy & location
behavior"). Pure function so both the lightweight discovery cards and
the full event-detail response apply the exact same rule — there's
only one place this decision gets made, and it's server-side, so an
unauthorized client never receives the exact coordinates/address in
the first place (nothing to hide via devtools on the frontend).
"""
import hashlib
import math

from app.models import Event


def reveal_location(event: Event, *, is_authorized: bool, is_approved: bool) -> str:
    """
    Returns the location string this specific requester should see.
    `is_authorized` = has a going event_participants row, or is the host.
    `is_approved` = has an approved event_join_requests row (a strict
    subset of is_authorized in practice, since approval leads to a
    participant row — kept separate for clarity at call sites).
    """
    if event.access_mode == "public" or event.location_reveal == "immediate":
        return event.location_label or event.approx_location_label or "Location available at the event page"
    if is_authorized:
        return event.location_label or event.approx_location_label
    if event.location_reveal == "after_approval" and is_approved:
        return event.location_label or event.approx_location_label
    return event.approx_location_label or "Exact location shared once you're authorized"


def jittered_point(lat: float, lng: float, seed: str, radius_meters: float = 350) -> tuple[float, float]:
    """
    Deterministically offsets a coordinate within `radius_meters` of the
    original point, seeded by `seed` (an event id) so repeated requests
    for the same event return the SAME fuzzed point — a pin that jumped
    around between page loads would be more suspicious/annoying than
    reassuring. Not cryptographically hidden (anyone motivated could
    narrow it down over many samples of *different* events), but that's
    true of every "approximate location" feature in every app — the
    goal is "don't hand out the exact address on a map," not
    "information-theoretically unlocatable."
    """
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    angle = (h % 3600) / 3600 * 2 * math.pi
    distance = radius_meters * (0.4 + 0.6 * ((h // 3600) % 1000) / 1000)  # never right on top of the real point, never at the extreme edge either
    dlat = (distance * math.cos(angle)) / 111_320  # meters per degree latitude, ~constant
    dlng = (distance * math.sin(angle)) / (111_320 * math.cos(math.radians(lat)) or 1)
    return lat + dlat, lng + dlng


def reveal_coordinates(event: Event, *, lat: float, lng: float, is_authorized: bool, is_approved: bool) -> tuple[float, float]:
    """
    The map-pin equivalent of reveal_location(): exact coordinates for
    public/immediate/authorized cases, otherwise a stable fuzzed point
    near (but not at) the real location. A visually precise pin on a
    map is at least as revealing as the exact address text, so it goes
    through the identical authorization check rather than a separate,
    looser one.
    """
    if event.access_mode == "public" or event.location_reveal == "immediate" or is_authorized:
        return lat, lng
    if event.location_reveal == "after_approval" and is_approved:
        return lat, lng
    return jittered_point(lat, lng, str(event.id))
