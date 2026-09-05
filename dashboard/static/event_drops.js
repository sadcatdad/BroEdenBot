(() => {
  const form = document.querySelector('#drop-editor');
  if (!form) return;
  const value = name => form.elements.namedItem(name)?.value || '';
  const text = (id, content) => { document.getElementById(id).textContent = content; };
  const colors = {primary:'#5865f2',secondary:'#4e5058',success:'#248046',danger:'#da373c'};
  const preview = () => {
    text('drop-preview-title', value('title'));
    text('drop-preview-body', value('description'));
    text('drop-preview-expires', `${value('emoji')} Expires in ${value('claim_minutes') || '10'} minutes.`);
    text('drop-preview-button', `${value('button_emoji')} ${value('button_label')}`);
    document.getElementById('drop-preview-embed').style.borderLeftColor = value('color');
    document.getElementById('drop-preview-button').style.backgroundColor = colors[value('button_style')];
    const thumb = document.getElementById('drop-preview-thumb');
    const url = value('thumbnail');
    thumb.hidden = !url.startsWith('https://');
    if (!thumb.hidden && thumb.getAttribute('src') !== url) { thumb.src = url; thumb.referrerPolicy = 'no-referrer'; }
    form.querySelector('[data-fixed]').hidden = value('schedule_mode') !== 'fixed';
    form.querySelector('[data-random]').hidden = value('schedule_mode') !== 'random';
    const selected = [...form.querySelectorAll('input[name="channels"]:checked')];
    text('drop-selection-count', `${selected.length} individual channels selected`);
  };
  form.addEventListener('input', preview);
  document.getElementById('drop-channel-search').addEventListener('input', event => {
    const query = event.target.value.toLowerCase();
    form.querySelectorAll('.drop-channel-option').forEach(el => { el.hidden = !el.dataset.search.includes(query); });
  });
  let objectUrl;
  form.elements.images.addEventListener('change', event => {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    const image = document.getElementById('drop-preview-image');
    const file = event.target.files[0];
    if (file) { objectUrl = URL.createObjectURL(file); image.src = objectUrl; image.hidden = false; }
  });
  preview();
})();
