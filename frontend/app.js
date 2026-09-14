import { apiClient, ApiError, isBackendReachable } from './api/client.js';
import * as AuthApi from './api/auth.js';
import * as EventsApi from './api/events.js';
import * as UsersApi from './api/users.js';
import * as PostsApi from './api/posts.js';
import * as NotificationsApi from './api/notifications.js';
import * as MediaApi from './api/media.js';
import * as FriendsApi from './api/friends.js';
import * as ChatApi from './api/chat.js';

/* ============================================================
   AROUND — wired to a real FastAPI/PostgreSQL backend.

   This file replaced the previous prototype's in-memory mock arrays.
   Every action that changes attendance (join/leave/request/approve/
   guest/waitlist/check-in) calls the API and re-renders from the
   server's response — nothing here assumes a spot exists just because
   the UI showed one a moment earlier (spec §8, §22). Capacity-sensitive
   actions are never optimistic; only cosmetic ones (opening a sheet,
   toggling a filter chip) update instantly.

   Set window.AROUND_API_BASE before this script runs to override the
   hostname-aware API base selected by api/client.js.
   ============================================================ */

const CATS = {
  sports:{e:'🏀',label:'Sports'}, party:{e:'🎉',label:'Party'}, study:{e:'☕',label:'Study'},
  food:{e:'🍕',label:'Food'}, music:{e:'🎵',label:'Music'}, gaming:{e:'🎮',label:'Gaming'},
  culture:{e:'🎨',label:'Culture'}, fitness:{e:'🏋️',label:'Fitness'}, meetup:{e:'👥',label:'Meetup'},
  outdoor:{e:'🏖',label:'Outdoor'}
};
const ACCESS = {
  public:   {icon:'🌐', label:'Public',            sub:'Anyone can discover and join'},
  approval: {icon:'🔒', label:'Approval required',  sub:'Host reviews every request'},
  friends:  {icon:'👥', label:'Friends only',       sub:"Only the host's friends can join"},
  invite_only:{icon:'✉️', label:'Invite only',      sub:'Needs a valid invite code'},
  private:  {icon:'🙈', label:'Private',            sub:'Not publicly discoverable'},
};
const GUEST_POLICY = {
  none:     {label:'No guests', max:0},
  one:      {label:'1 guest per participant', max:1},
  two:      {label:'2 guests per participant', max:2},
  host_approval: {label:'Guests by host approval', max:2},
};
const REVEAL = {
  after_approval:  'Exact location shown after your request is approved',
  confirmed_attendees: "Exact location shown once you\u2019re a confirmed attendee",
  immediate: 'Exact location shown immediately',
};
const VILNIUS_CENTER = { lat: 54.6872, lng: 25.2797 };

/* ============================================================
   APP STATE
   ============================================================ */
let session = { user: null };            // current UserOut, or null if signed out
let geo = { ...VILNIUS_CENTER, isReal: false };
let eventsCache = new Map();             // id -> adapted event object (card fields + lazily-loaded detail fields)
let spontaneousPosts = [];
let notifications = [];
let imFreeStatus = null;                 // {when_window, looking_for, radius_km, ...} or null
let imFreeNearby = [];
let imFreeNearbyLoading = false;
let imFreeNearbyError = false;
let profileStats = null;
let chatSocket = null;                   // active WS handle for the currently-open event, if any
let demoMode = false;                    // true when no backend was reachable at boot — see enterDemoMode()
let eventsHasMore = false;
let eventsNextOffset = 0;
let eventsLoadingMore = false;

let state = {
  view:'map', activeCatFilter:null,
  mapFilter:null, discoverCat:null, activeSheet:null, currentEventId:null,
  createStep:1, createDraft:{}, manageAdvancedOpen:false, authMode:'login', pendingOtpEmail:null, pendingResetEmail:null, currentScreen:'home', activityReturnScreen:'home', profileSourceEventId:null,
};

const AVATAR_COLORS = ['#C8FF3E','#FF6B4E','#6E8CFF','#FFD166','#B892FF','#5CD6C0'];
function avColor(name){ let h=0; for(const c of (name||'?')) h+=c.charCodeAt(0); return AVATAR_COLORS[h%AVATAR_COLORS.length]; }
function initials(name){ return (name||'?').split(' ').map(w=>w[0]).slice(0,2).join('').toUpperCase(); }

function toast(msg){
  const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(window._toastTimer);
  window._toastTimer=setTimeout(()=>t.classList.remove('show'),2600);
}

/**
 * Central error-to-toast handler for API calls. Distinguishes network
 * failure, expired auth, and normal "the server said no" cases so the
 * message shown always matches what actually happened (spec §21).
 */
function handleApiError(err, fallbackMsg) {
  if (!(err instanceof ApiError)) { toast(fallbackMsg || 'Something went wrong'); console.error(err); return; }
  if (err.isNetworkError) { toast("Can't reach the server — check your connection"); return; }
  if (err.isAuthError) { toast('Your session expired — please sign in again'); session.user = null; renderAuthScreen(); showScreen('auth'); return; }
  toast(err.message || fallbackMsg || 'Something went wrong');
}

/** Wraps a button click handler with a busy state so it can't be double-clicked and never gets stuck (spec §21). */
async function withBusy(btn, fn) {
  if (!btn || btn.dataset.busy === '1') return;
  const original = btn.innerHTML;
  if (btn) { btn.dataset.busy = '1'; btn.style.opacity = '0.6'; btn.style.pointerEvents = 'none'; }
  try {
    await fn();
  } finally {
    if (btn) { btn.dataset.busy = '0'; btn.style.opacity = ''; btn.style.pointerEvents = ''; btn.innerHTML = original; }
  }
}

/* ============================================================
   TIME HELPERS — the backend deals in ISO 8601 UTC; the UI wants
   "Today · 18:00" style labels the same way the old mock data did.
   ============================================================ */
function minutesUntil(iso){ return Math.round((new Date(iso).getTime() - Date.now()) / 60000); }
function dateLabel(iso){
  const d = new Date(iso), now = new Date();
  const days = Math.floor((d.setHours(0,0,0,0) - new Date(now).setHours(0,0,0,0)) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  return new Date(iso).toLocaleDateString(undefined, { weekday:'short', month:'short', day:'numeric' });
}
function clockLabel(iso){ return new Date(iso).toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' }); }
function timeRangeLabel(startsAt, endsAt){ return endsAt ? `${clockLabel(startsAt)}\u2013${clockLabel(endsAt)}` : clockLabel(startsAt); }

/* ============================================================
   ADAPTER — maps backend EventCard/EventDetail into the single shape
   every render function below consumes. Keeping one shape (rather than
   threading `card.access_mode` vs `detail.access_mode` differences
   through every template) is what let the existing render functions
   survive this integration mostly unchanged, per spec §2.
   ============================================================ */
function adaptCard(card) {
  return {
    id: card.id, title: card.title, cat: card.category, desc: card.description || '',
    distKm: card.distance_km, startsAt: card.starts_at, endsAt: card.ends_at,
    dateLabel: dateLabel(card.starts_at), timeLabel: timeRangeLabel(card.starts_at, card.ends_at),
    capacity: card.capacity, occupancy: card.occupancy, isFull: card.is_full,
    accessMode: card.access_mode, guestPolicy: card.guest_policy,
    loc: card.location_label, host: card.host_name,
    lat: card.latitude, lng: card.longitude, coverImageUrl: card.cover_image_url || null,
    isHost: !!card.is_host, isAttending: !!card.is_attending, status: card.status || 'active',
    tags: [],
    // detail-only fields — populated by mergeDetail() once the event is opened
    participants: null, pendingRequests: null, myStatus: null, chat: null, chatEnabled: true,
  };
}
function mergeDetail(existing, detail) {
  const base = existing || { id: detail.id };
  return {
    ...base,
    id: detail.id, title: detail.title, cat: detail.category, desc: detail.description || '',
    startsAt: detail.starts_at, endsAt: detail.ends_at,
    dateLabel: dateLabel(detail.starts_at), timeLabel: timeRangeLabel(detail.starts_at, detail.ends_at),
    distKm: base.distKm, // detail response doesn't recompute distance — keep whatever the card had
    capacity: detail.capacity, occupancy: detail.occupancy, isFull: detail.is_full,
    accessMode: detail.access_mode, guestPolicy: detail.guest_policy,
    loc: detail.location_label, locationReveal: detail.location_reveal,
    host: detail.host_name, hostUserId: detail.host_user_id || null, isHost: !!detail.is_host,
    isAttending: !!detail.my_status.is_attending,
    chatEnabled: detail.chat_enabled, status: detail.status || 'active',
    participants: detail.participants, pendingRequests: detail.pending_requests,
    myStatus: detail.my_status,
    chat: base.chat || [],
  };
}
function occupancy(ev){ return ev.occupancy; }
function spotsLeft(ev){ return ev.capacity - ev.occupancy; }
function isFull(ev){ return ev.isFull; }
function isHost(ev){ return ev.isHost; }
function isAttending(ev){ return ev.isAttending; }
function myGuests(ev){ return (ev.participants||[]).filter(p => p.type==='guest' && p.invited_by === session.user.display_name); }
function guestLimit(ev){ return (GUEST_POLICY[ev.guestPolicy]||GUEST_POLICY.none).max; }
function canInviteGuest(ev){ return isAttending(ev) && ev.guestPolicy!=='none' && myGuests(ev).length < guestLimit(ev); }
function myWaitlistPosition(ev){ return ev.myStatus ? ev.myStatus.waitlist_position : null; }
function myParticipantEntry(ev){ return (ev.participants||[]).find(p => p.name === session.user?.display_name && p.type !== 'guest'); }
function myWaitlistStatus(ev){ return ev.myStatus ? ev.myStatus.waitlist_status : null; }
function myRequestPending(ev){ return ev.myStatus ? ev.myStatus.has_pending_request : false; }

function timeMeta(ev){
  const startMin = minutesUntil(ev.startsAt);
  const left = spotsLeft(ev);
  const soon = startMin <= 60;
  const hot = left <= 2 && left > 0;
  const t = startMin < 0 ? 'started' : (startMin < 60 ? `in ${startMin}m` : `in ${Math.round(startMin/60)}h`);
  return { soon, hot, t, full: isFull(ev) };
}

/* Client only orders what the server already returned (spec §6: "do not
   perform all search logic in the browser" — the backend's /discovery/
   nearby already ranks via app/ranking.py; this just keeps that order
   stable through client-side category filtering). */
function sortedEvents(list){ return list; }

/* ============================================================
   DEMO DATA — used only when no backend is reachable at boot (see
   enterDemoMode() below). Shaped exactly like mergeDetail()'s output so
   every render function works completely unmodified; every field a
   real event-detail response would have is present, including
   participants and a couple of chat lines, so the event sheet doesn't
   look broken when opened. Nothing here is ever sent anywhere.
   ============================================================ */
function inMinutes(mins){ return new Date(Date.now() + mins*60000).toISOString(); }
const DEMO_EVENTS = [
  {
    id:'demo-1', title:'Basketball at Vingis Park', cat:'sports', desc:'Casual 5v5, all levels welcome. Bring light and dark shirts.',
    startsAt:inMinutes(45), endsAt:inMinutes(165), dateLabel:'Today', timeLabel:'18:00\u201320:00',
    capacity:10, occupancy:8, isFull:false, accessMode:'public', guestPolicy:'none',
    loc:'Vingis Park, Courts', host:'Tomas', distKm:0.8, lat:54.6821, lng:25.2497, coverImageUrl:null, isHost:false, isAttending:false, chatEnabled:true,
    participants:[
      {participant_id:'p1',name:'Tomas',type:'host',invited_by:null,checked_in:false},
      {participant_id:'p2',name:'Jonas',type:'participant',invited_by:null,checked_in:false},
      {participant_id:'p3',name:'Eva',type:'participant',invited_by:null,checked_in:false},
    ],
    pendingRequests:[], myStatus:{is_attending:false, has_pending_request:false, waitlist_position:null, waitlist_status:null},
    chat:[{id:'c1',user_name:'Jonas',body:'Where exactly?',created_at:inMinutes(-30)},{id:'c2',user_name:'Eva',body:"I'm bringing a ball 🏀",created_at:inMinutes(-20)}],
  },
  {
    id:'demo-2', title:'Study Session — Cafe Nova', cat:'study', desc:'Studying for finals, come join if you need a quiet spot with company.',
    startsAt:inMinutes(5), endsAt:inMinutes(245), dateLabel:'Today', timeLabel:'Until 21:00',
    capacity:8, occupancy:3, isFull:false, accessMode:'public', guestPolicy:'none',
    loc:'Cafe Nova, Gedimino pr.', host:'Elena', distKm:0.4, lat:54.6867, lng:25.2833, coverImageUrl:null, isHost:false, isAttending:false, chatEnabled:true,
    participants:[
      {participant_id:'p4',name:'Elena',type:'host',invited_by:null,checked_in:false},
      {participant_id:'p5',name:'Mia',type:'participant',invited_by:null,checked_in:false},
    ],
    pendingRequests:[], myStatus:{is_attending:false, has_pending_request:false, waitlist_position:null, waitlist_status:null},
    chat:[], },
  {
    id:'demo-3', title:'House Party — Dorm 3', cat:'party', desc:"Back to school party, BYOB. Address shared once you're approved.",
    startsAt:inMinutes(270), endsAt:inMinutes(750), dateLabel:'Tonight', timeLabel:'21:00\u2013late',
    capacity:30, occupancy:24, isFull:false, accessMode:'approval', guestPolicy:'one',
    loc:'Near Antakalnis', host:'Ugne', distKm:2.6, lat:54.6739, lng:25.3092, coverImageUrl:null, isHost:false, isAttending:false, chatEnabled:true, locationReveal:'confirmed_attendees',
    participants:[
      {participant_id:'p6',name:'Ugne',type:'host',invited_by:null,checked_in:false},
      {participant_id:'p7',name:'Simas',type:'participant',invited_by:null,checked_in:false},
      {participant_id:'p8',name:'Alex',type:'guest',invited_by:'Simas',checked_in:false},
    ],
    pendingRequests:[], myStatus:{is_attending:false, has_pending_request:false, waitlist_position:null, waitlist_status:null},
    chat:[], },
  {
    id:'demo-4', title:'5v5 Football — Full', cat:'sports', desc:'Rented the small pitch — full for now, join the waitlist.',
    startsAt:inMinutes(160), endsAt:inMinutes(220), dateLabel:'Today', timeLabel:'20:30\u201321:30',
    capacity:10, occupancy:10, isFull:true, accessMode:'public', guestPolicy:'none',
    loc:'Zalgirio Stadium Field 2', host:'Dovydas', distKm:3.4, lat:54.7397, lng:25.2436, coverImageUrl:null, isHost:false, isAttending:false, chatEnabled:true,
    participants:[{participant_id:'p9',name:'Dovydas',type:'host',invited_by:null,checked_in:false}],
    pendingRequests:[], myStatus:{is_attending:false, has_pending_request:false, waitlist_position:null, waitlist_status:null},
    chat:[], },
];
const DEMO_POSTS = [
  {id:'dp1', user_name:'Deivid', body:'Anyone at Vingis Park right now? Bored.', expires_at:inMinutes(22)},
  {id:'dp2', user_name:'Kotryna', body:'Going to a club tonight, anyone know where the crowd is best?', expires_at:inMinutes(40)},
];
const DEMO_NOTIFICATIONS = [
  {id:'dn1', type:'joined', payload:{message:'Anna joined your event: Coffee Meetup.'}, read:false, created_at:inMinutes(-12)},
  {id:'dn2', type:'reminder', payload:{message:'Your event Study Session starts in 1 hour.'}, read:false, created_at:inMinutes(-25)},
];

/* ============================================================
   BOOTSTRAP
   ============================================================ */
/* ============================================================
   PWA — installability only, not offline support (see frontend/sw.js).

   iOS Safari never fires beforeinstallprompt and has no programmatic
   install API at all — Apple's only path is the user manually using
   Share -> Add to Home Screen, so this only ever shows instructions
   there, never a fake one-tap "install" button. Android/desktop
   Chrome (and other browsers implementing the same API) DO support
   triggering the real native install prompt via a captured
   beforeinstallprompt event.
   ============================================================ */
let deferredInstallPrompt = null;
let installBannerShownThisSession = false;
const INSTALL_DISMISSED_KEY = 'around:pwa_prompt_dismissed';

function isStandaloneDisplay(){
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}
function isIOSDevice(){
  return /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
}
function registerServiceWorker(){
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(err => console.warn('[Around] service worker registration failed', err));
  }
}
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault(); // suppress the browser's own mini-infobar — we show our own banner, on our own timing, instead
  deferredInstallPrompt = e;
  maybeShowInstallBanner();
});

