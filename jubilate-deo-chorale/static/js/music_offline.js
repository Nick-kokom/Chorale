/* =====================================================================
   JUBILATE DEO CHORALE - MUSIC LIBRARY (offline page script)
   Lists the files saved on THIS device (from localStorage "jdc.music.offline") and links to them.
   The links are answered by the service worker (music_sw.js) from the saved copies.
   ===================================================================== */
(function () {
  "use strict";
  var list = document.querySelector("[data-off-list]");
  var empty = document.querySelector("[data-off-empty]");
  var state = document.querySelector("[data-off-state]");
  var reconnect = document.querySelector("[data-off-reconnect]");
  if (!list) return;

  function mineAll() {
    // This page has no sign-in, so show every saved file on the device (the device already holds them).
    var items = [];
    try {
      var all = JSON.parse(localStorage.getItem("jdc.music.offline")) || {};
      Object.keys(all).forEach(function (user) {
        Object.keys(all[user]).forEach(function (id) { var it = all[user][id]; it.id = id; items.push(it); });
      });
    } catch (e) { /* nothing saved, or storage blocked */ }
    items.sort(function (a, b) { return String(a.title).localeCompare(String(b.title)); });
    return items;
  }

  function text(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    return node;
  }

  function render() {
    list.textContent = "";
    var items = mineAll();
    empty.hidden = items.length > 0;
    items.forEach(function (item) {
      var row = document.createElement("li");
      row.className = "ml-off__item";
      var body = document.createElement("div");
      body.className = "ml-off__text";
      body.appendChild(text("strong", "", item.title || "Untitled"));
      var meta = [item.category, item.voice, item.composer, item.ext && String(item.ext).toUpperCase(), item.size].filter(Boolean).join(" · ");
      body.appendChild(text("small", "", meta));
      var links = document.createElement("div");
      links.className = "ml-off__links";
      var open = text("a", "btn btn--primary btn--sm", "Open");
      open.href = "/music/files/" + encodeURIComponent(item.id) + "/file";
      var save = text("a", "btn btn--outline btn--sm", "Download");
      save.href = "/music/files/" + encodeURIComponent(item.id) + "/download";
      save.setAttribute("download", "");
      links.appendChild(open); links.appendChild(save);
      row.appendChild(body); row.appendChild(links);
      list.appendChild(row);
    });
  }

  function network() {
    var online = navigator.onLine;
    if (state) {
      state.textContent = online ? "You are back online. The full library is available again."
                                 : "You are offline. These files are saved on this device.";
      state.classList.toggle("is-online", online);
    }
    if (reconnect) reconnect.hidden = false;
  }

  window.addEventListener("online", network);
  window.addEventListener("offline", network);
  render();
  network();
})();