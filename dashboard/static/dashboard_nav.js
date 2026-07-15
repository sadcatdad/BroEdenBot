(() => {
  const body = document.body;
  const toggle = document.querySelector("[data-nav-toggle]");
  const sidebar = document.querySelector(".site-sidebar");
  const closeControls = document.querySelectorAll("[data-nav-close]");

  if (!toggle || !sidebar) return;

  const setOpen = (open, returnFocus = false) => {
    body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      sidebar.querySelector("[data-nav-close]")?.focus();
    } else if (returnFocus) {
      toggle.focus();
    }
  };

  toggle.addEventListener("click", () => setOpen(!body.classList.contains("nav-open")));
  closeControls.forEach((control) => control.addEventListener("click", () => setOpen(false, true)));
  sidebar.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => setOpen(false)));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && body.classList.contains("nav-open")) setOpen(false, true);
  });
  window.matchMedia("(min-width: 981px)").addEventListener("change", (event) => {
    if (event.matches) setOpen(false);
  });
})();
