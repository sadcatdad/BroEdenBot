class DiscordObjectPicker extends HTMLElement {
  constructor() {
    super();
    this.objects = [];
    this.groups = [];
    this.selected = new Set();
    this.collapsed = new Set();
    this.loaded = false;
    this.error = "";
  }

  connectedCallback() {
    this.mode = this.getAttribute("mode") || "object";
    this.endpoint = this.getAttribute("endpoint") || "/api/discord/guild-structure";
    this.inputName = this.getAttribute("input-name") || this.getAttribute("name") || "";
    this.settingKey = this.getAttribute("setting-key") || "";
    this.placeholder = this.getAttribute("placeholder") || "Search Discord objects";
    this.selected = new Set(this.initialValues());
    this.renderShell();
    this.loadObjects();
  }

  initialValues() {
    const raw = this.getAttribute("value") || "";
    if (!raw.trim()) return [];
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch (_error) {
      return raw.split(",").map((item) => item.trim()).filter(Boolean);
    }
    return [];
  }

  async loadObjects() {
    this.setState("loading");
    try {
      const response = await fetch(this.endpoint, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      this.applyData(data);
      this.loaded = true;
      this.error = "";
    } catch (_error) {
      this.loaded = true;
      this.error = "Could not load live Discord roles/channels. Check bot guild access and required intents.";
      this.objects = [];
      this.groups = [];
    }
    this.draw();
  }

  applyData(data) {
    if (this.mode === "role") {
      this.objects = Array.isArray(data) ? data : data.roles || [];
      this.groups = [{ id: "roles", name: "Roles", objects: this.objects }];
      return;
    }
    const structure = data || {};
    if (this.mode === "category") {
      this.objects = (structure.categories || []).map((category) => ({
        id: category.id,
        name: category.name,
        type: "category",
        position: category.position,
        child_channel_ids: category.child_channel_ids || [],
      }));
      this.groups = [{ id: "categories", name: "Categories", objects: this.objects }];
      return;
    }
    this.groups = (structure.categories || []).map((category) => ({
      id: category.id,
      name: category.name,
      objects: category.channels || [],
    }));
    const unparented = structure.uncategorized || [];
    if (unparented.length) {
      this.groups.push({ id: "uncategorized", name: "Uncategorized", objects: unparented });
    }
    this.objects = this.groups.flatMap((group) => group.objects);
  }

  renderShell() {
    this.classList.add("discord-picker");
    this.innerHTML = `
      <div class="discord-picker-selected"></div>
      <label class="discord-picker-search-label">
        <span>Search</span>
        <input class="discord-picker-search" type="search" placeholder="${this.escape(this.placeholder)}">
      </label>
      <div class="discord-picker-status muted">Loading live Discord metadata…</div>
      <div class="discord-picker-options"></div>
      <div class="discord-picker-missing"></div>
      <input class="discord-picker-value" type="hidden" name="${this.escape(this.inputName)}">
    `;
    this.querySelector(".discord-picker-search").addEventListener("input", () => this.draw());
    this.drawSelected();
  }

  setState(message) {
    const status = this.querySelector(".discord-picker-status");
    if (status) status.textContent = message === "loading" ? "Loading live Discord metadata…" : "";
  }

  draw() {
    this.drawSelected();
    const status = this.querySelector(".discord-picker-status");
    const options = this.querySelector(".discord-picker-options");
    const missing = this.querySelector(".discord-picker-missing");
    if (!status || !options || !missing) return;
    if (this.error) {
      status.innerHTML = `<div class="alert error">${this.escape(this.error)}</div>`;
      options.innerHTML = "";
      this.drawMissing();
      return;
    }
    status.textContent = "";
    const query = (this.querySelector(".discord-picker-search")?.value || "").toLowerCase();
    const visibleGroups = this.visibleGroups(query);
    if (!visibleGroups.length) {
      options.innerHTML = `<div class="empty-state">No matching live Discord ${this.mode}s found.</div>`;
      this.drawMissing();
      return;
    }
    options.innerHTML = visibleGroups.map((group) => this.groupHtml(group)).join("");
    options.querySelectorAll("[data-toggle-group]").forEach((button) => {
      button.addEventListener("click", () => {
        const id = String(button.dataset.toggleGroup);
        if (this.collapsed.has(id)) this.collapsed.delete(id);
        else this.collapsed.add(id);
        this.draw();
      });
    });
    options.querySelectorAll("[data-select-group]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const group = visibleGroups.find((item) => String(item.id) === String(checkbox.dataset.selectGroup));
        if (!group) return;
        group.objects.forEach((item) => {
          if (checkbox.checked) this.selected.add(String(item.id));
          else this.selected.delete(String(item.id));
        });
        this.draw();
      });
    });
    options.querySelectorAll("[data-clear-group]").forEach((button) => {
      button.addEventListener("click", () => {
        const group = visibleGroups.find((item) => String(item.id) === String(button.dataset.clearGroup));
        if (!group) return;
        group.objects.forEach((item) => this.selected.delete(String(item.id)));
        this.draw();
      });
    });
    options.querySelectorAll("input[data-object-id]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const value = String(checkbox.dataset.objectId);
        if (checkbox.checked) this.selected.add(value);
        else this.selected.delete(value);
        this.draw();
      });
    });
    this.drawMissing();
  }

  visibleGroups(query) {
    return this.groups
      .map((group) => ({
        ...group,
        objects: group.objects.filter((item) => this.matches(item, query)),
      }))
      .filter((group) => group.objects.length || !query);
  }

  matches(item, query) {
    if (!query) return true;
    return `${item.name || ""} ${item.id || ""}`.toLowerCase().includes(query);
  }

  groupHtml(group) {
    const collapsed = this.collapsed.has(String(group.id));
    const allSelected = group.objects.length && group.objects.every((item) => this.selected.has(String(item.id)));
    const rows = collapsed ? "" : group.objects.map((item) => this.optionHtml(item)).join("");
    const groupControls = this.mode === "channel" ? `
      <label class="category-select">
        <input type="checkbox" data-select-group="${this.escape(group.id)}" ${allSelected ? "checked" : ""}>
        Select all in category
      </label>
    ` : "";
    return `
      <section class="discord-picker-group">
        <div class="discord-picker-group-header">
          <button type="button" class="button secondary small" data-toggle-group="${this.escape(group.id)}">${collapsed ? "Expand" : "Collapse"}</button>
          <strong>${this.escape(group.name)}</strong>
          ${groupControls}
          <button type="button" class="button secondary small" data-clear-group="${this.escape(group.id)}">Clear section</button>
        </div>
        <div class="discord-picker-group-rows">${rows}</div>
      </section>
    `;
  }

  optionHtml(item) {
    const checked = this.selected.has(String(item.id)) ? "checked" : "";
    const swatch = this.mode === "role"
      ? `<span class="role-swatch" style="background:${this.escape(item.color || "#6b6d78")}"></span>`
      : `<span class="channel-kind">${this.mode === "category" ? "▸" : this.channelIcon(item)}</span>`;
    const meta = this.mode === "role"
      ? [
          item.member_count !== null && item.member_count !== undefined ? `${item.member_count} members` : "",
          item.managed ? "managed" : "",
          item.is_bot_role ? "bot role" : "",
        ].filter(Boolean).join(" · ")
      : [
          item.type || "",
          item.nsfw ? "NSFW" : "",
          item.parent_name ? `in ${item.parent_name}` : "",
        ].filter(Boolean).join(" · ");
    return `
      <label class="discord-picker-option">
        <input type="checkbox" data-object-id="${this.escape(item.id)}" ${checked}>
        ${swatch}
        <span>
          ${this.escape(this.displayName(item))}
          <small>${this.escape(meta)}${meta ? " · " : ""}${this.escape(item.id)}</small>
        </span>
      </label>
    `;
  }

  channelIcon(item) {
    const type = String(item.type || "").toLowerCase();
    if (type.includes("voice")) return "🔊";
    if (type.includes("forum")) return "◇";
    if (type.includes("stage")) return "🎙";
    return "#";
  }

  displayName(item) {
    if (this.mode === "role") return item.name || `Role ${item.id}`;
    if (this.mode === "category") return item.name || `Category ${item.id}`;
    const prefix = this.channelIcon(item);
    return `${prefix}${prefix === "#" ? "" : " "}${item.name || item.id}`;
  }

  drawSelected() {
    const hidden = this.querySelector(".discord-picker-value");
    if (hidden) hidden.value = JSON.stringify([...this.selected]);
    const selected = this.querySelector(".discord-picker-selected");
    if (!selected) return;
    const chips = [...this.selected].map((id) => {
      const item = this.objects.find((object) => String(object.id) === id);
      const label = item ? this.displayName(item) : `Missing: ${id}`;
      return `
        <button class="discord-picker-chip ${item ? "" : "missing-chip"}" type="button" data-remove-id="${this.escape(id)}">
          ${this.escape(label)} <span>×</span>
        </button>
      `;
    }).join("");
    selected.innerHTML = `
      <p class="label">Selected</p>
      <div class="discord-picker-chips">${chips || '<span class="muted">Nothing selected.</span>'}</div>
    `;
    selected.querySelectorAll("[data-remove-id]").forEach((button) => {
      button.addEventListener("click", () => {
        this.selected.delete(String(button.dataset.removeId));
        this.draw();
      });
    });
  }

  drawMissing() {
    const missing = this.querySelector(".discord-picker-missing");
    if (!missing) return;
    const liveIds = new Set(this.objects.map((item) => String(item.id)));
    const missingIds = [...this.selected].filter((id) => !liveIds.has(String(id)));
    if (!missingIds.length) {
      missing.innerHTML = "";
      return;
    }
    const noun = this.mode === "role" ? "roles" : this.mode === "category" ? "categories" : "channels";
    missing.innerHTML = `
      <div class="missing-panel">
        <p class="label">Missing saved ${noun}</p>
        ${missingIds.map((id) => `
          <div class="missing-row">
            <code>${this.escape(id)}</code>
            <button type="button" class="button secondary small" data-remove-missing="${this.escape(id)}">Remove</button>
          </div>
        `).join("")}
      </div>
    `;
    missing.querySelectorAll("[data-remove-missing]").forEach((button) => {
      button.addEventListener("click", () => {
        this.selected.delete(String(button.dataset.removeMissing));
        this.draw();
      });
    });
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#039;",
    }[char]));
  }
}

customElements.define("discord-object-picker", DiscordObjectPicker);
customElements.define("role-multi-select", class extends DiscordObjectPicker {
  connectedCallback() {
    this.setAttribute("mode", "role");
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/roles");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search roles");
    super.connectedCallback();
  }
});
customElements.define("channel-multi-select", class extends DiscordObjectPicker {
  connectedCallback() {
    this.setAttribute("mode", "channel");
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/guild-structure");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search channels");
    super.connectedCallback();
  }
});
customElements.define("category-multi-select", class extends DiscordObjectPicker {
  connectedCallback() {
    this.setAttribute("mode", "category");
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/guild-structure");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search categories");
    super.connectedCallback();
  }
});
