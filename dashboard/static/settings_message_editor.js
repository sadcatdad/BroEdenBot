(() => {
  "use strict";

  const emojiChoices = [
    ["😀", "grinning happy smile"], ["😄", "smile laugh"], ["😂", "joy tears laugh"],
    ["🤣", "rolling laugh"], ["😊", "blush happy"], ["😍", "heart eyes love"],
    ["🥰", "hearts love"], ["😎", "cool sunglasses"], ["🤔", "thinking"],
    ["🫡", "salute"], ["😭", "cry sob"], ["🥳", "party celebrate"],
    ["🤩", "star eyes"], ["👍", "thumbs up yes"], ["👎", "thumbs down no"],
    ["👏", "clap applause"], ["🙌", "raised hands celebrate"], ["🙏", "pray thanks"],
    ["👋", "wave hello"], ["👉", "point right"], ["💪", "strong muscle"],
    ["🤝", "handshake"], ["🫶", "heart hands"], ["👀", "eyes look"],
    ["❤️", "red heart love"], ["🩷", "pink heart"], ["💛", "yellow heart"],
    ["💚", "green heart"], ["💙", "blue heart"], ["💜", "purple heart"],
    ["✅", "check yes done"], ["❌", "cross no"], ["⚠️", "warning"],
    ["❗", "exclamation"], ["❓", "question"], ["💯", "hundred"],
    ["✨", "sparkles"], ["🔥", "fire"], ["🌈", "rainbow"],
    ["⭐", "star"], ["🌟", "glowing star"], ["🎉", "party popper celebrate"],
    ["🎊", "confetti"], ["🎁", "gift reward"], ["🏆", "trophy winner"],
    ["🥇", "gold medal first"], ["🎮", "game controller"], ["🔔", "bell reminder"],
    ["📣", "megaphone announce"], ["📌", "pin"], ["🔗", "link"],
    ["🛡️", "shield safety"], ["🔒", "lock private"], ["💡", "idea light"],
    ["📅", "calendar"], ["⏰", "alarm time"], ["💥", "boom bump explosion"],
    ["🚀", "rocket launch"], ["🌍", "world globe"], ["🍕", "pizza"],
    ["🍰", "cake"], ["☕", "coffee"], ["🍻", "cheers"],
  ];

  function insertAtCursor(input, value) {
    const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
    const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
    input.setRangeText(value, start, end, "end");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  }

  document.querySelectorAll("[data-setting-message-editor]").forEach((editor) => {
    const input = editor.querySelector("[data-setting-message-input]");
    const toggle = editor.querySelector("[data-setting-emoji-toggle]");
    const popover = editor.querySelector("[data-setting-emoji-popover]");
    const search = editor.querySelector("[data-setting-emoji-search]");
    const results = editor.querySelector("[data-setting-emoji-results]");

    const render = () => {
      const query = search.value.trim().toLocaleLowerCase();
      const matches = emojiChoices.filter(([, keywords]) => !query || keywords.includes(query));
      results.replaceChildren();
      matches.forEach(([emoji, keywords]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "setting-emoji-option";
        button.textContent = emoji;
        button.title = keywords;
        button.setAttribute("aria-label", keywords);
        button.addEventListener("click", () => insertAtCursor(input, emoji));
        results.append(button);
      });
      if (!matches.length) {
        const empty = document.createElement("div");
        empty.className = "setting-emoji-empty";
        empty.textContent = "No matching emoji.";
        results.append(empty);
      }
    };

    toggle.addEventListener("click", () => {
      popover.hidden = !popover.hidden;
      if (!popover.hidden) {
        render();
        search.focus();
      }
    });
    search.addEventListener("input", render);
    editor.querySelectorAll("[data-setting-placeholder]").forEach((button) => {
      button.addEventListener("click", () => insertAtCursor(input, button.dataset.settingPlaceholder));
    });
  });
})();
