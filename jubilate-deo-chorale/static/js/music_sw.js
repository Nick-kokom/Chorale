/* =====================================================================
   JUBILATE DEO CHORALE - MUSIC LIBRARY service worker (served at /music/sw.js, scope /music/)
   Lets files that were switched to "Available offline" open when there is no network.
   Rules:
     - Only files the person saved are ever kept (music.js puts them in the cache). This script never adds new ones.
     - Online, the server is always asked first, so permission changes apply immediately. If the server says
       403/404 the saved copy is deleted. Offline, the saved copy is used.
     - Pages under /music/ fall back to /music/offline when the network is down.
   ===================================================================== */
var FILES = "jdc-music-files-v1";
var SHELL = "jdc-music-shell-v1";
var SHELL_URLS = ["/music/offline", "/static/css/style.css", "/static/css/music.css", "/static/js/music_offline.js"];
var FILE_RE = /^\/music\/files\/(\d+)\/(file|download)$/;

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(SHELL).then(function (cache) { return cache.addAll(SHELL_URLS); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.filter(function (n) { return n.indexOf("jdc-music-shell-") === 0 && n !== SHELL; })
                              .map(function (n) { return caches.delete(n); }));
    }).then(function () { return self.clients.claim(); })
  );
});

// A saved file answered for a "Range" request (audio and video skip ahead this way)
function sliceRange(response, rangeHeader) {
  var match = /^bytes=(\d*)-(\d*)$/.exec((rangeHeader || "").trim());
  if (!match || (match[1] === "" && match[2] === "")) return Promise.resolve(response);
  return response.arrayBuffer().then(function (buffer) {
    var size = buffer.byteLength, start, end;
    if (match[1] === "") { start = Math.max(size - parseInt(match[2], 10), 0); end = size - 1; }
    else { start = parseInt(match[1], 10); end = match[2] ? Math.min(parseInt(match[2], 10), size - 1) : size - 1; }
    if (start >= size || start > end) return new Response(null, { status: 416, headers: { "Content-Range": "bytes */" + size } });
    var headers = new Headers(response.headers);
    headers.set("Content-Range", "bytes " + start + "-" + end + "/" + size);
    headers.set("Content-Length", String(end - start + 1));
    return new Response(buffer.slice(start, end + 1), { status: 206, headers: headers });
  });
}

function fromCache(request, cacheKey) {
  return caches.open(FILES).then(function (cache) { return cache.match(cacheKey); }).then(function (hit) {
    if (!hit) return Response.error();
    return sliceRange(hit, request.headers.get("Range"));
  });
}

function handleFile(request, id) {
  var key = "/music/files/" + id + "/file";
  return fetch(request).then(function (response) {
    if (response.status === 403 || response.status === 404) {          // no longer allowed / gone: forget the saved copy
      return caches.open(FILES).then(function (cache) { return cache.delete(key); }).then(function () { return response; });
    }
    return response;
  }, function () { return fromCache(request, key); });                   // network down: use the saved copy
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  var file = FILE_RE.exec(url.pathname);
  if (file) { event.respondWith(handleFile(request, file[1])); return; }

  if (url.pathname === "/music/offline" || SHELL_URLS.indexOf(url.pathname) !== -1) {
    event.respondWith(caches.match(request, { ignoreSearch: true }).then(function (hit) { return hit || fetch(request); }));
    return;
  }

  if (request.mode === "navigate" && url.pathname.indexOf("/music/") === 0) {
    event.respondWith(fetch(request).catch(function () { return caches.match("/music/offline"); }));
  }
});