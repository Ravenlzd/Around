-- ============================================================
-- AROUND — PostgreSQL schema
-- Architecture notes:
--  - PostGIS is used for all location columns (geography type) so
--    distance queries (ST_DWithin, ST_Distance) are fast and accurate.
--  - Every user-generated, time-bound thing (spontaneous_posts,
--    im_free_status) has an `expires_at` and is cleaned up by a
--    scheduled job (see backend/README section "Expiry job").
--  - `city_id` is on every location-bearing row so multi-city
--    expansion is just: insert a new city row + seed data.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- for gen_random_uuid()

-- ---------- Cities ----------
CREATE TABLE cities (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,               -- 'Vilnius'
    country_code TEXT NOT NULL,                -- 'LT'
    center       GEOGRAPHY(POINT, 4326) NOT NULL,
    timezone     TEXT NOT NULL DEFAULT 'Europe/Vilnius',
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Users ----------
CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               TEXT UNIQUE NOT NULL,
    password_hash       TEXT,                  -- null if OAuth-only
    oauth_provider      TEXT,                   -- 'google' | 'apple' | null
    oauth_subject       TEXT,
    display_name        TEXT NOT NULL,
    date_of_birth       DATE,
    city_id             UUID REFERENCES cities(id),
    university_or_work  TEXT,
    bio                 TEXT,
    avatar_url          TEXT,
    -- privacy
    location_precision  TEXT NOT NULL DEFAULT 'approximate' CHECK (location_precision IN ('approximate','exact_to_participants')),
    hide_from_nearby    BOOLEAN NOT NULL DEFAULT false,
    restrict_messages   TEXT NOT NULL DEFAULT 'everyone' CHECK (restrict_messages IN ('everyone','friends_only')),
    -- live location (write-through, not queried directly by other users)
    last_location       GEOGRAPHY(POINT, 4326),
    last_location_at    TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','deleted')),
    -- New signups start unverified; a fresh local database has no
    -- pre-existing users to backfill, unlike migrations/0003 in
    -- production, so the plain column default is correct here.
    email_verified      BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_city ON users(city_id);

CREATE TABLE user_interests (
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    interest    TEXT NOT NULL,          -- 'music','fashion','nightlife', free-form tag
    PRIMARY KEY (user_id, interest)
);

-- ---------- Communities ----------
CREATE TABLE communities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id     UUID REFERENCES cities(id),
    name        TEXT NOT NULL,           -- 'VGTU', 'Erasmus students'
    description TEXT,
    avatar_url  TEXT,
    created_by  UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE community_members (
    community_id UUID REFERENCES communities(id) ON DELETE CASCADE,
    user_id      UUID REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member','moderator','admin')),
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, user_id)
);