/** Called after the user has actually done something (a nav tap) — see go() — rather than immediately on load. */
function maybeShowInstallBanner(){
  if (installBannerShownThisSession || isStandaloneDisplay()) return;
  let dismissed = false;
  try { dismissed = localStorage.getItem(INSTALL_DISMISSED_KEY) === '1'; } catch (_) { /* private browsing etc. — just won't remember dismissal across sessions */ }
  if (dismissed) return;
  if (!isIOSDevice() && !deferredInstallPrompt) return; // nothing to offer: not iOS, and no captured install event yet
  installBannerShownThisSession = true;
  const body = document.getElementById('installBannerBody');
  if (isIOSDevice()) {
    body.innerHTML = `
      <div class="t">Get Around on your Home Screen</div>
      <div class="s">Add Around to your Home Screen for faster access.</div>
      <ol><li>Tap the Share button in Safari</li><li>Select <b>Add to Home Screen</b></li><li>Tap <b>Add</b></li></ol>`;
  } else {
    body.innerHTML = `
      <div class="t">Install Around</div>
      <div class="s">Add Around to your device for faster access, like a real app.</div>
      <button class="install-cta" onclick="AroundApp.triggerInstallPrompt()">Install Around</button>`;
  }
  document.getElementById('installBanner').classList.add('show');
}
async function triggerInstallPrompt(){
  if (!deferredInstallPrompt) return;
  dismissInstallBanner();
  deferredInstallPrompt.prompt();
  try { await deferredInstallPrompt.userChoice; } catch (_) { /* ignore */ }
  deferredInstallPrompt = null;
}
function dismissInstallBanner(){
  document.getElementById('installBanner').classList.remove('show');
  try { localStorage.setItem(INSTALL_DISMISSED_KEY, '1'); } catch (_) { /* ignore — worst case it can show again next session */ }
}

async function boot() {
  registerServiceWorker();
  wireAuthScreen();
  backendReachable = await isBackendReachable();
  if (!backendReachable) {
    renderAuthScreen(); // re-render with the "backend unreachable" notice now that we know
    showScreen('auth');
    return;
  }
  const hasSession = await AuthApi.hasStoredSession();
  if (!hasSession) { showScreen('auth'); return; }
  try {
    session.user = await AuthApi.me();
  } catch (err) {
    showScreen('auth');
    return;
  }
  await enterApp();
}

async function enterApp() {
  showScreen('app');
  await resolveLocation();
  await Promise.all([loadNearbyEvents(), loadNotifications(), loadNearbyPosts(), loadTrustState(), loadProfileStats(), loadMyInterests()]);
  renderImfreeBar();
  setView('map');
  renderProfile();
}

/**
 * Lets this file work standalone — opened directly, with no backend
 * running (spec: this file should still be openable and browsable on
 * its own). Populates the same in-memory state real API responses
 * would, so every render function runs unmodified; every *mutating*
 * action is blocked with an explanatory toast via blockedInDemo()
 * rather than silently failing against a nonexistent server.
 */
function enterDemoMode(){
  demoMode = true;
  session.user = { id: 'demo', display_name: 'Guest', university_or_work: null };
  eventsCache = new Map(DEMO_EVENTS.map(ev => [ev.id, ev]));
  spontaneousPosts = DEMO_POSTS;
  notifications = DEMO_NOTIFICATIONS;
  trustState = { label: 'New Host', checkmark: false };
  showScreen('app');
  injectDemoBanner();
  renderImfreeBar();
  setView('map');
  renderProfile();
}

function injectDemoBanner(){
  if (document.getElementById('demoBanner')) return;
  const bar = document.createElement('div');
  bar.id = 'demoBanner';
  bar.className = 'demo-banner';
  bar.innerHTML = `🔌 Demo mode — no backend connected, showing sample data. <button onclick="location.reload()">Retry connection</button>`;
  document.getElementById('app').prepend(bar);
}

/** Every mutating action checks this first — see enterDemoMode() docstring. */
function blockedInDemo(){
  if (!demoMode) return false;
  toast("This is sample data — connect a backend to actually do this");
  return true;
}

function resolveLocation() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) { resolve(); return; }
    navigator.geolocation.getCurrentPosition(
      (pos) => { geo = { lat: pos.coords.latitude, lng: pos.coords.longitude, isReal: true }; resolve(); },
      () => { resolve(); }, // denied/unavailable — silently fall back to Vilnius center, no error toast for this
      { timeout: 4000 }
    );
  });
}

async function loadNearbyEvents() {
  try {
    const page = await EventsApi.nearby({ lat: geo.lat, lng: geo.lng, radiusKm: 15 });
    eventsCache = new Map(page.results.map(c => [c.id, adaptCard(c)]));
    eventsHasMore = page.has_more;
    eventsNextOffset = page.results.length;
  } catch (err) {
    handleApiError(err, "Couldn't load nearby events");
  }
}

/** Loads the next page of nearby events and merges it in — used by the feed's "Load more" (spec §12: avoid loading the entire database up front). */
async function loadMoreNearbyEvents() {
  if (!eventsHasMore || eventsLoadingMore) return;
  eventsLoadingMore = true;
  try {
    const page = await EventsApi.nearby({ lat: geo.lat, lng: geo.lng, radiusKm: 15, offset: eventsNextOffset });
    page.results.forEach(c => eventsCache.set(c.id, adaptCard(c)));
    eventsHasMore = page.has_more;
    eventsNextOffset += page.results.length;
    renderFeed(); renderMap();
  } catch (err) {
    handleApiError(err, "Couldn't load more events");
  } finally {
    eventsLoadingMore = false;
  }
}

async function loadNotifications() {
  try { notifications = await NotificationsApi.listNotifications(); }
  catch (err) { console.error(err); }
}

async function loadNearbyPosts() {
  try { spontaneousPosts = await PostsApi.nearbyPosts(geo.lat, geo.lng); }
  catch (err) { console.error(err); }
}

let trustState = { label: 'New Host', checkmark: false };
async function loadTrustState() {
  try { trustState = await UsersApi.getMyTrustState(); }
  catch (err) { console.error(err); }
}

async function loadProfileStats() {
  try { profileStats = await UsersApi.getMyProfileStats(); }
  catch (err) { console.error(err); }
}

let myInterests = [];
async function loadMyInterests() {
  try { myInterests = (await UsersApi.getMyInterests()).interests || []; }
  catch (err) { console.error(err); }
}

/* ============================================================
   SCREENS (auth vs app)
   ============================================================ */
function showScreen(which) {
  document.getElementById('authScreen').style.display = which === 'auth' ? 'flex' : 'none';
  document.getElementById('app').style.display = which === 'app' ? 'flex' : 'none';
}

function wireAuthScreen(){ renderAuthScreen(); }

let backendReachable = null; // null = not checked yet, true/false after boot()'s health check

function maskEmail(email){
  const at = email.indexOf('@');
  if (at <= 0) return email;
  return `${email[0]}***${email.slice(at)}`;
}

function renderAuthScreen(){
  const el = document.getElementById('authContent');
  const mode = state.authMode;
  const offlineNotice = backendReachable === false ? `
    <div class="offline-notice">
      <div class="t">🔌 No backend reachable right now</div>
      <div class="s">Sign-in needs a live Around server. You can still look around with sample data.</div>
      <button class="next-btn" style="margin-top:10px;" onclick="AroundApp.enterDemoMode()">Preview with sample data</button>
    </div>` : '';

  if (mode === 'otp') {
    el.innerHTML = `
      <div class="auth-brand"><span class="dot"></span>Around</div>
      <div style="margin-top:22px; text-align:center;">
        <div class="profile-name" style="font-size:19px;">Verify your email</div>
        <div class="auth-tag">We sent a 6-digit code to ${escapeHtml(maskEmail(state.pendingOtpEmail || ''))}</div>
      </div>
      <input class="text-input otp-input" id="otpInput" placeholder="______" inputmode="numeric" pattern="[0-9]*" autocomplete="one-time-code" maxlength="6" style="margin-top:20px;" onkeydown="if(event.key==='Enter')AroundApp.submitOtpVerification()"/>
      <button class="next-btn" id="otpSubmitBtn" onclick="AroundApp.submitOtpVerification()">Verify email</button>
      <button class="back-link" id="otpResendBtn" style="width:100%; margin-top:14px; padding:10px; cursor:pointer;" onclick="AroundApp.resendOtp(this)">Resend code</button>
      <div class="back-link" style="cursor:pointer;" onclick="AroundApp.cancelOtpVerification()">← Use a different email</div>
    `;
    // Right after landing here (fresh signup or a page reload with a
    // still-pending email), the backend's own RESEND_COOLDOWN already
    // started ticking from whenever the code was last sent — reflect
    // that immediately instead of letting the button look falsely
    // available for 45s.
    startOtpResendCooldown(45);
    const input = document.getElementById('otpInput');
    if (input) input.focus();
    return;
  }

  if (mode === 'forgot') {
    el.innerHTML = `
      <div class="auth-brand"><span class="dot"></span>Around</div>
      <div style="margin-top:22px; text-align:center;">
        <div class="profile-name" style="font-size:19px;">Reset your password</div>
        <div class="auth-tag">Enter your account email and we'll send you a code</div>
      </div>
      <input class="text-input" id="forgotEmail" placeholder="Email" type="email" style="margin-top:20px;" onkeydown="if(event.key==='Enter')AroundApp.submitForgotPassword()"/>
      <button class="next-btn" id="forgotSubmitBtn" onclick="AroundApp.submitForgotPassword()">Send reset code</button>
      <div class="back-link" style="cursor:pointer;" onclick="AroundApp.setAuthMode('login')">← Back to sign in</div>
    `;
    const input = document.getElementById('forgotEmail');
    if (input) input.focus();
    return;
  }

  if (mode === 'reset') {
    el.innerHTML = `
      <div class="auth-brand"><span class="dot"></span>Around</div>
      <div style="margin-top:22px; text-align:center;">
        <div class="profile-name" style="font-size:19px;">Enter your new password</div>
        <div class="auth-tag">We sent a 6-digit code to ${escapeHtml(maskEmail(state.pendingResetEmail || ''))}</div>
      </div>
      <input class="text-input otp-input" id="resetOtpInput" placeholder="______" inputmode="numeric" pattern="[0-9]*" autocomplete="one-time-code" maxlength="6" style="margin-top:20px;"/>
      <input class="text-input" id="resetNewPassword" placeholder="New password" type="password" style="margin-top:10px;"/>
      <input class="text-input" id="resetConfirmPassword" placeholder="Confirm new password" type="password" style="margin-top:10px;" onkeydown="if(event.key==='Enter')AroundApp.submitResetPassword()"/>
      <button class="next-btn" id="resetSubmitBtn" onclick="AroundApp.submitResetPassword()">Reset password</button>
      <button class="back-link" id="resetResendBtn" style="width:100%; margin-top:14px; padding:10px; cursor:pointer;" onclick="AroundApp.resendPasswordReset(this)">Resend code</button>
      <div class="back-link" style="cursor:pointer;" onclick="AroundApp.setAuthMode('login')">← Use a different email</div>
    `;
    startResetResendCooldown(45);
    const input = document.getElementById('resetOtpInput');
    if (input) input.focus();
    return;
  }

  el.innerHTML = `
    <div class="auth-brand"><span class="dot"></span>Around</div>
    <div class="auth-tag">The live social layer of your city.</div>
    ${offlineNotice}
    <div class="view-toggle" style="margin:22px 0 18px 0;">
      <button class="${mode==='login'?'active':''}" onclick="AroundApp.setAuthMode('login')">Sign in</button>
      <button class="${mode==='register'?'active':''}" onclick="AroundApp.setAuthMode('register')">Create account</button>
    </div>
    ${mode==='register' ? `<input class="text-input" id="authName" placeholder="Name" style="margin-bottom:10px;"/>` : ''}
    <input class="text-input" id="authEmail" placeholder="Email" style="margin-bottom:10px;" type="email"/>
    <input class="text-input" id="authPassword" placeholder="Password" type="password" onkeydown="if(event.key==='Enter')AroundApp.submitAuth()"/>
    ${mode==='login' ? `<div class="back-link" style="text-align:right; cursor:pointer; margin-top:8px;" onclick="AroundApp.setAuthMode('forgot')">Forgot password?</div>` : ''}
    <button class="next-btn" id="authSubmitBtn" onclick="AroundApp.submitAuth()">${mode==='login'?'Sign in':'Create account'}</button>
    <button class="request-pending-btn" style="margin-top:10px;" onclick="AroundApp.googleStub()">Continue with Google</button>
  `;
}
function setAuthMode(mode){ state.authMode = mode; renderAuthScreen(); }
function googleStub(){ toast("Google sign-in isn't available yet — use email and password"); }

async function submitAuth(){
  const btn = document.getElementById('authSubmitBtn');
  await withBusy(btn, async () => {
    const email = document.getElementById('authEmail').value.trim();
    const password = document.getElementById('authPassword').value;
    if (!email || !password) { toast('Enter your email and password'); return; }
    try {
      if (state.authMode === 'register') {
        const name = document.getElementById('authName').value.trim();
        if (!name) { toast('Enter your name'); return; }
        // Which of these happens depends on the server's own
        // REQUIRE_SIGNUP_OTP setting — see api/auth.js's register().
        const res = await AuthApi.register({ email, password, display_name: name });
        if (res && res.access_token) {
          // OTP step currently disabled server-side — account already exists, log straight in.
          session.user = await AuthApi.me();
          toast(`Welcome${session.user.display_name ? ', ' + session.user.display_name : ''} 👋`);
          await enterApp();
          return;
        }
        // OTP required — no account exists yet. See
        // submitOtpVerification() for where it's actually created.
        state.pendingOtpEmail = email;
        state.authMode = 'otp';
        renderAuthScreen();
        toast('Code sent — check your email');
        return;
      }
      await AuthApi.login({ email, password });
      session.user = await AuthApi.me();
      toast(`Welcome${session.user.display_name ? ', ' + session.user.display_name : ''} 👋`);
      await enterApp();
    } catch (err) {
      // Deliberately NOT handleApiError() here: its 401 branch assumes
      // an already-logged-in session's token just expired (shows
      // "session expired", wipes session.user, redirects to /auth) —
      // but a failed login attempt is ALSO a 401 with nobody logged in
      // yet, so that branch was overwriting the backend's actual
      // "Wrong email or password." with a confusing, wrong message.
      // (This toast is now actually visible too — see the fix moving
      // #toast out from inside #app, which is display:none here.)
      if (err.isNetworkError) { toast("Can't reach the server — check your connection"); return; }
      if (err instanceof ApiError && err.status === 409) {
        // Signup: email already registered — point at login instead of
        // leaving the user stuck on a form that will never succeed.
        toast(err.message);
        setAuthMode('login');
        return;
      }
      toast((err instanceof ApiError && err.message) || (state.authMode === 'register' ? "Couldn't create your account" : 'Wrong email or password.'));
    }
  });
}

let otpResendCooldownUntil = 0;
function startOtpResendCooldown(seconds){
  otpResendCooldownUntil = Date.now() + seconds * 1000;
  tickOtpResendButton();
}
function tickOtpResendButton(){
  const btn = document.getElementById('otpResendBtn');
  if (!btn) return;
  const remaining = Math.ceil((otpResendCooldownUntil - Date.now()) / 1000);
  if (remaining > 0) {
    btn.style.opacity = '0.5';
    btn.style.pointerEvents = 'none';
    btn.textContent = `Resend code (${remaining}s)`;
    setTimeout(tickOtpResendButton, 1000);
  } else {
    btn.style.opacity = '';
    btn.style.pointerEvents = '';
    btn.textContent = 'Resend code';
  }
}

async function submitOtpVerification(){
  const btn = document.getElementById('otpSubmitBtn');
  await withBusy(btn, async () => {
    const otp = (document.getElementById('otpInput').value || '').trim();
    if (!/^\d{6}$/.test(otp)) { toast('Enter the 6-digit code'); return; }
    try {
      await AuthApi.verifySignupOtp(state.pendingOtpEmail, otp);
      session.user = await AuthApi.me();
      state.pendingOtpEmail = null;
      toast(`Welcome${session.user.display_name ? ', ' + session.user.display_name : ''} 👋`);
      await enterApp();
    } catch (err) {
      if (err.isNetworkError) { toast("Can't reach the server — check your connection"); return; }
      // 429 here is the MAX_OTP_ATTEMPTS case ("Too many attempts...")
      // — the backend's own message is already the right copy.
      toast((err instanceof ApiError && err.message) || 'Incorrect verification code.');
    }
  });
}

async function resendOtp(btn){
  if (Date.now() < otpResendCooldownUntil) return;
  await withBusy(btn, async () => {
    try {
      await AuthApi.resendSignupOtp(state.pendingOtpEmail);
      toast('New code sent');
      startOtpResendCooldown(45);
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        toast(err.message); // "Please wait Ns before requesting another code."
        const match = /(\d+)s/.exec(err.message || '');
        startOtpResendCooldown(match ? parseInt(match[1], 10) : 45);
        return;
      }
      handleApiError(err, "Couldn't resend the code");
    }
  });
}

function cancelOtpVerification(){
  state.pendingOtpEmail = null;
  setAuthMode('register');
}

/* ============================================================
   FORGOT / RESET PASSWORD — mirrors the signup-OTP flow above
   (same UX shape: request a code, enter it + a new password).
   ============================================================ */
async function submitForgotPassword(){
  const btn = document.getElementById('forgotSubmitBtn');
  await withBusy(btn, async () => {
    const email = (document.getElementById('forgotEmail').value || '').trim();
    if (!email) { toast('Enter your email'); return; }
    try {
      await AuthApi.requestPasswordReset(email);
      state.pendingResetEmail = email;
      state.authMode = 'reset';
      renderAuthScreen();
      // Deliberately the same message regardless of whether an account
      // exists for this email — the backend never reveals that either
      // (see app/routers/auth.py::request_password_reset), so the UI
      // shouldn't imply anything different.
      toast("If that account exists, we've sent a reset code");
    } catch (err) {
      if (err.isNetworkError) { toast("Can't reach the server — check your connection"); return; }
      handleApiError(err, "Couldn't send the reset code");
    }
  });
}

