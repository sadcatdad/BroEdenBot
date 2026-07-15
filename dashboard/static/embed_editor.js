(() => {
  const form = document.querySelector("#embed-editor-form");
  if (!form) return;

  const byId = (id) => document.getElementById(id);
  const assetType = form.dataset.assetType === "message" ? "message" : "embed";
  const initial = JSON.parse(byId("embed-initial-data").textContent || "{}");
  const embed = initial.embed || {};
  const unicodeEmojiChoices = [
    ["😀", "grinning happy smile", "Faces"], ["😃", "smile happy", "Faces"], ["😄", "smile laugh", "Faces"],
    ["😁", "beam grin", "Faces"], ["😂", "joy tears laugh", "Faces"], ["🤣", "rolling laugh", "Faces"],
    ["😊", "blush happy", "Faces"], ["😍", "heart eyes love", "Faces"], ["🥰", "hearts love", "Faces"],
    ["😎", "cool sunglasses", "Faces"], ["🤔", "thinking", "Faces"], ["🫡", "salute", "Faces"],
    ["😭", "cry sob", "Faces"], ["😡", "angry", "Faces"], ["🥳", "party celebrate", "Faces"],
    ["🤯", "mind blown", "Faces"], ["😇", "angel", "Faces"], ["🤩", "star eyes", "Faces"],
    ["👍", "thumbs up yes", "People"], ["👎", "thumbs down no", "People"], ["👏", "clap applause", "People"],
    ["🙌", "raised hands celebrate", "People"], ["🙏", "pray please thanks", "People"], ["👋", "wave hello", "People"],
    ["👉", "point right", "People"], ["💪", "strong muscle", "People"], ["🤝", "handshake", "People"],
    ["🫶", "heart hands", "People"], ["👀", "eyes look", "People"], ["🧠", "brain", "People"],
    ["❤️", "red heart love", "Symbols"], ["🩷", "pink heart", "Symbols"], ["🧡", "orange heart", "Symbols"],
    ["💛", "yellow heart", "Symbols"], ["💚", "green heart", "Symbols"], ["💙", "blue heart", "Symbols"],
    ["💜", "purple heart", "Symbols"], ["🖤", "black heart", "Symbols"], ["✅", "check yes done", "Symbols"],
    ["❌", "cross no", "Symbols"], ["⚠️", "warning", "Symbols"], ["❗", "exclamation", "Symbols"],
    ["❓", "question", "Symbols"], ["💯", "hundred", "Symbols"], ["♻️", "recycle refresh", "Symbols"],
    ["✨", "sparkles", "Nature"], ["🔥", "fire", "Nature"], ["🌈", "rainbow", "Nature"],
    ["⭐", "star", "Nature"], ["🌟", "glowing star", "Nature"], ["☀️", "sun", "Nature"],
    ["🌙", "moon", "Nature"], ["🌸", "flower", "Nature"], ["🍀", "clover lucky", "Nature"],
    ["🐸", "frog", "Nature"], ["🐶", "dog", "Nature"], ["🐱", "cat", "Nature"],
    ["🎉", "party popper celebrate", "Activities"], ["🎊", "confetti", "Activities"], ["🎁", "gift reward", "Activities"],
    ["🏆", "trophy winner", "Activities"], ["🥇", "gold medal first", "Activities"], ["🎮", "game controller", "Activities"],
    ["🎵", "music", "Activities"], ["🎨", "art", "Activities"], ["⚽", "soccer ball", "Activities"],
    ["🔔", "bell reminder notification", "Objects"], ["📣", "megaphone announce", "Objects"], ["📌", "pin", "Objects"],
    ["🔗", "link", "Objects"], ["🛡️", "shield safety", "Objects"], ["🔒", "lock private", "Objects"],
    ["💡", "idea light", "Objects"], ["📅", "calendar", "Objects"], ["⏰", "alarm time", "Objects"],
    ["💥", "boom bump explosion", "Objects"], ["🚀", "rocket launch", "Travel"], ["🌍", "world globe", "Travel"],
    ["🍕", "pizza", "Food"], ["🍔", "burger", "Food"], ["🍰", "cake", "Food"],
    ["☕", "coffee", "Food"], ["🍻", "beer cheers", "Food"], ["🍓", "strawberry", "Food"],
  ];
  let serverEmojiChoices = [];
  let activeEmojiTarget = byId("message-content");
  let activeEmojiCategory = "All";

  function serverEmojiMarkup(emoji) {
    return `<${emoji.animated ? "a" : ""}:${emoji.name}:${emoji.id}>`;
  }

  function serverEmojiUrl(emoji) {
    const extension = emoji.animated ? "gif" : "webp";
    return `https://cdn.discordapp.com/emojis/${emoji.id}.${extension}?size=64&quality=lossless`;
  }

  function availableEmojiChoices() {
    const unicode = unicodeEmojiChoices.map(([value, keywords, category]) => ({
      value,
      keywords,
      category,
      custom: false,
    }));
    const custom = serverEmojiChoices.map((emoji) => ({
      value: serverEmojiMarkup(emoji),
      keywords: `${emoji.name} ${emoji.id} custom server ${emoji.animated ? "animated" : "static"}`,
      category: "Server",
      custom: true,
      emoji,
    }));
    return [...custom, ...unicode];
  }

  function setEmojiPickerStatus(message, isError = false) {
    const status = byId("emoji-server-status");
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("error-text", isError);
  }

  function serverEmojiById(id) {
    return serverEmojiChoices.find((emoji) => emoji.id === String(id)) || null;
  }

  function normalizeEmojiValue(value) {
    const text = String(value || "").trim();
    if (!text) return { value: "", error: "" };
    const numericId = text.match(/^\d{17,20}$/);
    const fullMarkup = text.match(/^<(a?):([A-Za-z0-9_]+):(\d{17,20})>$/);
    const shorthandMarkup = text.match(/^<([A-Za-z0-9_]+):(\d{17,20})>$/);
    const id = numericId ? numericId[0] : (fullMarkup ? fullMarkup[3] : (shorthandMarkup ? shorthandMarkup[2] : ""));
    const known = id ? serverEmojiById(id) : null;
    if (known) return { value: serverEmojiMarkup(known), error: "" };
    if (fullMarkup) return { value: text, error: "" };
    if (shorthandMarkup) return { value: `<:${shorthandMarkup[1]}:${shorthandMarkup[2]}>`, error: "" };
    if (numericId) {
      return {
        value: "",
        error: "That ID is not in the latest server emoji snapshot. Refresh Discord Metadata or paste full <:name:id> or <a:name:id> markup.",
      };
    }
    return { value: text, error: "" };
  }

  async function loadServerEmojis() {
    setEmojiPickerStatus("Loading custom server emojis…");
    try {
      const response = await fetch("/api/discord/emojis", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      serverEmojiChoices = (Array.isArray(payload) ? payload : []).filter((emoji) => (
        /^\d{17,20}$/.test(String(emoji.id || ""))
        && /^[A-Za-z0-9_]+$/.test(String(emoji.name || ""))
        && emoji.available !== false
      )).map((emoji) => ({
        id: String(emoji.id),
        name: String(emoji.name),
        animated: Boolean(emoji.animated),
      }));
      setEmojiPickerStatus(
        serverEmojiChoices.length
          ? `${serverEmojiChoices.length} custom server emoji${serverEmojiChoices.length === 1 ? "" : "s"} available.`
          : "No custom server emojis are in the current metadata snapshot."
      );
      if (!byId("global-emoji-picker").hidden) {
        renderEmojiCategories();
        renderEmojiPicker();
      }
      updatePreview();
    } catch (_error) {
      serverEmojiChoices = [];
      setEmojiPickerStatus("Custom server emojis could not be loaded. Refresh Discord Metadata and try again.", true);
    }
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function inlineDiscordMarkdown(value) {
    let source = String(value || "")
      .replace(/\{user\.feature\}/g, "<@111111111111111111>")
      .replace(/\{role\.feature\}/g, "<@&222222222222222222>");
    const tokens = [];
    const token = (html) => {
      const index = tokens.push(html) - 1;
      return `\uE000${index}\uE001`;
    };
    source = source.replace(/`([^`\n]+)`/g, (_match, code) => token(`<code class="md-inline-code">${escapeHtml(code)}</code>`));
    source = source.replace(/<@&(\d{17,20})>/g, () => token('<span class="md-mention">@role</span>'));
    source = source.replace(/<@!?(\d{17,20})>/g, () => token('<span class="md-mention">@member</span>'));
    source = source.replace(/<#(\d{17,20})>/g, () => token('<span class="md-mention">#channel</span>'));
    source = source.replace(/<(a?):([A-Za-z0-9_]+):(\d{17,20})>/g, (_match, animated, name, id) => {
      const extension = animated ? "gif" : "webp";
      const safeName = escapeHtml(name);
      return token(`<img class="md-custom-emoji" src="https://cdn.discordapp.com/emojis/${id}.${extension}?size=48&quality=lossless" alt=":${safeName}:" title=":${safeName}:">`);
    });
    source = source.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, url) => {
      try {
        const parsed = new URL(url);
        if (!['http:', 'https:'].includes(parsed.protocol)) return _match;
      } catch (_error) {
        return _match;
      }
      return token(`<a class="md-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    });
    let html = escapeHtml(source);
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/__([^_]+)__/g, "<u>$1</u>");
    html = html.replace(/~~([^~]+)~~/g, "<s>$1</s>");
    html = html.replace(/\|\|([^|]+)\|\|/g, '<span class="md-spoiler">$1</span>');
    html = html.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    html = html.replace(/(^|[^_])_([^_]+)_/g, "$1<em>$2</em>");
    return html.replace(/\uE000(\d+)\uE001/g, (_match, index) => tokens[Number(index)] || "");
  }

  function discordMarkdown(value) {
    const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
    const output = [];
    let codeLines = null;
    lines.forEach((line) => {
      if (/^```/.test(line)) {
        if (codeLines === null) codeLines = [];
        else {
          output.push(`<pre class="md-code-block"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
          codeLines = null;
        }
        return;
      }
      if (codeLines !== null) {
        codeLines.push(line);
        return;
      }
      if (!line) {
        output.push('<div class="md-spacer" aria-hidden="true"></div>');
        return;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        output.push(`<div class="md-heading md-heading-${heading[1].length}">${inlineDiscordMarkdown(heading[2])}</div>`);
        return;
      }
      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        output.push(`<div class="md-quote">${inlineDiscordMarkdown(quote[1])}</div>`);
        return;
      }
      const bullet = line.match(/^[-*]\s+(.+)$/);
      if (bullet) {
        output.push(`<div class="md-list-item"><span>•</span><span>${inlineDiscordMarkdown(bullet[1])}</span></div>`);
        return;
      }
      const ordered = line.match(/^(\d+)\.\s+(.+)$/);
      if (ordered) {
        output.push(`<div class="md-list-item"><span>${ordered[1]}.</span><span>${inlineDiscordMarkdown(ordered[2])}</span></div>`);
        return;
      }
      output.push(`<div class="md-line">${inlineDiscordMarkdown(line)}</div>`);
    });
    if (codeLines !== null) {
      output.push(`<pre class="md-code-block"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    }
    return output.join("");
  }

  function previewPlainText(value) {
    return String(value || "");
  }

  const fieldMap = {
    "message-content": initial.content,
    "author-name": embed.author_name,
    "author-url": embed.author_url,
    "author-icon-url": embed.author_icon_url,
    "embed-title": embed.title,
    "embed-url": embed.url,
    "embed-description": embed.description,
    "embed-color": embed.color || "#25b8b8",
    "embed-color-text": embed.color || "#25b8b8",
    "thumbnail-url": embed.thumbnail_url,
    "image-url": embed.image_url,
    "footer-text": embed.footer_text,
    "footer-icon-url": embed.footer_icon_url,
  };
  Object.entries(fieldMap).forEach(([id, value]) => {
    const element = byId(id);
    if (element) element.value = value || "";
  });

  function emojiTargetLabel(target) {
    return target && target.dataset.emojiLabel ? target.dataset.emojiLabel : "Regular message";
  }

  function setActiveEmojiTarget(target) {
    if (!target) return;
    activeEmojiTarget = target;
    const label = byId("emoji-picker-target");
    if (label) label.textContent = `Inserting into ${emojiTargetLabel(target)}`;
  }

  function renderEmojiPicker() {
    const search = byId("emoji-search").value.trim().toLocaleLowerCase();
    const matches = availableEmojiChoices().filter((choice) => (
      (activeEmojiCategory === "All" || activeEmojiCategory === choice.category)
      && (!search || `${choice.keywords} ${choice.category}`.toLocaleLowerCase().includes(search))
    ));
    const results = byId("emoji-results");
    results.innerHTML = "";
    matches.forEach((choice) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `emoji-result${choice.custom ? " custom-emoji-result" : ""}`;
      if (choice.custom) {
        const image = document.createElement("img");
        image.src = serverEmojiUrl(choice.emoji);
        image.alt = `:${choice.emoji.name}:`;
        image.loading = "lazy";
        button.append(image);
      } else {
        button.textContent = choice.value;
      }
      button.title = choice.custom
        ? `:${choice.emoji.name}: (${choice.emoji.animated ? "animated" : "static"})`
        : choice.keywords;
      button.setAttribute("aria-label", button.title);
      button.addEventListener("click", () => insertEmoji(choice.value));
      results.append(button);
    });
    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state emoji-empty-state";
      empty.textContent = "No matching emoji.";
      results.append(empty);
    }
  }

  function renderEmojiCategories() {
    const categories = ["All", "Server", ...new Set(unicodeEmojiChoices.map((item) => item[2]))];
    const container = byId("emoji-categories");
    container.innerHTML = "";
    categories.forEach((category) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `emoji-category${category === activeEmojiCategory ? " active" : ""}`;
      button.textContent = category;
      button.addEventListener("click", () => {
        activeEmojiCategory = category;
        renderEmojiCategories();
        renderEmojiPicker();
      });
      container.append(button);
    });
  }

  function openEmojiPicker(target = activeEmojiTarget) {
    setActiveEmojiTarget(target || byId("message-content"));
    const picker = byId("global-emoji-picker");
    picker.hidden = false;
    renderEmojiCategories();
    renderEmojiPicker();
    byId("emoji-search").focus();
  }

  function closeEmojiPicker() {
    byId("global-emoji-picker").hidden = true;
    if (activeEmojiTarget) activeEmojiTarget.focus();
  }

  function insertEmoji(value) {
    if (!activeEmojiTarget || !value) return;
    const normalized = normalizeEmojiValue(value);
    if (normalized.error) {
      setEmojiPickerStatus(normalized.error, true);
      return;
    }
    const inserted = normalized.value;
    const start = Number.isInteger(activeEmojiTarget.selectionStart)
      ? activeEmojiTarget.selectionStart : activeEmojiTarget.value.length;
    const end = Number.isInteger(activeEmojiTarget.selectionEnd)
      ? activeEmojiTarget.selectionEnd : start;
    activeEmojiTarget.setRangeText(inserted, start, end, "end");
    activeEmojiTarget.dispatchEvent(new Event("input", { bubbles: true }));
    closeEmojiPicker();
  }

  function insertPlaceholder(value) {
    if (!activeEmojiTarget || !value) return;
    const start = Number.isInteger(activeEmojiTarget.selectionStart)
      ? activeEmojiTarget.selectionStart : activeEmojiTarget.value.length;
    const end = Number.isInteger(activeEmojiTarget.selectionEnd)
      ? activeEmojiTarget.selectionEnd : start;
    activeEmojiTarget.setRangeText(String(value), start, end, "end");
    activeEmojiTarget.dispatchEvent(new Event("input", { bubbles: true }));
    activeEmojiTarget.focus();
  }

  function registerEmojiTarget(target, label) {
    if (!target || target.dataset.emojiRegistered === "true") return;
    target.dataset.emojiRegistered = "true";
    target.dataset.emojiLabel = label;
    target.addEventListener("focus", () => setActiveEmojiTarget(target));
    if (target.closest(".emoji-input-row")) return;
    const action = document.createElement("button");
    action.type = "button";
    action.className = "emoji-field-button";
    action.textContent = "☺ Emoji";
    action.setAttribute("aria-label", `Insert emoji into ${label}`);
    action.addEventListener("click", () => openEmojiPicker(target));
    target.insertAdjacentElement("afterend", action);
  }

  function addField(value = {}) {
    const container = byId("embed-field-editors");
    if (container.children.length >= 25) return;
    const row = byId("embed-field-template").content.firstElementChild.cloneNode(true);
    row.querySelector('[data-field="name"]').value = value.name || "";
    row.querySelector('[data-field="value"]').value = value.value || "";
    row.querySelector('[data-field="inline"]').checked = Boolean(value.inline);
    row.querySelector(".remove-row").addEventListener("click", () => {
      row.remove();
      updatePreview();
    });
    row.addEventListener("input", updatePreview);
    row.addEventListener("change", updatePreview);
    container.append(row);
    registerEmojiTarget(row.querySelector('[data-field="name"]'), "Field name");
    registerEmojiTarget(row.querySelector('[data-field="value"]'), "Field value");
    updatePreview();
  }

  function syncButtonTarget(row) {
    const isUrl = row.querySelector('[data-button="action"]').value === "url";
    row.querySelector(".button-target-role").hidden = isUrl;
    row.querySelector(".button-target-url").hidden = !isUrl;
    row.querySelector('[data-button="style"]').disabled = isUrl;
  }

  function addButton(value = {}) {
    const container = byId("button-editors");
    if (container.children.length >= 5) return;
    const row = byId("embed-button-template").content.firstElementChild.cloneNode(true);
    row.querySelector('[data-button="label"]').value = value.label || "";
    row.querySelector('[data-button="action"]').value = value.action || "add_role";
    row.querySelector('[data-button="style"]').value = value.style === "link" ? "secondary" : (value.style || "secondary");
    row.querySelector('[data-button="emoji"]').value = value.emoji || "";
    row.querySelector('[data-button="url"]').value = value.url || "";
    row.querySelector("role-single-select").setAttribute("value", value.role_id || "");
    row.querySelector(".remove-row").addEventListener("click", () => {
      row.remove();
      updatePreview();
    });
    const buttonEmojiInput = row.querySelector('[data-button="emoji"]');
    row.querySelector(".emoji-toggle").addEventListener("click", () => openEmojiPicker(buttonEmojiInput));
    row.querySelector('[data-button="action"]').addEventListener("change", () => {
      syncButtonTarget(row);
      updatePreview();
    });
    row.addEventListener("input", updatePreview);
    row.addEventListener("change", updatePreview);
    syncButtonTarget(row);
    container.append(row);
    registerEmojiTarget(row.querySelector('[data-button="label"]'), "Button label");
    registerEmojiTarget(buttonEmojiInput, "Button emoji");
    updatePreview();
  }

  function collectFields() {
    return [...document.querySelectorAll(".embed-field-editor")].map((row) => ({
      name: row.querySelector('[data-field="name"]').value,
      value: row.querySelector('[data-field="value"]').value,
      inline: row.querySelector('[data-field="inline"]').checked,
    }));
  }

  function collectButtons() {
    return [...document.querySelectorAll(".button-editor")].map((row) => {
      const action = row.querySelector('[data-button="action"]').value;
      const roleInput = row.querySelector(".discord-picker-value");
      return {
        label: row.querySelector('[data-button="label"]').value,
        action,
        style: action === "url" ? "link" : row.querySelector('[data-button="style"]').value,
        emoji: row.querySelector('[data-button="emoji"]').value,
        role_id: action === "url" ? "" : (roleInput ? roleInput.value : ""),
        url: action === "url" ? row.querySelector('[data-button="url"]').value : "",
      };
    });
  }

  function collectPayload() {
    return {
      content: byId("message-content").value,
      embed: {
        author_name: byId("author-name").value,
        author_url: byId("author-url").value,
        author_icon_url: byId("author-icon-url").value,
        title: byId("embed-title").value,
        url: byId("embed-url").value,
        description: byId("embed-description").value,
        color: byId("embed-color-text").value,
        thumbnail_url: byId("thumbnail-url").value,
        image_url: byId("image-url").value,
        footer_text: byId("footer-text").value,
        footer_icon_url: byId("footer-icon-url").value,
        fields: collectFields(),
      },
      buttons: collectButtons(),
    };
  }

  function setOptionalImage(element, url) {
    if (!url) {
      element.hidden = true;
      element.removeAttribute("src");
      return;
    }
    element.hidden = false;
    element.src = url;
    element.onerror = () => { element.hidden = true; };
  }

  function updatePreview() {
    const data = collectPayload();
    const card = byId("preview-embed");
    const color = /^#[0-9a-f]{6}$/i.test(data.embed.color) ? data.embed.color : "#25b8b8";
    byId("embed-color-rail").style.background = color;
    card.style.borderLeftColor = color;
    const previewContent = byId("preview-content");
    previewContent.innerHTML = discordMarkdown(data.content || "Write your message here!");
    previewContent.classList.toggle("placeholder", !data.content);
    byId("preview-author").textContent = previewPlainText(data.embed.author_name);
    byId("preview-author").hidden = !data.embed.author_name;
    const title = byId("preview-title");
    title.innerHTML = inlineDiscordMarkdown(data.embed.title);
    title.hidden = !data.embed.title;
    title.href = data.embed.url || "#";
    title.removeAttribute("target");
    const description = byId("preview-description");
    description.innerHTML = discordMarkdown(data.embed.description || "Write your embed message here!");
    description.classList.toggle("placeholder", !data.embed.description);
    setOptionalImage(byId("preview-thumbnail"), data.embed.thumbnail_url);
    setOptionalImage(byId("preview-image"), data.embed.image_url);
    const fields = byId("preview-fields");
    fields.innerHTML = "";
    data.embed.fields.forEach((field) => {
      const item = document.createElement("div");
      item.className = field.inline ? "preview-field inline" : "preview-field";
      const strong = document.createElement("strong");
      strong.innerHTML = inlineDiscordMarkdown(field.name || "Field name");
      const value = document.createElement("div");
      value.className = "preview-field-value";
      value.innerHTML = discordMarkdown(field.value || "Field value");
      item.append(strong, value);
      fields.append(item);
    });
    byId("preview-footer").textContent = previewPlainText(data.embed.footer_text);
    byId("preview-footer").hidden = !data.embed.footer_text;
    const buttons = byId("preview-buttons");
    buttons.innerHTML = "";
    data.buttons.forEach((button) => {
      const item = document.createElement("span");
      item.className = `preview-discord-button style-${button.style}`;
      const normalizedEmoji = normalizeEmojiValue(button.emoji || "");
      const emojiMarkup = normalizedEmoji.value;
      item.innerHTML = `${emojiMarkup ? `${inlineDiscordMarkdown(emojiMarkup)} ` : ""}${escapeHtml(previewPlainText(button.label || "Button"))}`;
      buttons.append(item);
    });
    const hasEmbed = Boolean(
      data.embed.author_name || data.embed.title || data.embed.description || data.embed.thumbnail_url ||
      data.embed.image_url || data.embed.footer_text || data.embed.fields.length
    );
    card.hidden = assetType === "message";
    card.classList.toggle("preview-empty", !hasEmbed);
  }

  (embed.fields || []).forEach(addField);
  (initial.buttons || []).forEach(addButton);
  [
    [byId("message-content"), "Regular message"],
    [byId("author-name"), "Author / header"],
    [byId("embed-title"), "Title"],
    [byId("embed-description"), "Description"],
    [byId("footer-text"), "Footer"],
  ].forEach(([target, label]) => registerEmojiTarget(target, label));
  byId("open-emoji-picker").addEventListener("click", () => openEmojiPicker());
  byId("close-emoji-picker").addEventListener("click", closeEmojiPicker);
  byId("emoji-search").addEventListener("input", renderEmojiPicker);
  byId("insert-custom-emoji").addEventListener("click", () => {
    const custom = byId("custom-emoji-value");
    if (!custom.value.trim()) return;
    insertEmoji(custom.value);
    custom.value = "";
  });
  byId("custom-emoji-value").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    byId("insert-custom-emoji").click();
  });
  document.querySelectorAll("[data-editor-placeholder]").forEach((button) => {
    button.addEventListener("click", () => insertPlaceholder(button.dataset.editorPlaceholder));
  });
  loadServerEmojis();
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !byId("global-emoji-picker").hidden) closeEmojiPicker();
  });
  byId("add-embed-field").addEventListener("click", () => addField());
  byId("add-embed-button").addEventListener("click", () => addButton());
  form.addEventListener("input", updatePreview);
  form.addEventListener("change", updatePreview);
  byId("embed-color").addEventListener("input", (event) => {
    byId("embed-color-text").value = event.target.value;
    updatePreview();
  });
  byId("embed-color-text").addEventListener("input", (event) => {
    if (/^#[0-9a-f]{6}$/i.test(event.target.value)) byId("embed-color").value = event.target.value;
  });
  form.addEventListener("submit", () => {
    byId("embed-payload-json").value = JSON.stringify(collectPayload());
  });
  updatePreview();
})();
