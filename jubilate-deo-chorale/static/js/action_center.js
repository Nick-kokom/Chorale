/* JUBILATE DEO CHORALE - ACTION CENTER (right-hand sidebar, administrators only).
   Draws the switches from the data the server sends and posts the choices back.
   Nothing here grants any permission: the server checks and enforces everything (see action_center_member in app.py).
   All member-supplied text is inserted with textContent, never as HTML. */
(function () {
  "use strict";

  var root = document.getElementById("action-center");
  if (!root) return;

  var scrim = document.querySelector("[data-ac-scrim]");
  var openers = document.querySelectorAll("[data-ac-open]");     // the "Action Center" item in the side menu
  var csrf = root.querySelector("[data-ac-csrf]").value;
  var searchUrl = root.dataset.searchUrl;
  var memberUrl = root.dataset.memberUrl;            // ends with /0; the member id replaces that 0
  var $ = function (sel) { return root.querySelector(sel); };

  var els = {
    search: $("[data-ac-search]"), results: $("[data-ac-results]"), picked: $("[data-ac-picked]"),
    empty: $("[data-ac-empty]"), editor: $("[data-ac-editor]"), note: $("[data-ac-note]"),
    panels: $("[data-ac-panels]"), actions: $("[data-ac-actions]"), voices: $("[data-ac-voices]"),
    status: $("[data-ac-status]"), save: $("[data-ac-save]"), reset: $("[data-ac-reset]")
  };

  var state = { data: null, saved: {}, busy: false };
  var searchTimer = null, searchToken = 0, lastFocus = null;

  // ---- small helpers ---------------------------------------------------------------------
  var ICONS = {   // fixed strings written by us (never member data)
    "panel.directory": '<path d="M2.5 20c0-3.6 3-6 6.5-6s6.5 2.4 6.5 6"/><circle cx="9" cy="8" r="3.5"/><circle cx="17.5" cy="9" r="2.5"/><path d="M17 14.2c2.6.2 4.5 2 4.5 4.8"/>',
    "panel.general_files": '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
    "panel.voice_files": '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    "panel.schedule": '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "panel.announcements": '<path d="M4 10v4h3l7 4V6L7 10zM18 9a4 4 0 010 6"/>',
    "panel.reports": '<path d="M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6"/>',
    "announcements.post": '<path d="M4 10v4h3l7 4V6L7 10zM18 9a4 4 0 010 6"/>',
    "schedule.manage": '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4M9 15l2 2 4-4"/>',
    "tasks.manage": '<path d="M5 12.5l4.2 4.2L19 7"/>',
    "attendance.manage": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-4 3-6 7-6s7 2 7 6M16 4l2 2 3-3"/>',
    "finance.post": '<path d="M4 7a2 2 0 012-2h12v4M4 7v10a2 2 0 002 2h14V9H6a2 2 0 01-2-2z"/><circle cx="16.5" cy="14" r="1"/>',
    "panel.music": '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    "music.upload": '<path d="M12 16V5M7 9l5-5 5 5M5 20h14"/>',
    "music.edit": '<path d="M4 20h4L19 9l-4-4L4 16zM13 7l4 4"/>',
    "music.archive": '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 001 1h12a1 1 0 001-1V8M10 12h4"/>',
    "gfile.upload": '<path d="M12 16V5M7 9l5-5 5 5M5 20h14"/>',
    "gfile.edit": '<path d="M4 20h4L19 9l-4-4L4 16zM13 7l4 4"/>',
    "gfile.delete": '<path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13"/>'
  };
  var FALLBACK_ICON = '<circle cx="12" cy="12" r="3"/>';

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function svg(paths) {
    var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "ac-icon"); s.setAttribute("aria-hidden", "true");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "1.6");
    s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
    s.innerHTML = paths;                              // fixed strings from ICONS above
    return s;
  }
  function memberAvatar(person) {
    var box = el("span", "ac-avatar");
    box.setAttribute("aria-hidden", "true");
    if (person.photo) {
      var img = document.createElement("img");
      img.src = person.photo; img.alt = ""; img.loading = "lazy";
      box.appendChild(img);
    } else { box.textContent = person.initials; }
    return box;
  }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json", "X-Requested-With": "fetch" } })
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); });
  }
  function postForm(url, fields) {
    var data = new FormData();
    data.append("csrf_token", csrf);
    fields.forEach(function (pair) { data.append(pair[0], pair[1]); });
    return fetch(url, { method: "POST", body: data, credentials: "same-origin", headers: { "Accept": "application/json", "X-Requested-With": "fetch" } })
      .then(function (r) { return r.json().then(function (body) { return { ok: r.ok, body: body }; }); });
  }
  function urlFor(id) { return memberUrl.replace(/0$/, String(id)); }
  function say(message, kind) {
    els.status.textContent = message || "";
    els.status.className = "ac__status" + (kind ? " is-" + kind : "");
  }

  // ---- open / close ------------------------------------------------------------------------
  function isOpen() { return root.classList.contains("is-open"); }
  function open(memberId) {
    lastFocus = document.activeElement;
    var navScrim = document.querySelector("[data-menu-scrim]");      // opened from the phone menu: close that menu first
    if (navScrim && !navScrim.hidden) navScrim.click();
    root.classList.add("is-open");
    scrim.hidden = false;
    document.body.classList.add("ac-lock");
    openers.forEach(function (btn) { btn.setAttribute("aria-expanded", "true"); });
    window.setTimeout(function () { els.search.focus(); }, 60);
    if (memberId) loadMember(memberId);
    else if (!state.data) runSearch("");
  }
  function close(force) {
    if (!force && isDirty() && !window.confirm("You have unsaved changes. Close without saving?")) return;
    root.classList.remove("is-open");
    scrim.hidden = true;
    document.body.classList.remove("ac-lock");
    openers.forEach(function (btn) { btn.setAttribute("aria-expanded", "false"); });
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  openers.forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (isOpen()) close(); else open(btn.dataset.memberId || null);
    });
  });
  root.querySelector("[data-ac-close]").addEventListener("click", function () { close(); });
  scrim.addEventListener("click", function () { close(); });
  document.addEventListener("keydown", function (e) {
    if (!isOpen()) return;
    if (e.key === "Escape") { close(); return; }
    if (e.key !== "Tab") return;                       // keep keyboard focus inside the open sidebar
    var focusable = Array.prototype.filter.call(
      root.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), summary'),
      function (n) { return n.offsetParent !== null; });
    if (!focusable.length) return;
    var first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  // ---- 1. choose a member ------------------------------------------------------------------
  function personButton(person, onClick, trailing) {
    var btn = el("button", "ac-person");
    btn.type = "button";
    btn.appendChild(memberAvatar(person));
    var text = el("span", "ac-person__text");
    text.appendChild(el("span", "ac-person__name", person.name));
    text.appendChild(el("span", "ac-person__meta", person.voice + " · Member ID: " + person.code));
    btn.appendChild(text);
    btn.appendChild(el("span", "ac-person__go", trailing || "›"));
    btn.addEventListener("click", onClick);
    return btn;
  }
  function runSearch(q) {
    var token = ++searchToken;
    getJSON(searchUrl + "?q=" + encodeURIComponent(q)).then(function (res) {
      if (token !== searchToken) return;               // an older answer arrived late: ignore it
      els.results.textContent = "";
      if (!res.results.length) {
        var none = el("li"); none.appendChild(el("p", "ac__hint", "No member matches that search."));
        els.results.appendChild(none);
      }
      res.results.forEach(function (person) {
        var li = el("li");
        li.appendChild(personButton(person, function () { chooseMember(person.id); }));
        els.results.appendChild(li);
      });
      els.results.hidden = false;
    }).catch(function () {
      if (token !== searchToken) return;
      say("Could not search. Refresh the page and sign in again if needed.", "error");
    });
  }
  els.search.addEventListener("input", function () {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(function () { runSearch(els.search.value.trim()); }, 220);
  });
  els.search.addEventListener("focus", function () { if (els.results.hidden) runSearch(els.search.value.trim()); });

  function chooseMember(id) {
    if (isDirty() && !window.confirm("You have unsaved changes for the current member. Switch without saving?")) return;
    loadMember(id);
  }
  function loadMember(id) {
    say("Loading…");
    getJSON(urlFor(id)).then(function (data) {
      if (!data.ok) throw new Error("not ok");
      state.data = data;
      els.results.hidden = true; els.search.value = "";
      render();
      say("");
    }).catch(function () { say("Could not load that member. Refresh the page and try again.", "error"); });
  }

  // ---- 2 and 3. the switches ----------------------------------------------------------------
  function switchRow(item, options) {
    options = options || {};
    var row = el("label", "ac-row");
    row.dataset.key = item.key;
    if (!options.compact) {
      var icon = el("span", "ac-row__icon"); icon.appendChild(svg(ICONS[item.key] || FALLBACK_ICON)); row.appendChild(icon);
    }
    var text = el("span", "ac-row__text");
    var title = el("strong", null, item.label);
    var pill = el("span", "ac-pill", "Custom"); pill.hidden = true; pill.dataset.pill = "1";
    title.appendChild(pill);
    text.appendChild(title);
    if (item.hint) text.appendChild(el("small", null, item.hint));
    row.appendChild(text);
    var sw = el("span", "ac-switch");
    var input = document.createElement("input");
    input.type = "checkbox"; input.setAttribute("role", "switch");
    input.dataset.key = item.key; input.dataset.default = item.default ? "1" : "0";
    input.checked = item.on;
    input.setAttribute("aria-label", item.label + (options.context ? " (" + options.context + ")" : ""));
    input.addEventListener("change", refresh);
    sw.appendChild(input); sw.appendChild(el("span", "ac-switch__track"));
    row.appendChild(sw);
    return row;
  }

  function render() {
    var d = state.data, m = d.member;
    els.empty.hidden = true; els.editor.hidden = false;

    els.picked.textContent = "";
    var card = personButton(m, function () { els.search.focus(); runSearch(""); }, "Change");
    card.querySelector(".ac-person__go").style.fontSize = "0.8rem";
    var meta = card.querySelector(".ac-person__meta");
    meta.textContent = m.voice + " · " + m.position + " · Member ID: " + m.code;
    els.picked.appendChild(card); els.picked.hidden = false;

    var notes = [];
    if (!m.has_login) notes.push("This member has no login yet. These settings apply as soon as a login is created.");
    if (m.status === "inactive") notes.push("This member is Inactive and cannot sign in. Settings are kept for when they are restored.");
    els.note.textContent = notes.join(" "); els.note.hidden = !notes.length;

    els.panels.textContent = ""; els.actions.textContent = ""; els.voices.textContent = "";
    d.panels.forEach(function (item) { els.panels.appendChild(switchRow(item)); });

    var title = el("p", "ac-voices__title", "Voice section files");
    els.voices.appendChild(title);
    d.voices.forEach(function (voice) {
      var det = document.createElement("details"); det.className = "ac-voice";
      if (voice.own) det.open = true;
      var sum = document.createElement("summary");
      sum.appendChild(document.createTextNode(voice.name));
      if (voice.own) sum.appendChild(el("span", "ac-pill", "Their section"));
      det.appendChild(sum);
      var body = el("div", "ac-voice__body");
      voice.items.forEach(function (item) {
        item.hint = "";
        body.appendChild(switchRow(item, { compact: true, context: voice.name }));
      });
      det.appendChild(body); els.voices.appendChild(det);
    });

    d.actions.forEach(function (item) { els.actions.appendChild(switchRow(item)); });

    state.saved = currentChoices();
    refresh();
  }

  function inputs() { return Array.prototype.slice.call(root.querySelectorAll('input[type="checkbox"][data-key]')); }
  function currentChoices() {
    var out = {}; inputs().forEach(function (box) { out[box.dataset.key] = box.checked; }); return out;
  }
  function isDirty() {
    if (!state.data) return false;
    var now = currentChoices();
    return Object.keys(now).some(function (key) { return now[key] !== state.saved[key]; });
  }
  function byKey(key) { return root.querySelector('input[data-key="' + key + '"]'); }

  // Update the "Custom" tags, dim rows that have no effect, and the Save button.
  function refresh() {
    inputs().forEach(function (box) {
      var pill = box.closest(".ac-row").querySelector("[data-pill]");
      if (pill) pill.hidden = (box.checked ? "1" : "0") === box.dataset.default;
    });
    var general = byKey("panel.general_files"), voicePanel = byKey("panel.voice_files");
    ["gfile.upload", "gfile.edit", "gfile.delete"].forEach(function (key) {
      var box = byKey(key); if (box && general) box.closest(".ac-row").classList.toggle("is-dim", !general.checked);
    });
    if (voicePanel) els.voices.classList.toggle("is-dim", !voicePanel.checked);
    state.data.voices.forEach(function (voice) {
      var view = byKey("vfile.view:" + voice.id);
      ["upload", "edit", "delete"].forEach(function (action) {
        var box = byKey("vfile." + action + ":" + voice.id);
        if (box && view) box.closest(".ac-row").classList.toggle("is-dim", !view.checked);
      });
    });
    var dirty = isDirty();
    els.save.disabled = state.busy || !dirty;
    els.reset.hidden = !(state.data.custom_count > 0 || dirty);
    if (dirty) say("Unsaved changes", "dirty"); else if (els.status.classList.contains("is-dirty")) say("");
  }

  // ---- save / reset -------------------------------------------------------------------------
  function applyResponse(res, fallback) {
    state.busy = false;
    if (!res.ok || !res.body.ok) { say(res.body && res.body.message || fallback, "error"); refresh(); return; }
    state.data = res.body;
    render();
    say(res.body.message, "ok");
  }
  els.save.addEventListener("click", function () {
    if (!state.data || state.busy || !isDirty()) return;
    state.busy = true; els.save.disabled = true; say("Saving…");
    var fields = [];
    var now = currentChoices();
    Object.keys(now).forEach(function (key) { if (now[key]) fields.push(["on", key]); });
    postForm(urlFor(state.data.member.id), fields)
      .then(function (res) { applyResponse(res, "The changes could not be saved."); })
      .catch(function () { state.busy = false; say("Could not save. Refresh the page and try again.", "error"); refresh(); });
  });
  els.reset.addEventListener("click", function () {
    if (!state.data || state.busy) return;
    if (!window.confirm("Reset " + state.data.member.name + " to the defaults of their choir position? All custom switches are cleared.")) return;
    state.busy = true; say("Resetting…");
    postForm(urlFor(state.data.member.id), [["action", "reset"]])
      .then(function (res) { applyResponse(res, "Could not reset."); })
      .catch(function () { state.busy = false; say("Could not reset. Refresh the page and try again.", "error"); refresh(); });
  });

  window.ActionCenter = { open: open, close: function () { close(); } };   // e.g. ActionCenter.open(memberId) from another page
})();