let resetResendCooldownUntil = 0;
function startResetResendCooldown(seconds){
  resetResendCooldownUntil = Date.now() + seconds * 1000;
  tickResetResendButton();
}
function tickResetResendButton(){
  const btn = document.getElementById('resetResendBtn');
  if (!btn) return;
  const remaining = Math.ceil((resetResendCooldownUntil - Date.now()) / 1000);
  if (remaining > 0) {
    btn.style.opacity = '0.5';
    btn.style.pointerEvents = 'none';
    btn.textContent = `Resend code (${remaining}s)`;
    setTimeout(tickResetResendButton, 1000);
  } else {
    btn.style.opacity = '';
    btn.style.pointerEvents = '';
    btn.textContent = 'Resend code';
  }
}
async function resendPasswordReset(btn){
  if (Date.now() < resetResendCooldownUntil) return;
  await withBusy(btn, async () => {
    try {
      await AuthApi.requestPasswordReset(state.pendingResetEmail);
      toast('New code sent');
      startResetResendCooldown(45);
    } catch (err) {
      handleApiError(err, "Couldn't resend the code");
    }
  });
}

async function submitResetPassword(){
  const btn = document.getElementById('resetSubmitBtn');
  await withBusy(btn, async () => {
    const otp = (document.getElementById('resetOtpInput').value || '').trim();
    const newPassword = document.getElementById('resetNewPassword').value;
    const confirmPassword = document.getElementById('resetConfirmPassword').value;
    if (!/^\d{6}$/.test(otp)) { toast('Enter the 6-digit code'); return; }
    if (!newPassword || newPassword.length < 8) { toast('Password must be at least 8 characters'); return; }
    if (newPassword !== confirmPassword) { toast("Passwords don't match"); return; }
    try {
      await AuthApi.resetPassword(state.pendingResetEmail, otp, newPassword);
      session.user = await AuthApi.me();
      state.pendingResetEmail = null;
      toast('Password reset — you\'re signed in');
      await enterApp();
    } catch (err) {
      if (err.isNetworkError) { toast("Can't reach the server — check your connection"); return; }
      // 429 here is the max-attempts case; the backend's own message is
      // already the right copy (mirrors submitOtpVerification above).
      toast((err instanceof ApiError && err.message) || 'That code is invalid or has expired.');
    }
  });
}

async function logout(){
  await AuthApi.logout();
  session.user = null;
  if (chatSocket) { chatSocket.close(); chatSocket = null; }
  closeAllSheets();
  showScreen('auth');
}

/* ============================================================
   NAV
   ============================================================ */
function go(screen){
  if (screen === 'activity' && state.currentScreen !== 'activity') state.activityReturnScreen = state.currentScreen || 'home';
  document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  document.getElementById('screen-'+screen).classList.add('active');
  document.querySelectorAll('.navbtn').forEach(b=>b.classList.remove('active'));
  const map={home:'nav-home',discover:'nav-discover',activity:'nav-activity',profile:'nav-profile',chat:'nav-chat'};
  if(map[screen]) document.getElementById(map[screen]).classList.add('active');
  state.currentScreen = screen;
  if(screen==='discover') renderDiscover();
  if(screen==='activity') openActivity();
  if(screen==='profile') renderProfile();
  if(screen==='chat') renderChatList();
  maybeShowInstallBanner(); // fires after real navigation, not on initial load — see its own docstring
}
function backFromActivity(){ go(state.activityReturnScreen || 'home'); }

function setView(v){
  state.view=v;
  document.getElementById('tabMap').classList.toggle('active', v==='map');
  document.getElementById('tabFeed').classList.toggle('active', v==='feed');
  document.getElementById('mapView').style.display = v==='map'?'block':'none';
  document.getElementById('feedView').style.display = v==='feed'?'flex':'none';
  if(v==='map') {
    renderMap();
    // container was display:none a moment ago — Leaflet doesn't notice
    // size changes on its own, so it needs an explicit nudge or tiles
    // render into a collapsed 0-height box until the next manual pan/zoom.
    if (leafletMap) setTimeout(() => leafletMap.invalidateSize(), 0);
  } else {
    renderFeed();
  }
}

/* ============================================================
   I'M FREE
   ============================================================ */
function renderImfreeBar(){
  const el=document.getElementById('imfreeBar');
  if(imFreeStatus){
    el.innerHTML = `<div class="imfree-status">
      <div class="live-dot"></div>
      <div class="txt">You're free <b>${imFreeStatus.when_window}</b> \u00b7 looking for <b>${imFreeStatus.looking_for}</b></div>
      <button class="end-btn" onclick="AroundApp.endImFree()">END</button>
    </div>`;
  } else {
    el.innerHTML = `<button class="imfree-btn" onclick="AroundApp.openFree()">
      <span>🟢 I'M FREE — let people know</span>
      <span class="live-ring"></span>
    </button>`;
  }
}
async function endImFreeAction(){
  if (blockedInDemo()) return;
  try { await UsersApi.endImFree(); imFreeStatus = null; renderImfreeBar(); toast('Status ended'); }
  catch (err) { handleApiError(err); }
}

/* ============================================================
   MAP (Leaflet + CartoDB dark tiles)

   CARTO's anonymous basemaps.cartocdn.com raster tiles now require a
   free API key (fair-use limit: 5M tile requests/month at no cost) —
   without one, CARTO serves a watermarked "API KEY REQUIRED" tile
   instead of an error, which is what was showing on the map. This key
   is a public, domain-restricted identifier (restrict it to your
   domains at https://carto.com/basemaps/apikey) — not a secret, so
   it's fine for it to live in this file. Get a free key there and
   paste it below; leaving it blank keeps the previous (watermarked)
   behavior instead of breaking anything.
   ============================================================ */
const CARTO_API_KEY = window.AROUND_CARTO_KEY || '';

/* ============================================================
   The map instance is created lazily on first render, once #app is
   actually visible (Leaflet needs a container with real dimensions at
   init time — creating it earlier, while the auth screen is showing,
   would size it to 0x0). `renderMap()` re-runs on every filter change
   and every event-list refresh; it clears and redraws the marker layer
   rather than trying to diff individual markers, which is simpler and
   plenty fast for the event counts this app deals with.

   Privacy: pins are placed at whatever latitude/longitude the backend
   returned for this specific requester — which is ALREADY either the
   real coordinates or a fuzzed approximate point, decided server-side
   by app/location.py::reveal_coordinates(). This file never has access
   to a "real" coordinate to accidentally leak; it only ever draws the
   point it was given. In demo mode, DEMO_EVENTS carry fixed sample
   coordinates around Vilnius — nothing is fetched or faked as backend
   data.
   ============================================================ */
let leafletMap = null;
let leafletMarkersLayer = null;
let leafletUserMarker = null;
let leafletLoadFailed = false;
let selfMapJitter = null; // {dlat, dlng} — computed once, so the user's own dot doesn't wander between renders

function computeSelfJitter(lat){
  if (selfMapJitter) return selfMapJitter;
  // small fixed session-only offset (~120m) purely for the visual dot on
  // OUR OWN map — never sent anywhere; real geo.lat/geo.lng are still
  // used for every actual API call (nearby search, event creation, etc.)
  const angle = Math.random() * 2 * Math.PI;
  const distance = 60 + Math.random() * 80; // meters
  const dlat = (distance * Math.cos(angle)) / 111320;
  const dlng = (distance * Math.sin(angle)) / (111320 * Math.cos(lat * Math.PI/180) || 1);
  selfMapJitter = { dlat, dlng };
  return selfMapJitter;
}

function ensureLeafletMap(){
  if (leafletMap || leafletLoadFailed) return leafletMap;
  if (typeof L === 'undefined') {
    leafletLoadFailed = true;
    const notice = document.getElementById('mapOfflineNotice');
    if (notice) notice.style.display = 'flex';
    return null;
  }
  const container = document.getElementById('leafletMap');
  if (!container) return null;

  leafletMap = L.map(container, { zoomControl: true, attributionControl: true }).setView([geo.lat, geo.lng], 14);
  const tileUrl = 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png' + (CARTO_API_KEY ? `?key=${encodeURIComponent(CARTO_API_KEY)}` : '');
  L.tileLayer(tileUrl, {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd', maxZoom: 19,
  }).addTo(leafletMap);
  leafletMarkersLayer = L.layerGroup().addTo(leafletMap);
  return leafletMap;
}

function renderMap(){
  const list = [...eventsCache.values()];
  document.getElementById('homeSub').textContent = `${list.length} things happening nearby right now`;
  const legend=document.getElementById('mapLegend');
  legend.innerHTML = `<div class="chip ${!state.mapFilter?'active':''}" onclick="AroundApp.setMapFilter(null)">All</div>` +
    Object.entries(CATS).map(([k,v])=>`<div class="chip ${state.mapFilter===k?'active':''}" onclick="AroundApp.setMapFilter('${k}')">${v.e} ${v.label}</div>`).join('');

  const map = ensureLeafletMap();
  if (!map) return; // Leaflet failed to load (offline CDN) — offline notice is already shown

  const filtered = state.mapFilter ? list.filter(e=>e.cat===state.mapFilter) : list;
  leafletMarkersLayer.clearLayers();

  filtered.forEach(ev => {
    if (ev.lat == null || ev.lng == null) return; // no coordinates for this event — skip its pin rather than guess
    const m = timeMeta(ev);
    const icon = L.divIcon({
      className: '', // avoid Leaflet's default white-box icon class
      html: `<div class="lp-bubble ${m.hot?'hot':''} ${m.soon?'soon':''}"><span>${m.full?'🔴':(CATS[ev.cat]?.e||'📍')}</span></div>`,
      iconSize: [38, 38], iconAnchor: [19, 38],
    });
    L.marker([ev.lat, ev.lng], { icon }).addTo(leafletMarkersLayer).on('click', () => previewPin(ev.id));
  });

  // self marker — approximate, jittered (see computeSelfJitter docstring)
  const jitter = computeSelfJitter(geo.lat);
  const selfLatLng = [geo.lat + jitter.dlat, geo.lng + jitter.dlng];
  const selfIcon = L.divIcon({
    className: '', html: `<div class="lp-user-wrap"><div class="lp-user-radar"></div><div class="lp-user-radar r2"></div><div class="lp-user-dot"></div></div>`,
    iconSize: [14, 14], iconAnchor: [7, 7],
  });
  if (leafletUserMarker) leafletUserMarker.setLatLng(selfLatLng);
  else leafletUserMarker = L.marker(selfLatLng, { icon: selfIcon, interactive: false, zIndexOffset: -100 }).addTo(map);
}
function setMapFilter(k){ state.mapFilter=k; renderMap(); }
function previewPin(id){
  const ev=eventsCache.get(id); if(!ev) return; const m=timeMeta(ev);
  const sheet=document.getElementById('mapSheet');
  sheet.classList.add('show');
  sheet.innerHTML = `<div class="emoji-box">${CATS[ev.cat]?.e||'📍'}</div>
    <div class="info"><div class="t">${ev.title}</div><div class="m">${ev.distKm!=null?ev.distKm+'KM \u00b7 ':''}starts ${m.t} \u00b7 ${m.full?'FULL':occupancy(ev)+'/'+ev.capacity+' going'}</div></div>
    <button class="go" onclick="AroundApp.openEvent('${ev.id}')">View</button>
    <button class="close" onclick="AroundApp.closeMapPreview()" title="Close event preview">×</button>`;
  if (leafletMap) leafletMap.panTo([ev.lat, ev.lng]);
}
function closeMapPreview(){
  const sheet = document.getElementById('mapSheet');
  sheet.classList.remove('show');
  sheet.innerHTML = '';
}

/* ============================================================
   FEED
   ============================================================ */
function renderFeed(){
  const list = [...eventsCache.values()];
  document.getElementById('homeSub').textContent = `${list.length} things happening nearby right now`;
  const wrap=document.getElementById('feedView');
  const ranked = sortedEvents(list);
  let html = `<div class="section-label">Happening soon near you</div>`;
  ranked.slice(0,5).forEach(ev=> html+=eventCardHtml(ev));
  html += `<div class="section-label" style="display:flex; align-items:center; justify-content:space-between;">Right now <button style="font-family:var(--font-mono); color:var(--accent); font-weight:700; font-size:10.5px;" onclick="AroundApp.openPost()">+ POST</button></div>`;
  spontaneousPosts.forEach(p=> html+=postCardHtml(p));
  html += `<div class="section-label">More around you</div>`;
  ranked.slice(5).forEach(ev=> html+=eventCardHtml(ev));
  wrap.innerHTML = html || `<div class="empty-state"><div class="e">🌙</div><div class="t">Nothing nearby yet — be the first to create something.</div></div>`;
}

function eventCardHtml(ev){
  const m=timeMeta(ev);
  const attending = isAttending(ev);
  const wlPos = myWaitlistPosition(ev);
  const thumb = ev.coverImageUrl
    ? `<img class="emoji-box event-thumb" src="${MediaApi.absoluteMediaUrl(ev.coverImageUrl)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=&quot;emoji-box&quot;>${CATS[ev.cat]?.e||'📍'}</div>'"/>`
    : `<div class="emoji-box">${CATS[ev.cat]?.e||'📍'}</div>`;
  return `<div class="event-card" onclick="AroundApp.openEvent('${ev.id}')">
    ${thumb}
    <div class="body">
      <div class="row1"><div class="title">${ev.title}${ev.accessMode!=='public'?' '+(ACCESS[ev.accessMode]?.icon||''):''}</div></div>
      <div class="meta">
        ${ev.distKm!=null?`<span>${ev.distKm}KM</span>\u00b7`:''}<span class="${m.soon?'soon':''}">${m.t}</span>\u00b7<span>${ev.dateLabel}</span>
        ${m.full?`<span class="hot">🔴 FULL</span>`:m.hot?`<span class="hot">🔥 ${spotsLeft(ev)} left</span>`:''}
        ${attending?`<span style="color:var(--accent)">✓ Going</span>`:wlPos?`<span style="color:var(--text-dim)">⏳ #${wlPos} waitlisted</span>`:''}
      </div>
      <div class="desc">${(ev.desc||'').slice(0,64)}${(ev.desc||'').length>64?'\u2026':''}</div>
      <div class="spots"><div class="spots-text">${occupancy(ev)}/${ev.capacity} spots \u00b7 hosted by ${ev.host||'someone'}</div></div>
    </div>
  </div>`;
}
function postCardHtml(p){
  const user = p.user_name || p.user || 'Someone';
  return `<div class="post-card">
    <div class="row1">
      <div class="av-round" style="background:${avColor(user)}20; color:${avColor(user)}">${initials(user)}</div>
      <div class="name">${user}</div>
      <div class="exp">${p.expires_at ? 'expires ' + new Date(p.expires_at).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}) : ''}</div>
    </div>
    <div class="txt">${p.body || p.text || ''}</div>
  </div>`;
}

/* ============================================================
   DISCOVER
   ============================================================ */
let discoverSearchTimer = null;
function renderDiscover(){
  const chips=document.getElementById('catChips');
  chips.innerHTML = `<div class="filter-chip ${!state.discoverCat?'active':''}" onclick="AroundApp.setDiscoverCat(null)">All</div>` +
    Object.entries(CATS).map(([k,v])=>`<div class="filter-chip ${state.discoverCat===k?'active':''}" onclick="AroundApp.setDiscoverCat('${k}')">${v.e} ${v.label}</div>`).join('');
  renderPeopleNearby();
  renderDiscoverResults();
}
async function renderPeopleNearby(){
  const el = document.getElementById('peopleScroll');
  if (demoMode) {
    el.innerHTML = `<div class="empty-mini">People discovery needs a live backend — showing sample events only in demo mode.</div>`;
    return;
  }
  el.innerHTML = `<div class="empty-mini">Loading\u2026</div>`;
  try {
    const people = await EventsApi.peopleNearby();
    el.innerHTML = people.length ? people.map(p => {
      const avatar = p.avatar_url
        ? `<img src="${MediaApi.absoluteMediaUrl(p.avatar_url)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'${escapeHtml(initials(p.display_name))}'}))"/>`
        : escapeHtml(initials(p.display_name));
      return `
      <div class="person-card" onclick="AroundApp.openUserProfile('${p.user_id}')">
        <div class="pav" style="background:${avColor(p.display_name)}30; color:${avColor(p.display_name)}">${avatar}</div>
        <div class="pname">${escapeHtml(p.display_name)}</div>
        <div class="pmeta">${p.shared_interests.length ? escapeHtml(p.shared_interests.slice(0,2).join(' \u00b7 ')) : escapeHtml(p.university_or_work || 'Around member')}</div>
        <div class="pshared">${p.mutual_events ? p.mutual_events + ' mutual event' + (p.mutual_events>1?'s':'') : p.shared_interests.length + ' shared interest' + (p.shared_interests.length!==1?'s':'')}${p.friendship_status==='accepted'?' \u00b7 Friends':p.friendship_status==='pending'?' \u00b7 Pending':''}</div>
      </div>`;
    }).join('')
      : `<div class="empty-mini">No one to show yet — add some interests in your profile or join a few events to find people with things in common.</div>`;
  } catch (err) {
    el.innerHTML = `<div class="empty-mini">Couldn't load people right now.</div>`;
  }
}
function setDiscoverCat(k){ state.discoverCat=k; renderDiscover(); }
function filterTonight(){ go('discover'); state.discoverCat=null; document.getElementById('searchInput').value='tonight'; renderDiscoverResults(); }

