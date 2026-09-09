// frontend/api/types.js
//
// JSDoc typedefs shared across api/*.js. This project runs directly in
// the browser with no build/bundle step, so these are plain JSDoc
// rather than .ts files — editors that understand JSDoc (VS Code, etc.)
// still get autocomplete and type-checking from these. If this project
// is later ported into the Next.js app described in the original spec
// (§21/§27), these map almost 1:1 onto `.ts` interface declarations —
// that's a mechanical conversion, not a redesign.

/**
 * @typedef {Object} UserOut
 * @property {string} id
 * @property {string} email
 * @property {string} display_name
 * @property {string|null} city_id
 * @property {string|null} university_or_work
 * @property {string|null} bio
 * @property {string|null} avatar_url
 * @property {'approximate'|'exact_to_participants'} location_precision
 * @property {boolean} hide_from_nearby
 * @property {'everyone'|'friends_only'} restrict_messages
 */

/**
 * @typedef {Object} ProfileUpdate
 * @property {string} [display_name]
 * @property {string} [university_or_work]
 * @property {string} [bio]
 * @property {string} [avatar_url]
 * @property {'approximate'|'exact_to_participants'} [location_precision]
 * @property {boolean} [hide_from_nearby]
 * @property {'everyone'|'friends_only'} [restrict_messages]
 * @property {string[]} [interests]
 */

/**
 * @typedef {'public'|'approval'|'friends'|'invite_only'|'private'} AccessMode
 * @typedef {'none'|'one'|'two'|'host_approval'} GuestPolicy
 */

/**
 * @typedef {Object} EventCard
 * @property {string} id
 * @property {string} title
 * @property {string} category
 * @property {string|null} description
 * @property {number|null} distance_km
 * @property {string} starts_at
 * @property {string|null} ends_at
 * @property {number} capacity
 * @property {number} occupancy
 * @property {boolean} is_full
 * @property {AccessMode} access_mode
 * @property {GuestPolicy} guest_policy
 * @property {string|null} location_label
 * @property {string|null} host_name
 */

/**
 * @typedef {Object} EventParticipantOut
 * @property {string} participant_id
 * @property {string} name
 * @property {'host'|'participant'|'guest'} type
 * @property {string|null} invited_by
 * @property {boolean} checked_in
 */

/**
 * @typedef {Object} EventDetail
 * @property {string} id
 * @property {string} title
 * @property {string} category
 * @property {string|null} description
 * @property {string} starts_at
 * @property {string|null} ends_at
 * @property {number} capacity
 * @property {number} occupancy
 * @property {boolean} is_full
 * @property {AccessMode} access_mode
 * @property {GuestPolicy} guest_policy
 * @property {boolean} invite_required
 * @property {string} location_label
 * @property {string} location_reveal
 * @property {string|null} host_name
 * @property {boolean} is_host
 * @property {boolean} chat_enabled
 * @property {EventParticipantOut[]} participants
 * @property {{id:string,user_id:string,name:string}[]} pending_requests
 * @property {{is_attending:boolean, has_pending_request:boolean, waitlist_position:number|null, waitlist_status:string|null}} my_status
 */

/**
 * @typedef {Object} EventCreatePayload
 * @property {string} title
 * @property {string} category
 * @property {string} [description]
 * @property {number} latitude
 * @property {number} longitude
 * @property {string} [location_label]
 * @property {string} [approx_location_label]
 * @property {string} starts_at - ISO 8601
 * @property {string} [ends_at] - ISO 8601
 * @property {number} capacity
 * @property {AccessMode} access_mode
 * @property {GuestPolicy} guest_policy
 * @property {'after_approval'|'confirmed_attendees'|'immediate'} location_reveal
 * @property {boolean} [chat_enabled]
 * @property {string[]} [tags]
 */

/**
 * @typedef {Object} NotificationOut
 * @property {string} id
 * @property {string} type
 * @property {Object} payload
 * @property {boolean} read
 * @property {string} created_at
 */

export {}; // this file only exports types
