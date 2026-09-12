"""
Curated interest catalog. Storage was already correct — UserInterest
(app/models.py) is already a normalized user_id<->interest table, used
by discovery.py's shared-interest matching and profile stats. What was
missing was a bounded, curated vocabulary: without one, "interests" is
free text, which is useless for matching (typos, synonyms, duplicates)
and impossible to keep clean for Discover. This is that vocabulary.

Grouped for the frontend's picker UI; the backend only cares about the
flat set (INTERESTS) for validation — any interest a client submits
that isn't in this set is rejected (see users.py's update_my_profile).
"""

INTEREST_GROUPS: dict[str, list[str]] = {
    "Sports": [
        "Basketball", "Football", "Tennis", "Volleyball", "Running",
        "Gym", "Cycling", "Hiking", "Swimming", "Yoga", "Martial Arts", "Skiing",
    ],
    "Music": [
        "Pop", "Hip-hop", "Rap", "Rock", "Electronic", "Jazz",
        "Classical", "K-pop", "Indie", "Metal", "R&B", "Country",
    ],
    "Social": [
        "Parties", "Clubs", "Bars", "Coffee", "Restaurants", "Movies",
        "Concerts", "Festivals", "Board Games", "Karaoke",
    ],
    "Study": [
        "Programming", "Business", "Finance", "Marketing", "Languages",
        "Mathematics", "Entrepreneurship", "AI", "Design", "Science",
    ],
    "Hobbies": [
        "Gaming", "Photography", "Drawing", "Cooking", "Fashion",
        "Travel", "Reading", "Cars", "Writing", "Volunteering",
        "Gardening", "DIY & Crafts",
    ],
    "Outdoors": [
        "Camping", "Beach", "Fishing", "Skateboarding", "Surfing", "Climbing",
    ],
}

INTERESTS: frozenset[str] = frozenset(
    interest for group in INTEREST_GROUPS.values() for interest in group
)