async function renderDiscoverResults(){
  const q=(document.getElementById('searchInput')?.value||'').trim();
  const res=document.getElementById('discoverResults');
  res.innerHTML = `<div class="empty-mini">Searching\u2026</div>`;
  try {
    const page = await EventsApi.search({ q, category: state.discoverCat || undefined });
    const adapted = page.results.map(adaptCard);
    adapted.forEach(a => eventsCache.set(a.id, { ...eventsCache.get(a.id), ...a }));
    const loadMoreBtn = page.has_more ? `<button class="request-pending-btn" style="margin-top:4px;" onclick="AroundApp.loadMoreSearchResults('${q.replace(/'/g,"\\'")}',${page.results.length})">Load more (${page.total - page.results.length} more)</button>` : '';
    res.innerHTML = adapted.length ? adapted.map(eventCardHtml).join('') + loadMoreBtn :
      `<div class="empty-state"><div class="e">🔍</div><div class="t">Nothing matches "${q}" yet — try a different search or category.</div></div>`;
  } catch (err) {
    res.innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">Couldn't search right now.</div></div>`;
    handleApiError(err);
  }
}
async function loadMoreSearchResults(q, offset){
  try {
    const page = await EventsApi.search({ q, category: state.discoverCat || undefined, offset });
    const adapted = page.results.map(adaptCard);
    adapted.forEach(a => eventsCache.set(a.id, { ...eventsCache.get(a.id), ...a }));
    const res = document.getElementById('discoverResults');
    const html = adapted.map(eventCardHtml).join('');
    const loadMoreBtn = page.has_more ? `<button class="request-pending-btn" style="margin-top:4px;" onclick="AroundApp.loadMoreSearchResults('${q.replace(/'/g,"\\'")}',${offset+page.results.length})">Load more (${page.total - offset - page.results.length} more)</button>` : '';
    res.innerHTML = res.innerHTML.replace(/<button class="request-pending-btn"[^>]*>Load more[^<]*<\/button>/, '') + html + loadMoreBtn;
  } catch (err) { handleApiError(err); }
}
function onSearchInput(){
  clearTimeout(discoverSearchTimer);
  discoverSearchTimer = setTimeout(renderDiscoverResults, 250); // debounced — backend does the filtering, not the browser
}

/* ============================================================
   ACTIVITY (notifications)
   ============================================================ */
function renderActivity(){
  const unread = notifications.filter(n=>!n.read).length;
  document.getElementById('notifBadge').textContent = unread || '';
  document.getElementById('notifBadge').style.display = unread ? 'flex':'none';
  const el=document.getElementById('activityList');
  el.innerHTML = notifications.length ? notifications.map(n=>`<div class="notif-item ${!n.read?'unread':''}" onclick="AroundApp.markNotifRead('${n.id}')">
      <div class="nicon">${notifIcon(n.type)}</div>
      <div style="flex:1; min-width:0;">
        <div class="ntxt">${notifText(n)}</div>
        <div class="ntime">${new Date(n.created_at).toLocaleString()}</div>
        ${isActionablePendingFriendRequest(n) ? `
          <div class="notif-action-row" onclick="event.stopPropagation();">
            <button class="mini-btn accept" onclick="AroundApp.respondFriendRequestFromNotif('${n.id}','${n.payload.requester_id}',true)">Accept</button>
            <button class="mini-btn reject" onclick="AroundApp.respondFriendRequestFromNotif('${n.id}','${n.payload.requester_id}',false)">Decline</button>
          </div>` : ''}
      </div>
    </div>`).join('') : `<div class="empty-state"><div class="e">🔔</div><div class="t">Nothing yet — activity on your events will show up here.</div></div>`;
}
/**
 * A friend_request notification is only actionable here if the backend
 * says the underlying Friendship row is still 'pending' — see
 * backend/app/routers/notifications.py's list_notifications(), which
 * computes this fresh on every fetch from the same Friendship rows
 * friends.py/events.py already treat as the source of truth. That's
 * what makes this survive a refresh correctly: once the request has
 * been accepted/declined (from here, or from the sender's profile
 * sheet, or a second time from a stale tab), still_pending goes false
 * and the buttons stop appearing — no separate "resolved" flag needed.
 * Older notifications created before this field existed lack
 * requester_id/still_pending entirely and correctly render with no
 * action row (missing/falsy), rather than erroring.
 */
function isActionablePendingFriendRequest(n){
  return n.type === 'friend_request' && n.payload && n.payload.requester_id && n.payload.still_pending;
}
function notifIcon(type){
  return {
    join_request:'👋', approved:'✅', rejected:'\u{1F6AB}', invited:'✉️', waitlist_offer:'🎉',
    reminder:'⏰', checked_in:'✅', event_updated:'\u270F\uFE0F', event_cancelled:'\u{1F534}',
    guest_invited:'👥', guest_cancelled:'\u{1F6AB}', friend_request:'🤝', friend_accepted:'🤝',
    friend_declined:'\u{1F6AB}', direct_message:'💬',
    joined:'✅', spots_low:'\u{1F525}',
  }[type] || '✨';
}
function notifText(n){
  return (n.payload && n.payload.message) || n.type.replace(/_/g,' ');
}
async function markNotifRead(id){
  try { await NotificationsApi.markRead(id); const n = notifications.find(x=>x.id===id); if(n) n.read = true; renderActivity(); }
  catch (err) { handleApiError(err); }
}

/**
 * Accept/Decline straight from the Activity notification — reuses the
 * exact same friends.py endpoints the profile sheet's Add Friend flow
 * already calls (FriendsApi.acceptRequest/rejectRequest); this is not
 * a second friendship system, just a second entry point into the same
 * one, which is the actual gap this fixes (previously the only way to
 * respond to a request was to go find the sender's profile again).
 */
async function respondFriendRequestFromNotif(notifId, requesterId, accept){
  try {
    if (accept) { await FriendsApi.acceptRequest(requesterId); toast('Friend request accepted'); }
    else { await FriendsApi.rejectRequest(requesterId); toast('Request declined'); }
  } catch (err) {
    // A 404 here means the request was already accepted/declined
    // elsewhere (e.g. the sender's profile sheet, or a second device) —
    // friends.py's accept/reject endpoints 404 once the Friendship row
    // is no longer 'pending'. The outcome the user wanted (this request
    // no longer being pending) is already true, so treat it as success
    // rather than an error.
    if (err.status !== 404) { handleApiError(err); return; }
  }
  const n = notifications.find(x => x.id === notifId);
  if (n && n.payload) n.payload.still_pending = false;
  await markNotifRead(notifId);
}

/**
 * Opening Activity previously only re-rendered from whatever was
 * already in the in-memory `notifications` array — it never called the
 * backend's existing POST /notifications/read-all, so the unread badge
 * kept showing the same stale count until every item was clicked one
 * by one. This marks everything read (server-side, via the endpoint
 * that already existed and was already wrapped as NotificationsApi.
 * markAllRead — just never called from anywhere) as soon as the screen
 * is actually viewed, then re-renders so the badge clears immediately.
 */
async function openActivity(){
  renderActivity();
  if (demoMode) return; // sample data isn't backed by a real endpoint — nothing to mark read
  if (!notifications.some(n => !n.read)) return;
  try {
    await NotificationsApi.markAllRead();
    notifications = notifications.map(n => ({ ...n, read: true }));
    renderActivity();
  } catch (_) { /* non-critical — badge just stays as-is until the next successful view */ }
}

/* ============================================================
   CHAT (friend-to-friend direct messaging)

   Reuses backend/app/routers/chat.py, which itself reuses the
   direct_messages table and app/ws.py's ChatConnectionManager already
   built for event chat (see that router's own docstring) — this is
   the same HTTP-POST-to-persist + WebSocket-to-broadcast pattern
   event chat already uses, applied to a friend pair instead of an
   event id, not a separate design.

   Note on data-* + this.dataset instead of embedding names in
   onclick="..." strings below: a display name is user-controlled text
   that can contain an apostrophe. escapeHtml() correctly turns that
   into &#39; for an HTML attribute VALUE — but if that same escaped
   text were interpolated into a JS string literal inside an onclick
   attribute, the browser decodes &#39; back to a literal ' before
   handing the attribute to the JS parser, breaking out of the string
   early. Reading it back via this.dataset.name (already decoded, never
   re-parsed as JS) avoids that trap entirely.
   ============================================================ */
let dmSocket = null;
let currentDmFriendId = null;
let conversationsCache = [];

async function renderChatList(){
  const el = document.getElementById('conversationList');
  if (blockedInDemo()) { el.innerHTML = `<div class="empty-state"><div class="e">💬</div><div class="t">Chat needs a live backend — connect one to message friends.</div></div>`; return; }
  el.innerHTML = `<div class="empty-mini" style="padding:20px;">Loading…</div>`;
  try {
    conversationsCache = await ChatApi.listConversations();
    updateChatBadge(conversationsCache);
    el.innerHTML = conversationsCache.length ? conversationsCache.map(c => {
      const avatar = c.avatar_url
        ? `<img src="${MediaApi.absoluteMediaUrl(c.avatar_url)}" alt=""/>`
        : escapeHtml(initials(c.display_name));
      const time = c.last_message_at ? new Date(c.last_message_at).toLocaleDateString(undefined,{month:'short',day:'numeric'}) : '';
      return `<div class="conversation-item ${c.unread_count?'unread':''}" data-friend-id="${c.friend_user_id}" data-name="${escapeHtml(c.display_name)}" onclick="AroundApp.openDmThread(this.dataset.friendId, this.dataset.name)">
        <div class="cav" style="background:${avColor(c.display_name)}30; color:${avColor(c.display_name)}">${avatar}</div>
        <div class="cbody">
          <div class="cname"><span>${escapeHtml(c.display_name)}</span><span class="ctime">${time}</span></div>
          <div class="clast">${c.last_message ? escapeHtml(c.last_message) : 'Say hi \u{1F44B}'}</div>
        </div>
        ${c.unread_count ? `<div class="cunread">${c.unread_count}</div>` : ''}
      </div>`;
    }).join('') : `<div class="empty-state"><div class="e">💬</div><div class="t">No conversations yet — message a friend from their profile.</div></div>`;
  } catch (err) {
    el.innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">Couldn't load chats right now.</div></div>`;
    handleApiError(err);
  }
}

function updateChatBadge(conversations){
  const dot = document.getElementById('chatBadgeDot');
  if (!dot) return;
  dot.hidden = !conversations.some(c => c.unread_count > 0);
}

function dmMsgHtml(m){
  const mine = session.user && m.sender_id === session.user.id;
  return `<div class="chat-msg"${mine ? ' style="flex-direction:row-reverse;"' : ''}>
    <div class="a" style="background:${avColor(m.sender_id)}30; color:${avColor(m.sender_id)}">${mine ? 'Me' : '\u{1F464}'}</div>
    <div class="b">${escapeHtml(m.body)}</div>
  </div>`;
}

/**
 * The messages actually rendered for the currently-open DM thread,
 * keyed by server message id. This — not string-concatenating HTML —
 * is the real fix for the double-send bug: sendDmMessage() appended
 * the POST response directly, AND the backend's WS broadcast (which
 * includes the sender's own open socket, same as event chat's
 * broadcast does) delivered that identical message again to
 * connectDmSocket()'s onMessage, which also appended unconditionally.
 * Both paths now go through appendDmMessageIfNew(), so whichever of
 * the two (HTTP response, WS echo) arrives first renders it and
 * records its id; the other finds the id already present and no-ops.
 * No optimistic/temporary local message is ever created, so there's
 * nothing to "reconcile" — the first render IS the authoritative
 * server copy either way.
 */
let currentDmMessages = [];

function appendDmMessageIfNew(msg){
  if (currentDmMessages.some(m => m.id === msg.id)) return;
  currentDmMessages.push(msg);
  const el = document.getElementById('dmThreadMessages');
  if (el) { el.innerHTML += dmMsgHtml(msg); el.scrollTop = el.scrollHeight; }
}

async function openDmThread(friendUserId, displayName){
  if (blockedInDemo()) return;
  currentDmFriendId = friendUserId;
  currentDmMessages = [];
  const cached = conversationsCache.find(c => c.friend_user_id === friendUserId);
  document.getElementById('dmThreadTitle').textContent = displayName || (cached && cached.display_name) || 'Chat';
  document.getElementById('dmThreadMessages').innerHTML = `<div class="empty-mini">Loading…</div>`;
  openSheet('sheetDmThread');
  try {
    const messages = await ChatApi.getMessages(friendUserId);
    renderDmMessages(messages);
    connectDmSocket(friendUserId);
    renderChatList(); // opening a thread marks it read server-side — refresh the list/badge to match
  } catch (err) {
    document.getElementById('dmThreadMessages').innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">Couldn't open this conversation.</div></div>`;
    handleApiError(err);
  }
}
function renderDmMessages(messages){
  currentDmMessages = [...messages];
  const el = document.getElementById('dmThreadMessages');
  if (!el) return;
  el.innerHTML = messages.length ? messages.map(dmMsgHtml).join('') : `<div class="empty-mini">No messages yet — say hi.</div>`;
  el.scrollTop = el.scrollHeight;
}
async function connectDmSocket(friendUserId){
  if (dmSocket) { dmSocket.close(); dmSocket = null; }
  try {
    dmSocket = await ChatApi.openDmSocket(friendUserId, {
      onMessage: (msg) => {
        if (currentDmFriendId !== friendUserId) return;
        appendDmMessageIfNew(msg);
      },
    });
  } catch (err) { console.error(err); }
}
function closeDmThread(){
  if (dmSocket) { dmSocket.close(); dmSocket = null; }
  currentDmFriendId = null;
  currentDmMessages = [];
  closeAllSheets();
}
async function sendDmMessage(){
  if (blockedInDemo()) return;
  const friendUserId = currentDmFriendId;
  if (!friendUserId) return;
  const inp = document.getElementById('dmMessageInput');
  const val = inp.value.trim();
  if (!val) return;
  inp.value = '';
  try {
    const msg = await ChatApi.sendMessage(friendUserId, val);
    appendDmMessageIfNew(msg);
  } catch (err) { handleApiError(err, "Couldn't send that message"); inp.value = val; }
}

/* ============================================================
   PROFILE
   ============================================================ */
async function renderProfile(){
  if (!session.user) return;
  const mine = [...eventsCache.values()].filter(ev=>isAttending(ev));
  if (profileStats) {
    document.getElementById('statUpcoming').textContent = profileStats.upcoming;
    document.getElementById('statHosting').textContent = profileStats.hosting;
    document.getElementById('statFriends').textContent = profileStats.friends;
  }
  const el=document.getElementById('profileUpcoming');
  el.innerHTML = mine.length? mine.map(ev=>`<div class="event-card" onclick="AroundApp.openEvent('${ev.id}')">
      <div class="emoji-box">${CATS[ev.cat]?.e||'📍'}</div>
      <div class="body"><div class="title">${ev.title}</div><div class="meta">${ev.dateLabel} \u00b7 ${ev.timeLabel}${isHost(ev)?' \u00b7 Hosting':''}</div></div>
    </div>`).join('') : `<div class="empty-state"><div class="e">🗓️</div><div class="t">No upcoming plans yet — join something from the map.</div></div>`;
  document.getElementById('trustBadgeRow').innerHTML = `<span class="badge-pill ${trustState.checkmark?'ok':''}">${trustState.checkmark?'✓ ':''}${trustState.label}</span>`;
  const nameEl = document.getElementById('profileNameDisplay');
  if (nameEl) nameEl.textContent = session.user.display_name;
  const locEl = document.getElementById('profileLocDisplay');
  if (locEl) locEl.textContent = 'Vilnius 🇱🇹' + (session.user.university_or_work ? ` \u00b7 ${session.user.university_or_work}` : '');
  const avEl = document.getElementById('profileAvatarInitial');
  if (avEl) {
    if (session.user.avatar_url) {
      avEl.innerHTML = `<img src="${MediaApi.absoluteMediaUrl(session.user.avatar_url)}" alt=""/>`;
    } else {
      avEl.textContent = initials(session.user.display_name);
    }
  }
  const interestsEl = document.getElementById('profileInterestsRow');
  if (interestsEl) {
    interestsEl.innerHTML = myInterests.length
      ? myInterests.map(i => `<span>${escapeHtml(i)}</span>`).join('')
      : `<span style="opacity:0.6;">No interests added yet</span>`;
  }
  // No verify-banner here anymore: signup now verifies email via OTP
  // *before* an account exists at all (see the auth screen's OTP step),
  // so every account reaching this screen is already verified by
  // construction. (A handful of accounts created under the previous
  // link-based flow, before this change, may still have
  // email_verified=false — that flag no longer gates anything, per
  // app/config.py's REQUIRE_EMAIL_VERIFICATION, and there's no
  // resend-for-an-existing-account flow to point them at anymore; this
  // is an accepted, harmless, one-time edge case, not something a
  // banner needs to surface.)
}

/* ============================================================
   FRIENDS LIST (Profile → Friends)

   No existing privacy setting governs who can see a friend list (the
   only privacy toggles are hide_from_nearby and restrict_messages —
   neither covers this), so the least-invasive default is chosen here:
   a user's OWN friend list is visible only to themselves (this sheet
   is only ever opened from your own Profile tab — there's no "view
   someone else's friend list" entry point anywhere in the UI, which is
   the safer default until a real privacy setting exists for it).
   ============================================================ */
