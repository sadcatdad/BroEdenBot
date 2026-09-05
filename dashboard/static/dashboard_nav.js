(() => {
  const body = document.body;
  const toggle = document.querySelector("[data-nav-toggle]");
  const sidebar = document.querySelector(".site-sidebar");
  const closeControls = document.querySelectorAll("[data-nav-close]");

  if (!toggle || !sidebar) return;
  const workspace = document.querySelector(".app-workspace");
  const skipLink = document.querySelector(".skip-link");
  const mobile = window.matchMedia("(max-width: 980px)");
  const focusable = () => [...sidebar.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"]'
  )].filter((element) => element.getClientRects().length && !element.closest("[inert]"));

  const setOpen = (open, returnFocus = false) => {
    open = open && mobile.matches;
    body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    sidebar.inert = mobile.matches && !open;
    if (workspace) workspace.inert = open;
    if (skipLink) skipLink.inert = open;
    if (open) {
      sidebar.setAttribute("role", "dialog");
      sidebar.setAttribute("aria-modal", "true");
    } else {
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-modal");
    }
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
    if (event.key !== "Tab" || !body.classList.contains("nav-open")) return;
    const elements = focusable();
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  });
  mobile.addEventListener("change", () => setOpen(false));
  setOpen(false);
})();
