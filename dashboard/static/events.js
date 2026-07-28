(() => {
  const filters = [...document.querySelectorAll("[data-event-filter]")];
  filters.forEach((button) => button.addEventListener("click", () => {
    filters.forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll("[data-event-type]").forEach((card) => {
      card.hidden = button.dataset.eventFilter !== "all" && card.dataset.eventType !== button.dataset.eventFilter;
    });
  }));
  const calendarMonth = document.querySelector("[data-event-calendar-month]");
  const calendarYear = document.querySelector("[data-event-calendar-year]");
  const changeCalendar = () => {
    if (!calendarMonth?.value || !calendarYear?.value) return;
    const month = String(calendarMonth.value).padStart(2, "0");
    const search = new URLSearchParams(window.location.search);
    search.set("month", `${calendarYear.value}-${month}`);
    window.location.search = search.toString();
  };
  calendarMonth?.addEventListener("change", changeCalendar);
  calendarYear?.addEventListener("change", changeCalendar);
  const eventsHeading = document.querySelector("[data-events-revision]");
  if (eventsHeading) {
    let checking = false;
    const checkForUpdates = async () => {
      if (checking || document.hidden) return;
      checking = true;
      try {
        const response = await fetch(eventsHeading.dataset.eventsStateUrl, {
          headers: {Accept: "application/json"},
          cache: "no-store",
        });
        if (!response.ok) return;
        const result = await response.json();
        if (result.revision && result.revision !== eventsHeading.dataset.eventsRevision) {
          window.location.reload();
        }
      } catch (_) {
        // A transient refresh failure should not interrupt the current page.
      } finally {
        checking = false;
      }
    };
    window.setInterval(checkForUpdates, 60000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) checkForUpdates();
    });
  }
  const editor = document.querySelector("[data-event-editor]");
  if (editor) {
    const radios = [...editor.querySelectorAll('[name="entity_type"]')];
    const stage = editor.querySelector("[data-stage-channel]");
    const voice = editor.querySelector("[data-voice-channel]");
    const location = editor.querySelector("[data-location]");
    const update = () => {
      const value = radios.find((radio) => radio.checked)?.value || "stage";
      editor.querySelectorAll("[data-channel-group]").forEach((group) => { group.hidden = group.dataset.channelGroup !== value; });
      stage.name = value === "stage" ? "channel_id" : ""; stage.required = value === "stage";
      voice.name = value === "voice" ? "channel_id" : ""; voice.required = value === "voice";
      location.name = value === "external" ? "location" : ""; location.required = value === "external";
    };
    radios.forEach((radio) => radio.addEventListener("change", update)); update();
  }
  const action = document.querySelector("[data-event-action-url]");
  if (action) {
    const check = async () => {
      try {
        const response = await fetch(action.dataset.eventActionUrl, {headers: {Accept: "application/json"}, cache: "no-store"});
        if (!response.ok) return;
        const result = await response.json();
        action.textContent = result.status === "failed" ? `Event action failed: ${result.failure_reason || "Unknown error"}` : result.status === "completed" ? (result.result_message || "Event action completed.") : `Event action is ${result.status}…`;
        if (["pending", "processing"].includes(result.status)) window.setTimeout(check, 2000);
      } catch (_) { action.textContent = "Event action status is temporarily unavailable."; }
    }; check();
  }
})();