async function openFriendsList(){
  document.getElementById('friendsListContent').innerHTML = `<div class="empty-mini" style="padding:20px;">Loading…</div>`;
  openSheet('sheetFriendsList');
  try {
    const friends = await FriendsApi.listFriends();
    const el = document.getElementById('friendsListContent');
    el.innerHTML = friends.length ? friends.map(f => {
      const avatar = f.avatar_url
        ? `<img src="${MediaApi.absoluteMediaUrl(f.avatar_url)}" alt=""/>`
        : escapeHtml(initials(f.display_name));
      return `<div class="conversation-item" data-friend-id="${f.user_id}" onclick="AroundApp.closeAllSheets(); AroundApp.openUserProfile(this.dataset.friendId);">
        <div class="cav" style="background:${avColor(f.display_name)}30; color:${avColor(f.display_name)}">${avatar}</div>
        <div class="cbody"><div class="cname">${escapeHtml(f.display_name)}</div></div>
      </div>`;
    }).join('') : `<div class="empty-state"><div class="e">👥</div><div class="t">No friends yet — add friends from their profile.</div></div>`;
  } catch (err) {
    document.getElementById('friendsListContent').innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">Couldn't load friends right now.</div></div>`;
    handleApiError(err);
  }
}

/* ============================================================
   EDIT PROFILE (avatar, nickname, bio, university, interests)

   All fields go through the existing PATCH /users/me — no new profile
   endpoint. Backend-side moderation (nickname/bio) and the curated
   interest catalog are enforced server-side regardless of what this UI
   allows through, per spec — this form just gives the user a clear
   error instead of a raw 400 when that happens.
   ============================================================ */
let editProfileDraft = { interests: [] };
let interestCatalogCache = null;

async function openEditProfile(){
  if (!interestCatalogCache) {
    try { interestCatalogCache = (await UsersApi.getInterestCatalog()).groups; }
    catch (err) { interestCatalogCache = {}; }
  }
  editProfileDraft = {
    display_name: session.user.display_name || '',
    bio: session.user.bio || '',
    university_or_work: session.user.university_or_work || '',
    interests: [...myInterests],
  };
  renderEditProfile();
  openSheet('sheetEditProfile');
}

function renderEditProfile(){
  const d = editProfileDraft;
  const avatar = session.user.avatar_url
    ? `<img src="${MediaApi.absoluteMediaUrl(session.user.avatar_url)}" alt=""/>`
    : escapeHtml(initials(d.display_name));
  document.getElementById('editProfileContent').innerHTML = `
    <div style="text-align:center;">
      <div class="avatar-upload-wrap">
        <div class="profile-avatar">${avatar}</div>
        <button class="avatar-edit-btn-lg" onclick="document.getElementById('editAvatarFileInput').click()">✏️</button>
        <input type="file" id="editAvatarFileInput" accept="image/jpeg,image/png,image/webp,image/gif" style="display:none;" onchange="AroundApp.handleAvatarFileChosen(this)"/>
      </div>
    </div>
    <div class="field-label">Nickname</div>
    <input class="text-input" id="editNickname" maxlength="40" value="${escapeHtml(d.display_name)}"/>
    <div class="field-label">University / work</div>
    <input class="text-input" id="editUniversity" maxlength="100" value="${escapeHtml(d.university_or_work)}"/>
    <div class="field-label">Bio</div>
    <textarea class="textarea-input" id="editBio" maxlength="280">${escapeHtml(d.bio)}</textarea>
    <div class="field-label">Interests</div>
    <div class="selected-interests" id="selectedInterestsRow">${renderSelectedInterestChips()}</div>
    <input class="text-input" id="interestSearch" placeholder="Search interests…" style="margin-top:10px;" oninput="AroundApp.filterInterestPicker(this.value)"/>
    <div id="interestPickerGroups" style="margin-top:6px;">${renderInterestPickerGroups('')}</div>
    <button class="next-btn" id="saveProfileBtn" onclick="AroundApp.saveEditProfile(this)">Save changes</button>
  `;
}

function renderSelectedInterestChips(){
  if (!editProfileDraft.interests.length) return `<span style="font-size:12px; color:var(--text-faint);">None selected yet</span>`;
  return editProfileDraft.interests.map(i => `
    <div class="rm-chip">${escapeHtml(i)}<button onclick="AroundApp.toggleInterest('${escapeHtml(i).replace(/"/g,'&quot;')}')">✕</button></div>
  `).join('');
}

function renderInterestPickerGroups(filter){
  const groups = interestCatalogCache || {};
  const q = filter.trim().toLowerCase();
  return Object.entries(groups).map(([group, items]) => {
    const visible = items.filter(i => !q || i.toLowerCase().includes(q));
    if (!visible.length) return '';
    return `
      <div class="interest-group-label">${escapeHtml(group)}</div>
      <div class="interest-picker-grid">
        ${visible.map(i => `<button class="interest-opt ${editProfileDraft.interests.includes(i)?'selected':''}" onclick="AroundApp.toggleInterest('${escapeHtml(i).replace(/"/g,'&quot;')}')">${escapeHtml(i)}</button>`).join('')}
      </div>`;
  }).join('');
}

function filterInterestPicker(value){
  document.getElementById('interestPickerGroups').innerHTML = renderInterestPickerGroups(value);
}

function toggleInterest(interest){
  const idx = editProfileDraft.interests.indexOf(interest);
  if (idx === -1) editProfileDraft.interests.push(interest);
  else editProfileDraft.interests.splice(idx, 1);
  document.getElementById('selectedInterestsRow').innerHTML = renderSelectedInterestChips();
  const search = document.getElementById('interestSearch');
  document.getElementById('interestPickerGroups').innerHTML = renderInterestPickerGroups(search ? search.value : '');
}

async function saveEditProfile(btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    const displayName = document.getElementById('editNickname').value.trim();
    const university = document.getElementById('editUniversity').value.trim();
    const bio = document.getElementById('editBio').value.trim();
    if (displayName.length < 2) { toast('Nickname is too short'); return; }
    try {
      const updated = await UsersApi.updateMyProfile({
        display_name: displayName,
        university_or_work: university || null,
        bio: bio || null,
        interests: editProfileDraft.interests,
      });
      session.user = { ...session.user, ...updated };
      myInterests = [...editProfileDraft.interests];
      renderProfile();
      closeAllSheets();
      toast('Profile updated');
    } catch (err) { handleApiError(err, "Couldn't save your profile"); }
  });
}

function escapeHtml(value){
  return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' })[char]);
}
async function openUserProfile(userId, sourceEventId){
  state.profileSourceEventId = sourceEventId || null;
  state.viewingProfileUserId = userId;
  document.getElementById('userProfileContent').innerHTML = `<div class="empty-mini" style="padding:30px 0; text-align:center;">Loading…</div>`;
  openSheet('sheetUserProfile');
  try {
    const profile = await UsersApi.getPublicProfile(userId);
    const avatar = profile.avatar_url
      ? `<img src="${MediaApi.absoluteMediaUrl(profile.avatar_url)}" alt=""/>`
      : escapeHtml(initials(profile.display_name));
    const isSelf = profile.friendship_status === 'self';
    document.getElementById('userProfileContent').innerHTML = `
      <div class="profile-hero">
        <div class="profile-avatar">${avatar}</div>
        <div class="profile-name">${escapeHtml(profile.display_name)}</div>
        <div class="profile-loc">${profile.university_or_work ? escapeHtml(profile.university_or_work) : ''}</div>
        ${profile.bio ? `<div class="ed-desc">${escapeHtml(profile.bio)}</div>` : ''}
        ${renderFriendAction(profile.friendship_status, userId, profile.display_name)}
        ${isSelf ? '' : `
          <div style="display:flex; gap:8px; justify-content:center; margin-top:18px;">
            <button class="block-user-link" style="margin:0;" onclick="AroundApp.blockUserAction('${userId}')">🚫 Block user</button>
            <button class="block-user-link" style="margin:0;" data-name="${escapeHtml(profile.display_name || '')}" onclick="AroundApp.openReportTarget('user','${userId}', this.dataset.name)">🚩 Report</button>
          </div>`}
      </div>`;
  } catch (err) {
    document.getElementById('userProfileContent').innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">This profile isn't available.</div></div>`;
    handleApiError(err);
  }
}

async function blockUserAction(userId){
  if (blockedInDemo()) return;
  // Read the already-escaped, already-rendered name back out of the DOM
  // (via textContent, not re-injected) rather than threading a
  // user-controlled display name through an inline onclick="" string,
  // which would be an HTML-attribute-escaping trap for any name
  // containing a quote character.
  const nameEl = document.querySelector('#userProfileContent .profile-name');
  const displayName = nameEl ? nameEl.textContent : 'this person';
  if (!confirm(`Block ${displayName}? They won't be able to see your profile, and you won't see each other in Discover or I'm Free.`)) return;
  try {
    await UsersApi.blockUser(userId);
    toast(`Blocked ${displayName}`);
    closeUserProfile();
    // Blocked users are excluded server-side from /discovery/people and
    // /im-free/nearby — refresh whichever of those lists is currently
    // loaded so the person disappears immediately instead of lingering
    // until the next natural refresh.
    if (state.currentScreen === 'discover') renderPeopleNearby();
  } catch (err) { handleApiError(err); }
}

/**
 * Add Friend / Pending / Friends action for the profile sheet (spec:
 * "opening another person's profile only shows picture and name" —
 * this is the missing wiring). Reuses the same Friendship rows
 * friends.py and events.py already treat as the single source of
 * truth (via /users/{id}'s friendship_status field) — not a second
 * friendship system. Blocked users never reach this code at all:
 * GET /users/{id} 404s for them before friendship_status is computed.
 */
function renderFriendAction(status, userId, displayName){
  if (status === 'self' || !userId) return '';
  if (status === 'accepted') return `
    <div class="friend-action-row">
      <div class="friend-status-pill" style="flex:1;">✓ Friends</div>
      <button class="next-btn" style="flex:1; margin-top:0;" data-name="${escapeHtml(displayName || '')}" onclick="AroundApp.openDmThread('${userId}', this.dataset.name)">💬 Message</button>
    </div>`;
  if (status === 'pending_sent') return `<div class="friend-status-pill">Request sent</div>`;
  if (status === 'pending_received') return `
    <div class="friend-action-row">
      <button class="next-btn" style="margin-top:0;" onclick="AroundApp.respondFriendRequest('${userId}', true)">Accept</button>
      <button class="friend-decline-btn" onclick="AroundApp.respondFriendRequest('${userId}', false)">Decline</button>
    </div>`;
  return `<button class="next-btn" style="margin-top:14px;" onclick="AroundApp.sendFriendRequest('${userId}')">+ Add Friend</button>`;
}

async function sendFriendRequest(userId){
  try {
    await FriendsApi.sendRequest(userId);
    toast('Friend request sent');
    openUserProfile(userId, state.profileSourceEventId);
  } catch (err) { handleApiError(err); }
}

async function respondFriendRequest(userId, accept){
  try {
    if (accept) { await FriendsApi.acceptRequest(userId); toast('Friend request accepted'); }
    else { await FriendsApi.rejectRequest(userId); toast('Request declined'); }
    openUserProfile(userId, state.profileSourceEventId);
  } catch (err) { handleApiError(err); }
}
function closeUserProfile(){
  const eventId = state.profileSourceEventId;
  state.profileSourceEventId = null;
  if (eventId) openEvent(eventId);
  else closeAllSheets();
}

/* ============================================================
   EVENT DETAIL
   ============================================================ */
async function openEvent(id){
  state.currentEventId=id;
  document.getElementById('eventDetailContent').innerHTML = `<div class="empty-mini" style="padding:30px 0; text-align:center;">Loading\u2026</div>`;
  openSheet('sheetEvent');

  if (demoMode) {
    // Demo events are already fully detail-shaped (participants, chat,
    // myStatus, etc. — see DEMO_EVENTS) and there's no backend to ask,
    // so render straight from the cache instead of calling the API.
    // Chat is intentionally NOT connected to a WebSocket here — it's
    // sample history only, matching the demo banner's promise of
    // read-only sample data (spec: "don't make map/API requests fail
    // noisily" in demo mode extends to chat too).
    const ev = eventsCache.get(id);
    if (ev) renderEventDetail(ev);
    else document.getElementById('eventDetailContent').innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">Sample event not found.</div></div>`;
    return;
  }

  try {
    const detail = await EventsApi.getEvent(id);
    const merged = mergeDetail(eventsCache.get(id), detail);
    eventsCache.set(id, merged);
    renderEventDetail(merged);
    if (isAttending(merged) && merged.chatEnabled) {
      loadChat(id);
      connectChatSocket(id);
    }
  } catch (err) {
    document.getElementById('eventDetailContent').innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">Couldn't load this event.</div></div>`;
    handleApiError(err);
  }
}

