// frontend/sw.js
//
// Deliberately does almost nothing. A registered service worker with a
// fetch handler is what Chrome/Android's install-eligibility check
// looks for — this file exists to satisfy that, not to add offline
// support or caching. Around's data (auth, Discover, notifications,
// profile, chat, I'm Free) must always be live; caching any of it
// would mean showing a signed-in user stale or wrong-account data,
// which is worse than the app simply requiring a network connection.
// Every request is passed straight through to the network, uncached,
// unmodified. If real offline support is ever wanted later, this is
// the file to add a Cache Storage strategy to — deliberately not
// attempted in this first PWA pass.
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
});
