(() => {
  const forms = document.querySelectorAll("[data-settings-form]");
  forms.forEach((form) => {
    const bar = form.querySelector("[data-save-bar]");
    const save = form.querySelector("[data-save-settings]");
    const discard = form.querySelector("[data-discard-settings]");
    if (!bar || !save) return;

    let dirty = false;
    const markDirty = () => {
      dirty = true;
      bar.hidden = false;
      save.disabled = false;
    };
    form.addEventListener("input", markDirty);
    form.addEventListener("change", markDirty);
    form.addEventListener("discord-picker-change", markDirty);
    discard?.addEventListener("click", () => {
      dirty = false;
      window.location.reload();
    });
    form.addEventListener("submit", () => {
      dirty = false;
      save.disabled = true;
      save.textContent = "Saving…";
    });
    window.addEventListener("beforeunload", (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
  });
})();