function renderEventDetail(ev){
  const m=timeMeta(ev);
  const attending=isAttending(ev);
  const requested=myRequestPending(ev);
  const wlPos = myWaitlistPosition(ev);
  const wlStatus = myWaitlistStatus(ev);
  const gLimit=guestLimit(ev);
  const acc=ACCESS[ev.accessMode] || ACCESS.public;

  const hostsAndParticipants = (ev.participants||[]).filter(p=>p.type!=='guest');
  const plistHtml = hostsAndParticipants.map(p=>{
    const guests = (ev.participants||[]).filter(g=>g.type==='guest' && g.invited_by===p.name);
    const rowMain = p.user_id
      ? `<button class="pchip person-link" onclick="AroundApp.openUserProfile('${p.user_id}', '${ev.id}')"><div class="a" style="background:${avColor(p.name)}30; color:${avColor(p.name)}">${initials(p.name)}</div><span class="n">${p.name}${p.type==='host'?' \u00b7 Host':''}</span></button>`
      : `<div class="pchip"><div class="a" style="background:${avColor(p.name)}30; color:${avColor(p.name)}">${initials(p.name)}</div><span class="n">${p.name}</span></div>`;
    const guestRows = guests.map(g=>`<div class="pchip guest-chip"><div class="a" style="background:${avColor(g.name)}30; color:${avColor(g.name)}">${initials(g.name)}</div><span class="n">+ ${g.name} — Guest</span></div>`).join('');
    return rowMain + guestRows;
  }).join('');

  const cancelled = ev.status === 'cancelled';
  let actionHtml = '';
  if (cancelled) {
    actionHtml = `<div class="full-banner">🔴 This event was cancelled by the host${attending?' \u2014 you were going':''}</div>`;
  } else if(attending){
    const mine = myParticipantEntry(ev);
    const checkinBlock = (mine && !mine.checked_in && !isHost(ev)) ? `
      <div class="checkin-self-row">
        <input id="checkinCodeInput" placeholder="Check-in code from the host"/>
        <button onclick="AroundApp.selfCheckIn('${ev.id}', this)">Check in</button>
        <button class="qr-scan-btn" onclick="AroundApp.openQrScanner('${ev.id}')" title="Scan QR code">📷</button>
      </div>` : (mine && mine.checked_in ? `<div class="checkin-confirmed">✅ You're checked in</div>` : '');
    actionHtml = `<div class="join-cta">
        <button class="join-btn going" id="leaveBtn" onclick="AroundApp.leaveEventFlow('${ev.id}', this)">✓ ${isHost(ev)?'HOSTING':'GOING'}${myGuests(ev).length?' + '+myGuests(ev).length+' guest'+(myGuests(ev).length>1?'s':''):''}</button>
        ${checkinBlock}
      </div>`;
  } else if(wlPos){
    if(wlStatus==='offered'){
      actionHtml = `<div class="waitlist-banner">
          <div class="t">🎉 A spot opened up for you!</div>
          <div class="s">Claim it before the offer expires or it goes to the next person in line.</div>
        </div>
        <div class="join-cta" style="display:flex; gap:8px;">
          <button class="join-btn" style="flex:1;" onclick="AroundApp.claimWaitlistOffer('${ev.id}', this)">Claim spot</button>
          <button class="join-btn going" style="flex:1;" onclick="AroundApp.leaveWaitlist('${ev.id}', this)">Pass</button>
        </div>`;
    } else {
      actionHtml = `<div class="waitlist-banner">
          <div class="t">You're #${wlPos} on the waitlist</div>
          <div class="s">We'll offer you the next open spot in order, with a limited time to claim it.</div>
        </div>
        <div class="join-cta"><button class="join-btn going" onclick="AroundApp.leaveWaitlist('${ev.id}', this)">Leave waitlist</button></div>`;
    }
  } else if(m.full){
    actionHtml = `<div class="full-banner">🔴 Event Full</div>
      <div class="join-cta"><button class="join-btn" onclick="AroundApp.joinWaitlist('${ev.id}', this)">Join Waitlist</button></div>`;
  } else if(ev.accessMode==='approval'){
    actionHtml = requested
      ? `<div class="join-cta"><button class="request-pending-btn" onclick="AroundApp.cancelRequestFlow('${ev.id}', this)">Request pending \u2014 tap to cancel</button></div>`
      : `<div class="join-cta"><button class="join-btn" onclick="AroundApp.requestToJoinFlow('${ev.id}', this)">Request to Join</button></div>`;
  } else if(ev.accessMode==='invite_only'){
    actionHtml = `<div class="field-label" style="margin-top:20px;">Have an invite code?</div>
      <div class="code-row"><input id="inviteCodeInput" placeholder="Invite code"/><button onclick="AroundApp.redeemInviteCode('${ev.id}', this)">Unlock</button></div>`;
  } else {
    actionHtml = `<div class="join-cta"><button class="join-btn" onclick="AroundApp.joinEventFlow('${ev.id}', this)">JOIN</button></div>`;
  }

  let guestBlockHtml = '';
  if(attending && ev.guestPolicy!=='none'){
    const guests = myGuests(ev);
    const guestRows = guests.map(g=>`<div class="guest-line"><div class="g1">+ ${g.name} — your guest</div><button onclick="AroundApp.cancelMyGuest('${ev.id}','${g.participant_id}')">Remove</button></div>`).join('');
    const canAdd = canInviteGuest(ev);
    guestBlockHtml = `<div class="ed-participants">
        <div class="section-label" style="padding:0; display:flex; justify-content:space-between;"><span>Your guests</span><span>${guests.length}/${gLimit}</span></div>
        ${guestRows}
        ${canAdd? `<div class="guest-input-row"><input id="guestNameInput" placeholder="Guest's name"/><button onclick="AroundApp.inviteGuestSubmit('${ev.id}', this)">Invite Guest</button></div>` : ''}
      </div>`;
  }

  document.getElementById('eventDetailContent').innerHTML = `
    <div class="ed-cover">${CATS[ev.cat]?.e||'📍'}</div>
    <div class="ed-title">${ev.title}</div>
    <div class="ed-cat">${CATS[ev.cat]?.label||ev.cat} \u00b7 hosted by ${ev.host||'someone'}</div>
    <div class="badge-row">
      <span class="badge-pill">${acc.icon} ${acc.label}</span>
      <span class="badge-pill">👥 ${(GUEST_POLICY[ev.guestPolicy]||GUEST_POLICY.none).label}</span>
      ${ev.locationReveal && ev.locationReveal!=='immediate'? `<span class="badge-pill">📍 ${REVEAL[ev.locationReveal]||''}</span>`:''}
    </div>
    <div class="capacity-bar-wrap">
      <div class="capacity-label"><span>Capacity</span><b>${occupancy(ev)} / ${ev.capacity}</b></div>
      <div class="capacity-bar"><div class="fill ${m.full?'full':''}" style="width:${Math.min(100,occupancy(ev)/ev.capacity*100)}%"></div></div>
    </div>
    <div class="ed-meta-grid">
      <div class="ed-meta-box"><div class="l">When</div><div class="v">${ev.dateLabel} \u00b7 ${ev.timeLabel}</div></div>
      <div class="ed-meta-box"><div class="l">Distance</div><div class="v">${ev.distKm!=null?ev.distKm+' km \u00b7 ':''}starts ${m.t}</div></div>
      <div class="ed-meta-box" style="grid-column:1/-1;"><div class="l">Location</div><div class="v">📍 ${ev.loc||'\u2014'}</div></div>
    </div>
    <div class="ed-desc">${ev.desc||''}</div>
    ${guestBlockHtml}
    <div class="ed-participants">
      <div class="section-label" style="padding:0;">Going</div>
      <div class="ed-plist">${plistHtml || '<div class="empty-mini">No one yet — be the first.</div>'}</div>
    </div>
    ${isHost(ev)? `<button class="manage-link" onclick="AroundApp.openManage('${ev.id}')">🛠 Manage event ${(ev.pendingRequests||[]).length?`\u00b7 ${ev.pendingRequests.length} request${ev.pendingRequests.length>1?'s':''}`:''}</button>`:''}
    <div class="ed-chat">
      <div class="section-label" style="padding:0; display:flex; align-items:center; justify-content:space-between;">
        <span>Chat</span>
        <span id="chatConnBadge" class="chat-conn-badge" style="display:none;"></span>
      </div>
      <div id="chatMsgs" style="margin-top:10px;">${attending ? '' : '<div class="empty-mini">Join to see and send messages.</div>'}</div>
      <div class="chat-input-row">
        <input id="chatInput" placeholder="${cancelled?'Chat is read-only \u2014 event cancelled':attending?'Message the group\u2026':'Join to chat'}" ${(attending && !cancelled)?'':'disabled'} onkeydown="if(event.key==='Enter')AroundApp.sendChat()"/>
        <button onclick="AroundApp.sendChat()" ${(attending && !cancelled)?'':'style="opacity:0.4;pointer-events:none;"'}>➤</button>
      </div>
    </div>
    ${actionHtml}
    ${isHost(ev) ? '' : `<button class="block-user-link" data-title="${escapeHtml(ev.title || '')}" onclick="AroundApp.openReportTarget('event','${ev.id}', this.dataset.title)">🚩 Report event</button>`}
  `;
}

function chatMsgHtml(c){
  const name = c.user_name || c.u || 'Someone';
  const body = c.body || c.m || '';
  return `<div class="chat-msg"><div class="a" style="background:${avColor(name)}30; color:${avColor(name)}">${initials(name)}</div>
    <div class="b"><span class="u">${name}</span>${body}</div></div>`;
}

async function loadChat(eventId){
  try {
    const msgs = await EventsApi.getChatMessages(eventId);
    const ev = eventsCache.get(eventId); if (ev) ev.chat = msgs;
    const el = document.getElementById('chatMsgs');
    if (el && state.currentEventId === eventId) el.innerHTML = msgs.map(chatMsgHtml).join('') || '<div class="empty-mini">No messages yet — say hi.</div>';
  } catch (err) { console.error(err); }
}

async function connectChatSocket(eventId){
  if (chatSocket) { chatSocket.close(); chatSocket = null; }
  try {
    chatSocket = await EventsApi.openChatSocket(eventId, {
      onMessage: (msg) => {
        if (state.currentEventId !== eventId) return; // sheet moved on — drop it
        const ev = eventsCache.get(eventId); if (ev) { ev.chat = ev.chat || []; ev.chat.push(msg); }
        const el = document.getElementById('chatMsgs');
        if (el) el.innerHTML += chatMsgHtml(msg);
      },
      onError: () => { /* silent — HTTP polling via loadChat() on open still works */ },
      onStatus: (statusName) => {
        if (state.currentEventId !== eventId) return;
        const badge = document.getElementById('chatConnBadge');
        if (!badge) return;
        if (statusName === 'connected') badge.style.display = 'none';
        else if (statusName === 'reconnecting') { badge.style.display = ''; badge.textContent = 'Reconnecting…'; }
        else if (statusName === 'closed') { badge.style.display = ''; badge.textContent = 'Chat disconnected — reopen this event to retry'; }
      },
    });
  } catch (err) { console.error(err); }
}

async function sendChat(){
  if (blockedInDemo()) return;
  const ev = eventsCache.get(state.currentEventId); if (!ev) return;
  const inp=document.getElementById('chatInput'); const val=inp.value.trim();
  if(!val || !isAttending(ev)) return;
  inp.value='';
  try {
    const msg = await EventsApi.sendChatMessage(ev.id, val);
    // the WS broadcast will also deliver this back to us; render immediately anyway in case the socket isn't connected
    const el = document.getElementById('chatMsgs');
    if (el && !(ev.chat||[]).some(m=>m.id===msg.id)) { ev.chat = ev.chat||[]; ev.chat.push(msg); el.innerHTML += chatMsgHtml(msg); }
  } catch (err) { handleApiError(err, "Couldn't send that message"); inp.value = val; }
}

/* ============================================================
   JOIN / LEAVE / REQUEST / WAITLIST / GUEST — all server-authoritative.
   Every one of these calls the API, then re-fetches the event detail
   so what's on screen always matches what the database says — no
   optimistic capacity mutation anywhere here (spec §22).
   ============================================================ */
async function refreshEventEverywhere(id){
  try {
    const detail = await EventsApi.getEvent(id);
    const merged = mergeDetail(eventsCache.get(id), detail);
    eventsCache.set(id, merged);
    if (state.currentEventId === id && state.activeSheet === 'sheetEvent') renderEventDetail(merged);
    if (state.view==='map') renderMap(); else if (document.getElementById('feedView').style.display!=='none') renderFeed();
    await loadProfileStats();
    renderProfile();
  } catch (err) { console.error(err); }
}

async function joinEventFlow(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try { await EventsApi.joinEvent(id); toast('You joined \ud83c\udf89'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err, "Couldn't join this event"); await refreshEventEverywhere(id); }
  });
}
async function leaveEventFlow(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try { await EventsApi.leaveEvent(id); toast('You left the event'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err); }
  });
}
async function requestToJoinFlow(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try { await EventsApi.requestToJoin(id); toast('Request sent \u2014 the host will review it'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err, "Couldn't send your request"); }
  });
}
async function cancelRequestFlow(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try { await EventsApi.cancelMyJoinRequest(id); toast('Request cancelled'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err, "Couldn't cancel your request"); }
  });
}
async function redeemInviteCode(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    const code = document.getElementById('inviteCodeInput').value.trim();
    if (!code) { toast('Enter a code first'); return; }
    try { await EventsApi.joinEventWithCode(id, code); toast('Code accepted \u2014 you\u2019re in \ud83c\udf89'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err, 'Invalid code'); }
  });
}
async function joinWaitlist(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try { const res = await EventsApi.joinWaitlist(id); toast(`Added to the waitlist \u2014 you're #${res.position}`); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err); }
  });
}
async function leaveWaitlist(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try { await EventsApi.leaveWaitlist(id); toast('Left the waitlist'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err); }
  });
}
async function claimWaitlistOffer(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try { await EventsApi.claimWaitlistSpot(id); toast('Spot claimed \u2014 you\u2019re going! \ud83c\udf89'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err, 'That offer is no longer available'); await refreshEventEverywhere(id); }
  });
}
async function inviteGuestSubmit(id, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    const input = document.getElementById('guestNameInput');
    const name = (input?.value||'').trim();
    try { const res = await EventsApi.inviteGuest(id, name); toast(res.token ? `Invite sent \u2014 code ${res.token}` : 'Guest invited'); await refreshEventEverywhere(id); }
    catch (err) { handleApiError(err, "Couldn't invite a guest"); }
  });
}
async function cancelMyGuest(id, guestParticipantId){
  if (blockedInDemo()) return;
  try { await EventsApi.cancelGuest(id, guestParticipantId); toast('Invite cancelled'); await refreshEventEverywhere(id); }
  catch (err) { handleApiError(err); }
}

/* ============================================================
   HOST MANAGEMENT
   ============================================================ */
async function openManage(id){
  state.currentEventId=id;
  document.getElementById('manageContent').innerHTML = `<div class="empty-mini" style="padding:30px 0; text-align:center;">Loading\u2026</div>`;
  openSheet('sheetManage');
  await renderManage(id);
}
async function renderManage(id){
  try {
    const [detail, attendance] = await Promise.all([EventsApi.getEvent(id), EventsApi.getAttendance(id)]);
    const merged = mergeDetail(eventsCache.get(id), detail);
    eventsCache.set(id, merged);

    const reqRows = attendance.pending_requests.length ? attendance.pending_requests.map(r=>`
      <div class="attendee-row">
        <div class="a" style="background:${avColor(r.user_id)}30; color:${avColor(r.user_id)}">${initials(merged.pendingRequests.find(p=>p.id===r.id)?.name || '?')}</div>
        <div class="info"><div class="nm">${merged.pendingRequests.find(p=>p.id===r.id)?.name || 'Someone'}</div><div class="sub">Wants to join</div></div>
        <div class="attendee-actions">
          <button class="mini-btn accept" onclick="AroundApp.approveRequest('${id}','${r.id}')">Accept</button>
          <button class="mini-btn reject" onclick="AroundApp.rejectRequest('${id}','${r.id}')">Decline</button>
        </div>
      </div>`).join('') : `<div class="empty-mini">No pending requests.</div>`;

    const partRows = attendance.participants.map(p=>`
      <div class="attendee-row">
        <div class="a" style="background:${avColor(p.user_id)}30; color:${avColor(p.user_id)}">${initials(merged.participants.find(x=>x.participant_id===p.id)?.name || '?')}</div>
        <div class="info"><div class="nm">${merged.participants.find(x=>x.participant_id===p.id)?.name || 'Someone'} ${p.type==='host'?'<span class="role-tag host">Host</span>':''}</div><div class="sub">${p.checked_in?'✅ Checked in':'Not checked in'}</div></div>
        <div class="attendee-actions">
          <div class="checkin-dot ${p.checked_in?'done':''}" onclick="AroundApp.toggleCheckIn('${id}','${p.id}')">${p.checked_in?'\u2713':''}</div>
          ${p.type!=='host'? `<button class="mini-btn danger" data-confirm="0" onclick="AroundApp.confirmDangerClick(this,'${id}','${p.id}',false)">Remove</button>` : ''}
        </div>
      </div>`).join('') || `<div class="empty-mini">No participants yet.</div>`;

    const guestRows = attendance.guests.map(g=>`
      <div class="attendee-row">
        <div class="a" style="background:${avColor(g.name||'?')}30; color:${avColor(g.name||'?')}">${initials(g.name||'?')}</div>
        <div class="info"><div class="nm">${g.name||'Guest'} <span class="role-tag guest">Guest</span></div><div class="sub">${g.checked_in?'✅ Checked in':'Not checked in'}</div></div>
        <div class="attendee-actions">
          <div class="checkin-dot ${g.checked_in?'done':''}" onclick="AroundApp.toggleCheckIn('${id}','${g.id}')">${g.checked_in?'\u2713':''}</div>
          <button class="mini-btn danger" data-confirm="0" onclick="AroundApp.confirmDangerClick(this,'${id}','${g.id}',false)">Remove</button>
        </div>
      </div>`).join('') || `<div class="empty-mini">No guests yet.</div>`;

    const waitRows = attendance.waitlist.length ? attendance.waitlist.map((w,i)=>`
      <div class="attendee-row">
        <div class="a" style="background:${avColor(w.user_id)}30; color:${avColor(w.user_id)}">${initials('?')}</div>
        <div class="info"><div class="nm">Waitlisted user</div><div class="sub">${w.status==='offered'?'Offered \u2014 claim window open':`#${i+1} in line`}</div></div>
      </div>`).join('') : `<div class="empty-mini">Nobody's waiting.</div>`;

    document.getElementById('manageContent').innerHTML = `
      ${merged.status==='cancelled' ? `<div class="full-banner" style="margin-bottom:14px;">🔴 This event is cancelled</div>` : `
      <div class="manage-edit-row">
        <button class="mini-btn" onclick="AroundApp.openEditEvent('${id}')">\u270F\uFE0F Edit event</button>
        <button class="mini-btn danger" data-confirm="0" onclick="AroundApp.confirmCancelEventClick(this,'${id}')">Cancel event</button>
      </div>`}
      <div class="capacity-bar-wrap">
        <div class="capacity-label"><span>Capacity</span><b>${attendance.occupancy} / ${attendance.capacity}</b></div>
        <div class="capacity-bar"><div class="fill ${attendance.occupancy>=attendance.capacity?'full':''}" style="width:${Math.min(100,attendance.occupancy/attendance.capacity*100)}%"></div></div>
      </div>
      <div class="manage-section"><div class="manage-section-title"><span>Join Requests</span><span>${attendance.pending_requests.length}</span></div>${reqRows}</div>
      <div class="manage-section">
        <div class="manage-section-title"><span>Check-in</span><span>${attendance.checked_in} in \u00b7 ${attendance.not_checked_in} not</span></div>
        <div class="checkin-summary"><div class="txt">Attendees enter this code at the door to check in</div><button onclick="AroundApp.toggleQR('${id}')">Show code</button></div>
        <div class="qr-box" id="qrBox"><div class="qr-grid" id="qrGridInner"></div><div class="qr-caption" id="qrCaptionText">Generating\u2026</div><div class="qr-manual-code" id="qrManualCode" hidden><code id="qrTokenText"></code><button class="qr-copy-btn" onclick="AroundApp.copyCheckinToken()" title="Copy manual check-in code">Copy</button></div></div>
      </div>
      <div class="manage-section"><div class="manage-section-title"><span>Participants</span><span>${attendance.participants.length}</span></div>${partRows}</div>
      <div class="manage-section"><div class="manage-section-title"><span>Guests</span><span>${attendance.guests.length}</span></div>${guestRows}</div>
      <div class="manage-section"><div class="manage-section-title"><span>Waitlist</span><span>${attendance.waitlist.length}</span></div>${waitRows}</div>
    `;
  } catch (err) {
    document.getElementById('manageContent').innerHTML = `<div class="empty-state"><div class="e">⚠️</div><div class="t">Couldn't load attendee management.</div></div>`;
    handleApiError(err);
  }
}
function qrCells(seedStr){
  let s=0; for(const c of seedStr) s+=c.charCodeAt(0); s = s*9301+49297;
  let out='';
  for(let i=0;i<144;i++){ s=(s*9301+49297)%233280; out += (s/233280>0.5)? '<div></div>' : '<div style="background:#fff"></div>'; }
  return out;
}
async function selfCheckIn(eventId, btn){
  if (blockedInDemo()) return;
  const code = document.getElementById('checkinCodeInput').value.trim();
  if (!code) { toast('Enter the code the host gave you'); return; }
  await withBusy(btn, async () => {
    try {
      const res = await EventsApi.checkInWithToken(eventId, code);
      toast(res.status === 'already_checked_in' ? "You're already checked in" : "Checked in \u2705");
      await refreshEventEverywhere(eventId);
    } catch (err) { handleApiError(err, "That code didn't work"); }
  });
}
/* ============================================================
   QR CAMERA SCANNER (check-in)

   Reuses the exact same validation path as manual entry — decoding a
   QR code here only ever writes the decoded text into #checkinCodeInput
   and calls the existing selfCheckIn(), which posts to the same
   /events/{id}/check-in endpoint that validates the signed, expiring,
   event-scoped token server-side. Nothing about that validation is
   duplicated or weakened here; this is purely an alternate way to fill
   in the same code a person could otherwise type by hand — manual entry
   stays fully functional if the camera is unavailable or denied.

   Uses jsQR (loaded via CDN in index.html) rather than the newer native
   BarcodeDetector API because BarcodeDetector isn't available in Safari/
   iOS — a dealbreaker for a consumer app where a large share of users
   are on iPhones. jsQR is a small, dependency-free, pure-JS decoder that
   works on any browser with getUserMedia + <canvas>.
   ============================================================ */
let qrScannerStream = null;
let qrScannerRafId = null;
let qrScannerCanvas = null;

async function openQrScanner(eventId){
  if (blockedInDemo()) return;
  if (typeof jsQR === 'undefined') {
    toast("Camera scanning isn't available right now — use the code field instead");
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("This browser can't access the camera — use the code field instead");
    return;
  }

  const overlay = document.getElementById('qrScannerOverlay');
  const video = document.getElementById('qrScannerVideo');
  const status = document.getElementById('qrScannerStatus');
  status.textContent = 'Point your camera at the host’s QR code';
  overlay.classList.add('show');

  try {
    qrScannerStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
  } catch (err) {
    overlay.classList.remove('show');
    if (err && (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError')) {
      toast('Camera access denied — use the code field instead');
    } else if (err && err.name === 'NotFoundError') {
      toast('No camera found — use the code field instead');
    } else {
      toast("Couldn't start the camera — use the code field instead");
    }
    return;
  }

  video.srcObject = qrScannerStream;
  await video.play();

  if (!qrScannerCanvas) qrScannerCanvas = document.createElement('canvas');
  const canvas = qrScannerCanvas;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });

  const scanFrame = () => {
    if (!qrScannerStream) return; // scanner was closed
    if (video.readyState === video.HAVE_ENOUGH_DATA) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const code = jsQR(imageData.data, imageData.width, imageData.height, { inversionAttempts: 'dontInvert' });
      if (code && code.data) {
        const token = code.data.trim();
        closeQrScanner();
        const input = document.getElementById('checkinCodeInput');
        if (input) {
          input.value = token;
          selfCheckIn(eventId, document.querySelector('.checkin-self-row button'));
        }
        return;
      }
    }
    qrScannerRafId = requestAnimationFrame(scanFrame);
  };
  qrScannerRafId = requestAnimationFrame(scanFrame);
}

