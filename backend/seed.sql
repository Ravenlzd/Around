-- Seed data for the Vilnius demo environment (spec §20, §28).
-- Run after schema.sql: psql $DATABASE_URL -f seed.sql
--
-- Mirrors the scenarios in frontend/index.html so the prototype and the
-- real backend tell the same story:
--   A — almost full (9/10, no guests)              -> Basketball
--   B + F — full, with a waitlist already forming    -> 5v5 Football
--   C — guests allowed, room to spare (7/10, 1 guest) -> Picnic
--   D — plain approval flow                          -> Coffee Meetup
--   E + G — private approval-required house party,    -> House Party
--           hosted by the demo user, approximate
--           location until approved

INSERT INTO cities (id, name, country_code, center, timezone)
VALUES ('11111111-1111-1111-1111-111111111111', 'Vilnius', 'LT', ST_SetSRID(ST_MakePoint(25.2797, 54.6872), 4326), 'Europe/Vilnius');

INSERT INTO users (id, email, password_hash, display_name, city_id, university_or_work)
VALUES
  ('22222222-2222-2222-2222-222222222220', 'ravan@around.city',  crypt('demo1234', gen_salt('bf')), 'Ravan',  '11111111-1111-1111-1111-111111111111', 'Vilnius Tech'), -- the demo user ("You")
  ('22222222-2222-2222-2222-222222222221', 'tomas@around.city',  crypt('demo1234', gen_salt('bf')), 'Tomas',  '11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222222', 'dovydas@around.city',crypt('demo1234', gen_salt('bf')), 'Dovydas','11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222223', 'anna@around.city',   crypt('demo1234', gen_salt('bf')), 'Anna',   '11111111-1111-1111-1111-111111111111', 'VU'),
  ('22222222-2222-2222-2222-222222222224', 'elena@around.city',  crypt('demo1234', gen_salt('bf')), 'Elena',  '11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222225', 'justina@around.city',crypt('demo1234', gen_salt('bf')), 'Justina','11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222226', 'leo@around.city',    crypt('demo1234', gen_salt('bf')), 'Leo',    '11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222227', 'max@around.city',    crypt('demo1234', gen_salt('bf')), 'Max',    '11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222228', 'aiste@around.city',  crypt('demo1234', gen_salt('bf')), 'Aiste',  '11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222229', 'karolis@around.city',crypt('demo1234', gen_salt('bf')), 'Karolis','11111111-1111-1111-1111-111111111111', NULL),
  ('22222222-2222-2222-2222-222222222230', 'justas@around.city', crypt('demo1234', gen_salt('bf')), 'Justas', '11111111-1111-1111-1111-111111111111', NULL);

INSERT INTO user_stats (user_id, events_hosted, events_completed, positive_feedback, reputation_score)
VALUES ('22222222-2222-2222-2222-222222222220', 9, 8, 15, 0.93); -- Ravan reads as "Trusted Host ✓" — see app/trust.py

INSERT INTO user_interests (user_id, interest) VALUES
  ('22222222-2222-2222-2222-222222222220','sports'), ('22222222-2222-2222-2222-222222222220','music'),
  ('22222222-2222-2222-2222-222222222220','fitness'), ('22222222-2222-2222-2222-222222222220','party');

-- ===== Scenario A — almost full, plain public event =====
INSERT INTO events (id, host_user_id, city_id, title, category, description, location, location_label, starts_at, ends_at, capacity, access_mode, guest_policy)
VALUES (
  'aaaaaaaa-0000-0000-0000-000000000001', '22222222-2222-2222-2222-222222222221', '11111111-1111-1111-1111-111111111111',
  'Basketball at Vingis Park', 'sports', 'Casual 5v5, all levels welcome.',
  ST_SetSRID(ST_MakePoint(25.2497, 54.6821), 4326), 'Vingis Park, Courts',
  now() + interval '25 minutes', now() + interval '2 hours', 10, 'public', 'none'
);
INSERT INTO event_participants (event_id, user_id, type) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222221','host'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222220','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222223','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222224','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222225','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222226','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222227','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222228','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000001','22222222-2222-2222-2222-222222222229','participant');
-- 1 host + 8 participants = occupancy 9/10 — matches the prototype's Scenario A exactly.

