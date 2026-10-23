/* JUBILATE DEO CHORALE - small, optional interactions (no frameworks). */
(function () {
  "use strict";

  // [DEFAULT AVATAR] A member photo (<img data-avatar-img>, see templates/_avatar.html) that cannot be loaded is
  // removed, which uncovers the silhouette drawn behind it. Works for pictures added later by other scripts too.
  function useDefaultAvatar(img) {
    var box = img.closest("[data-avatar]");
    if (img.parentNode) img.parentNode.removeChild(img);
    if (!box) return;
    box.classList.remove("has-photo");
    var label = box.getAttribute("data-nophoto-label");
    if (label && box.getAttribute("role") === "img") box.setAttribute("aria-label", label);
  }
  function checkAvatars(root) {
    (root || document).querySelectorAll("img[data-avatar-img]").forEach(function (img) {
      if (img.complete && img.naturalWidth === 0) useDefaultAvatar(img);   // it already failed before this script ran
    });
  }
  document.addEventListener("error", function (event) {                  // "error" does not bubble, so listen while capturing
    var el = event.target;
    if (el && el.tagName === "IMG" && el.hasAttribute("data-avatar-img")) useDefaultAvatar(el);
  }, true);
  checkAvatars();

  // Dismiss flash messages
  document.querySelectorAll("[data-dismiss]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var flash = btn.closest(".flash");
      if (flash) flash.remove();
    });
  });

  // Show / hide password
  document.querySelectorAll("[data-toggle-password]").forEach(function (btn) {
    var input = document.getElementById(btn.dataset.togglePassword);
    if (!input) return;
    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
  });

  // Live "passwords match" check on the setup form
  var matchForm = document.querySelector("[data-match-form]");
  if (matchForm) {
    var pw = matchForm.querySelector("#password");
    var confirm = matchForm.querySelector("#confirm_password");
    var message = matchForm.querySelector("[data-match-message]");
    var check = function () {
      var mismatch = confirm.value.length > 0 && pw.value !== confirm.value;
      message.hidden = !mismatch;
    };
    pw.addEventListener("input", check);
    confirm.addEventListener("input", check);
  }

  // Side menu. The same code runs the member area's phone menu and the administrator menu.
  //   Phone / tablet (960px and narrower): the menu is an overlay, opened with the menu button in the top bar and
  //   closed with the X, the dark area behind it, or Esc.
  //   Desktop (administrator menu only): the arrow button collapses the menu to a narrow icon rail. The choice is
  //   remembered in this browser (localStorage "jdc.sidebar"), and base.html applies it before the page is drawn.
  var toggle = document.querySelector("[data-menu-toggle]");
  var sidebar = document.getElementById("sidebar");
  var scrim = document.querySelector("[data-menu-scrim]");
  var isAdminMenu = !!(sidebar && sidebar.classList.contains("sidebar"));
  var isMemberMenu = !!(sidebar && sidebar.classList.contains("msidebar"));
  var phoneWidth = window.matchMedia("(max-width: 960px)");

  if (toggle && sidebar && scrim) {
    var setOpen = function (open) {
      sidebar.classList.toggle("is-open", open);
      scrim.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (isAdminMenu) {
        document.body.classList.toggle("nav-lock", open);
        if (open) {                                      // move keyboard focus into the menu, and back when it closes
          var first = sidebar.querySelector("[data-menu-close], .nav__item[href]");
          if (first) first.focus();
        } else if (sidebar.contains(document.activeElement)) { toggle.focus(); }
      }
    };
    toggle.addEventListener("click", function () {
      setOpen(!sidebar.classList.contains("is-open"));
    });
    scrim.addEventListener("click", function () { setOpen(false); });
    document.querySelectorAll("[data-menu-close]").forEach(function (btn) {
      btn.addEventListener("click", function () { setOpen(false); });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && sidebar.classList.contains("is-open")) setOpen(false);
      // Administrator overlay: keep Tab inside the open menu
      if (isAdminMenu && e.key === "Tab" && sidebar.classList.contains("is-open")) {
        var items = Array.prototype.filter.call(
          sidebar.querySelectorAll("a[href], button:not([disabled])"), function (n) { return n.offsetParent !== null; });
        if (!items.length) return;
        var firstItem = items[0], lastItem = items[items.length - 1];
        if (e.shiftKey && document.activeElement === firstItem) { e.preventDefault(); lastItem.focus(); }
        else if (!e.shiftKey && document.activeElement === lastItem) { e.preventDefault(); firstItem.focus(); }
      }
    });
    // Growing the window to desktop size closes the phone overlay, so nothing is left locked or dimmed
    var closeOnDesktop = function () { if (!phoneWidth.matches && sidebar.classList.contains("is-open")) setOpen(false); };
    if (phoneWidth.addEventListener) phoneWidth.addEventListener("change", closeOnDesktop);
    else if (phoneWidth.addListener) phoneWidth.addListener(closeOnDesktop);
  }

  // Collapse to icons (desktop), remembered between pages. Administrator menu: the arrow button. Member menu: the
  // vertical MENU tab, which on phones and tablets opens the menu drawer instead.
  var collapseBtn = document.querySelector("[data-sidebar-collapse]");
  if ((isAdminMenu || isMemberMenu) && collapseBtn) {
    var STORE_KEY = "jdc.sidebar";
    var rootEl = document.documentElement;
    var readStored = function () { try { return localStorage.getItem(STORE_KEY) === "collapsed"; } catch (e) { return false; } };
    var writeStored = function (collapsed) { try { localStorage.setItem(STORE_KEY, collapsed ? "collapsed" : "expanded"); } catch (e) { /* storage blocked */ } };

    // Mark the page as collapsed or expanded, and keep the button, tooltips and labels in step with it
    var showState = function (collapsed) {
      rootEl.classList.toggle("sidebar-collapsed", collapsed);
      var railNow = collapsed && !phoneWidth.matches;      // the rail only exists on desktop
      var opensDrawer = isMemberMenu && phoneWidth.matches; // member tab on a phone / tablet opens the drawer
      var label = opensDrawer ? "Open menu" : (railNow ? "Expand sidebar" : "Collapse sidebar");
      collapseBtn.setAttribute("aria-expanded", String(opensDrawer ? false : !railNow));
      collapseBtn.setAttribute("aria-label", label);
      collapseBtn.title = label;
      // Tooltips show the names while only icons are visible
      sidebar.querySelectorAll("[data-label]").forEach(function (item) {
        if (railNow) item.setAttribute("title", item.dataset.label); else item.removeAttribute("title");
      });
    };

    collapseBtn.addEventListener("click", function () {
      if (isMemberMenu && phoneWidth.matches) { if (toggle) toggle.click(); return; }   // phone / tablet: open the drawer
      var collapsed = !rootEl.classList.contains("sidebar-collapsed");
      writeStored(collapsed);
      showState(collapsed);
    });
    // Another tab changed the choice: follow it, so every open page agrees
    window.addEventListener("storage", function (e) { if (e.key === STORE_KEY) showState(readStored()); });
    var refreshRail = function () { showState(rootEl.classList.contains("sidebar-collapsed")); };
    if (phoneWidth.addEventListener) phoneWidth.addEventListener("change", refreshRail);
    else if (phoneWidth.addListener) phoneWidth.addListener(refreshRail);

    showState(readStored());
  }

  // Tell screen readers which menu item is the current page (the page marks it with "is-active")
  if (isAdminMenu) {
    sidebar.querySelectorAll("a.nav__item.is-active").forEach(function (link) { link.setAttribute("aria-current", "page"); });
  }
  // Signing out also forgets the Music Library files saved for offline use on this device (static/js/music.js
  // keeps them), so the next person who uses the device cannot open them.
  document.querySelectorAll('form[action$="/logout"]').forEach(function (form) {
    form.addEventListener("submit", function () {
      try { localStorage.removeItem("jdc.music.offline"); } catch (e) { /* storage blocked */ }
      if (window.caches && caches.keys) {
        caches.keys().then(function (names) {
          names.filter(function (n) { return n.indexOf("jdc-music-") === 0; }).forEach(function (n) { caches.delete(n); });
        });
      }
    });
  });

  // LOGIN PAGE: the round button swaps the splash screen for the burgundy login box
  var splash = document.querySelector("[data-splash]");
  var card = document.querySelector("[data-login-card]");
  var openBtn = document.querySelector("[data-open-login]");
  if (card) {
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Only jump into the email box on computers; on phones it would pop up the keyboard
    var focusEmail = function () {
      var email = card.querySelector("#identifier, #email");
      if (email && window.matchMedia("(pointer: fine)").matches) email.focus();
    };
    if (splash && openBtn) {
      openBtn.addEventListener("click", function () {
        splash.classList.add("is-leaving");
        // 320 = milliseconds to wait for the splash to fade out (see splash-out in style.css)
        window.setTimeout(function () {
          splash.hidden = true;
          card.hidden = false;
          focusEmail();
        }, reduceMotion ? 0 : 320);
      });
    }
    // If the box is already showing (for example after a wrong password)
    if (!card.hidden) focusEmail();
  }
  // ===========================================================================
  // MEMBERS
  // Quick feedback in the browser. The server repeats every check, because only
  // the server can be trusted. Nothing here grants any permission.
  // ===========================================================================

  // The visible title of a field, without the * and (optional) marks
  function labelText(field) {
    var label = field.id ? document.querySelector('label[for="' + field.id + '"]') : null;
    if (!label) return "This field";
    var copy = label.cloneNode(true);
    copy.querySelectorAll(".req, .optional").forEach(function (el) { el.remove(); });
    return copy.textContent.trim() || "This field";
  }

  // Show (or clear) the red message under a field
  function setFieldError(field, message) {
    var box = field.closest(".field, .checkbox");
    if (!box) return;
    var isCheckbox = box.classList.contains("checkbox");
    var note = isCheckbox
      ? (box.nextElementSibling && box.nextElementSibling.classList.contains("field__error") ? box.nextElementSibling : null)
      : box.querySelector(".field__error");
    if (message) {
      if (!note) {
        note = document.createElement("p");
        note.className = "field__error";
        if (isCheckbox) { box.insertAdjacentElement("afterend", note); } else { box.appendChild(note); }
      }
      note.textContent = message;
      note.id = "error-" + (field.name || field.id);
      field.setAttribute("aria-invalid", "true");
      field.setAttribute("aria-describedby", note.id);
      box.classList.add("field--error");
    } else {
      if (note) note.remove();
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
      box.classList.remove("field--error");
    }
  }

  // Checks the browser cannot do by itself
  function runCustomChecks(field) {
    if (field.hasAttribute("data-letters")) {
      var text = field.value.trim();
      field.setCustomValidity(text && !/\p{L}/u.test(text) ? labelText(field) + " must contain letters." : "");
    }
    if (field.hasAttribute("data-phone")) {
      var raw = field.value.trim();
      var digits = raw.replace(/[\s().\-]/g, "");
      field.setCustomValidity(raw && !/^\+?[0-9]{7,15}$/.test(digits)
        ? "Enter a valid contact number, for example 0917 123 4567 or +63 917 123 4567." : "");
    }
  }

  function messageFor(field) {
    var v = field.validity;
    if (v.customError) return field.validationMessage;
    if (v.valueMissing) return field.dataset.requiredMessage || labelText(field) + " is required.";
    if (v.typeMismatch) return field.type === "email" ? "Enter a valid email address, for example name@example.com." : "Enter a valid value.";
    if (v.rangeOverflow) return field.dataset.maxMessage || "This value is too high.";
    if (v.rangeUnderflow) return "Enter a date from 1900 onward.";
    if (v.badInput) return "Enter a valid value.";
    return field.validationMessage;
  }

  // Forms marked data-validate: check everything on submit, then keep errors up to date
  document.querySelectorAll("form[data-validate]").forEach(function (form) {
    var fields = Array.prototype.slice.call(form.elements).filter(function (el) {
      return el.willValidate && el.name;
    });
    var check = function (field) {
      runCustomChecks(field);
      if (field.checkValidity()) { setFieldError(field, ""); return true; }
      setFieldError(field, messageFor(field));
      return false;
    };
    form.addEventListener("submit", function (event) {
      var firstBad = null;
      fields.forEach(function (field) { if (!check(field) && !firstBad) firstBad = field; });
      if (firstBad) { event.preventDefault(); firstBad.focus(); }
    });
    fields.forEach(function (field) {
      var recheck = function () { if (field.hasAttribute("aria-invalid")) check(field); };
      field.addEventListener("input", recheck);
      field.addEventListener("change", recheck);
      field.addEventListener("blur", function () { if (field.value) check(field); });
    });
  });

  // Profile photo: check the type and size, then show a preview
  document.querySelectorAll("[data-photo-input]").forEach(function (input) {
    var preview = document.querySelector("[data-photo-preview]");
    var maxBytes = (parseFloat(input.dataset.maxMb) || 2) * 1024 * 1024;
    input.addEventListener("change", function () {
      input.setCustomValidity("");
      var file = input.files && input.files[0];
      if (!file) { setFieldError(input, ""); return; }
      if (["image/jpeg", "image/png", "image/webp"].indexOf(file.type) === -1) {
        input.setCustomValidity("Upload a JPG, PNG or WebP image.");
      } else if (file.size > maxBytes) {
        input.setCustomValidity("The photo must be " + (maxBytes / 1048576) + " MB or smaller.");
      }
      if (!input.checkValidity()) { setFieldError(input, input.validationMessage); return; }
      setFieldError(input, "");
      if (preview && window.URL && URL.createObjectURL) {
        preview.textContent = "";
        var img = document.createElement("img");
        img.alt = "Selected photo preview";
        img.src = URL.createObjectURL(file);
        preview.appendChild(img);
      }
    });
  });

  // Forms with data-confirm ask "Are you sure?" first (for example Deactivate)
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // [CONFIRM DIALOG] The trash icon (data-dialog-open="the dialog's id") opens the matching <dialog> from
  // _confirm_dialog.html. Cancel, Esc and a click on the dark area close it. The red button inside is an ordinary form
  // submit, so the server still does (and checks) the real work. Without <dialog> support it falls back to the plain
  // "Are you sure?" box, and without JavaScript the icon does nothing, so nothing can be deleted by accident.
  document.querySelectorAll("[data-dialog-open]").forEach(function (opener) {
    var dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
    if (!dialog) return;
    var dialogForm = dialog.querySelector("form");
    opener.addEventListener("click", function () {
      if (typeof dialog.showModal !== "function") {
        if (dialogForm && window.confirm(dialog.getAttribute("data-confirm") || "Are you sure?")) dialogForm.submit();
        return;
      }
      dialog.showModal();
      var cancel = dialog.querySelector("[data-dialog-close]");
      if (cancel) cancel.focus();                       // the safe choice has focus, so pressing Enter does not delete
    });
    dialog.querySelectorAll("[data-dialog-close]").forEach(function (btn) {
      btn.addEventListener("click", function () { dialog.close(); });
    });
    dialog.addEventListener("click", function (event) { if (event.target === dialog) dialog.close(); });   // dark area
    dialog.addEventListener("close", function () { opener.focus(); });
    if (dialogForm) dialogForm.addEventListener("submit", function () {
      var send = dialogForm.querySelector('[type="submit"]');
      if (send) send.disabled = true;                   // a double click cannot send it twice
    });
  });

  // Upload box (Add person): show the chosen file's name inside the dashed box
  document.querySelectorAll("[data-photo-input]").forEach(function (input) {
    var box = input.closest(".mf-drop");
    var label = box && box.querySelector("[data-photo-name]");
    if (!label) return;
    var original = label.textContent;
    input.addEventListener("change", function () {
      label.textContent = input.files && input.files[0] ? input.files[0].name : original;
    });
  });

  // Icon buttons: Esc hides their tooltip while the pointer or keyboard focus is still on them
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".icon-btn").forEach(function (btn) {
      if (btn.matches(":hover, :focus-visible")) btn.setAttribute("data-tip-off", "");
    });
  });
  document.querySelectorAll(".icon-btn").forEach(function (btn) {
    var tipOn = function () { btn.removeAttribute("data-tip-off"); };
    btn.addEventListener("mouseleave", tipOn); btn.addEventListener("blur", tipOn);
  });

  // Filter dropdowns apply as soon as they change (the Search button still works without JavaScript)
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { if (select.form) select.form.submit(); });
  });

  // Add a new voice section / choir position right from the member form, without leaving the page.
  document.querySelectorAll("[data-add-choice]").forEach(function (button) {
    var field = button.closest(".field");
    var select = field && field.querySelector("select");
    var form = button.closest("form");
    if (!select || !form) return;
    button.addEventListener("click", function () {
      if (field.querySelector(".inline-add")) { field.querySelector(".inline-add input").focus(); return; }
      var box = document.createElement("div");
      box.className = "inline-add";
      var input = document.createElement("input");
      input.type = "text"; input.maxLength = 40; input.autocomplete = "off";
      input.setAttribute("aria-label", "New " + button.dataset.noun + " name");
      input.placeholder = "New " + button.dataset.noun;
      var add = document.createElement("button");
      add.type = "button"; add.className = "btn btn--primary btn--sm"; add.textContent = "Add";
      var cancel = document.createElement("button");
      cancel.type = "button"; cancel.className = "btn btn--outline btn--sm"; cancel.textContent = "Cancel";
      var note = document.createElement("p");
      note.className = "field__error"; note.hidden = true;
      var row = document.createElement("div");
      row.className = "inline-add__row";
      row.appendChild(input); row.appendChild(add); row.appendChild(cancel);
      box.appendChild(row); box.appendChild(note);
      button.insertAdjacentElement("afterend", box);
      input.focus();

      var close = function () { box.remove(); };
      var save = function () {
        var data = new FormData();
        data.append("csrf_token", form.querySelector('input[name="csrf_token"]').value);
        data.append("kind", button.dataset.addChoice);
        data.append("action", "add");
        data.append("name", input.value);
        add.disabled = true;
        fetch(button.dataset.url, { method: "POST", body: data, headers: { "X-Requested-With": "fetch" } })
          .then(function (r) { return r.json(); })
          .then(function (result) {
            add.disabled = false;
            if (!result.ok) { note.textContent = result.message; note.hidden = false; return; }
            var option = document.createElement("option");
            option.value = String(result.id); option.textContent = result.name;
            select.appendChild(option);
            select.value = option.value;
            select.dispatchEvent(new Event("change", { bubbles: true }));
            close();
          })
          .catch(function () {
            add.disabled = false;
            note.textContent = "Could not save. Refresh the page and try again."; note.hidden = false;
          });
      };
      add.addEventListener("click", save);
      cancel.addEventListener("click", close);
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        if (e.key === "Escape") { close(); }
      });
    });
  });

  // ===========================================================================
  // MEMBER AREA
  // ===========================================================================

  // Profile photo menu (top right): opens on click, closes when you click elsewhere or press Esc
  document.querySelectorAll("[data-dropdown]").forEach(function (wrap) {
    var btn = wrap.querySelector("[data-dropdown-toggle]");
    var menu = wrap.querySelector("[data-dropdown-menu]");
    if (!btn || !menu) return;
    var setOpen = function (open) {
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    };
    btn.addEventListener("click", function () { setOpen(menu.hidden); });
    document.addEventListener("click", function (e) { if (!wrap.contains(e.target)) setOpen(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || menu.hidden) return;
      var focusInside = menu.contains(document.activeElement);
      setOpen(false);
      if (focusInside) btn.focus();                      // keyboard users land back on the profile button
    });
  });

  // Attendance page: "Mark everyone present" / "Clear all" tick every row at once
  document.querySelectorAll("[data-mark-all]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var form = btn.closest("form");
      if (!form) return;
      form.querySelectorAll('input[type="radio"]').forEach(function (radio) {
        if (radio.value === btn.dataset.markAll) radio.checked = true;
      });
    });
  });

  // Tasks page: "Select everyone" / "Clear" for the member checkboxes
  document.querySelectorAll("[data-checkbox-group]").forEach(function (group) {
    var setAll = function (checked) {
      group.querySelectorAll('input[type="checkbox"]').forEach(function (box) { box.checked = checked; });
    };
    var all = group.querySelector("[data-select-all]");
    var none = group.querySelector("[data-select-none]");
    if (all) all.addEventListener("click", function () { setAll(true); });
    if (none) none.addEventListener("click", function () { setAll(false); });
  });

  // "Copy login details" button on the member page
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.dataset.copy;
      var label = btn.textContent;
      var done = function () {
        btn.textContent = "Copied";
        window.setTimeout(function () { btn.textContent = label; }, 2000);
      };
      var fallback = function () {            // older browsers, or a page that is not https
        var area = document.createElement("textarea");
        area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
        document.body.appendChild(area); area.select();
        try { document.execCommand("copy"); done(); } catch (err) { /* nothing more to try */ }
        area.remove();
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else { fallback(); }
    });
  });

  // Schedule page: ticking "only activities for me" applies the filter straight away
  document.querySelectorAll("input[data-autosubmit-check]").forEach(function (box) {
    box.addEventListener("change", function () { box.form.submit(); });
  });

  // "About you" form: show the age as soon as a birthday is picked (the server works it out again when saving)
  var birthday = document.querySelector("[data-birthday]");
  var ageBox = document.querySelector("[data-age-output]");
  if (birthday && ageBox) {
    var showAge = function () {
      var born = new Date(birthday.value + "T00:00:00");
      if (!birthday.value || isNaN(born)) { ageBox.value = ""; return; }
      var now = new Date();
      var age = now.getFullYear() - born.getFullYear();
      if (now.getMonth() < born.getMonth() || (now.getMonth() === born.getMonth() && now.getDate() < born.getDate())) { age -= 1; }
      ageBox.value = age >= 0 ? age : "";
    };
    birthday.addEventListener("input", showAge);
    showAge();
  }
  // Organizational structure (Members page): the bar collapses the tree and opens it again. The choice is
  // remembered in this browser. The header and the top three boxes stay visible when it is collapsed.
  document.querySelectorAll("[data-org]").forEach(function (panel) {
    var button = panel.querySelector("[data-org-toggle]");
    var rest = panel.querySelector("[data-org-rest]");
    if (!button || !rest) return;
    var KEY = "jdc.org";
    var show = function (collapsed) {
      panel.classList.toggle("is-collapsed", collapsed);
      button.setAttribute("aria-expanded", String(!collapsed));
      if (collapsed) rest.setAttribute("inert", ""); else rest.removeAttribute("inert");   // hidden links cannot be tabbed to
    };
    var saved = "";
    try { saved = localStorage.getItem(KEY) || ""; } catch (e) { /* storage blocked: it simply starts open */ }
    panel.classList.add("no-anim");                  // no animation while restoring the saved choice
    show(saved === "collapsed");
    window.requestAnimationFrame(function () { window.requestAnimationFrame(function () { panel.classList.remove("no-anim"); }); });
    button.addEventListener("click", function () {
      var collapsed = !panel.classList.contains("is-collapsed");
      show(collapsed);
      try { localStorage.setItem(KEY, collapsed ? "collapsed" : "expanded"); } catch (e) { /* storage blocked */ }
    });
  });

  // ===========================================================================
  // ADD PERSON: the Record type switch (Choir Member / Non-member Organizational Leader)
  // Sections marked data-only="member" are hidden (and their fields disabled, so they are neither checked nor
  // sent) when "leader" is chosen. The server repeats the rule, so this is only for convenience.
  // ===========================================================================
  document.querySelectorAll("[data-record-form]").forEach(function (form) {
    var apply = function () {
      var checked = form.querySelector('input[type="radio"][name="record_type"]:checked');
      var type = checked ? checked.value : (form.dataset.recordType || "member");
      form.dataset.recordType = type;
      form.querySelectorAll("[data-only]").forEach(function (box) {
        var show = box.dataset.only === type;
        box.hidden = !show;
        box.querySelectorAll("input, select, textarea").forEach(function (el) { el.disabled = !show; });
      });
      // "Ordinary Member" is a choir-member position, so it is not offered for a leader
      form.querySelectorAll("option[data-ordinary]").forEach(function (opt) {
        opt.hidden = type === "leader";
        opt.disabled = type === "leader";
        if (type === "leader" && opt.selected && opt.parentNode) opt.parentNode.value = "";
      });
      form.querySelectorAll(".mf-type").forEach(function (card) {
        var radio = card.querySelector("input");
        card.classList.toggle("is-selected", !!(radio && radio.checked));
      });
    };
    form.querySelectorAll('input[type="radio"][name="record_type"]').forEach(function (r) { r.addEventListener("change", apply); });
    apply();
  });
})();