(function () {
  const pwInput = document.getElementById("password");
  const pwToggle = document.getElementById("password-toggle");
  if (pwInput && pwToggle) {
    pwToggle.addEventListener("click", () => {
      const showing = pwInput.type === "text";
      pwInput.type = showing ? "password" : "text";
      pwToggle.setAttribute("aria-pressed", String(!showing));
      pwToggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      pwToggle.setAttribute("data-tooltip", showing ? "Show password" : "Hide password");
      pwToggle.innerHTML = icon(showing ? "eye" : "eye-off", "icon-sm");
      pwInput.focus({ preventScroll: true });
    });
  }

  const form = document.getElementById("login-form");
  const submitBtn = document.getElementById("login-submit");
  // Inline validation: errors appear under the field they belong to,
  // and clear as soon as the field has a value again.
  function check(input, errId) {
    const err = document.getElementById(errId);
    const bad = !input.value.trim();
    input.setAttribute("aria-invalid", String(bad));
    if (err) err.hidden = !bad;
    return !bad;
  }
  if (form && submitBtn) {
    const user = document.getElementById("username");
    [[user, "username-error"], [pwInput, "password-error"]].forEach(([el, id]) => {
      if (el) el.addEventListener("input", () => { if (el.getAttribute("aria-invalid") === "true") check(el, id); });
    });
    form.addEventListener("submit", (e) => {
      const ok = [check(user, "username-error"), check(pwInput, "password-error")].every(Boolean);
      if (!ok) {
        e.preventDefault();
        (user.getAttribute("aria-invalid") === "true" ? user : pwInput).focus();
        return;
      }
      submitBtn.classList.add("is-loading");
      submitBtn.disabled = true;
    });
  }
})();