function closeQrScanner(){
  const overlay = document.getElementById('qrScannerOverlay');
  overlay.classList.remove('show');
  if (qrScannerRafId) { cancelAnimationFrame(qrScannerRafId); qrScannerRafId = null; }
  if (qrScannerStream) { qrScannerStream.getTracks().forEach(t => t.stop()); qrScannerStream = null; }
  const video = document.getElementById('qrScannerVideo');
  if (video) video.srcObject = null;
}

async function toggleQR(eventId){
  const box = document.getElementById('qrBox');
  box.classList.toggle('show');
  if (!box.classList.contains('show')) return;
  if (blockedInDemo()) {
    const caption = document.getElementById('qrCaptionText');
    document.getElementById('qrGridInner').innerHTML = qrCells(eventId);
    caption.textContent = 'Demo mode \u2014 not a real check-in code';
    document.getElementById('qrManualCode').hidden = true;
    return;
  }
  await refreshCheckinQr(eventId);
}
async function refreshCheckinQr(eventId){
  const grid = document.getElementById('qrGridInner');
  const caption = document.getElementById('qrCaptionText');
  grid.innerHTML = qrCells(eventId); // decorative placeholder shown instantly while the real token loads
  caption.textContent = 'Generating\u2026';
  try {
    const res = await EventsApi.createCheckinToken(eventId);
    renderRealQr(grid, res.token);
    const expiry = new Date(res.expires_at).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
    caption.innerHTML = `Valid until ${expiry} \u00b7 <button class="qr-refresh-btn" onclick="event.stopPropagation(); AroundApp.refreshCheckinQr('${eventId}')">Refresh</button>`;
    const box = document.getElementById('qrBox');
    box.dataset.token = res.token;
    document.getElementById('qrTokenText').textContent = res.token;
    document.getElementById('qrManualCode').hidden = false;
  } catch (err) {
    document.getElementById('qrManualCode').hidden = true;
    caption.textContent = "Couldn't generate a check-in code";
    handleApiError(err);
  }
}
async function copyCheckinToken(){
  const token = document.getElementById('qrBox')?.dataset.token;
  if (!token) return;
  try {
    await navigator.clipboard.writeText(token);
    toast('Code copied');
  } catch (_) {
    const code = document.getElementById('qrTokenText');
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(code);
    selection.removeAllRanges();
    selection.addRange(range);
    toast('Select and copy the code');
  }
}
/**
 * Renders an actual scannable QR code encoding the real check-in token
 * — this is the fix for the previous decorative-only checkerboard.
 * Uses the qrcode-generator library (loaded via CDN in index.html);
 * falls back to the decorative pattern + a plain-text code if the
 * library failed to load (e.g. no internet reaching the CDN), so the
 * manual copy-paste check-in flow still works either way.
 */
function renderRealQr(container, token){
  if (typeof qrcode === 'undefined') {
    container.innerHTML = qrCells(token); // library unavailable — decorative fallback, manual entry still works
    return;
  }
  try {
    const qr = qrcode(0, 'M'); // typeNumber 0 = auto-select smallest version for the data length
    qr.addData(token);
    qr.make();
    container.innerHTML = qr.createSvgTag(4, 2);
  } catch (err) {
    container.innerHTML = qrCells(token);
  }
}

async function approveRequest(eventId, requestId){
  if (blockedInDemo()) return;
  try { await EventsApi.approveRequest(eventId, requestId); toast('Approved'); await renderManage(eventId); await refreshEventEverywhere(eventId); }
  catch (err) { handleApiError(err, "Couldn't approve — check capacity"); await renderManage(eventId); }
}
async function rejectRequest(eventId, requestId){
  if (blockedInDemo()) return;
  try { await EventsApi.rejectRequest(eventId, requestId); toast('Declined'); await renderManage(eventId); }
  catch (err) { handleApiError(err); }
}
async function toggleCheckIn(eventId, participantId){
  if (blockedInDemo()) return;
  try { await EventsApi.checkIn(eventId, { participantId, method:'manual' }); await renderManage(eventId); }
  catch (err) { handleApiError(err); }
}
function confirmDangerClick(btn, eventId, participantId, ban){
  if(btn.dataset.confirm!=='1'){
    btn.dataset.confirm='1'; btn.textContent='Confirm?'; btn.classList.add('confirming');
    clearTimeout(btn._t); btn._t=setTimeout(()=>{ btn.dataset.confirm='0'; btn.textContent='Remove'; btn.classList.remove('confirming'); }, 3000);
    return;
  }
  clearTimeout(btn._t);
  removeAttendee(eventId, participantId, ban);
}
async function removeAttendee(eventId, participantId, ban){
  if (blockedInDemo()) return;
  try { await EventsApi.removeAttendee(eventId, participantId, { ban }); toast('Removed'); await renderManage(eventId); await refreshEventEverywhere(eventId); }
  catch (err) { handleApiError(err); }
}

/* ============================================================
   SHEETS
   ============================================================ */
function openSheet(id){
  document.getElementById('overlay').classList.add('show');
  document.querySelectorAll('.sheet').forEach(s=>s.classList.remove('show'));
  document.getElementById(id).classList.add('show');
  state.activeSheet=id;
}
function closeAllSheets(){
  document.getElementById('overlay').classList.remove('show');
  document.querySelectorAll('.sheet').forEach(s=>s.classList.remove('show'));
  state.activeSheet=null;
  if (chatSocket) { chatSocket.close(); chatSocket = null; }
  if (dmSocket) { dmSocket.close(); dmSocket = null; currentDmFriendId = null; }
}

/* ============================================================
   CREATE EVENT
   ============================================================ */
function openCreate(){
  state.createStep=1; state.manageAdvancedOpen=false;
  state.createDraft={cat:null,title:'',loc:'',date:'Today',start:'',end:'',capacity:6,accessMode:'public',guestPolicy:'none',locationReveal:'immediate',desc:'',chat:true,coverImageUrl:null,editingEventId:null,dateTimeTouched:false,originalStartsAt:null,originalEndsAt:null};
  renderCreateStep(); openSheet('sheetCreate');
}
/**
 * Host-only edit entry point — reuses the exact same 3-step wizard as
 * create, pre-filled from the event's current values (spec: "use the
 * existing create-event UI patterns rather than creating an entirely
 * separate design"). The tricky part is the date/time step: the
 * wizard only offers three casual date choices (Today/Tomorrow/This
 * weekend), which can't exactly represent an arbitrary future date.
 * Recomputing starts_at/ends_at from those three choices unconditionally
 * would silently shift an event's real date on ANY edit, even one that
 * only touched the title. `dateTimeTouched` guards against that: the
 * original exact timestamps are preserved and sent unchanged unless the
 * host actually interacts with the date/time controls.
 */
async function openEditEvent(id){
  const ev = eventsCache.get(id) || await EventsApi.getEvent(id).then(mergeDetail.bind(null, null)).catch(() => null);
  if (!ev) { toast("Couldn't load this event to edit"); return; }
  state.createStep=1; state.manageAdvancedOpen=false;
  const d = new Date(ev.startsAt);
  const startHHMM = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  let endHHMM = '';
  if (ev.endsAt) { const e=new Date(ev.endsAt); endHHMM = `${String(e.getHours()).padStart(2,'0')}:${String(e.getMinutes()).padStart(2,'0')}`; }
  state.createDraft = {
    cat: ev.cat, title: ev.title, loc: ev.loc, date: ev.dateLabel==='Tomorrow'?'Tomorrow':'Today',
    start: startHHMM, end: endHHMM, capacity: ev.capacity, accessMode: ev.accessMode, guestPolicy: ev.guestPolicy,
    locationReveal: ev.locationReveal || 'immediate', desc: ev.desc || '', chat: ev.chatEnabled !== false,
    coverImageUrl: ev.coverImageUrl || null,
    editingEventId: id, dateTimeTouched: false, originalStartsAt: ev.startsAt, originalEndsAt: ev.endsAt,
  };
  closeAllSheets();
  renderCreateStep(); openSheet('sheetCreate');
}
function setDraftTime(field, value){
  state.createDraft[field] = value;
  state.createDraft.dateTimeTouched = true; // see openEditEvent() docstring — only recompute the real date once the host actually touches it
}
function renderCreateStep(){
  [1,2,3].forEach(i=>document.getElementById('cdot'+i).classList.toggle('active', i<=state.createStep));
  const d=state.createDraft;
  const el=document.getElementById('createContent');
  if(state.createStep===1){
    el.innerHTML = `
      <div class="field-label">What are you doing?</div>
      <div class="cat-grid">${Object.entries(CATS).map(([k,v])=>`<div class="cat-opt ${d.cat===k?'selected':''}" onclick="AroundApp.pickCat('${k}')"><div class="e">${v.e}</div><div class="t">${v.label}</div></div>`).join('')}</div>
      <div class="field-label">Or give it a custom title</div>
      <input class="text-input" id="cTitle" placeholder="e.g. Sunset run by the river" value="${d.title}" oninput="AroundApp.state.createDraft.title=this.value"/>
      <button class="next-btn" onclick="AroundApp.createNext()">Continue</button>
      <div class="back-link" onclick="AroundApp.closeAllSheets(); AroundApp.openPost();" style="cursor:pointer;">Just want to post something quick? →</div>
    `;
  } else if(state.createStep===2){
    el.innerHTML = `
      <div class="field-label">Location</div>
      <input class="text-input" id="cLoc" placeholder="Where's it happening?" value="${d.loc}" oninput="AroundApp.state.createDraft.loc=this.value"/>
      <div class="field-label">Date</div>
      <div class="dist-row">${['Today','Tomorrow','This weekend'].map(x=>`<button class="${d.date===x?'selected':''}" onclick="AroundApp.setDraftField('date','${x}')">${x}</button>`).join('')}</div>
      <div class="field-label">Time</div>
      <div class="field-row2">
        <input class="text-input" placeholder="Start (e.g. 18:00)" value="${d.start}" oninput="AroundApp.setDraftTime('start',this.value)"/>
        <input class="text-input" placeholder="End (e.g. 20:00)" value="${d.end}" oninput="AroundApp.setDraftTime('end',this.value)"/>
      </div>
      <div class="field-label">Capacity — max attendees</div>
      <div class="spots-stepper"><button onclick="AroundApp.stepCapacity(-1)">−</button><span id="capacityVal">${d.capacity}</span><button onclick="AroundApp.stepCapacity(1)">+</button></div>

      <div class="field-label">Who can join?</div>
      <div class="access-list">${Object.entries(ACCESS).map(([k,v])=>`
        <button class="access-opt ${d.accessMode===k?'selected':''}" onclick="AroundApp.setDraftField('accessMode','${k}')">
          <span class="ic">${v.icon}</span><span class="tx"><span class="l">${v.label}</span><span class="s">${v.sub}</span></span>
        </button>`).join('')}</div>

      <div class="field-label">Guests</div>
      <div class="guest-opt-row">${Object.entries(GUEST_POLICY).map(([k,v])=>`<button class="${d.guestPolicy===k?'selected':''}" onclick="AroundApp.setDraftField('guestPolicy','${k}')">${v.label}</button>`).join('')}</div>

      <div class="advanced-toggle" onclick="AroundApp.toggleAdvanced()"><span>📍 Location privacy</span><span>${state.manageAdvancedOpen?'−':'+'}</span></div>
      <div class="advanced-body ${state.manageAdvancedOpen?'open':''}" id="advancedBody">
        <div class="empty-mini" style="padding:8px 2px 0 2px;">Controls when the exact address is revealed.</div>
        <div class="dist-row" style="margin-top:8px;">
          <button class="${d.locationReveal==='after_approval'?'selected':''}" onclick="AroundApp.setDraftField('locationReveal','after_approval')">After approval</button>
          <button class="${d.locationReveal==='confirmed_attendees'?'selected':''}" onclick="AroundApp.setDraftField('locationReveal','confirmed_attendees')">Confirmed only</button>
          <button class="${d.locationReveal==='immediate'?'selected':''}" onclick="AroundApp.setDraftField('locationReveal','immediate')">Immediately</button>
        </div>
      </div>

      <button class="next-btn" onclick="AroundApp.createNext()">Continue</button>
      <div class="back-link" onclick="AroundApp.createBack()">← Back</div>
    `;
  } else {
    el.innerHTML = `
      <div class="field-label">Cover image (optional)</div>
      <div class="cover-upload-row">
        ${d.coverImageUrl ? `<img class="cover-preview" src="${MediaApi.absoluteMediaUrl(d.coverImageUrl)}" alt=""/>` : `<div class="cover-preview cover-preview-empty">${CATS[d.cat]?.e||'📍'}</div>`}
        <div class="cover-upload-actions">
          <input type="file" id="coverFileInput" accept="image/jpeg,image/png,image/webp,image/gif" style="display:none;" onchange="AroundApp.handleCoverFileChosen(this)"/>
          <button class="mini-btn" onclick="document.getElementById('coverFileInput').click()">${d.coverImageUrl?'Change photo':'Add photo'}</button>
          ${d.coverImageUrl ? `<button class="mini-btn danger" onclick="AroundApp.clearCoverImage()">Remove</button>` : ''}
        </div>
      </div>
      <div class="field-label">Description (optional)</div>
      <textarea class="textarea-input" id="cDesc" placeholder="Add any details people should know…" oninput="AroundApp.state.createDraft.desc=this.value">${d.desc}</textarea>
      <div class="field-label">Summary</div>
      <div class="badge-row">
        <span class="badge-pill">${ACCESS[d.accessMode].icon} ${ACCESS[d.accessMode].label}</span>
        <span class="badge-pill">👥 ${GUEST_POLICY[d.guestPolicy].label}</span>
        <span class="badge-pill">${d.capacity} spots</span>
      </div>
      <button class="next-btn" id="publishBtn" onclick="AroundApp.publishEvent(this)">${d.editingEventId?'\u2705 Save changes':'🚀 Publish'}</button>
      <div class="back-link" onclick="AroundApp.createBack()">← Back</div>
      ${d.editingEventId? `<button class="mini-btn danger" data-confirm="0" style="width:100%; margin-top:14px; padding:12px;" onclick="AroundApp.confirmCancelEventClick(this,'${d.editingEventId}')">Cancel this event</button>` : ''}
    `;
  }
}
async function handleCoverFileChosen(input){
  const file = input.files && input.files[0];
  if (!file) return;
  if (blockedInDemo()) { input.value = ''; return; }
  const btn = input.nextElementSibling;
  toast('Uploading photo\u2026');
  try {
    const res = await MediaApi.uploadImage(file);
    state.createDraft.coverImageUrl = res.url;
    renderCreateStep();
    toast('Photo added');
  } catch (err) {
    handleApiError(err, "Couldn't upload that image");
  }
  input.value = '';
}
function clearCoverImage(){ state.createDraft.coverImageUrl = null; renderCreateStep(); }
async function handleAvatarFileChosen(input){
  const file = input.files && input.files[0];
  if (!file) return;
  if (blockedInDemo()) { input.value = ''; return; }
  toast('Uploading photo\u2026');
  try {
    const res = await MediaApi.uploadImage(file);
    await UsersApi.updateMyProfile({ avatar_url: res.url });
    session.user.avatar_url = res.url;
    renderProfile();
    toast('Profile photo updated');
  } catch (err) {
    handleApiError(err, "Couldn't upload that image");
  }
  input.value = '';
}
function pickCat(k){ state.createDraft.cat=k; renderCreateStep(); }
function setDraftField(f,v){ state.createDraft[f]=v; if(f==='date') state.createDraft.dateTimeTouched=true; renderCreateStep(); }
function stepCapacity(d){ state.createDraft.capacity=Math.max(2,Math.min(300,state.createDraft.capacity+d)); document.getElementById('capacityVal').textContent=state.createDraft.capacity; }
function toggleAdvanced(){ state.manageAdvancedOpen=!state.manageAdvancedOpen; renderCreateStep(); }
function createNext(){
  if(state.createStep===1 && !state.createDraft.cat && !state.createDraft.title){ toast('Pick a category or add a title'); return; }
  if(state.createStep===2 && !state.createDraft.loc){ toast('Add a location'); return; }
  state.createStep++; renderCreateStep();
}
function createBack(){ state.createStep--; renderCreateStep(); }

