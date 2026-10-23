/* JUBILATE DEO CHORALE - Members page: the member gallery (search + filters) and the profile side panel.
   Used by templates/member_members.html. The panel's data comes from choir_member_card() in app.py, which decides
   what this member may see. No frameworks.
     [MEMBER GALLERY]  live search, the All / Officers / Regular members filter, empty state
     [PROFILE PANEL]   open from a card, tabs, loading and error states, close with X / outside click / Escape */
(function () {
  "use strict";

  var gallery = document.querySelector("[data-gallery]");
  if (!gallery) return;

  // =========================================================================
  // [MEMBER GALLERY]
  // =========================================================================
  var grid = gallery.querySelector("[data-gallery-grid]");
  var cards = grid ? Array.prototype.slice.call(grid.querySelectorAll("[data-member-card]")) : [];
  var search = gallery.querySelector("[data-gallery-search]");
  var form = gallery.querySelector("[data-gallery-form]");
  var empty = gallery.querySelector("[data-gallery-empty]");
  var countBox = gallery.querySelector("[data-gallery-count]");
  var statusBox = gallery.querySelector("[data-gallery-status]");
  var chips = Array.prototype.slice.call(gallery.querySelectorAll("[data-filter]"));
  var filter = "all";
  var statusTimer = null;

  gallery.classList.add("is-live");                    // hides the Search button: the list now narrows as you type

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  function applyFilters() {
    var words = (search ? search.value : "").toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    var counts = { all: 0, officers: 0, members: 0 };
    cards.forEach(function (card) {
      var haystack = card.getAttribute("data-search") || "";
      var textOk = words.every(function (word) { return haystack.indexOf(word) !== -1; });
      var officer = card.getAttribute("data-officer") === "1";
      if (textOk) { counts.all += 1; if (officer) counts.officers += 1; else counts.members += 1; }
      var visible = textOk && (filter === "all" || (filter === "officers" ? officer : !officer));
      card.hidden = !visible;
      if (visible) shown += 1;
    });
    chips.forEach(function (chip) {
      var badge = gallery.querySelector('[data-filter-count="' + chip.getAttribute("data-filter") + '"]');
      if (badge) badge.textContent = counts[chip.getAttribute("data-filter")];
    });
    if (countBox) countBox.textContent = plural(shown, "member") + (words.length || filter !== "all" ? " found" : "");
    if (empty && cards.length) empty.hidden = shown > 0;
    if (statusBox) {                                   // announced politely, not on every keystroke
      window.clearTimeout(statusTimer);
      statusTimer = window.setTimeout(function () {
        statusBox.textContent = shown ? plural(shown, "member") + " shown." : "No members match.";
      }, 400);
    }
    try {                                              // keep ?q= in the address so a refresh shows the same list
      var url = new URL(window.location.href);
      if (words.length) url.searchParams.set("q", search.value.trim()); else url.searchParams.delete("q");
      window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    } catch (e) { /* old browser: the list still works */ }
  }

  if (search) search.addEventListener("input", applyFilters);
  if (form) form.addEventListener("submit", function (event) { event.preventDefault(); applyFilters(); });
  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      filter = chip.getAttribute("data-filter");
      chips.forEach(function (other) {
        var on = other === chip;
        other.classList.toggle("is-active", on);
        other.setAttribute("aria-pressed", on ? "true" : "false");
      });
      applyFilters();
    });
  });
  applyFilters();

  // =========================================================================
  // [PROFILE PANEL]
  // =========================================================================
  var modal = document.querySelector("[data-pm]");
  if (!modal) return;

  var panel = modal.querySelector("[data-pm-panel]");
  var avatarBox = modal.querySelector(".pm__avatar");
  var nameEl = modal.querySelector("[data-pm-name]");
  var nickEl = modal.querySelector("[data-pm-nick]");
  var posEl = modal.querySelector("[data-pm-pos]");
  var badgeEl = modal.querySelector("[data-pm-badge]");
  var tabsBox = modal.querySelector("[data-pm-tabs]");
  var tabs = Array.prototype.slice.call(modal.querySelectorAll("[data-pm-tab]"));
  var panes = Array.prototype.slice.call(modal.querySelectorAll("[data-pm-pane]"));
  var bodyEl = modal.querySelector("[data-pm-body]");
  var stateEl = modal.querySelector("[data-pm-state]");
  var officerTab = modal.querySelector('[data-pm-tab="officer"]');
  var cardUrl = gallery.getAttribute("data-card-url") || "";       // .../members/0/card ; the 0 is swapped for the member id
  var paneFor = {};
  panes.forEach(function (pane) { paneFor[pane.getAttribute("data-pm-pane")] = pane; });

  var current = { id: null, token: 0, trigger: null, controller: null, closeTimer: null };
  var activeTab = "profile";
  var ready = false;
  var INERT = [".mtop", ".mshell", ".mfoot", ".member-bg"];          // the page behind the panel cannot be reached while it is open

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // ---- picture: real photo, or the default silhouette that is already drawn behind it ----
  function setAvatar(url, name) {
    var old = avatarBox.querySelector("img");
    if (old) old.parentNode.removeChild(old);
    var noPhoto = "No profile photo provided for " + name + " (default avatar shown)";
    avatarBox.setAttribute("data-nophoto-label", noPhoto);
    if (!url) {
      avatarBox.classList.remove("has-photo");
      avatarBox.setAttribute("aria-label", noPhoto);
      return;
    }
    var img = document.createElement("img");
    img.alt = "";
    img.decoding = "async";
    img.setAttribute("data-avatar-img", "");
    img.addEventListener("error", function () {                      // the file cannot be loaded: show the silhouette
      if (img.parentNode) img.parentNode.removeChild(img);
      avatarBox.classList.remove("has-photo");
      avatarBox.setAttribute("aria-label", noPhoto);
    });
    img.src = url;
    avatarBox.insertBefore(img, avatarBox.firstChild);
    avatarBox.classList.add("has-photo");
    avatarBox.setAttribute("aria-label", "Profile photo of " + name);
  }

  function fillHeader(data) {
    nameEl.textContent = data.name;
    nickEl.textContent = [data.nickname, data.voice].filter(Boolean).join(" \u00b7 ");
    posEl.textContent = data.position;
    badgeEl.hidden = !data.officer;
    officerTab.hidden = !data.officer;
    setAvatar(data.photo, data.name);
  }

  // ---- tabs ----
  function showPanes() {
    tabs.forEach(function (tab) {
      var on = tab.getAttribute("data-pm-tab") === activeTab;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
    });
    panes.forEach(function (pane) { pane.hidden = !ready || pane.getAttribute("data-pm-pane") !== activeTab; });
  }
  function activate(name, focusTab) {
    activeTab = name;
    showPanes();
    bodyEl.scrollTop = 0;
    if (focusTab) {
      var tab = tabs.filter(function (t) { return t.getAttribute("data-pm-tab") === name; })[0];
      if (tab) tab.focus();
    }
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { activate(tab.getAttribute("data-pm-tab"), false); });
  });
  tabsBox.addEventListener("keydown", function (event) {
    var list = tabs.filter(function (t) { return !t.hidden; });
    var index = list.indexOf(document.activeElement);
    if (index < 0) return;
    var next = null;
    if (event.key === "ArrowRight") next = list[(index + 1) % list.length];
    else if (event.key === "ArrowLeft") next = list[(index - 1 + list.length) % list.length];
    else if (event.key === "Home") next = list[0];
    else if (event.key === "End") next = list[list.length - 1];
    if (next) { event.preventDefault(); activate(next.getAttribute("data-pm-tab"), true); }
  });

  // ---- loading / error ----
  function showState(kind, message) {
    stateEl.hidden = false;
    stateEl.textContent = "";
    bodyEl.setAttribute("aria-busy", kind === "loading" ? "true" : "false");
    if (kind === "loading") {
      stateEl.appendChild(make("span", "pm__spinner"));
      stateEl.lastChild.setAttribute("aria-hidden", "true");
      stateEl.appendChild(make("p", null, "Loading profile\u2026"));
      return;
    }
    stateEl.appendChild(make("p", null, message));
    var retry = make("button", "btn btn--outline btn--sm", "Try again");
    retry.type = "button";
    retry.addEventListener("click", function () { load(current.id); });
    stateEl.appendChild(retry);
  }

  // ---- building the answers (text is always inserted as text, never as HTML) ----
  function formatStamp(stamp) {
    if (!stamp) return "";
    var when = new Date(String(stamp).replace(" ", "T") + "Z");
    if (isNaN(when.getTime())) return "";
    return when.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
  }

  function valueCell(item) {
    var cell = make("dd");
    var value = (item.value || "").trim();
    if (item.kind === "private") {
      cell.appendChild(make("span", "pm__lock", "Private. Only this member and choir administrators can read it."));
      return cell;
    }
    if (!value) { cell.appendChild(make("span", "pm__muted", "Not provided")); return cell; }
    var link = null;
    if (item.kind === "email") { link = make("a", null, value); link.href = "mailto:" + value; }
    else if (item.kind === "phone") { link = make("a", null, value); link.href = "tel:" + value.replace(/[^\d+]/g, ""); }
    else if (item.kind === "link" && /^https?:\/\//i.test(value)) {
      link = make("a", null, value);
      link.href = value;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    if (link) cell.appendChild(link); else cell.textContent = value;
    return cell;
  }

  function sections(list) {
    var fragment = document.createDocumentFragment();
    (list || []).forEach(function (section) {
      var box = make("section", "pm__sec");
      box.appendChild(make("h3", null, section.title));
      var dl = make("dl", "pm__dl");
      section.items.forEach(function (item) {
        var row = make("div", "pm__row" + (item.kind === "long" ? " pm__row--long" : ""));
        row.appendChild(make("dt", null, item.label));
        row.appendChild(valueCell(item));
        dl.appendChild(row);
      });
      box.appendChild(dl);
      fragment.appendChild(box);
    });
    return fragment;
  }

  function note(text) {
    var box = make("div", "pm__sec");
    box.appendChild(make("p", "pm__muted", text));
    return box;
  }

  function listSection(title, entries, withHint) {
    var box = make("section", "pm__sec");
    box.appendChild(make("h3", null, title));
    var list = make("ul", "pm__list");
    entries.forEach(function (entry) {
      var item = make("li");
      if (withHint) {
        item.appendChild(make("strong", null, entry.label));
        if (entry.hint) item.appendChild(make("small", null, entry.hint));
      } else {
        item.textContent = entry;
      }
      list.appendChild(item);
    });
    box.appendChild(list);
    return box;
  }

  function renderRegistration(data) {
    var fragment = document.createDocumentFragment();
    if (!data.registration.completed) {
      fragment.appendChild(note(data.name + " has not filled in the registration form yet."));
      return fragment;
    }
    var when = formatStamp(data.registration.submitted);
    if (when) fragment.appendChild(make("p", "pm__submitted", "Submitted on " + when));
    fragment.appendChild(sections(data.registration.sections));
    return fragment;
  }

  function renderOfficer(data) {
    var fragment = document.createDocumentFragment();
    var officer = data.officer;
    if (!officer) return fragment;
    var box = make("section", "pm__sec");
    box.appendChild(make("h3", null, "Position"));
    var card = make("div", "pm__officer");
    card.appendChild(make("p", "pm__officer-title", officer.title));
    if (officer.description) card.appendChild(make("p", null, officer.description));
    else card.appendChild(make("p", "pm__muted", "No official description has been added for this position yet."));
    box.appendChild(card);
    fragment.appendChild(box);
    if (officer.duties && officer.duties.length) fragment.appendChild(listSection("Duties and responsibilities", officer.duties, false));
    if (officer.abilities && officer.abilities.length) fragment.appendChild(listSection("What this position can do in this app", officer.abilities, true));
    return fragment;
  }

  function render(data) {
    fillHeader({ name: data.name, nickname: data.nickname, voice: data.voice, position: data.position,
                 officer: data.is_officer, photo: data.photo_url });
    ["profile", "registration", "officer"].forEach(function (key) { paneFor[key].textContent = ""; });
    paneFor.profile.appendChild(sections(data.profile));
    paneFor.registration.appendChild(renderRegistration(data));
    paneFor.officer.appendChild(renderOfficer(data));
    if (activeTab === "officer" && !data.is_officer) activeTab = "profile";
    ready = true;
    stateEl.hidden = true;
    bodyEl.setAttribute("aria-busy", "false");
    showPanes();
  }

  function load(id) {
    current.token += 1;
    var token = current.token;
    if (current.controller) current.controller.abort();
    current.controller = window.AbortController ? new AbortController() : null;
    ready = false;
    showPanes();
    showState("loading");
    var options = { credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (current.controller) options.signal = current.controller.signal;
    fetch(cardUrl.replace("/0/", "/" + id + "/"), options)
      .then(function (response) {
        if (response.status === 403) throw new Error("You do not have permission to view this profile.");
        if (response.status === 404) throw new Error("This member's profile is no longer available.");
        if (!response.ok) throw new Error("The profile could not be loaded. Please try again.");
        return response.json().catch(function () {
          throw new Error("Your session may have expired. Reload the page and sign in again.");
        });
      })
      .then(function (data) { if (token === current.token) render(data); })
      .catch(function (error) {
        if (token !== current.token || (error && error.name === "AbortError")) return;
        showState("error", error instanceof TypeError
          ? "You seem to be offline. Check your connection and try again."
          : error.message);
      });
  }

  // ---- open / close ----
  function setInert(on) {
    INERT.forEach(function (selector) {
      document.querySelectorAll(selector).forEach(function (node) {
        if (on) node.setAttribute("inert", ""); else node.removeAttribute("inert");
      });
    });
  }

  function open(card, trigger) {
    window.clearTimeout(current.closeTimer);
    current.id = card.getAttribute("data-id");
    current.trigger = trigger;
    activeTab = "profile";
    ready = false;
    fillHeader({ name: card.getAttribute("data-name"), nickname: card.getAttribute("data-nickname"),
                 voice: card.getAttribute("data-voice"), position: card.getAttribute("data-position"),
                 officer: card.getAttribute("data-officer") === "1", photo: card.getAttribute("data-photo") });
    modal.hidden = false;
    document.body.classList.add("pm-lock");
    setInert(true);
    void modal.offsetWidth;                                           // let the browser draw it closed first, so it can slide in
    modal.classList.add("is-open");
    panel.focus({ preventScroll: true });
    load(current.id);
  }

  function close() {
    if (modal.hidden || !modal.classList.contains("is-open")) return;
    current.token += 1;                                               // ignore an answer that arrives after closing
    if (current.controller) current.controller.abort();
    modal.classList.remove("is-open");
    setInert(false);
    document.body.classList.remove("pm-lock");
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    current.closeTimer = window.setTimeout(function () { modal.hidden = true; }, still ? 0 : 220);
    var back = current.trigger;
    if (back && document.body.contains(back) && !back.closest("[hidden]")) back.focus({ preventScroll: true });
    else if (search) search.focus({ preventScroll: true });
  }

  gallery.addEventListener("click", function (event) {
    var link = event.target.closest("[data-open-profile]");
    if (!link || event.defaultPrevented) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;   // "open in new tab" still works
    var card = link.closest("[data-member-card]");
    if (!card) return;
    event.preventDefault();
    open(card, link);
  });

  modal.addEventListener("click", function (event) {                  // the X, the Close button, or anywhere outside the panel
    if (event.target.closest("[data-pm-close]")) close();
  });

  function focusable() {
    return Array.prototype.slice.call(panel.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'))
      .filter(function (node) { return node.getClientRects().length > 0; });
  }
  document.addEventListener("keydown", function (event) {
    if (modal.hidden || !modal.classList.contains("is-open")) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); return; }
    if (event.key !== "Tab") return;
    var items = focusable();
    if (!items.length) { event.preventDefault(); panel.focus(); return; }
    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;
    if (event.shiftKey && (active === first || active === panel)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
  }, true);
})();