-- ---------- Businesses (secondary target, lightweight in MVP) ----------
CREATE TABLE businesses (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id     UUID REFERENCES cities(id),
    owner_user_id UUID REFERENCES users(id),
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,           -- cafe, restaurant, club, gym, venue, university
    location    GEOGRAPHY(POINT, 4326) NOT NULL,
    address     TEXT,
    verified    BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Events ----------
-- access_mode replaces the old boolean `is_private` (spec: "the backend
-- should store a real access mode, not just a boolean"). guest_policy
-- caps how many guests each participant may bring. location_reveal only
-- matters when the event isn't fully public; it controls when
-- `location` (exact) is shown vs `approx_location_label` (safe default).
CREATE TABLE events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_user_id   UUID REFERENCES users(id),
    host_business_id UUID REFERENCES businesses(id),
    city_id        UUID REFERENCES cities(id) NOT NULL,
    title          TEXT NOT NULL,
    category       TEXT NOT NULL,        -- sports, party, study, food, music, gaming, culture, fitness, meetup, education, shopping, outdoor
    description    TEXT,
    cover_image_url TEXT,
    location       GEOGRAPHY(POINT, 4326) NOT NULL,          -- exact location — never sent to the client until authorized
    location_label TEXT,                                      -- "Vingis Park, Courts" (exact, human-readable)
    approx_location_label TEXT,                                -- "Near Antakalnis" — safe to show pre-authorization
    starts_at      TIMESTAMPTZ NOT NULL,
    ends_at        TIMESTAMPTZ,
    capacity       INTEGER NOT NULL CHECK (capacity > 0),      -- max TOTAL attendance: participants + guests + host
    access_mode    TEXT NOT NULL DEFAULT 'public'
                     CHECK (access_mode IN ('public','approval','friends','invite_only','private')),
    guest_policy   TEXT NOT NULL DEFAULT 'none'
                     CHECK (guest_policy IN ('none','one','two','host_approval')),
    location_reveal TEXT NOT NULL DEFAULT 'immediate'
                     CHECK (location_reveal IN ('after_approval','confirmed_attendees','immediate')),
    invite_code    TEXT,                                       -- set when access_mode = 'invite_only'
    waitlist_claim_minutes INTEGER NOT NULL DEFAULT 20,
    min_age        INTEGER,
    chat_enabled   BOOLEAN NOT NULL DEFAULT true,
    community_id   UUID REFERENCES communities(id),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled','completed')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT host_present CHECK (host_user_id IS NOT NULL OR host_business_id IS NOT NULL)
);
CREATE INDEX idx_events_location ON events USING GIST (location);
CREATE INDEX idx_events_starts_at ON events(starts_at);
CREATE INDEX idx_events_city_status ON events(city_id, status);

CREATE TABLE event_tags (
    event_id UUID REFERENCES events(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (event_id, tag)
);

-- ---------- Attendance: the core trust primitive ----------
-- Every physically-present person maps to exactly one row here, with an
-- explicit `type` and (for guests) `invited_by_user_id`. This is what
-- makes "why is this person authorized to be here?" answerable for any
-- attendee, always (spec §21). Capacity = count of rows with
-- status='going' across ALL types (host + participant + guest) — see
-- README "Capacity & concurrency strategy" for how this is enforced
-- under concurrent joins.
CREATE TABLE event_participants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      UUID REFERENCES events(id) ON DELETE CASCADE,
    user_id       UUID REFERENCES users(id) ON DELETE CASCADE,   -- NULL for a guest with no Around account yet
    guest_name    TEXT,                                          -- set when user_id is NULL (unregistered guest)
    type          TEXT NOT NULL CHECK (type IN ('host','participant','guest')),
    invited_by_user_id UUID REFERENCES users(id),                 -- required and only meaningful when type='guest'
    status        TEXT NOT NULL DEFAULT 'going'
                    CHECK (status IN ('going','left','removed','banned')),
    checked_in_at TIMESTAMPTZ,
    checked_in_by UUID REFERENCES users(id),                      -- host doing a manual check-in, if not self-scanned
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT guest_has_inviter CHECK (type != 'guest' OR invited_by_user_id IS NOT NULL),
    CONSTRAINT identifiable CHECK (user_id IS NOT NULL OR guest_name IS NOT NULL)
);
CREATE INDEX idx_event_participants_event ON event_participants(event_id) WHERE status = 'going';
CREATE UNIQUE INDEX idx_event_participants_unique_user ON event_participants(event_id, user_id) WHERE user_id IS NOT NULL AND status = 'going';
-- Capacity itself is NOT a CHECK constraint (Postgres can't cheaply CHECK
-- a sibling-row count) — it's enforced in the join transaction instead.

-- Guest invitations: the token a participant hands to their +1. Guest
-- permissions never cascade — only rows where inviter.type IN
-- ('host','participant') may create one (enforced in the API layer,
-- since a participant who is themselves a guest has no invited_by
-- chain to draw on).
CREATE TABLE guest_invitations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID REFERENCES events(id) ON DELETE CASCADE,
    invited_by_user_id UUID REFERENCES users(id) NOT NULL,
    token            TEXT UNIQUE NOT NULL,                        -- e.g. "RAVAN-7F2K"
    guest_name       TEXT,
    status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','claimed','cancelled','expired')),
    claimed_by_participant_id UUID REFERENCES event_participants(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ
);
CREATE INDEX idx_guest_invitations_event ON guest_invitations(event_id);

-- Approval-mode join requests. Pending requests do NOT consume capacity
-- (see README) — a row only becomes an event_participants row on approve.
CREATE TABLE event_join_requests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id    UUID REFERENCES events(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id) NOT NULL,
    guest_count_requested INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    decided_by  UUID REFERENCES users(id)
);
CREATE UNIQUE INDEX idx_join_requests_one_pending ON event_join_requests(event_id, user_id) WHERE status = 'pending';

-- Waitlist. `position` is derived (ORDER BY created_at) rather than
-- stored, so removals never require renumbering. `status='offered'`
-- entries carry `offer_expires_at`; the expiry job (see README) flips
-- expired offers to 'expired' and calls offer-next.
CREATE TABLE event_waitlist (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id    UUID REFERENCES events(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id) NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting'
                 CHECK (status IN ('waiting','offered','claimed','expired','cancelled')),
    offered_at  TIMESTAMPTZ,
    offer_expires_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_waitlist_one_active_entry ON event_waitlist(event_id, user_id) WHERE status IN ('waiting','offered');
CREATE INDEX idx_waitlist_order ON event_waitlist(event_id, created_at) WHERE status = 'waiting';

CREATE TABLE event_check_ins (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      UUID REFERENCES events(id) ON DELETE CASCADE,
    participant_id UUID REFERENCES event_participants(id) NOT NULL,
    checked_in_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checked_in_by UUID REFERENCES users(id),   -- self-scan (=participant's own user) or host manual override
    method        TEXT NOT NULL DEFAULT 'qr' CHECK (method IN ('qr','manual'))
);
CREATE INDEX idx_checkins_event ON event_check_ins(event_id);

-- Host bans: separate from a generic `blocks` row because a ban is
-- scoped to one event/host relationship and must survive independent of
-- whether the two users ever block each other globally.
CREATE TABLE event_bans (
    event_id   UUID REFERENCES events(id) ON DELETE CASCADE,
    user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
    banned_by  UUID REFERENCES users(id),
    reason     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, user_id)
);

-- Lightweight post-event feedback (spec §12 — "not Uber"). Tags are a
-- fixed small vocabulary, no 1-5 star rating.
CREATE TABLE event_ratings (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id   UUID REFERENCES events(id) ON DELETE CASCADE,
    rater_user_id UUID REFERENCES users(id) NOT NULL,
    host_user_id  UUID REFERENCES users(id) NOT NULL,
    tags       TEXT[] NOT NULL DEFAULT '{}',  -- subset of: reliable_host, matched_description, well_organized, would_join_again
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, rater_user_id)
);

