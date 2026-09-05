(() => {
  document.addEventListener("submit", (event) => {
    const message = event.target.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-auto-submit]")) event.target.form?.requestSubmit();
  });
})();
