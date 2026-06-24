class DiscordObjectPicker extends HTMLElement {
  constructor() {
    super();
    this.objects = [];
    this.selected = new Set();
  }

  connectedCallback() {
    this.endpoint = this.getAttribute("endpoint") || "";
    this.inputName = this.getAttribute("input-name") || this.getAttribute("name") || "";
    this.placeholder = this.getAttribute("placeholder") || "Search Discord objects";
    this.selected = new Set(this.initialValues());
    this.render();
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
    if (!this.endpoint) return;
    try {
      const response = await fetch(this.endpoint, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      this.objects = Array.isArray(data) ? data : data.objects || [];
    } catch (_error) {
      this.objects = [];
    }
    this.drawOptions();
  }

  render() {
    this.classList.add("discord-picker");
    this.innerHTML = `
      <input class="discord-picker-search" type="search" placeholder="${this.escape(this.placeholder)}">
      <div class="discord-picker-chips"></div>
      <div class="discord-picker-options"></div>
      <input class="discord-picker-value" type="hidden" name="${this.escape(this.inputName)}">
    `;
    this.querySelector(".discord-picker-search").addEventListener("input", () => this.drawOptions());
    this.drawChips();
  }

  drawOptions() {
    const query = (this.querySelector(".discord-picker-search")?.value || "").toLowerCase();
    const options = this.querySelector(".discord-picker-options");
    if (!options) return;
    const objects = this.objects
      .filter((item) => `${item.name || ""} ${item.id || ""}`.toLowerCase().includes(query))
      .slice(0, 80);
    options.innerHTML = objects.map((item) => {
      const checked = this.selected.has(String(item.id)) ? "checked" : "";
      const missing = item.missing ? " · missing" : "";
      return `
        <label class="discord-picker-option">
          <input type="checkbox" value="${this.escape(item.id)}" ${checked}>
          <span>${this.escape(item.name || `Missing: ${item.id}`)}<small>${this.escape(item.id)}${missing}</small></span>
        </label>
      `;
    }).join("") || `<p class="muted">No matching Discord objects found.</p>`;
    options.querySelectorAll("input[type='checkbox']").forEach((checkbox) => {
      checkbox.addEventListener("change", (event) => {
        const value = String(event.target.value);
        if (event.target.checked) this.selected.add(value);
        else this.selected.delete(value);
        this.drawChips();
      });
    });
    this.drawChips();
  }

  drawChips() {
    const chips = this.querySelector(".discord-picker-chips");
    const hidden = this.querySelector(".discord-picker-value");
    if (hidden) hidden.value = JSON.stringify([...this.selected]);
    if (!chips) return;
    chips.innerHTML = [...this.selected].map((id) => {
      const item = this.objects.find((object) => String(object.id) === id);
      const label = item?.name || `Missing: ${id}`;
      return `
        <button class="discord-picker-chip" type="button" data-id="${this.escape(id)}">
          ${this.escape(label)} <span>×</span>
        </button>
      `;
    }).join("");
    chips.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        this.selected.delete(String(button.dataset.id));
        this.drawOptions();
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
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/roles");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search roles");
    super.connectedCallback();
  }
});
customElements.define("channel-multi-select", class extends DiscordObjectPicker {
  connectedCallback() {
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/channels");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search channels");
    super.connectedCallback();
  }
});
customElements.define("category-multi-select", class extends DiscordObjectPicker {
  connectedCallback() {
    this.setAttribute("endpoint", this.getAttribute("endpoint") || "/api/discord/categories");
    this.setAttribute("placeholder", this.getAttribute("placeholder") || "Search categories");
    super.connectedCallback();
  }
});