CREATE TABLE event_messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id   UUID REFERENCES events(id) ON DELETE CASCADE,
    user_id    UUID REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_event_messages_event ON event_messages(event_id, created_at);

-- ---------- Spontaneous posts (ephemeral, lightweight) ----------
CREATE TABLE spontaneous_posts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    city_id    UUID REFERENCES cities(id),
    body       TEXT NOT NULL,
    location   GEOGRAPHY(POINT, 4326),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_spontaneous_expiry ON spontaneous_posts(expires_at);
CREATE INDEX idx_spontaneous_location ON spontaneous_posts USING GIST (location);

-- ---------- "I'm Free" status ----------
CREATE TABLE im_free_status (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID REFERENCES users(id) UNIQUE, -- one active status per user
    when_window  TEXT NOT NULL CHECK (when_window IN ('now','tonight','tomorrow','this_weekend')),
    looking_for  TEXT NOT NULL,           -- party, food, sports, study, coffee, gaming, adventure, anything
    radius_km    NUMERIC,                 -- null = anywhere nearby
    location     GEOGRAPHY(POINT, 4326),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_imfree_expiry ON im_free_status(expires_at);

-- ---------- Friends / social graph ----------
CREATE TABLE friendships (
    user_id_a UUID REFERENCES users(id) ON DELETE CASCADE,
    user_id_b UUID REFERENCES users(id) ON DELETE CASCADE,
    status    TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','blocked')),
    requested_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id_a, user_id_b),
    CONSTRAINT ordered_pair CHECK (user_id_a < user_id_b)
);

CREATE TABLE direct_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_id   UUID REFERENCES users(id),
    recipient_id UUID REFERENCES users(id),
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at     TIMESTAMPTZ
);
CREATE INDEX idx_dm_thread ON direct_messages(least(sender_id::text, recipient_id::text), greatest(sender_id::text, recipient_id::text), created_at);

-- ---------- Email verification ----------
-- Added by migrations/0003 in an already-deployed database (with
-- existing users backfilled to email_verified=true — see that
-- migration's docstring); included here too so a brand new local
-- database matches. Nothing reads/writes this table until a mail
-- provider is configured and app/config.py's REQUIRE_EMAIL_VERIFICATION
-- is turned on.
CREATE TABLE email_verification_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_email_verification_user ON email_verification_tokens(user_id);

-- ---------- Reports / moderation ----------
CREATE TABLE reports (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporter_id  UUID REFERENCES users(id),
    target_type  TEXT NOT NULL CHECK (target_type IN ('user','event','post','message')),
    target_id    UUID NOT NULL,
    reason       TEXT NOT NULL,
    details      TEXT,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','reviewing','resolved','dismissed')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE blocks (
    blocker_id UUID REFERENCES users(id) ON DELETE CASCADE,
    blocked_id UUID REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (blocker_id, blocked_id)
);

-- ---------- Notifications ----------
CREATE TABLE notifications (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,   -- spots_low, joined, starting_soon, friends_going, invited, recommendation
    payload    JSONB NOT NULL DEFAULT '{}',
    read_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications_user ON notifications(user_id, created_at DESC);

-- ---------- Gamification & trust (kept minimal / subtle per product spec) ----------
-- Trust state (new_host / established_host / trusted_host) is DERIVED,
-- not stored — see README "Trust & reputation model" for the exact
-- thresholds and why it's computed rather than a mutable flag a client
-- could spoof.
CREATE TABLE user_stats (
    user_id            UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    events_attended    INTEGER NOT NULL DEFAULT 0,
    events_hosted      INTEGER NOT NULL DEFAULT 0,
    events_completed   INTEGER NOT NULL DEFAULT 0,  -- hosted events that ran and had >=1 attendee show up
    connections_made   INTEGER NOT NULL DEFAULT 0,
    positive_feedback  INTEGER NOT NULL DEFAULT 0,   -- sum of event_ratings tags received
    reputation_score   NUMERIC NOT NULL DEFAULT 0     -- positive_feedback / (feedback_opportunities), updated by trigger/job
);

-- ---------- Full text search (Postgres FTS now, Elasticsearch-ready later) ----------
ALTER TABLE events ADD COLUMN search_vector tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(category,''))) STORED;
CREATE INDEX idx_events_search ON events USING GIN (search_vector);

-- ---------- Blocks apply to attendance too ----------
-- No new table needed — `blocks` (defined above) is checked at join time
-- (see events.py: a blocked user can't request/join/be-invited-as-guest
-- to any event where they've blocked, or been blocked by, the host).