/** Combines the casual "Today/Tomorrow/This weekend" + "HH:MM" picks into a real ISO datetime for the API. */
function draftToIso(dateChoice, timeStr){
  const now = new Date();
  const d = new Date(now);
  if (dateChoice === 'Tomorrow') d.setDate(d.getDate()+1);
  if (dateChoice === 'This weekend') { const addDays = (6 - d.getDay() + 7) % 7 || 6; d.setDate(d.getDate()+addDays); }
  const m = /^(\d{1,2}):(\d{2})$/.exec((timeStr||'').trim());
  if (m) d.setHours(parseInt(m[1],10), parseInt(m[2],10), 0, 0);
  else d.setHours(d.getHours()+2, 0, 0, 0); // no time given — default a couple hours out
  return d.toISOString();
}

async function publishEvent(btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    const d=state.createDraft;
    const title = d.title || (CATS[d.cat]?.label+' Meetup');
    // See openEditEvent()'s docstring: only recompute starts_at/ends_at
    // from the casual date/time picker if the host actually touched it —
    // otherwise the original exact timestamps pass through unchanged,
    // so editing just the title/capacity/etc. can't silently move the date.
    let startsAt, endsAt;
    if (d.editingEventId && !d.dateTimeTouched) {
      startsAt = d.originalStartsAt; endsAt = d.originalEndsAt;
    } else {
      startsAt = draftToIso(d.date, d.start);
      endsAt = null;
      if (d.end) { const e = draftToIso(d.date, d.end); endsAt = e > startsAt ? e : null; }
    }

    if (d.editingEventId) {
      const payload = {
        title, category: d.cat || 'meetup', description: d.desc || '',
        location_label: d.loc, starts_at: startsAt, ends_at: endsAt,
        capacity: d.capacity, access_mode: d.accessMode, guest_policy: d.guestPolicy,
        location_reveal: d.locationReveal, cover_image_url: d.coverImageUrl || undefined,
      };
      try {
        await EventsApi.updateEvent(d.editingEventId, payload);
        closeAllSheets();
        toast('Event updated');
        await refreshEventEverywhere(d.editingEventId);
        openEvent(d.editingEventId);
      } catch (err) {
        handleApiError(err, "Couldn't save those changes");
      }
      return;
    }

    const payload = {
      title, category: d.cat || 'meetup', description: d.desc || '',
      latitude: geo.lat, longitude: geo.lng, location_label: d.loc,
      approx_location_label: d.accessMode!=='public' ? 'Near you' : undefined,
      starts_at: startsAt, ends_at: endsAt,
      capacity: d.capacity, access_mode: d.accessMode, guest_policy: d.guestPolicy,
      location_reveal: d.locationReveal, chat_enabled: true, tags: [], cover_image_url: d.coverImageUrl || undefined,
    };
    try {
      const created = await EventsApi.createEvent(payload);
      eventsCache.set(created.id, adaptCard(created));
      await loadProfileStats();
      closeAllSheets();
      toast("Published — it's live on the map 🎉");
      go('home'); setView('feed');
    } catch (err) {
      handleApiError(err, "Couldn't publish this event");
    }
  });
}

function confirmCancelEventClick(btn, eventId){
  if(btn.dataset.confirm!=='1'){
    btn.dataset.confirm='1'; btn.textContent='Tap again to confirm cancellation'; btn.classList.add('confirming');
    clearTimeout(btn._t); btn._t=setTimeout(()=>{ btn.dataset.confirm='0'; btn.textContent='Cancel this event'; btn.classList.remove('confirming'); }, 4000);
    return;
  }
  clearTimeout(btn._t);
  cancelEventNow(eventId, btn);
}
async function cancelEventNow(eventId, btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try {
      await EventsApi.cancelEvent(eventId);
      closeAllSheets();
      toast('Event cancelled \u2014 attendees have been notified');
      await refreshEventEverywhere(eventId);
      await loadNearbyEvents();
      if (state.view==='map') renderMap(); else renderFeed();
    } catch (err) {
      handleApiError(err, "Couldn't cancel this event");
    }
  });
}

/* ============================================================
   I'M FREE FLOW
   ============================================================ */
let freeDraft={when:'tonight',look:'anything',dist:5};
function openFree(){
  freeDraft={when:'tonight',look:'anything',dist:5};
  imFreeNearby = [];
  imFreeNearbyError = false;
  renderFree(); openSheet('sheetFree');
  loadImFreeNearby();
}
async function loadImFreeNearby(){
  imFreeNearbyLoading = true;
  renderFree();
  try { imFreeNearby = await UsersApi.nearbyImFreePeople(geo.lat, geo.lng); }
  catch (err) { imFreeNearbyError = true; handleApiError(err, "Couldn't load nearby availability"); }
  finally { imFreeNearbyLoading = false; renderFree(); }
}
function renderFree(){
  const whens=['now','tonight','tomorrow','this_weekend'];
  const looks=['party','food','sports','study','coffee','gaming','adventure','anything'];
  document.getElementById('freeContent').innerHTML = `
    <div class="field-label">When?</div>
    <div class="opt-grid">${whens.map(w=>`<div class="opt-chip ${freeDraft.when===w?'selected':''}" onclick="AroundApp.setFree('when','${w}')">${w.replace('_',' ')}</div>`).join('')}</div>
    <div class="field-label">What are you looking for?</div>
    <div class="opt-grid">${looks.map(l=>`<div class="opt-chip ${freeDraft.look===l?'selected':''}" onclick="AroundApp.setFree('look','${l}')">${l}</div>`).join('')}</div>
    <div class="field-label">Distance</div>
    <div class="dist-row">
      ${[1,5,10].map(k=>`<button class="${freeDraft.dist===k?'selected':''}" onclick="AroundApp.setFree('dist',${k})">${k}km</button>`).join('')}
      <button class="${freeDraft.dist==='any'?'selected':''}" onclick="AroundApp.setFree('dist','any')">Anywhere</button>
    </div>
    <div class="section-label" style="padding-left:0; margin-top:20px;">People free near you</div>
    <div style="display:flex; flex-direction:column; gap:8px; margin-top:6px;">
      ${imFreeNearbyLoading ? `<div class="empty-mini">Finding people who are free…</div>` : imFreeNearby.length ? imFreeNearby.map(p=>{
          const avatar = p.avatar_url
            ? `<img src="${MediaApi.absoluteMediaUrl(p.avatar_url)}" alt="" style="width:100%; height:100%; object-fit:cover; border-radius:14px;"/>`
            : '?';
          const name = p.display_name ? escapeHtml(p.display_name) : 'Someone';
          return `<div class="event-card" onclick="AroundApp.closeAllSheets(); AroundApp.openUserProfile('${p.user_id}')">
          <div class="emoji-box" style="background:${avColor(p.user_id)}30; color:${avColor(p.user_id)}; font-weight:800; overflow:hidden;">${avatar}</div>
          <div class="body"><div class="title">${name} \u00b7 free ${(p.when||'').replace('_',' ')}</div>
          <div class="meta">Looking for <b style="color:var(--accent)">${p.looking_for}</b> \u00b7 ${p.distance_km}km away</div></div>
        </div>`;
        }).join('') : imFreeNearbyError ? `<div class="empty-mini">Couldn't load people who are free right now.</div>` : `<div class="empty-mini">No one eligible is free nearby right now. People appear here only after another visible member in your city turns on I'm Free.</div>`}
    </div>
    <button class="next-btn" onclick="AroundApp.activateFree(this)">Go free ${freeDraft.when.replace('_',' ')}</button>
  `;
}
function setFree(f,v){ freeDraft[f]=v; renderFree(); }
async function activateFree(btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    try {
      await UsersApi.activateImFree({ whenWindow: freeDraft.when, lookingFor: freeDraft.look, radiusKm: freeDraft.dist==='any'?null:freeDraft.dist, lat: geo.lat, lng: geo.lng });
      imFreeStatus = { when_window: freeDraft.when, looking_for: freeDraft.look };
      renderImfreeBar(); closeAllSheets();
      toast(`You're free ${freeDraft.when.replace('_',' ')} 🟢`);
    } catch (err) { handleApiError(err); }
  });
}

/* ============================================================
   QUICK POST
   ============================================================ */
function openPost(){
  document.getElementById('postContent').innerHTML = `
    <div class="field-label">What's happening right now?</div>
    <textarea class="textarea-input" id="postText" placeholder="Anyone at Vingis Park? Going to a club tonight? Need 2 more for basketball…"></textarea>
    <div class="field-label">Expires after</div>
    <div class="dist-row">${[30,60,120,240].map(m=>`<button class="${m===60?'selected':''}" onclick="AroundApp.pickExp(this,${m})">${m<60?m+'m':m/60+'h'}</button>`).join('')}</div>
    <button class="next-btn" id="postBtn" onclick="AroundApp.publishPost(this)">Post</button>
  `;
  window._postExp=60;
  openSheet('sheetPost');
}
function pickExp(btn,m){ document.querySelectorAll('#postContent .dist-row button').forEach(b=>b.classList.remove('selected')); btn.classList.add('selected'); window._postExp=m; }
async function publishPost(btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    const txt=document.getElementById('postText').value.trim();
    if(!txt){ toast('Write something first'); return; }
    try {
      await PostsApi.createPost({ body: txt, lat: geo.lat, lng: geo.lng, expiresInMinutes: window._postExp });
      await loadNearbyPosts();
      closeAllSheets(); toast('Posted — visible to people nearby'); go('home'); setView('feed');
    } catch (err) { handleApiError(err, "Couldn't post that"); }
  });
}

/* ============================================================
   REPORT A PROBLEM (app-level feedback — unrelated to reporting a
   specific person/event below, kept separate on purpose: this is
   feedback to Around about the app itself, not a moderation signal
   about another member.

   Previously a static toast with nothing behind it. Reuses the same
   Report model events.py's per-event report endpoint already writes
   to (see backend/app/routers/users.py's report_problem()) rather than
   inventing a separate feedback mechanism.
   ============================================================ */
function openReportProblem(){
  document.getElementById('reportSheetTitle').textContent = 'Report a problem';
  document.getElementById('reportContent').innerHTML = `
    <div class="field-label">What went wrong?</div>
    <textarea class="textarea-input" id="reportText" style="min-height:120px;" placeholder="Describe the problem — what happened, what you expected, and any steps to reproduce it…" maxlength="1000"></textarea>
    <button class="next-btn" id="reportSubmitBtn" onclick="AroundApp.submitReportProblem(this)">Submit report</button>
  `;
  openSheet('sheetReport');
}
async function submitReportProblem(btn){
  if (blockedInDemo()) return;
  await withBusy(btn, async () => {
    const msg = document.getElementById('reportText').value.trim();
    if (!msg) { toast('Describe the problem first'); return; }
    try {
      await UsersApi.reportProblem(msg);
      closeAllSheets();
      toast("Thanks — we've received your report");
    } catch (err) { handleApiError(err, "Couldn't submit that report"); }
  });
}

/* ============================================================
   REPORT A USER / REPORT AN EVENT

   Moderation reports on a specific member or event — separate from
   Report a Problem above (that's app feedback; this goes to
   POST /users/{id}/report or /events/{id}/report for review — see
   backend/app/reports.py). Reuses the same sheetReport sheet/markup,
   just with a target-aware title and submit handler, rather than a
   second sheet duplicating the same layout.
   ============================================================ */
const REPORT_REASONS = ['Harassment or abuse', 'Spam', 'Inappropriate content', 'Fake or misleading', 'Safety concern', 'Other'];
let reportTarget = null; // { type: 'user'|'event', id: string, label: string }

function openReportTarget(type, id, label){
  if (blockedInDemo()) return;
  reportTarget = { type, id, label: label || (type === 'user' ? 'this person' : 'this event') };
  document.getElementById('reportSheetTitle').textContent = type === 'user' ? 'Report user' : 'Report event';
  document.getElementById('reportContent').innerHTML = `
    <div class="field-label">Why are you reporting ${escapeHtml(reportTarget.label)}?</div>
    <div class="dist-row" id="reportReasonRow" style="flex-wrap:wrap;">${REPORT_REASONS.map((r,i)=>`<button class="${i===0?'selected':''}" onclick="AroundApp.pickReportReason(this,'${r}')">${r}</button>`).join('')}</div>
    <div class="field-label" style="margin-top:14px;">Additional details (optional)</div>
    <textarea class="textarea-input" id="reportDetails" style="min-height:90px;" placeholder="Anything else that would help us review this…" maxlength="1000"></textarea>
    <button class="next-btn" id="reportTargetSubmitBtn" onclick="AroundApp.submitReportTarget(this)">Submit report</button>
  `;
  window._reportReason = REPORT_REASONS[0];
  openSheet('sheetReport');
}
function pickReportReason(btn, reason){
  document.querySelectorAll('#reportReasonRow button').forEach(b=>b.classList.remove('selected'));
  btn.classList.add('selected');
  window._reportReason = reason;
}
async function submitReportTarget(btn){
  if (blockedInDemo() || !reportTarget) return;
  await withBusy(btn, async () => {
    const details = document.getElementById('reportDetails').value.trim();
    const reason = window._reportReason || REPORT_REASONS[0];
    try {
      const fn = reportTarget.type === 'user' ? UsersApi.reportUser : EventsApi.reportEvent;
      const res = await fn(reportTarget.id, reason, details);
      closeAllSheets();
      toast(res && res.status === 'already_reported' ? "You've already reported this — we're on it" : 'Thanks — our team will review this');
      reportTarget = null;
    } catch (err) { handleApiError(err, "Couldn't submit that report"); }
  });
}

/* ============================================================
   PUBLIC API — everything the inline onclick="" handlers call
   ============================================================ */
window.AroundApp = {
  state, go, setView, setAuthMode, submitAuth, googleStub, logout, toast, enterDemoMode,
  submitOtpVerification, resendOtp, cancelOtpVerification,
  submitForgotPassword, resendPasswordReset, submitResetPassword,
  endImFree: endImFreeAction, openFree, setFree, activateFree,
  setMapFilter, previewPin, closeMapPreview, openEvent, openUserProfile, closeUserProfile,
  sendFriendRequest, respondFriendRequest, blockUserAction, openReportProblem, submitReportProblem,
  openReportTarget, pickReportReason, submitReportTarget,
  setDiscoverCat, filterTonight, onSearchInput, loadMoreSearchResults,
  markNotifRead, backFromActivity, respondFriendRequestFromNotif,
  openManage, approveRequest, rejectRequest, toggleCheckIn, confirmDangerClick, toggleQR, refreshCheckinQr, copyCheckinToken,
  openSheet, closeAllSheets,
  openCreate, openEditEvent, pickCat, setDraftField, setDraftTime, stepCapacity, toggleAdvanced, createNext, createBack, publishEvent,
  confirmCancelEventClick, cancelEventNow,
  handleCoverFileChosen, clearCoverImage, handleAvatarFileChosen,
  openPost, pickExp, publishPost,
  joinEventFlow, leaveEventFlow, requestToJoinFlow, cancelRequestFlow, redeemInviteCode,
  joinWaitlist, leaveWaitlist, claimWaitlistOffer,
  inviteGuestSubmit, cancelMyGuest, sendChat, selfCheckIn, openQrScanner, closeQrScanner,
  openFriendsList, openEditProfile, toggleInterest, filterInterestPicker, saveEditProfile,
  openDmThread, closeDmThread, sendDmMessage,
  triggerInstallPrompt, dismissInstallBanner,
};
// legacy onclick="renderDiscoverResults()"-style calls in the static
// markup reference a couple of functions directly for readability —
// keep a thin global alias so those keep working without editing the HTML:
window.renderDiscoverResults = renderDiscoverResults;

boot();
