// Settings page chrome only (config.js owns every real action):
//  - scroll-spy: highlights the section nav entry for the section in view
//  - save feedback: each [data-settings-form] section shows "Unsaved
//    changes" once edited, "Saving…" while its primary button is busy,
//    and "Saved at HH:MM" when a success toast lands during that save.
(function () {
  const links = Array.from(document.querySelectorAll(".settings-nav-link"));
  const sections = links.map((a) => document.querySelector(a.getAttribute("href"))).filter(Boolean);

  function setActive(id) {
    links.forEach((a) => {
      const on = a.getAttribute("href") === "#" + id;
      a.classList.toggle("is-active", on);
      if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    });
  }
  // Active = the last section whose top has scrolled past the floating
  // topbar (rAF-throttled scroll listener; passive, read-only).
  let ticking = false;
  function spy() {
    ticking = false;
    const line = 140;
    let current = sections[0];
    for (const sec of sections) { if (sec.getBoundingClientRect().top <= line) current = sec; }
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) current = sections[sections.length - 1];
    if (current) setActive(current.id);
  }
  window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(spy); } }, { passive: true });
  if (location.hash) setActive(location.hash.slice(1)); else if (sections[0]) setActive(sections[0].id);

  // ---------------------------------------------------------------- save state
  const toasts = document.getElementById("toasts");
  document.querySelectorAll("[data-settings-form]").forEach((section) => {
    const state = section.querySelector(".save-state");
    const primary = section.querySelector(".form-actions .btn-primary");
    if (!state || !primary) return;
    let saving = false;

    function show(text, cls, iconName) {
      state.className = "save-state" + (cls ? " " + cls : "");
      state.innerHTML = text ? (iconName ? icon(iconName, "icon-sm") : "") + `<span>${text}</span>` : "";
    }

    section.addEventListener("input", (e) => {
      if (e.target.closest(".form-actions")) return;
      if (!saving) show("Unsaved changes", "is-dirty", "pencil");
    });
    section.addEventListener("change", (e) => {
      if (e.target.closest(".form-actions")) return;
      if (!saving) show("Unsaved changes", "is-dirty", "pencil");
    });
    primary.addEventListener("click", () => {
      saving = true;
      show("Saving…", "", null);
      setTimeout(() => { if (saving) { saving = false; show("", "", null); } }, 15000);
    });
    if (toasts) {
      new MutationObserver((muts) => {
        if (!saving) return;
        for (const m of muts) {
          for (const n of m.addedNodes) {
            if (!(n instanceof HTMLElement) || !n.classList.contains("toast")) continue;
            saving = false;
            if (n.classList.contains("err")) { show("Not saved - see the message", "is-error", "alert-circle"); return; }
            const t = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            show(`Saved at ${t}`, "is-ok", "check-circle-2");
            return;
          }
        }
      }).observe(toasts, { childList: true });
    }
  });
})();
