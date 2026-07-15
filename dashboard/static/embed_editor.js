(() => {
  const form = document.querySelector("#embed-editor-form");
  if (!form) return;

  const byId = (id) => document.getElementById(id);
  const initial = JSON.parse(byId("embed-initial-data").textContent || "{}");
  const embed = initial.embed || {};
  const emojiChoices = ["🔔", "✅", "❌", "❤️", "💥", "🎉", "⭐", "👑", "📣", "👉", "🔗", "🎁", "🚀", "✨", "👍", "👎", "📌", "🛡️", "🌈", "🔥"];

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
    updatePreview();
  }

  function drawEmojiPicker(row) {
    const picker = row.querySelector(".emoji-picker");
    picker.innerHTML = emojiChoices.map((emoji) => `<button type="button" data-emoji="${emoji}">${emoji}</button>`).join("");
    picker.querySelectorAll("[data-emoji]").forEach((button) => {
      button.addEventListener("click", () => {
        row.querySelector('[data-button="emoji"]').value = button.dataset.emoji;
        picker.hidden = true;
        updatePreview();
      });
    });
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
    row.querySelector(".emoji-toggle").addEventListener("click", () => {
      const picker = row.querySelector(".emoji-picker");
      picker.hidden = !picker.hidden;
    });
    row.querySelector('[data-button="action"]').addEventListener("change", () => {
      syncButtonTarget(row);
      updatePreview();
    });
    row.addEventListener("input", updatePreview);
    row.addEventListener("change", updatePreview);
    drawEmojiPicker(row);
    syncButtonTarget(row);
    container.append(row);
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
    byId("preview-content").textContent = data.content || "Write your message here!";
    byId("preview-content").classList.toggle("placeholder", !data.content);
    byId("preview-author").textContent = data.embed.author_name;
    byId("preview-author").hidden = !data.embed.author_name;
    const title = byId("preview-title");
    title.textContent = data.embed.title;
    title.hidden = !data.embed.title;
    title.href = data.embed.url || "#";
    title.removeAttribute("target");
    byId("preview-description").textContent = data.embed.description || "Write your embed message here!";
    byId("preview-description").classList.toggle("placeholder", !data.embed.description);
    setOptionalImage(byId("preview-thumbnail"), data.embed.thumbnail_url);
    setOptionalImage(byId("preview-image"), data.embed.image_url);
    const fields = byId("preview-fields");
    fields.innerHTML = "";
    data.embed.fields.forEach((field) => {
      const item = document.createElement("div");
      item.className = field.inline ? "preview-field inline" : "preview-field";
      const strong = document.createElement("strong");
      strong.textContent = field.name || "Field name";
      const value = document.createElement("span");
      value.textContent = field.value || "Field value";
      item.append(strong, value);
      fields.append(item);
    });
    byId("preview-footer").textContent = data.embed.footer_text;
    byId("preview-footer").hidden = !data.embed.footer_text;
    const buttons = byId("preview-buttons");
    buttons.innerHTML = "";
    data.buttons.forEach((button) => {
      const item = document.createElement("span");
      item.className = `preview-discord-button style-${button.style}`;
      item.textContent = `${button.emoji ? `${button.emoji} ` : ""}${button.label || "Button"}`;
      buttons.append(item);
    });
    const hasEmbed = Boolean(
      data.embed.author_name || data.embed.title || data.embed.description || data.embed.thumbnail_url ||
      data.embed.image_url || data.embed.footer_text || data.embed.fields.length
    );
    card.classList.toggle("preview-empty", !hasEmbed);
  }

  (embed.fields || []).forEach(addField);
  (initial.buttons || []).forEach(addButton);
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
