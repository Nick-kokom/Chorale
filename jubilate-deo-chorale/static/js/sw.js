/* JUBILATE DEO CHORALE - tiny service worker for the Members page.
   1. Choir files saved with "Available offline" are served from the browser's cache when there is no internet.
   2. The Members page and the site's styles are kept after each visit, so the page still opens offline.
   It never stores anything by itself: files are saved only when a member turns the switch on (app.js). */
"use strict";
var PAGES = "jdc-pages-v1";
var FILES = "jdc-offline-files";

self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  var path = url.pathname;

  // A choir file: use the saved copy if there is one (the ?v= number makes a replaced file a new copy)
  if (/^\/member-area\/files\/\d+\/download$/.test(path)) {
    event.respondWith(caches.open(FILES).then(function (cache) { return cache.match(request); })
      .then(function (hit) { return hit || fetch(request); }));
    return;
  }
  // Members page, member profiles and the site's own files: network first, saved copy when offline
  if (path === "/member-area/members" || path.indexOf("/member-area/members/") === 0 || path.indexOf("/static/") === 0) {
    event.respondWith(networkFirst(request));
  }
});

function networkFirst(request) {
  return caches.open(PAGES).then(function (cache) {
    return fetch(request).then(function (response) {
      if (response.ok && !response.redirected && response.type === "basic") cache.put(request, response.clone());
      return response;
    }).catch(function () {
      return cache.match(request).then(function (hit) {
        if (hit) return hit;
        if (request.mode === "navigate") {
          return new Response("You are offline, and this page has not been opened on this device yet.",
            { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
        }
        return Response.error();
      });
    });
  });
}