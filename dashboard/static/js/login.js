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
  if (form && submitBtn) {
    form.addEventListener("submit", () => {
      submitBtn.classList.add("is-loading");
      submitBtn.disabled = true;
    });
  }
})();
