(() => {
  "use strict";
  const root = document.getElementById("automation-manager");
  if (!root) return;

  const by = (name) => document.getElementById(`automation-${name}`);
  const canManage = root.dataset.manage === "yes";
  const moduleOn = root.dataset.enabled === "yes";
  const endpoint = "/api/automations";
  const state = { rows: [], selected: null, draft: null, initial: null, catalog: null, discord: {}, labels: {}, loading: false };
  let nextId = 0;
  const newId = () => `auto-field-${++nextId}`;
  const copy = (data) => JSON.parse(JSON.stringify(data));
  const editable = () => canManage && !state.selected?.archived;
  const changed = () => state.draft && JSON.stringify(state.draft) !== (state.selected ? JSON.stringify(state.selected.rule) : state.initial);
  const date = (seconds) => new Date(seconds * 1000).toLocaleString();
  const title = (value) => value.replaceAll("_", " ").replace(/^./, (char) => char.toUpperCase());

  function el(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) {
      if (value !== undefined && value !== null) node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child !== null && child !== undefined && typeof child !== "boolean") node.append(child);
    }
    return node;
  }

  function button(label, run, extra = "", disabled = false) {
    const node = el("button", { type: "button", class: `button secondary auto-compact ${extra}` }, label);
    node.disabled = disabled;
    node.addEventListener("click", run);
    return node;
  }

  function field(label, control, hint) {
    control.id = control.id || newId();
    return el("div", { class: "auto-field" },
      el("label", { for: control.id }, label), control,
      hint && el("small", { class: "muted" }, hint));
  }

  function input(label, value, change, options = {}) {
    const { type = "text", hint, disabled = !editable(), ...attributes } = options;
    const node = el(type === "textarea" ? "textarea" : "input",
      type === "textarea" ? { rows: 3, ...attributes } : { type, ...attributes });
    node.value = value ?? "";
    node.disabled = disabled;
    node.addEventListener("input", () => change(type === "number" ? (node.value === "" ? undefined : Number(node.value)) : node.value));
    return field(label, node, hint);
  }

  function select(label, value, choices, change, disabled = !editable()) {
    const node = el("select", {}, choices.map(([key, label]) => el("option", { value: key }, label)));
    node.value = value ?? "";
    node.disabled = disabled;
    node.addEventListener("change", () => change(node.value));
    return field(label, node);
  }

  function check(label, checked, change, disabled = !editable()) {
    const node = el("input", { type: "checkbox" });
    node.checked = checked;
    node.disabled = disabled;
    node.addEventListener("change", () => change(node.checked));
    return el("label", { class: "auto-check" }, node, " ", label);
  }

  function indexLabels() {
    for (const [kind, choices] of Object.entries(state.discord)) {
      const prefix = kind === "channel" ? "#" : kind === "role" ? "@" : "";
      const names = choices.map(({ name, parent_name }) => parent_name ? parent_name + " / " + prefix + name : prefix + name);
      const counts = new Map();
      names.forEach((name) => counts.set(name, (counts.get(name) || 0) + 1));
      state.labels[kind] = new Map(choices.map(({ id }, i) => [id,
        counts.get(names[i]) > 1 ? `${names[i]} · ${id}` : names[i]]));
    }
  }

  function picker(label, value, set, kind, disabled = !editable()) {
    const choices = state.discord[kind] || [];
    const id = choices.length ? newId() : undefined;
    const labels = state.labels[kind];
    const names = new Map(labels ? [...labels].map(([id, name]) => [name, id]) : []);
    const display = labels?.get(value) || value;
    const wrapper = input(label, display, (raw) => set(names.get(raw) || raw.trim()), {
      list: id, placeholder: kind === "emoji" ? "Emoji or custom emoji ID" : choices.length ? "Search by name or paste an ID" : "Discord ID",
      inputmode: id || kind === "emoji" ? undefined : "numeric", disabled,
    });
    if (id) {
      wrapper.append(el("datalist", { id }, [...names].map(([name, key]) =>
        el("option", { value: name, label: key }))));
    }
    return wrapper;
  }

  function arrayPicker(label, values, kind, set, disabled = !editable()) {
    const wrapper = el("div", { class: "auto-array" });
    function remove(index) {
      values.splice(index, 1);
      set(values);
      render();
    }
    function render() {
      const chips = [];
      const fields = [];
      values.forEach((value, index) => {
        const name = state.labels[kind]?.get(value);
        if (name) {
          const dismiss = disabled ? null : button("×", () => remove(index), "auto-chip-remove");
          dismiss?.setAttribute("aria-label", `Remove ${name}`);
          chips.push(el("span", { class: "auto-chip" }, name, dismiss));
          return;
        }
        const control = picker(`${label} ${index + 1}`, value, (id) => { values[index] = id; set(values); }, kind, disabled);
        fields.push(el("div", { class: "auto-array-row" }, control,
          disabled ? null : button("Remove", () => remove(index))));
      });
      wrapper.replaceChildren();
      if (chips.length) wrapper.append(el("div", { class: "auto-chips", "aria-label": `${label} selections` }, chips));
      wrapper.append(...fields);
      if (!disabled) {
        wrapper.append(el("div", {}, button(`Add ${label.toLowerCase()}`, () => {
          values.push(""); set(values); render();
          Array.from(wrapper.querySelectorAll(".auto-array-row input")).pop()?.focus();
        }, "", values.length >= 25)));
      }
    }
    render();
    return wrapper;
  }

  function targetPicker(label, value, set) {
    const type = value === "actor" || value === "subject" ? value : "explicit";
    const choices = [["actor", "Actor (who triggered the event)"], ["subject", "Subject (related user)"], ["explicit", "Specific user ID"]];
    const userInput = picker("Specific user ID", type === "explicit" ? value : "", set, "user");
    userInput.hidden = type !== "explicit";
    return el("div", {},
      select(label, type, choices, (key) => {
        userInput.hidden = key !== "explicit";
        const control = userInput.querySelector("input");
        set(key === "explicit" ? control.value : key);
        if (key === "explicit") control.focus();
      }), userInput);
  }

  function put(item, key, value) {
    if (value === undefined || value === "") delete item[key];
    else item[key] = value;
  }

  async function api(path = "", method = "GET", body) {
    const response = await fetch(endpoint + path, {
      method, redirect: "error",
      headers: method === "GET" ? {} : { "Content-Type": "application/json", "X-CSRF-Token": root.dataset.csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (Array.isArray(data.detail)) {
        throw new Error(data.detail.map(({ path, message }) => path ? `${path} — ${message}` : message).join("\n"));
      }
      throw new Error(data.detail || `Request failed (HTTP ${response.status}).`);
    }
    return data;
  }

  function feedback(node, message, failure = false) {
    node.textContent = message || "";
    node.classList.toggle("auto-error", failure);
    node.hidden = !message;
  }

  async function work(control, task, output = by("status")) {
    control.disabled = true;
    try {
      await task();
    } catch (error) {
      feedback(output, error.message, true);
    } finally {
      control.disabled = false;
    }
  }

  function renderList() {
    const filter = by("search").value.trim().toLowerCase();
    const matches = state.rows.filter(({ rule }) => [rule.name, triggerLabel(rule.trigger)]
      .some((value) => value.toLowerCase().includes(filter)));
    const list = by("rules");
    list.replaceChildren(...matches.map((row) => {
      const open = el("button", { type: "button", class: "automation-rule-open", "data-rule-id": row.id }, row.rule.name);
      open.addEventListener("click", () => openRule(row));
      const badges = el("div", { class: "auto-row" },
        el("span", { class: "badge" }, triggerLabel(row.rule.trigger)),
        el("span", { class: "badge" }, row.archived ? "Archived" : row.rule.enabled ? "Enabled" : "Disabled"));
      const status = el("small", { class: "muted" }, `#${row.id} · ${row.last_triggered_at ? "Last triggered " + date(row.last_triggered_at) : "Never triggered"}`);
      const toggle = canManage && !row.archived && (moduleOn || row.rule.enabled)
        ? button(row.rule.enabled ? "Disable" : "Enable", (event) =>
          work(event.currentTarget, () => toggleRule(row, by("status")))) : null;
      if (toggle) toggle.setAttribute("aria-label", `${toggle.textContent} ${row.rule.name}`);
      return el("li", {}, open, badges, status, toggle);
    }));
    if (!matches.length) list.append(el("li", { class: "muted" }, filter ? "No rules match your search." :
      by("archived").checked ? "No archived rules." : "No automation rules."));
  }

  async function loadRules() {
    if (state.loading) return;
    state.loading = true;
    by("rules").setAttribute("aria-busy", "true");
    try {
      const data = await api(by("archived").checked ? "?archived=true" : "");
      state.rows = data.rules;
      state.catalog = data.editor;
      state.discord = data.discord;
      indexLabels();
      renderList();
    } finally {
      state.loading = false;
      by("rules").removeAttribute("aria-busy");
    }
  }

  function replaceRow(row) {
    const index = state.rows.findIndex(({ id }) => id === row.id);
    if (index >= 0) state.rows[index] = row;
    else if (!by("archived").checked) state.rows.unshift(row);
    renderList();
  }

  async function toggleRule(row, output) {
    const saved = await api(`/${row.id}/toggle`, "POST", { revision: row.revision, enabled: !row.rule.enabled });
    if (state.selected?.id === saved.id && state.selected.revision === row.revision) {
      state.selected = saved;
      state.draft.enabled = saved.rule.enabled;
      updateEditorHeader();
    }
    replaceRow(saved);
    feedback(output, saved.rule.enabled ? "Rule enabled." : "Rule disabled.");
  }

  function openRule(row = null, draft = null) {
    if (!state.catalog) return;
    state.selected = row;
    state.draft = copy(draft || row?.rule || {
      name: "", trigger: { type: "" }, allow_bots: false, actions: [],
    });
    state.initial = draft ? null : JSON.stringify(state.draft);
    by("list-view").hidden = true;
    by("editor").hidden = false;
    feedback(by("error"), "");
    renderEditor();
    window.scrollTo(0, 0);
    by("editor-title").focus({ preventScroll: true });
    return true;
  }

  function closeRule() {
    if (changed() && !window.confirm("Discard unsaved rule changes?")) return;
    const id = state.selected?.id;
    state.selected = null;
    state.draft = null;
    by("editor").hidden = true;
    by("list-view").hidden = false;
    renderList();
    window.scrollTo(0, 0);
    (by("rules").querySelector(`[data-rule-id="${id}"]`) || by("list-title")).focus({ preventScroll: true });
    return true;
  }

  function updateEditorHeader() {
    const row = state.selected;
    by("editor-title").textContent = row ? row.rule.name : "New rule";
    by("revision").textContent = row ? `#${row.id} · Revision ${row.revision} · ${row.archived ? "Archived" : row.rule.enabled ? "Enabled" : "Disabled"}` : "Unsaved";
    by("history").hidden = !row;
    by("test").hidden = Boolean(row?.archived);
    if (canManage) {
      by("save").hidden = !editable();
      by("validate").hidden = !editable();
      by("archive").hidden = !row || row.archived;
      const toggle = by("editor-toggle");
      toggle.hidden = !row || row.archived || (!moduleOn && !row.rule.enabled);
      toggle.textContent = row?.rule.enabled ? "Disable rule" : "Enable rule";
    }
  }

  let pickerActive;
  const pickerDialog = by("picker-dialog");
  const pickerSearch = by("picker-search");

  function drawChoices() {
    const query = pickerSearch.value.trim().toLowerCase();
    const { choices, pick } = pickerActive;
    const items = choices.filter((choice) => [choice.label, choice.group, choice.keywords]
      .some((value) => value?.toLowerCase().includes(query)));
    const grid = by("picker-options");
    grid.replaceChildren(...items.map((choice) => {
      const control = button(choice.label, () => {
        pickerDialog.close();
        pick(choice);
      }, "auto-choice");
      control.setAttribute("aria-label", choice.label);
      if (choice.group) control.prepend(el("small", {}, choice.group));
      return control;
    }));
    if (!items.length) grid.append(el("p", { class: "muted" }, "No matching options."));
  }

  function choose(title, choices, pick, kind) {
    pickerActive = { choices, pick };
    pickerDialog.dataset.kind = kind;
    by("picker-title").textContent = title;
    pickerSearch.value = "";
    drawChoices();
    pickerDialog.showModal();
    pickerSearch.focus();
  }
  pickerSearch.addEventListener("input", drawChoices);

  function stage(label, kind, body) {
    return el("section", { class: "auto-stage", "data-kind": kind },
      el("h2", { class: "auto-stage-label" }, label), body);
  }

  function addTile(label, handler) {
    const control = button("", handler, "auto-add");
    control.append(el("span", { class: "auto-plus", "aria-hidden": "true" }, "+"), label);
    return control;
  }

  function renderEditor() {
    updateEditorHeader();
    const trigger = el("div");
    const conditions = el("div");
    const actions = el("div");
    by("fields").replaceChildren(
      el("div", { class: "auto-rule-name" },
        input("Rule name", state.draft.name, (name) => { state.draft.name = name; }, { maxlength: 100 })),
      el("div", { class: "auto-stages" },
        stage("When", "trigger", trigger),
        stage("If", "condition", conditions),
        stage("Do", "action", actions)));
    drawTrigger(trigger);
    drawConditions(conditions);
    drawActions(actions);
  }

  function triggerOptions() {
    const extras = [
      { type: "MEMBER_UPDATE", member_change: "role_added", label: "Member gains a role", group: "Members" },
      { type: "MEMBER_UPDATE", member_change: "role_removed", label: "Member loses a role", group: "Members" },
      { type: "VOICE_STATE_UPDATE", voice_change: "join", label: "Member joins voice", group: "Voice" },
      { type: "VOICE_STATE_UPDATE", voice_change: "leave", label: "Member leaves voice", group: "Voice" },
      { type: "VOICE_STATE_UPDATE", voice_change: "move", label: "Member moves voice channels", group: "Voice" },
    ];
    const categories = {
      MESSAGE: "Messages", COMMAND: "Messages", REACTION_ADD: "Messages", REACTION_REMOVE: "Messages", BUTTON_CLICK: "Messages",
      THREAD_CREATE: "Threads", MEMBER_JOIN: "Members", MEMBER_UPDATE: "Members", VOICE_STATE_UPDATE: "Voice",
    };
    return Object.entries(state.catalog.triggers).map(([type, label]) => ({
      type, label, group: categories[type],
    })).concat(extras);
  }

  function triggerLabel(trigger) {
    const shortcut = triggerOptions().find((option) => option.type === trigger.type &&
      ((option.member_change && option.member_change === trigger.member_change) ||
       (option.voice_change && option.voice_change === trigger.voice_change)));
    return shortcut?.label || state.catalog.triggers[trigger.type];
  }

  function drawTrigger(container) {
    const trigger = state.draft.trigger;
    const catalog = state.catalog;
    function selectTrigger() {
      choose("Choose a trigger", triggerOptions(), ({ type, member_change, voice_change }) => {
        if (trigger.type !== type) {
          state.draft.trigger = { type };
          if (type === "COMMAND") {
            state.draft.trigger.command = { name: "", require_mention: true, access: "administrators" };
          }
          if (trigger.channel_ids?.length && catalog.channel_triggers.includes(type)) {
            state.draft.trigger.channel_ids = trigger.channel_ids;
          }
        }
        put(state.draft.trigger, "member_change", member_change);
        put(state.draft.trigger, "voice_change", voice_change);
        if (!["role_added", "role_removed"].includes(member_change)) delete state.draft.trigger.role_id;
        drawTrigger(container);
        container.querySelector("button")?.focus();
      }, "trigger");
    }
    if (!trigger.type) {
      container.replaceChildren(addTile("Choose trigger", selectTrigger));
      return;
    }
    const grid = el("div", { class: "auto-grid" });
    const set = (key) => (value) => put(trigger, key, value);
    if (trigger.type === "COMMAND") {
      const command = trigger.command;
      const changeAccess = (access) => {
        command.access = access;
        if (access !== "roles") delete command.role_ids;
        drawTrigger(container);
        container.querySelector("[data-command-access] select")?.focus();
      };
      grid.append(
        input("Command name", command.name, (name) => { command.name = name.trim(); }, {
          maxlength: 33, placeholder: "?verify",
          hint: "Start with ? and use letters, numbers, underscores or hyphens.",
          autocapitalize: "off", spellcheck: "false",
        }),
        el("div", { "data-command-access": "" }, select("Who can run it", command.access, [
          ["administrators", "Discord administrators"], ["roles", "Members with selected roles"], ["everyone", "Everyone in the server"],
        ], changeAccess)),
        check("Require a mentioned member", command.require_mention, (required) => {
          command.require_mention = required;
        }),
        command.access === "roles" && arrayPicker("Allowed role", command.role_ids || [], "role", (roles) => { command.role_ids = roles; }));
    }
    if (["REACTION_ADD", "REACTION_REMOVE"].includes(trigger.type)) {
      grid.append(picker("Filter by emoji", trigger.emoji, set("emoji"), "emoji"));
    }
    if (trigger.type === "BUTTON_CLICK") {
      grid.append(input("Button ID", trigger.button_id, set("button_id"), { maxlength: 32 }),
        input("Posting rule ID (optional)", trigger.source_rule_id, set("source_rule_id"), { type: "number", min: 1 }));
    }
    if (trigger.type === "MEMBER_UPDATE") {
      grid.append(select("Member change", trigger.member_change || "any", catalog.member_changes.map((key) => [key, title(key)]), (kind) => {
        trigger.member_change = kind;
        if (!["role_added", "role_removed"].includes(kind)) delete trigger.role_id;
        drawTrigger(container);
        container.querySelector("select")?.focus();
      }));
      if (["role_added", "role_removed"].includes(trigger.member_change)) {
        grid.append(picker("Filter by role", trigger.role_id, set("role_id"), "role"));
      }
    }
    if (trigger.type === "VOICE_STATE_UPDATE" && !["join", "leave", "move"].includes(trigger.voice_change)) {
      grid.append(select("Voice change", trigger.voice_change || "any", catalog.voice_changes.map((key) => [key, title(key)]), (kind) => {
        trigger.voice_change = kind;
        drawTrigger(container);
        container.querySelector("button")?.focus();
      }));
    }
    const filters = el("details", { class: "auto-advanced" }, el("summary", {}, "Trigger filters"));
    if (catalog.channel_triggers.includes(trigger.type)) {
      filters.append(arrayPicker("Trigger channel", trigger.channel_ids || [], "channel", set("channel_ids")));
    }
    filters.append(check("Allow other bots to trigger this rule", state.draft.allow_bots, (checked) => {
      state.draft.allow_bots = checked;
    }));
    filters.open = Boolean(trigger.channel_ids?.length || state.draft.allow_bots);
    container.replaceChildren(el("div", { class: "auto-node" },
      el("div", { class: "auto-node-head auto-row auto-between" },
        el("h3", {}, triggerLabel(trigger)),
        editable() && button("Change trigger", selectTrigger, "auto-quiet")),
      grid.children.length ? grid : null,
      trigger.type === "COMMAND" && el("p", { class: "muted auto-command-hint" },
        "The actor runs the command. The mentioned member is the subject."),
      filters));
  }

  function newCondition(type, target) {
    const [, kind, targeted] = state.catalog.conditions[type];
    const defaults = { boolean: true, number: 0, presence: "online", cooldown: { scope: "user", seconds: 60 } };
    const value = defaults[kind] ?? (["users", "channels", "roles", "categories"].includes(kind) ? [] : "");
    return targeted && target ? { type, target, value } : { type, value };
  }

  function chooseCondition(onPick, change = false) {
    const groupNames = {
      users: "Members", user: "Members", presence: "Members", role: "Roles", roles: "Roles",
      channels: "Channels", categories: "Channels", id: "Channels",
      text: "Messages", regex: "Messages", emoji: "Reactions", button: "Buttons", cooldown: "Cooldown",
    };
    const options = Object.entries(state.catalog.conditions)
      .filter(([type]) => !state.catalog.condition_aliases[type])
      .map(([type, [label, kind]]) => ({
        type, label, group: groupNames[kind], keywords: state.catalog.negative_conditions[type],
      }));
    choose(change ? "Change condition" : "Add a condition", options, ({ type }) => onPick(type), "condition");
  }

  function drawConditions(container) {
    const group = state.draft.conditions;
    if (!group) {
      const add = addTile("Add condition", () => chooseCondition((type) => {
        state.draft.conditions = { operator: "AND", rules: [newCondition(type)] };
        drawConditions(container);
        container.querySelector("select")?.focus();
      }));
      container.replaceChildren(editable()
        ? el("div", {}, add, el("small", { class: "muted auto-optional" }, "Optional. Every matching event runs the actions without a condition."))
        : el("p", { class: "muted" }, "No conditions."));
      return;
    }
    const redraw = () => drawConditions(container);
    container.replaceChildren(renderGroup(group, 1, redraw, editable() ? () => {
      delete state.draft.conditions; redraw();
    } : null));
  }

  function renderGroup(group, depth, redraw, remove) {
    const scope = el("div", { class: `auto-group ${depth === 1 ? "auto-root-group" : ""}`, role: "group", "aria-label": `${group.operator} condition group` });
    scope.append(el("div", { class: "auto-group-bar auto-row auto-between" },
      select("Group logic", group.operator, [["AND", "Match all (AND)"], ["OR", "Match any (OR)"], ["NOT", "Negate (NOT)"]], (operator) => {
        group.operator = operator; redraw();
      }),
      remove && button(depth === 1 ? "Clear conditions" : "Remove group", remove, "auto-quiet")));
    if (group.operator === "NOT" && group.rules.length !== 1) {
      scope.append(el("p", { class: "auto-error" }, "NOT needs one condition or group."));
    }
    const children = el("div", { class: "auto-steps" });
    group.rules.forEach((node, index) => {
      const removeChild = editable() ? () => {
        group.rules.splice(index, 1);
        if (!group.rules.length && remove) remove();
        else redraw();
      } : null;
      const replace = (type) => {
        if (type !== node.type) group.rules[index] = newCondition(type, node.target);
        redraw();
      };
      if (index) children.append(el("span", { class: "auto-join" }, group.operator));
      children.append(node.operator ? renderGroup(node, depth + 1, redraw, removeChild)
        : renderCondition(node, redraw, removeChild, replace));
    });
    scope.append(children);
    if (editable() && group.rules.length < (group.operator === "NOT" ? 1 : 20) && depth < state.catalog.max_depth) {
      scope.append(addTile("Add condition", () => chooseCondition((type) => {
        group.rules.push(newCondition(type)); redraw();
      })));
      if (depth < state.catalog.max_depth - 1) scope.append(button("Add nested group", () => {
        chooseCondition((type) => {
          group.rules.push({ operator: "AND", rules: [newCondition(type)] }); redraw();
        });
      }, "auto-quiet auto-group-add"));
    }
    return scope;
  }

  function renderCondition(node, redraw, remove, replace) {
    const canonical = state.catalog.condition_aliases[node.type] || node.type;
    const [label, type, hasTarget] = state.catalog.conditions[canonical];
    const negativeLabel = state.catalog.negative_conditions[canonical];
    const negative = Boolean(node.negate) !== (canonical !== node.type || node.value === false);
    const heading = el("h3", {}, negative ? negativeLabel : label);
    const comparison = select("Comparison", negative ? "negative" : "positive", [
      ["positive", label], ["negative", negativeLabel],
    ], (choice) => {
      node.type = canonical;
      if (type === "boolean") node.value = true;
      node.negate = choice === "negative";
      heading.textContent = node.negate ? negativeLabel : label;
    });
    const set = (value) => { node.value = value; };
    const pickerKinds = { users: "user", channels: "channel", roles: "role", categories: "category" };
    const list = (items) => items.map((key) => [key, title(key)]);
    let config;
    if (pickerKinds[type]) {
      config = arrayPicker(title(pickerKinds[type]), Array.isArray(node.value) ? node.value : [], pickerKinds[type], set);
    } else if (["id", "user", "role", "emoji"].includes(type)) {
      config = picker(type === "id" ? "Forum tag ID" : title(type), node.value, set, type);
    } else if (type === "presence") {
      config = select("Presence", node.value, list(state.catalog.presences), set);
    } else if (type === "cooldown") {
      const value = node.value;
      config = el("div", { class: "auto-grid" },
        select("Cooldown scope", value.scope, list(["user", "channel", "server"]), (scope) => {
          value.scope = scope;
          if (scope !== "user") delete value.target;
          redraw();
        }),
        input("Cooldown seconds", value.seconds, (seconds) => put(value, "seconds", seconds), { type: "number", min: 1 }),
        value.scope === "user" && targetPicker("Cooldown user", value.target || "actor", (target) => put(value, "target", target)));
    } else if (type !== "boolean") {
      const labels = { regex: "Regular expression", button: "Button ID", text: "Match text", number: "Minimum value" };
      config = input(labels[type], node.value, set, type === "number" ? { type: "number", min: 0 } : {});
    }
    return el("div", { class: "auto-node auto-condition" },
      el("div", { class: "auto-node-head auto-row auto-between" },
        heading,
        editable() && el("div", { class: "auto-row" },
          button("Change", () => chooseCondition(replace, true), "auto-quiet"),
          button("Remove condition", remove, "auto-quiet auto-danger"))),
      hasTarget && targetPicker("Check user", node.target || "actor", (target) => put(node, "target", target)),
      comparison, config);
  }

  function actionChoices() {
    const messageTriggers = state.catalog.message_triggers;
    const groups = {
      SEND_MESSAGE: "Messages", REPLY_MESSAGE: "Messages", DM_USER: "Messages", EDIT_MESSAGE: "Messages",
      DELETE_TRIGGER_MESSAGE: "Messages", PIN_MESSAGE: "Messages", EPHEMERAL_REPLY: "Messages",
      ADD_ROLE: "Members and roles", REMOVE_ROLE: "Members and roles", TIMEOUT_USER: "Members and roles",
      CREATE_THREAD: "Threads and forums", ADD_USER_TO_THREAD: "Threads and forums",
      APPLY_FORUM_TAG: "Threads and forums", CLOSE_FORUM_POST: "Threads and forums",
    };
    return Object.entries(state.catalog.actions).filter(([type]) =>
      (type !== "EPHEMERAL_REPLY" || state.draft.trigger.type === "BUTTON_CLICK") &&
      (!["REPLY_MESSAGE", "DELETE_TRIGGER_MESSAGE"].includes(type) || messageTriggers.includes(state.draft.trigger.type))
    ).map(([type, [label]]) => ({ type, label, group: groups[type] }));
  }

  function drawActions(container) {
    const actions = state.draft.actions;
    function redraw() { drawActions(container); }
    function selectAction(index, change) {
      choose(change ? "Change action" : "Add an action", actionChoices(), ({ type }) => {
        if (change && actions[index].type === type) return;
        actions[index] = { type };
        if (state.catalog.actions[type][1].includes("target")) {
          actions[index].target = state.draft.trigger.command?.require_mention ? "subject" : "actor";
        }
        redraw();
        container.querySelectorAll(".auto-action")[index]?.querySelector("button")?.focus();
      }, "action");
    }

    const steps = el("div", { class: "auto-steps" });
    actions.forEach((action, index) => {
      const [label, required, optional] = state.catalog.actions[action.type];
      function move(offset) {
        [actions[index], actions[index + offset]] = [actions[index + offset], actions[index]];
        redraw();
        container.querySelectorAll(".auto-action")[index + offset]?.querySelector("button")?.focus();
      }
      const tools = el("div", { class: "auto-row" },
        button("Change", () => selectAction(index, true), "auto-quiet"),
        button("Move up", () => move(-1), "auto-quiet", index === 0),
        button("Move down", () => move(1), "auto-quiet", index === actions.length - 1),
        button("Remove action", () => { actions.splice(index, 1); redraw(); }, "auto-quiet auto-danger"));
      if (index) steps.append(el("span", { class: "auto-join" }, "Then"));
      const primary = [...required, ...optional.filter((key) => !["embed", "buttons"].includes(key))];
      const controls = primary.map((key) => actionField(action, key, required.includes(key), redraw));
      const extras = optional.filter((key) => ["embed", "buttons"].includes(key));
      if ("template" in action || optional.includes("template")) {
        controls.push(el("details", { class: "auto-advanced" }, el("summary", {}, "Message placeholders"),
          el("p", { class: "muted" }, state.catalog.template_keys.map((key) => `{${key}}`).join(" · "))));
      }
      const advanced = extras.map((key) => actionField(action, key, false, redraw));
      steps.append(el("div", { class: "auto-node auto-action" },
        el("div", { class: "auto-node-head auto-row auto-between" }, el("h3", {}, label), editable() && tools),
        controls, advanced.length ? el("div", { class: "auto-message-extras" }, advanced) : null));
    });
    container.replaceChildren();
    if (actions.length) container.append(steps);
    if (editable() && actions.length < 12) {
      container.append(addTile("Add action", () => selectAction(actions.length, false)));
    }
  }

  function actionField(action, key, required, redraw) {
    const set = (value) => put(action, key, value);
    const labels = { template: "Message", name: "Thread name", reason: "Timeout reason", duration_seconds: "Timeout seconds",
      channel_id: "Destination channel", role_id: "Role", message_id: "Message ID", thread_id: "Thread ID", tag_id: "Forum tag ID" };
    if (key === "target") return targetPicker("Target user", action.target || "actor", set);
    if (key === "embed") return embedField(action, redraw);
    if (key === "buttons") return buttonsField(action, redraw);
    if (key.endsWith("_id")) {
      const kind = key === "thread_id" ? "channel" : key.replace("_id", "");
      const hasDefaultChannel = state.catalog.channel_triggers.includes(state.draft.trigger.type);
      const explicitDestination = key === "channel_id" && !hasDefaultChannel &&
        ["SEND_MESSAGE", "CREATE_THREAD", "EDIT_MESSAGE", "PIN_MESSAGE"].includes(action.type);
      const label = required || explicitDestination ? labels[key] : `${labels[key]} (optional)`;
      return picker(label, action[key], set, kind);
    }
    return input(labels[key], action[key], set,
      key === "template" ? { type: "textarea", maxlength: 2000 } :
        key === "duration_seconds" ? { type: "number", min: 1, max: 2419200 } : {});
  }

  function embedField(action, redraw) {
    const embed = action.embed;
    if (!embed) return editable() ? button("Add embed", () => { action.embed = {}; redraw(); }) : null;
    const fields = embed.fields || [];
    const entries = fields.map((part, index) => el("div", { class: "auto-condition" },
      input(`Embed field ${index + 1} name`, part.name, (value) => put(part, "name", value), { maxlength: 256 }),
      input("Field value", part.value, (value) => put(part, "value", value), { type: "textarea", maxlength: 1024 }),
      check("Inline field", part.inline, (value) => { part.inline = value; }),
      editable() && button("Remove field", () => { fields.splice(index, 1); redraw(); })));
    return el("fieldset", {}, el("legend", {}, "Embed"),
      input("Embed title", embed.title, (value) => put(embed, "title", value), { maxlength: 256 }),
      input("Description", embed.description, (value) => put(embed, "description", value), { type: "textarea", maxlength: 4096 }),
      input("Footer", embed.footer, (value) => put(embed, "footer", value), { maxlength: 2048 }),
      input("Accent color", embed.color === undefined ? "#52956a" : `#${embed.color.toString(16).padStart(6, "0")}`,
        (value) => { embed.color = Number.parseInt(value.slice(1), 16); }, { type: "color" }),
      el("div", { class: "auto-nested" }, entries),
      editable() && el("div", { class: "auto-row" },
        button("Add embed field", () => { embed.fields = fields; fields.push({ name: "", value: "" }); redraw(); }, "", fields.length >= 25),
        button("Remove embed", () => { delete action.embed; redraw(); })));
  }

  function buttonsField(action, redraw) {
    const buttons = action.buttons || [];
    const styles = ["primary", "secondary", "success", "danger"].map((key) => [key, title(key)]);
    if (action.buttons === undefined) return editable() ? button("Add buttons", () => {
      action.buttons = [{ id: "", label: "", style: "secondary" }]; redraw();
    }) : null;
    return el("fieldset", {}, el("legend", {}, "Message buttons"),
      el("small", { class: "muted" }, "Use each ID in a Button clicked automation. IDs remain attached to posted messages."),
      el("div", { class: "auto-nested" }, buttons.map((entry, index) => el("div", { class: "auto-condition" },
        el("div", { class: "auto-grid" },
          input(`Button ${index + 1} ID`, entry.id, (value) => { entry.id = value.trim(); }, { maxlength: 32 }),
          input("Button label", entry.label, (value) => { entry.label = value; }, { maxlength: 80 }),
          select("Button style", entry.style, styles, (value) => { entry.style = value; })),
        editable() && button("Remove button", () => { buttons.splice(index, 1); redraw(); })))),
      editable() && el("div", { class: "auto-row" },
        button("Add button", () => { buttons.push({ id: "", label: "", style: "secondary" }); redraw(); }, "", buttons.length >= 5),
        button("Omit button changes", () => { delete action.buttons; redraw(); })));
  }

  function renderTrace(node) {
    const label = node.result === true ? "Pass" : node.result === false ? "Fail" : "Unavailable";
    return el("li", {}, el("strong", {}, label), ` · ${node.label}`,
      node.detail && el("small", { class: "muted auto-trace-detail" }, node.detail),
      node.children.length ? el("ul", {}, node.children.map(renderTrace)) : null);
  }

  function testInputs() {
    const sample = {};
    const set = (key) => (value) => put(sample, key, value);
    const optionalBool = (label, key) => select(label, "", [["", "Unspecified"], ["true", "Yes"], ["false", "No"]], (value) =>
      set(key)(value === "" ? undefined : value === "true"), false);
    const kind = state.draft.trigger.type;
    const trigger = state.draft.trigger;
    const command = trigger.command;
    const defaultChannel = trigger.channel_ids?.length === 1 ? trigger.channel_ids[0] : "";
    if (defaultChannel) sample.channel_id = defaultChannel;
    const actor = picker("Actor ID", "", set("actor_id"), "user", false);
    const subject = picker("Subject ID", "", set("subject_id"), "user", false);
    const details = el("div", { class: "auto-grid" }, actor, !command && subject);
    if (state.catalog.channel_triggers.includes(kind)) details.append(
      picker("Event channel", defaultChannel, set("channel_id"), "channel", false));
    let commandSample;
    if (command) {
      sample.message_content = command.name;
      commandSample = input("Sample command", command.name, set("message_content"), {
        maxlength: 4000, disabled: false,
        hint: "Use the member picker below or enter a mention as <@USER_ID>.",
      });
      details.append(commandSample);
      if (command.access === "administrators") {
        details.append(check("Actor is a Discord administrator", false, set("actor_is_admin"), false));
      }
    } else if (state.catalog.message_triggers.includes(kind)) details.append(
      input("Sample message", "", set("message_content"), { type: "textarea", maxlength: 4000, disabled: false }));
    if (["REACTION_ADD", "REACTION_REMOVE"].includes(kind)) {
      if (trigger.emoji) sample.emoji = trigger.emoji;
      details.append(picker("Sample emoji", trigger.emoji, set("emoji"), "emoji", false),
        input("Reaction count", "", set("reaction_count"), { type: "number", min: 0, disabled: false }));
    }
    if (kind === "BUTTON_CLICK") {
      if (trigger.button_id) sample.button_id = trigger.button_id;
      if (trigger.source_rule_id) sample.source_rule_id = trigger.source_rule_id;
      details.append(input("Clicked button ID", trigger.button_id, set("button_id"), { disabled: false }),
        input("Posting rule ID", trigger.source_rule_id, set("source_rule_id"), { type: "number", min: 1, disabled: false }));
    }
    if (kind === "MEMBER_UPDATE") details.append(
      select("Member change", "", [["", "Unspecified"], ...state.catalog.member_changes.map((key) => [key, title(key)])], set("member_change"), false),
      arrayPicker("Changed role", [], "role", set("changed_role_ids"), false));
    if (kind === "VOICE_STATE_UPDATE") details.append(
      select("Voice change", "", [["", "Unspecified"], ...state.catalog.voice_changes.map((key) => [key, title(key)])], set("voice_change"), false),
      picker("Previous voice channel", "", set("previous_channel_id"), "channel", false));
    const extras = el("details", {}, el("summary", {}, "Sample user and channel details"),
      el("div", { class: "auto-grid" },
        picker("Category", "", set("category_id"), "category", false),
        optionalBool("Is a thread", "is_thread"), optionalBool("Message has attachments", "has_attachment"), optionalBool("Message has embeds", "has_embed"),
        arrayPicker("Forum tag", [], "id", set("forum_tags"), false)));
    const simulatedUsers = [];
    const userList = el("div", { class: "auto-nested" });
    const drawUsers = () => {
      userList.replaceChildren(...simulatedUsers.map((record, index) => el("fieldset", {},
        el("legend", {}, `Sample user ${index + 1}`),
        el("div", { class: "auto-grid" },
          picker("User ID", record.id, (value) => { record.id = value; }, "user", false),
          input("Display name", record.name, (value) => put(record, "name", value), { disabled: false }),
          input("Account age (days)", record.account_age_days, (value) => put(record, "account_age_days", value), { type: "number", min: 0, disabled: false }),
          input("Membership age (days)", record.join_age_days, (value) => put(record, "join_age_days", value), { type: "number", min: 0, disabled: false }),
          select("User presence", record.status, [["", "Unspecified"], ...state.catalog.presences.map((key) => [key, title(key)])], (value) => put(record, "status", value), false),
          check("User is a bot", record.bot, (value) => { record.bot = value; }, false)),
        arrayPicker("Sample role", record.roles, "role", (values) => { record.roles = values; }, false),
        button("Remove sample user", () => { simulatedUsers.splice(index, 1); drawUsers(); }))));
    };
    extras.append(userList, button("Add sample user", () => {
      if (simulatedUsers.length < 10) { simulatedUsers.push({ id: sample.actor_id || "", roles: [] }); drawUsers(); }
    }));
    function chooseMember(member, key, control) {
      const index = simulatedUsers.findIndex((user) => user.id === member.id);
      if (index >= 0) simulatedUsers[index] = member;
      else if (simulatedUsers.length < 10) simulatedUsers.push(member);
      else return false;
      if (command && key === "subject_id") {
        const token = sample.message_content?.trim().split(/\s+/)[0] || command.name;
        sample.message_content = `${token} <@${member.id}>`;
        commandSample.querySelector("input").value = sample.message_content;
      } else {
        control.querySelector("input").value = member.id;
        sample[key] = member.id;
      }
      drawUsers();
      return true;
    }

    const memberSearch = el("details", {}, el("summary", {}, "Find a server member"));
    const memberMatches = el("div", { "aria-live": "polite" });
    let query = "";
    memberSearch.append(
      input("Discord username or nickname", "", (value) => { query = value.trim(); }, { maxlength: 80, disabled: false }),
      button("Search members", (event) => work(event.currentTarget, async () => {
        if (query.length < 2) throw new Error("Enter at least two characters.");
        const { members } = await api("/members", "POST", { query });
        memberMatches.replaceChildren(...members.map((member) => el("div", { class: "auto-row" },
          el("span", {}, member.name),
          button("Use as actor", () => feedback(memberMatches,
            chooseMember(member, "actor_id", actor) ? `${member.name} selected as actor.` : "Remove a sample user first.")),
          button("Use as subject", () => feedback(memberMatches,
            chooseMember(member, "subject_id", subject) ? `${member.name} selected as subject.` : "Remove a sample user first.")))));
        if (!members.length) memberMatches.textContent = "No matching members.";
      }, memberMatches)), memberMatches);
    by("test-inputs").replaceChildren(details, memberSearch, extras);
    sample.users = simulatedUsers;
    return sample;
  }

  let sample = null;
  by("back").addEventListener("click", closeRule);

  by("test").addEventListener("click", () => {
    if (!state.draft.trigger.type) {
      feedback(by("error"), "Choose a trigger before testing the rule.", true);
      return;
    }
    sample = testInputs();
    by("trace").replaceChildren();
    by("test-dialog").showModal();
  });
  by("test-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const control = event.submitter;
    const output = by("trace");
    output.replaceChildren(el("p", { class: "muted" }, "Testing rule…"));
    work(control, async () => {
      const payload = copy(sample);
      if (editable()) payload.rule = state.draft;
      const path = state.selected ? `/${state.selected.id}/test` : "/test";
      const result = await api(path, "POST", payload);
      output.replaceChildren(
        el("h3", {}, result.matched ? "Rule matched" : "Rule did not match"),
        !result.active && el("p", { class: "muted" }, "This rule is currently disabled."),
        el("ul", {}, renderTrace(result.trace)),
        result.actions.length ? el("section", {}, el("h3", {}, "Actions that would run"),
          el("ol", {}, result.actions.map((action) => el("li", {}, el("details", {},
            el("summary", {}, state.catalog.actions[action.type][0]),
            el("pre", {}, JSON.stringify(action, null, 2))))))) : null);
    }, output);
  });

  by("history").addEventListener("click", (event) => {
    const output = by("history-content");
    output.replaceChildren(el("p", { class: "muted" }, "Loading history…"));
    by("history-dialog").showModal();
    work(event.currentTarget, async () => {
      const result = await api(`/${state.selected.id}/history`);
      output.replaceChildren(
        el("h3", {}, "Recent runs"),
        result.runs.length ? el("ul", {}, result.runs.map((run) => el("li", {},
          el("details", {}, el("summary", {}, `${date(run.started_at)} · ${title(run.status)} · Revision ${run.revision}`),
            el("ul", {}, run.results.map((step) => el("li", {},
              `${state.catalog.actions[step.type]?.[0] || step.type} · ${title(step.status)}`, step.detail && ` · ${step.detail}`))))))) :
          el("p", { class: "muted" }, "No runs recorded."),
        el("h3", {}, "Revisions"),
        result.revisions.map((version) => el("details", {},
          el("summary", {}, `Revision ${version.revision} · ${title(version.operation)} · ${date(version.changed_at)}`),
          el("p", { class: "muted" }, `Changed by ${version.changed_by}`),
          el("pre", {}, JSON.stringify(version.definition, null, 2)))));
    }, output);
  });

  if (canManage) {
    by("new").addEventListener("click", () => openRule());
    by("editor-toggle").addEventListener("click", (event) => {
      const row = state.selected;
      if (!row.rule.enabled && changed()) {
        feedback(by("error"), "Save your changes before enabling this rule.", true);
        return;
      }
      work(event.currentTarget, () => toggleRule(row, by("error")), by("error"));
    });
    by("editor").addEventListener("submit", (event) => {
      event.preventDefault();
      if (!editable()) return;
      work(event.submitter, async () => {
        feedback(by("error"), "");
        const row = state.selected;
        const current = state.draft;
        const submitted = JSON.stringify(current);
        const saved = row ? await api(`/${row.id}`, "PUT", { revision: row.revision, rule: current })
          : await api("", "POST", current);
        if (state.draft === current) {
          state.selected = saved;
          if (submitted === JSON.stringify(current)) {
            state.draft = copy(saved.rule);
            renderEditor();
          } else {
            state.draft.enabled = saved.rule.enabled;
            updateEditorHeader();
          }
        }
        if (by("archived").checked) {
          by("archived").checked = false;
          await loadRules();
        }
        replaceRow(saved);
        feedback(by("error"), saved.rule.enabled ? "Changes saved to the enabled rule." : "Rule saved disabled.");
      }, by("error"));
    });
    by("validate").addEventListener("click", (event) => work(event.currentTarget, async () => {
      await api("/validate", "POST", state.draft);
      feedback(by("error"), "Rule is valid.");
    }, by("error")));
    by("archive").addEventListener("click", (event) => {
      const row = state.selected;
      if (!window.confirm(`Archive “${row.rule.name}”? This disables the rule and retains its history.`)) return;
      work(event.currentTarget, async () => {
        await api(`/${row.id}`, "DELETE", { revision: row.revision });
        state.draft = copy(row.rule);
        closeRule();
        feedback(by("status"), "Rule archived.");
        await loadRules();
      }, by("error"));
    });
  }

  const chat = [];
  let generated = null;
  if (by("gemini")) {
    const dialog = by("gemini-dialog");
    const chatBox = by("chat");
    const prompt = by("prompt");
    let pending = false;
    function showChat(error) {
      chatBox.replaceChildren(...chat.map((turn) => el("p", {},
        el("strong", {}, turn.role === "user" ? "You" : "Gemini"), " · ", turn.text)),
      pending ? el("p", { class: "muted" }, "Gemini is drafting…") : null,
      error ? el("p", { class: "auto-feedback auto-error", role: "alert" }, error) : null);
      by("use-draft").hidden = !generated;
      by("use-draft").disabled = pending;
      by("chat-reset").disabled = pending;
      chatBox.scrollTop = chatBox.scrollHeight;
    }
    by("gemini").addEventListener("click", () => dialog.showModal());
    by("chat-reset").addEventListener("click", () => {
      chat.length = 0; generated = null; prompt.value = ""; showChat(); prompt.focus();
    });
    by("use-draft").addEventListener("click", () => {
      if (generated && openRule(null, generated)) {
        dialog.close();
        by("editor-title").focus();
        feedback(by("error"), "Gemini draft opened. Review and test it before saving.");
      }
    });
    by("chat-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (pending || !prompt.value.trim()) return;
      const text = prompt.value.trim();
      const request = [...chat.slice(-6), { role: "user", text }];
      while (request.length > 1 && request.reduce((size, turn) => size + turn.text.length, 0) > 8000) request.shift();
      chat.push({ role: "user", text });
      pending = true;
      event.submitter.disabled = true;
      showChat();
      let error;
      try {
        const result = await api("/draft", "POST", { messages: request, previous_draft: generated });
        chat.push({ role: "assistant", text: result.message });
        if (result.rule) generated = result.rule;
        prompt.value = "";
      } catch (failure) {
        chat.pop();
        error = failure.message;
      } finally {
        pending = false;
        event.submitter.disabled = false;
        showChat(error);
      }
    });
  }

  root.querySelectorAll("dialog [data-close]").forEach((control) => {
    control.addEventListener("click", () => control.closest("dialog").close());
  });
  by("search").addEventListener("input", () => { if (state.catalog) renderList(); });
  by("archived").addEventListener("change", (event) => work(event.currentTarget, loadRules));
  by("refresh").addEventListener("click", (event) => work(event.currentTarget, async () => {
    feedback(by("status"), "");
    await loadRules();
  }));
  loadRules().catch((error) => feedback(by("status"), error.message, true));
})();
