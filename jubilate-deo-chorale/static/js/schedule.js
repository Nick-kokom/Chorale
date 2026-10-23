/* schedule.js - Schedule & Activities page and Add/Edit form.
   Everything here is an enhancement: with JavaScript off, the form still uses a plain select and
   "Clear schedules" opens the full confirmation page. The server re-checks every rule. */
(function () {
  "use strict";

  /* ---- Activity type: chips dropdown over the real <select> ------------------------------ */
  document.querySelectorAll("[data-chipsel]").forEach(function (root) {
    var select = root.querySelector("select");
    var ui = root.querySelector(".chipsel");
    var btn = root.querySelector(".chipsel__btn");
    var valueBox = root.querySelector("[data-chipsel-value]");
    var menu = root.querySelector(".chipsel__menu");
    if (!select || !ui || !btn || !menu) return;
    var options = Array.prototype.slice.call(menu.querySelectorAll("[role=option]"));

    select.classList.add("chipsel__native");     // keeps the select in the form (and for validation) but hides it
    ui.hidden = false;

    function paint() {
      var current = options.filter(function (o) { return o.dataset.value === select.value; })[0];
      options.forEach(function (o) { o.setAttribute("aria-selected", o === current ? "true" : "false"); });
      valueBox.innerHTML = "";
      if (current) {
        valueBox.appendChild(current.firstElementChild.cloneNode(true));
      } else {
        var ph = document.createElement("span");
        ph.className = "chipsel__placeholder";
        ph.textContent = select.options[0] ? select.options[0].text : "Choose";
        valueBox.appendChild(ph);
      }
    }
    function open() {
      menu.hidden = false; btn.setAttribute("aria-expanded", "true");
      var sel = options.filter(function (o) { return o.getAttribute("aria-selected") === "true"; })[0] || options[0];
      if (sel) sel.focus();
    }
    function close(focusBtn) {
      menu.hidden = true; btn.setAttribute("aria-expanded", "false");
      if (focusBtn) btn.focus();
    }
    function choose(opt) {
      select.value = opt.dataset.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));   // lets app.js clear an old error message
      paint(); close(true);
    }

    btn.addEventListener("click", function () { menu.hidden ? open() : close(false); });
    options.forEach(function (o, i) {
      o.addEventListener("click", function () { choose(o); });
      o.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(o); }
        else if (e.key === "ArrowDown") { e.preventDefault(); (options[i + 1] || options[0]).focus(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); (options[i - 1] || options[options.length - 1]).focus(); }
        else if (e.key === "Escape") { e.preventDefault(); close(true); }
        else if (e.key === "Tab") { close(false); }
      });
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" && menu.hidden) { e.preventDefault(); open(); }
    });
    document.addEventListener("click", function (e) { if (!root.contains(e.target)) close(false); });
    select.addEventListener("change", paint);
    paint();
  });

  /* ---- Clear schedules: confirmation dialog ---------------------------------------------- */
  var dialog = document.getElementById("clear-dialog");
  var openers = document.querySelectorAll("[data-clear-open]");
  if (dialog && typeof dialog.showModal === "function") {
    var form = dialog.querySelector("[data-clear-form]");
    var phrase = dialog.querySelector("[data-clear-phrase]");
    var password = dialog.querySelector("input[name=password]");
    var submit = dialog.querySelector("[data-clear-submit]");

    function ready() {
      if (!submit) return;
      var ok = phrase.value.trim() === phrase.getAttribute("data-clear-phrase") && password.value.length > 0;
      submit.disabled = !ok;
    }
    function reset() {
      if (!form) return;
      if (phrase) phrase.value = "";
      if (password) { password.value = ""; password.type = "password"; }
      var att = form.querySelector("input[name=with_attendance]");
      if (att) att.checked = false;
      ready();
    }
    openers.forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();               // no JavaScript: the link opens the full confirmation page instead
        reset();
        dialog.showModal();
      });
    });
    dialog.querySelectorAll("[data-clear-close]").forEach(function (b) {
      b.addEventListener("click", function () { dialog.close(); });
    });
    dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });   // click on the backdrop
    dialog.addEventListener("close", reset);
    if (submit) {
      submit.disabled = true;
      phrase.addEventListener("input", ready);
      password.addEventListener("input", ready);
      form.addEventListener("submit", function (e) {
        ready();
        if (submit.disabled) { e.preventDefault(); return; }
        var scope = form.querySelector("input[name=scope]:checked");
        var what = scope && scope.value === "all" ? "EVERY schedule" : "all cancelled schedules";
        if (!window.confirm("Last check: permanently delete " + what + "? This cannot be undone from the app.")) {
          e.preventDefault();
        }
      });
    }
  }
})();