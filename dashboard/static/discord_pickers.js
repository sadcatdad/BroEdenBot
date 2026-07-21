class DiscordObjectPicker extends HTMLElement {
  constructor() {
    super();
    this.objects = [];
    this.groups = [];
    this.selected = new Set();
    this.expandedGroups = new Set();
    this.panelOpen = false;
    this.showAllResults = false;
    this.loaded = false;
    this.error = "";
  }

  connectedCallback() {
    this.mode = this.getAttribute("mode") || "object";
    this.endpoint = this.getAttribute("endpoint") || "/api/discord/guild-structure";
    this.inputName = this.getAttribute("input-name") || this.getAttribute("name") || "";
    this.placeholder = this.getAttribute("placeholder") || "Search Discord objects";
    this.single = this.hasAttribute("single");
    this.valueFormat = this.getAttribute("value-format") || "json";
    this.selected = new Set(this.initialValues());
    this.renderShell();
    this.loadObjects();
  }

  initialValues() {
    const raw = this.getAttribute("value") || "";
    if (!raw.trim()) return [];
    const csvValues = () => raw.split(",").map((item) => item.trim()).filter(Boolean);
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch (_error) {
      return csvValues();
    }
    return csvValues();
  }

  async loadObjects() {
    this.setStatus("Loading live Discord metadata…");
    try {
      const response = await fetch(this.endpoint, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.applyData(await response.json());
      this.error = "";
    } catch (_error) {
      this.error = "Could not load live Discord roles/channels. Check bot guild access and required intents.";
      this.objects = [];
      this.groups = [];
    }
    this.loaded = true;
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
      <div class="discord-picker-controls">
        <label class="discord-picker-search-label">
          <span>Search</span>
          <input class="discord-picker-search" type="search" placeholder="${this.escape(this.placeholder)}">
        </label>
        <button type="button" class="button secondary picker-browse"></button>
      </div>
      <div class="discord-picker-status muted">Loading live Discord metadata…</div>
      <div class="discord-picker-panel" hidden>
        <div class="discord-picker-options"></div>
      </div>
      <div class="discord-picker-missing"></div>
      <input class="discord-picker-value" type="hidden" name="${this.escape(this.inputName)}">
    `;
    this.querySelector(".discord-picker-search").addEventListener("input", () => {
      this.showAllResults = false;
      if (this.searchQuery()) this.panelOpen = true;
      this.draw();
    });
    this.querySelector(".picker-browse").addEventListener("click", () => {
      this.panelOpen = !this.panelOpen;
      this.showAllResults = false;
      this.draw();
    });
    this.drawSelected();
    this.updateBrowseButton();
  }

  setStatus(message) {
    const status = this.querySelector(".discord-picker-status");
    if (status) status.textContent = message || "";
  }

  draw() {
    this.drawSelected();
    this.updateBrowseButton();
    const status = this.querySelector(".discord-picker-status");
    const panel = this.querySelector(".discord-picker-panel");
    const options = this.querySelector(".discord-picker-options");
    if (!status || !panel || !options) return;

    if (this.error) {
      status.innerHTML = `<div class="alert error">${this.escape(this.error)}</div>`;
      panel.hidden = true;
      options.innerHTML = "";
      this.drawMissing();
      return;
    }

    status.textContent = "";
    const query = this.searchQuery();
    const shouldShowPanel = this.panelOpen || Boolean(query);
    panel.hidden = !shouldShowPanel;
    if (!shouldShowPanel) {
      options.innerHTML = "";
      this.drawMissing();
      return;
    }

    const content = this.mode === "channel"
      ? this.channelGroupsHtml(query)
      : this.flatGroupsHtml(query);
    options.innerHTML = content || `<div class="empty-state">No matching live Discord ${this.noun()} found.</div>`;
    this.bindPanelEvents();
    this.drawMissing();
  }

  bindPanelEvents() {
    const options = this.querySelector(".discord-picker-options");
    if (!options) return;
    options.querySelectorAll("[data-toggle-group]").forEach((button) => {
      button.addEventListener("click", () => {
        const id = String(button.dataset.toggleGroup);
        if (this.expandedGroups.has(id)) this.expandedGroups.delete(id);
        else this.expandedGroups.add(id);
        this.draw();
      });
    });
    options.querySelectorAll("[data-clear-group]").forEach((button) => {
      button.addEventListener("click", () => {
        const group = this.groups.find((item) => String(item.id) === String(button.dataset.clearGroup));
        if (!group) return;
        group.objects.forEach((item) => this.selected.delete(String(item.id)));
        this.draw();
        this.notifyChange();
      });
    });
    options.querySelectorAll("[data-show-more]").forEach((button) => {
      button.addEventListener("click", () => {
        this.showAllResults = true;
        this.draw();
      });
    });
    options.querySelectorAll("input[data-object-id]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const value = String(checkbox.dataset.objectId);
        if (checkbox.checked) {
          if (this.single) this.selected.clear();
          this.selected.add(value);
        }
        else this.selected.delete(value);
        this.draw();
        this.notifyChange();
      });
    });
  }

  flatGroupsHtml(query) {
    const max = this.resultLimit();
    const matched = this.objects.filter((item) => this.matches(item, query));
    const limited = this.showAllResults ? matched : matched.slice(0, max);
    if (!limited.length) return "";
    const label = this.mode === "role" ? "Roles" : "Categories";
    return `
      <section class="discord-picker-group">
        <div class="discord-picker-group-header simple">
          <strong>${this.escape(label)}</strong>
          <span class="muted">${matched.length} match${matched.length === 1 ? "" : "es"}</span>
        </div>
        <div class="discord-picker-group-rows">
          ${limited.map((item) => this.optionHtml(item)).join("")}
        </div>
        ${this.moreButton(matched.length, limited.length)}
      </section>
    `;
  }

  channelGroupsHtml(query) {
    const searching = Boolean(query);
    let rendered = 0;
    const max = this.resultLimit();
    const groups = [];
    for (const group of this.groups) {
      const matchingChildren = group.objects.filter((item) => this.matches(item, query));
      const categoryMatches = this.matches({ id: group.id, name: group.name }, query);
      if (searching && !categoryMatches && !matchingChildren.length) continue;
      if (rendered >= max && !this.showAllResults) continue;
      groups.push(this.channelGroupHtml(group, searching, matchingChildren, categoryMatches));
      rendered += 1 + (searching || this.expandedGroups.has(String(group.id)) ? matchingChildren.length : 0);
    }
    const totalMatches = groups.length;
    const more = !this.showAllResults && rendered >= max
      ? `<button type="button" class="button secondary small picker-more" data-show-more="1">Show more results</button>`
      : "";
    return groups.join("") + more;
  }

  channelGroupHtml(group, searching, matchingChildren, categoryMatches) {
    const expanded = searching || this.expandedGroups.has(String(group.id));
    const children = searching ? matchingChildren : group.objects;
    const visibleChildren = expanded ? children.slice(0, this.showAllResults ? children.length : this.resultLimit()) : [];
    const selectedCount = group.objects.filter((item) => this.selected.has(String(item.id))).length;
    return `
      <section class="discord-picker-group">
        <div class="discord-picker-category-row">
          <button type="button" class="category-toggle" data-toggle-group="${this.escape(group.id)}" aria-label="${expanded ? "Hide channels" : "Browse channels"}">${expanded ? "▾" : "▸"}</button>
          <span class="channel-kind">◇</span>
          <span class="row-main">
            <span class="row-title">${this.escape(group.name)}</span>
            <span class="row-meta">${group.objects.length} channel${group.objects.length === 1 ? "" : "s"}${selectedCount ? ` · ${selectedCount} selected` : ""}</span>
          </span>
          <button type="button" class="button secondary small" data-clear-group="${this.escape(group.id)}">Clear category</button>
        </div>
        <div class="discord-picker-group-rows ${expanded ? "" : "collapsed"}">
          ${visibleChildren.map((item) => this.optionHtml(item)).join("")}
          ${searching && categoryMatches && !matchingChildren.length ? '<p class="muted picker-note">Category matched. Expand it to browse child channels.</p>' : ""}
        </div>
      </section>
    `;
  }

  moreButton(total, shown) {
    if (this.showAllResults || total <= shown) return "";
    return `<button type="button" class="button secondary small picker-more" data-show-more="1">Show more results</button>`;
  }

  optionHtml(item) {
    const checked = this.selected.has(String(item.id)) ? "checked" : "";
    const inputType = this.single ? "radio" : "checkbox";
    const swatch = this.mode === "role"
      ? `<span class="role-swatch" style="background:${this.escape(item.color || "#6b6d78")}"></span>`
      : `<span class="channel-kind">${this.mode === "category" ? "◇" : this.channelIcon(item)}</span>`;
    const meta = this.metaText(item);
    return `
      <label class="discord-picker-option">
        <input type="${inputType}" data-object-id="${this.escape(item.id)}" ${checked}>
        ${swatch}
        <span class="row-main">
          <span class="row-title">${this.escape(this.displayName(item))}</span>
          <span class="row-meta">${this.escape(meta)}${meta ? " · " : ""}${this.escape(item.id)}</span>
        </span>
      </label>
    `;
  }

  metaText(item) {
    if (this.mode === "role") {
      return [
        item.member_count !== null && item.member_count !== undefined ? `${item.member_count} members` : "",
        item.managed ? "managed" : "",
        item.is_bot_role ? "bot role" : "",
      ].filter(Boolean).join(" · ");
    }
    if (this.mode === "category") {
      const count = Array.isArray(item.child_channel_ids) ? item.child_channel_ids.length : 0;
      return `${count} channel${count === 1 ? "" : "s"}`;
    }
    return [
      item.type || "",
      item.nsfw ? "NSFW" : "",
      item.parent_name ? `in ${item.parent_name}` : "",
    ].filter(Boolean).join(" · ");
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
    if (this.mode === "category") return `Category: ${item.name || item.id}`;
    return `${this.channelIcon(item)} ${item.name || item.id}`;
  }

  drawSelected() {
    const hidden = this.querySelector(".discord-picker-value");
    if (hidden) {
      hidden.value = this.valueFormat === "csv"
        ? [...this.selected].join(",")
        : JSON.stringify([...this.selected]);
    }
    const selected = this.querySelector(".discord-picker-selected");
    if (!selected) return;
    const chips = [...this.selected].map((id) => {
      const item = this.objects.find((object) => String(object.id) === id);
      const label = item ? this.displayName(item) : `Missing: ${id}`;
      return `
        <button class="discord-picker-chip ${item ? "" : "missing-chip"}" type="button" data-remove-id="${this.escape(id)}">
          ${this.escape(label)} <span aria-hidden="true">×</span>
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
        this.notifyChange();
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
    missing.innerHTML = `
      <div class="missing-panel">
        <p class="label">Missing saved ${this.noun()}s</p>
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
        this.notifyChange();
      });
    });
  }

  notifyChange() {
    this.dispatchEvent(new CustomEvent("discord-picker-change", { bubbles: true }));
  }

  updateBrowseButton() {
    const button = this.querySelector(".picker-browse");
    if (!button) return;
    const label = this.mode === "role" ? "roles" : this.mode === "category" ? "categories" : "channels";
    button.textContent = this.panelOpen ? `Hide ${label}` : `Browse ${label}`;
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

  searchQuery() {
    return (this.querySelector(".discord-picker-search")?.value || "").trim().toLowerCase();
  }

  resultLimit() {
    return this.mode === "role" ? 25 : 50;
  }

  noun() {
    if (this.mode === "role") return "role";
    if (this.mode === "category") return "category";
    return "channel";
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
customElements.define("role-single-select", class extends DiscordObjectPicker {
  connectedCallback() {
    this.setAttribute("mode", "role");
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/roles");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search roles");
    this.setAttribute("single", "");
    this.setAttribute("value-format", this.getAttribute("value-format") || "csv");
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
customElements.define("channel-single-select", class extends DiscordObjectPicker {
  connectedCallback() {
    this.setAttribute("mode", "channel");
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/guild-structure");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search channels");
    this.setAttribute("single", "");
    this.setAttribute("value-format", this.getAttribute("value-format") || "csv");
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
