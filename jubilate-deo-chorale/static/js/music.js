/* =====================================================================
   JUBILATE DEO CHORALE - MUSIC LIBRARY (page script)
   Runs only on the library page (the wrapper has data-music). Plain JavaScript, no libraries.

     [DIALOGS]    Upload and preview boxes
     [UPLOAD]     Quick checks before a file is sent (the server checks again)
     [OFFLINE]    "Available offline": keep a copy of a file on THIS device
   Confirm boxes and auto-submitting filters are handled by app.js.
   ===================================================================== */
(function () {
  "use strict";
  var root = document.querySelector("[data-music]");
  if (!root) return;

  var statusLine = document.querySelector("[data-music-status]");
  function say(text, kind) {
    if (!statusLine) return;
    statusLine.textContent = text || "";
    statusLine.className = "ml-status" + (kind ? " is-" + kind : "");
  }

  /* ---------- [DIALOGS] ---------- */
  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  }
  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
  }
  var uploadDialog = document.getElementById("upload-dialog");
  var previewDialog = document.getElementById("preview-dialog");

  document.querySelectorAll("[data-open-upload]").forEach(function (button) {
    button.addEventListener("click", function () { openDialog(uploadDialog); });
  });
  if (uploadDialog && uploadDialog.hasAttribute("data-open")) openDialog(uploadDialog);

  document.querySelectorAll(".ml-dialog").forEach(function (dialog) {
    dialog.querySelectorAll("[data-close]").forEach(function (button) {
      button.addEventListener("click", function () { closeDialog(dialog); });
    });
    dialog.addEventListener("click", function (event) {       // a click on the dark area around the box closes it
      if (event.target === dialog) closeDialog(dialog);
    });
  });

  // Preview: build the right viewer for the kind of file, and empty it again on close (stops audio and video)
  var previewBody = previewDialog && previewDialog.querySelector("[data-preview-body]");
  function buildPreview(kind, url, title) {
    var element;
    if (kind === "image") { element = document.createElement("img"); element.alt = title; }
    else if (kind === "audio") { element = document.createElement("audio"); element.controls = true; element.preload = "metadata"; }
    else if (kind === "video") { element = document.createElement("video"); element.controls = true; element.preload = "metadata"; }
    else { element = document.createElement("iframe"); element.title = title; }   // pdf and text
    element.src = url;
    return element;
  }
  document.querySelectorAll("[data-preview]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!previewDialog || !previewBody) return;
      previewDialog.querySelector("#pv-title").textContent = button.dataset.title || "";
      previewDialog.querySelector("[data-preview-meta]").textContent = button.dataset.meta || "";
      previewDialog.querySelector("[data-preview-download]").href = button.dataset.download || "#";
      previewBody.textContent = "";
      previewBody.appendChild(buildPreview(button.dataset.kind, button.dataset.url, button.dataset.title || ""));
      openDialog(previewDialog);
    });
  });
  if (previewDialog) previewDialog.addEventListener("close", function () { if (previewBody) previewBody.textContent = ""; });

  // The "..." menus: only one open at a time; a click elsewhere or Escape closes them
  function closeMenus(except) {
    document.querySelectorAll("details.ml-menu[open]").forEach(function (menu) { if (menu !== except) menu.removeAttribute("open"); });
  }
  document.addEventListener("click", function (event) { closeMenus(event.target.closest("details.ml-menu")); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape") closeMenus(null); });

  /* ---------- [UPLOAD] ---------- */
  var uploadForm = document.querySelector("form[data-music-upload]");
  if (uploadForm) {
    var fileInput = uploadForm.querySelector('input[type="file"]');
    var titleInput = uploadForm.querySelector('input[name="title"]');
    var maxBytes = parseInt(root.dataset.maxBytes, 10) || 0;
    var maxMb = root.dataset.maxMb || "";
    var fileHint = fileInput && fileInput.closest(".field");

    function fileProblem() {
      var file = fileInput && fileInput.files && fileInput.files[0];
      if (!file) return "";
      if (file.size === 0) return "That file is empty.";
      if (maxBytes && file.size > maxBytes) return "That file is larger than " + maxMb + " MB.";
      return "";
    }
    function showFileProblem(message) {
      if (!fileHint) return;
      var box = fileHint.querySelector(".field__error[data-js]");
      fileHint.classList.toggle("field--error", !!message);
      if (message && !box) {
        box = document.createElement("p");
        box.className = "field__error"; box.setAttribute("data-js", ""); box.setAttribute("role", "alert");
        fileHint.appendChild(box);
      }
      if (box) { if (message) box.textContent = message; else box.remove(); }
    }
    var drop = uploadForm.querySelector("[data-drop]");
    function showChosen() {
      if (!drop) return;
      var file = fileInput.files && fileInput.files[0];
      drop.classList.toggle("has-file", !!file);
      drop.querySelector("[data-drop-name]").textContent = file ? file.name : "Drop a file here, or click to browse";
      var hint = drop.querySelector("[data-drop-hint]");
      if (file) hint.textContent = (file.size / 1048576 >= 0.1 ? (file.size / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(file.size / 1024)) + " KB") + " \u00B7 click to choose a different file";
    }
    if (drop) {
      ["dragenter", "dragover"].forEach(function (name) { drop.addEventListener(name, function () { drop.classList.add("is-over"); }); });
      ["dragleave", "drop"].forEach(function (name) { drop.addEventListener(name, function () { drop.classList.remove("is-over"); }); });
    }
    if (fileInput) {
      fileInput.addEventListener("change", function () {
        var problem = fileProblem();
        showFileProblem(problem);
        var file = fileInput.files && fileInput.files[0];
        if (problem) { fileInput.value = ""; showChosen(); return; }
        showChosen();
        if (file && titleInput && !titleInput.value.trim()) {          // suggest a title from the file name
          titleInput.value = file.name.replace(/\.[^.]+$/, "").replace(/[_]+/g, " ").trim().slice(0, 120);
        }
      });
    }
    uploadForm.addEventListener("submit", function (event) {
      var problem = fileProblem();
      if (problem) { event.preventDefault(); event.stopImmediatePropagation(); showFileProblem(problem); return; }
      var submit = uploadForm.querySelector('button[type="submit"]');
      if (submit && !event.defaultPrevented) setTimeout(function () { submit.disabled = true; submit.textContent = "Uploading..."; }, 0);
    });
  }

  /* ---------- [OFFLINE] ----------
     Each device keeps its own copies, so the list of saved files lives in this browser:
       localStorage "jdc.music.offline" = { "<user id>": { "<file id>": {v, title, ...} } }
     and the bytes live in the Cache API ("jdc-music-files-v1"), under the file's address.
     Signing out clears both (app.js). */
  var MANIFEST_KEY = "jdc.music.offline";
  var CACHE_NAME = "jdc-music-files-v1";
  var userId = String(root.dataset.user || "");
  var toggles = Array.prototype.slice.call(document.querySelectorAll("input[data-offline]"));
  var counter = document.querySelector("[data-offline-count]");
  var supported = !!(window.caches && window.fetch && window.localStorage);
  var index = {};
  try { JSON.parse(document.getElementById("music-index").textContent).forEach(function (item) { index[item.id] = item.v; }); } catch (e) { index = null; }

  function readAll() { try { return JSON.parse(localStorage.getItem(MANIFEST_KEY)) || {}; } catch (e) { return {}; } }
  function readMine() { return readAll()[userId] || {}; }
  function writeMine(mine) {
    try {
      var all = readAll();
      if (Object.keys(mine).length) all[userId] = mine; else delete all[userId];
      localStorage.setItem(MANIFEST_KEY, JSON.stringify(all));
      return true;
    } catch (e) { return false; }
  }
  function refreshCount() { if (counter) counter.textContent = String(Object.keys(readMine()).length); }

  // Cache entries are filed under the plain address (no "?v=" part), so a new version replaces the old one
  // and the service worker can find the file with the address it is asked for.
  function keyOf(url) { return String(url).split("?")[0]; }

  function setBusy(box, busy) {
    var label = box.closest(".ml-toggle");
    if (label) label.classList.toggle("is-busy", busy);
    box.disabled = busy;
  }

  function download(box) {
    var id = box.dataset.id;
    return fetch(box.dataset.url, { credentials: "same-origin", cache: "no-store" }).then(function (response) {
      // A redirect means "please sign in"; a missing header means it was not one of our files.
      if (response.redirected || response.status !== 200 || response.headers.get("X-Music-File") !== id) {
        throw new Error(response.status === 403 ? "You are no longer allowed to open this file." : "The file could not be downloaded.");
      }
      return caches.open(CACHE_NAME).then(function (cache) { return cache.put(keyOf(box.dataset.url), response); });
    });
  }
  function entryFor(box) {
    return { v: parseInt(box.dataset.v, 10) || 1, title: box.dataset.title || "", category: box.dataset.category || "",
             voice: box.dataset.voice || "", composer: box.dataset.composer || "", ext: box.dataset.ext || "",
             size: box.dataset.size || "", url: box.dataset.url, saved: Date.now() };
  }
  function remove(box) {
    var mine = readMine();
    delete mine[box.dataset.id];
    writeMine(mine);
    return caches.open(CACHE_NAME).then(function (cache) { return cache.delete(keyOf(box.dataset.url)); });
  }

  if (!supported || !window.isSecureContext) {
    toggles.forEach(function (box) { box.disabled = true; var l = box.closest(".ml-toggle"); if (l) { l.classList.add("is-off"); l.title = "Offline copies need a secure (https) connection"; } });
    if (toggles.length) say("Offline copies are not available here: they need https (or localhost) and a current browser.");
    return;
  }

  if ("serviceWorker" in navigator && root.dataset.sw) {
    navigator.serviceWorker.register(root.dataset.sw, { scope: "/music/" }).catch(function () {
      say("Saved files will be listed, but opening them without internet is not available in this browser.", "error");
    });
  }

  toggles.forEach(function (box) {
    box.addEventListener("change", function () {
      var wantOn = box.checked;
      setBusy(box, true);
      var work;
      if (wantOn) {
        work = download(box).then(function () {
          var mine = readMine();
          mine[box.dataset.id] = entryFor(box);
          if (!writeMine(mine)) throw new Error("This browser would not remember the saved file.");
          say("“" + box.dataset.title + "” is saved on this device.", "ok");
        });
      } else {
        work = remove(box).then(function () { say("“" + box.dataset.title + "” was removed from this device."); });
      }
      work.catch(function (error) {
        box.checked = !wantOn;
        if (wantOn) { try { caches.open(CACHE_NAME).then(function (c) { c.delete(keyOf(box.dataset.url)); }); } catch (e) {} }
        say(error && error.message ? error.message : "That did not work. Please try again.", "error");
      }).then(function () { setBusy(box, false); refreshCount(); });
    });
  });

  // On every visit: show what this device holds, drop copies that are no longer allowed, refresh changed ones.
  function reconcile() {
    var mine = readMine();
    var byId = {};
    toggles.forEach(function (box) { byId[box.dataset.id] = box; });
    caches.open(CACHE_NAME).then(function (cache) {
      var ids = Object.keys(mine);
      return Promise.all(ids.map(function (id) {
        var item = mine[id];
        return cache.match(keyOf(item.url)).then(function (hit) {
          if (!hit) { delete mine[id]; return; }                                  // the browser cleared its cache
          if (!navigator.onLine || index === null) return;
          if (!(id in index)) {                                                    // archived, or no longer allowed
            delete mine[id];
            return cache.delete(keyOf(item.url));
          }
          if (index[id] !== item.v) {                                              // a newer version exists
            var box = byId[id];
            if (box) return download(box).then(function () { mine[id] = entryFor(box); }).catch(function () {});
          }
        });
      }));
    }).then(function () {
      writeMine(mine);
      toggles.forEach(function (box) { box.checked = !!mine[box.dataset.id]; });
      refreshCount();
    }).catch(function () { refreshCount(); });
  }
  toggles.forEach(function (box) { box.checked = !!readMine()[box.dataset.id]; });
  refreshCount();
  reconcile();
})();