-- ===== Scenario B + F — full event with a waitlist already forming =====
INSERT INTO events (id, host_user_id, city_id, title, category, description, location, location_label, starts_at, ends_at, capacity, access_mode, guest_policy)
VALUES (
  'aaaaaaaa-0000-0000-0000-000000000005', '22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111',
  '5v5 Football — Need 3 more', 'sports', 'Rented the small pitch — full for now, waitlist is open.',
  ST_SetSRID(ST_MakePoint(25.2927, 54.6650), 4326), 'Zalgirio Stadium Field 2',
  now() + interval '160 minutes', now() + interval '4 hours', 10, 'public', 'none'
);
INSERT INTO event_participants (event_id, user_id, type) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000005', '22222222-2222-2222-2222-222222222222', 'host');
INSERT INTO event_participants (event_id, user_id, type)
  SELECT 'aaaaaaaa-0000-0000-0000-000000000005', id, 'participant' FROM users
  WHERE id NOT IN ('22222222-2222-2222-2222-222222222222', '22222222-2222-2222-2222-222222222228') LIMIT 9;
-- 1 host + 9 participants = occupancy 10/10 — FULL, matching Scenario B.
-- Aiste was deliberately left out of the participant set so she can wait:
INSERT INTO event_waitlist (event_id, user_id, status) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000005', '22222222-2222-2222-2222-222222222228', 'waiting');

-- ===== Scenario D — plain approval flow, small, hosted by someone else =====
INSERT INTO events (id, host_user_id, city_id, title, category, description, location, location_label, starts_at, ends_at, capacity, access_mode, guest_policy)
VALUES (
  'aaaaaaaa-0000-0000-0000-000000000011', '22222222-2222-2222-2222-222222222223', '11111111-1111-1111-1111-111111111111',
  'Coffee Meetup — New in Vilnius', 'meetup', 'For people who just moved to Vilnius.',
  ST_SetSRID(ST_MakePoint(25.2833, 54.6900), 4326), 'Spurga, Vilniaus g.',
  now() + interval '15 minutes', now() + interval '90 minutes', 6, 'approval', 'none'
);
INSERT INTO event_participants (event_id, user_id, type) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000011','22222222-2222-2222-2222-222222222223','host');
-- 'You' (Ravan) has not requested yet in this seed — request it from the app to test the flow end-to-end.

-- ===== Scenario E + G — private house party, approval required, hosted by the demo user =====
INSERT INTO events (id, host_user_id, city_id, title, category, description, location, location_label, approx_location_label, starts_at, ends_at, capacity, access_mode, guest_policy, location_reveal)
VALUES (
  'aaaaaaaa-0000-0000-0000-000000000010', '22222222-2222-2222-2222-222222222220', '11111111-1111-1111-1111-111111111111',
  'House Party — Dorm 3', 'party', "Back to school party, BYOB. Address shared once you're approved.",
  ST_SetSRID(ST_MakePoint(25.2611, 54.6739), 4326), 'VGTU Dorm 3, Room 214', 'Near Antakalnis',
  now() + interval '270 minutes', now() + interval '8 hours', 30, 'approval', 'one', 'confirmed_attendees'
);
INSERT INTO event_participants (event_id, user_id, type) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000010','22222222-2222-2222-2222-222222222220','host'),
  ('aaaaaaaa-0000-0000-0000-000000000010','22222222-2222-2222-2222-222222222225','participant'),
  ('aaaaaaaa-0000-0000-0000-000000000010','22222222-2222-2222-2222-222222222228','participant');
INSERT INTO event_join_requests (event_id, user_id) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000010','22222222-2222-2222-2222-222222222223'), -- Anna
  ('aaaaaaaa-0000-0000-0000-000000000010','22222222-2222-2222-2222-222222222226'), -- Leo
  ('aaaaaaaa-0000-0000-0000-000000000010','22222222-2222-2222-2222-222222222227'); -- Max
-- Open http://localhost:8000/docs and try:
--   POST /events/aaaaaaaa-0000-0000-0000-000000000010/join-requests/{request_id}/approve
-- as the host user to see capacity increase and the request clear.

-- Remaining events (Erasmus meetup, invite-only techno night, gaming
-- meetup, street photography walk, gym session, car meetup, picnic)
-- follow the same pattern as above — see frontend/index.html's `events`
-- array for the full set of 12 with matching coordinates/capacities if
-- you want the backend and prototype to line up exactly.
