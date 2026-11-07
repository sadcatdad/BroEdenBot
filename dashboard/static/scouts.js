(() => {
  document.querySelectorAll('[data-add-question]').forEach(button => {
    button.hidden = !button.closest('[data-form-builder]').querySelector('[data-extra-question][hidden]');
    button.addEventListener('click', () => {
      const row = button.closest('[data-form-builder]').querySelector('[data-extra-question][hidden]');
      if (!row) return;
      row.hidden = false;
      row.querySelector('input[type=checkbox]').checked = true;
      row.querySelector('input[maxlength="45"]').focus();
      if (!button.closest('[data-form-builder]').querySelector('[data-extra-question][hidden]')) button.hidden = true;
    });
  });
  document.querySelectorAll('[data-picker-search]').forEach(input => input.addEventListener('input', () => {
    input.parentElement.querySelectorAll('.scout-picker-options label').forEach(label => { label.hidden = !label.textContent.toLowerCase().includes(input.value.toLowerCase()); });
  }));
  document.querySelectorAll('[data-record-search]').forEach(input => {
    let timer, controller;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        if (controller) controller.abort();
        controller = new AbortController();
        const select = input.parentElement.querySelector('[data-record-select]');
        try {
          const response = await fetch('/scouts/api/records?q=' + encodeURIComponent(input.value), {signal: controller.signal, credentials: 'same-origin'});
          if (!response.ok) return;
          const rows = await response.json(), old = select.selectedOptions[0];
          select.replaceChildren(new Option('New standalone entry', ''));
          if (old && old.value && !rows.some(r => r.value === old.value)) select.add(new Option(old.textContent, old.value, true, true));
          rows.forEach(row => select.add(new Option(row.name, row.value, false, old && old.value === row.value)));
        } catch (error) { /* Keep the existing selection when connectivity changes. */ }
      }, 300);
    });
  });
})();
