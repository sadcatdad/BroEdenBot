(() => {
  const config = document.getElementById('drop-preview-config');
  if (!config) return;
  const campaignForm = document.getElementById('drop-editor');
  const variantForm = document.getElementById('variant-editor');
  const form = campaignForm || variantForm;
  const base = JSON.parse(config.dataset.campaign);
  const variants = JSON.parse(config.dataset.variants);
  const fields = ['emoji', 'title', 'description', 'color', 'thumbnail', 'button_label', 'button_emoji', 'button_style'];
  const picker = document.getElementById('drop-preview-variant');
  const colors = {primary:'#5865f2',secondary:'#4e5058',success:'#248046',danger:'#da373c'};
  let uploadUrls = [];
  const input = (name) => form?.elements.namedItem(name);
  const value = (name) => input(name)?.value || '';
  const checked = (name) => Boolean(input(name)?.checked);
  const text = (id, content) => { const el=document.getElementById(id); if(el) el.textContent=content; };

  function currentCampaign() {
    const campaign = {...base};
    if (campaignForm) {
      [...fields, 'points', 'singular', 'plural', 'claim_minutes'].forEach(key => { campaign[key]=value(key); });
      const removed = [...campaignForm.querySelectorAll('[name="remove_assets"]:checked')].map(el=>el.value);
      campaign.image_urls = [...uploadUrls, ...base.image_urls.filter(url=>!removed.includes(url.split('/').pop()))];
    }
    return campaign;
  }
  function currentVariant() {
    const item = {id:config.dataset.editingId || 'editing', name:value('name') || 'New variant',
      weight:Number(value('weight')), points:Number(value('points')), rarity:value('rarity'),
      enabled:checked('enabled'), show_reward:checked('show_reward'), show_rarity:checked('show_rarity'), image_mode:value('image_mode')};
    fields.forEach(field => { item[field+'_override']=checked('override_'+field) ? value(field+'_override') : null; });
    item.image_urls=[...uploadUrls,...variantForm.querySelectorAll('[name="asset_ids"]:checked')].map(item=>typeof item==='string'?item:item.dataset.imageUrl);
    return item;
  }
  function probabilities(draft) {
    const rows=variants.filter(v=>String(v.id)!==config.dataset.editingId).concat([draft]);
    const valid=rows.every(v=>!v.enabled || (Number.isFinite(Number(v.weight)) && Number(v.weight)>0));
    const total=rows.reduce((sum,v)=>sum+(v.enabled?Number(v.weight):0),0);
    const chance=v=>v.enabled && valid && total>0 ? Number(v.weight)/total*100 : 0;
    text('variant-estimate', valid ? `Estimated chance: ${chance(draft).toFixed(1)}%${draft.enabled?'':' · Disabled'}` : 'Enabled variants require a positive, finite weight.');
    const summary=document.getElementById('variant-probabilities');
    summary.replaceChildren();
    rows.forEach(v=>{
      const line=document.createElement('p');line.textContent=`${v.name} · ${chance(v).toFixed(1)}%${v.enabled?'':' · Disabled'}`;summary.append(line);
    });
  }
  function preview() {
    const campaign=currentCampaign();
    const draft=variantForm ? currentVariant() : null;
    const selected=picker.value==='editing' ? draft : variants.find(v=>String(v.id)===picker.value);
    const appearance={...campaign,show_reward:false,show_rarity:false};
    let images=campaign.image_urls;
    if(selected) {
      fields.forEach(field=>{ if(selected[field+'_override']!==null && selected[field+'_override']!==undefined) appearance[field]=selected[field+'_override']; });
      appearance.points=selected.points;appearance.show_reward=selected.show_reward;appearance.show_rarity=selected.show_rarity;
      if(selected.image_mode==='none') images=[];
      else if(selected.image_mode!=='inherit') images=selected.image_urls;
    }
    text('drop-preview-title',appearance.title);text('drop-preview-body',appearance.description);
    text('drop-preview-expires',`${appearance.emoji} Expires in ${appearance.claim_minutes || 10} minutes.`);
    text('drop-preview-button',`${appearance.button_emoji} ${appearance.button_label}`);
    const reward=document.getElementById('drop-preview-reward');reward.hidden=!appearance.show_reward;
    reward.textContent=`Worth: ${appearance.points} ${Number(appearance.points)===1?appearance.singular:appearance.plural}`;
    const rarity=document.getElementById('drop-preview-rarity');rarity.hidden=!(appearance.show_rarity && selected?.rarity);
    rarity.textContent=selected?.rarity ? `Rarity: ${selected.rarity}` : '';
    document.getElementById('drop-preview-embed').style.borderLeftColor=appearance.color;
    document.getElementById('drop-preview-button').style.backgroundColor=colors[appearance.button_style] || colors.primary;
    const thumb=document.getElementById('drop-preview-thumb');
    thumb.hidden=!appearance.thumbnail?.startsWith('https://');
    if(!thumb.hidden && thumb.getAttribute('src')!==appearance.thumbnail) {thumb.src=appearance.thumbnail;thumb.referrerPolicy='no-referrer';}
    const image=document.getElementById('drop-preview-image');image.hidden=!images?.length;
    if(!image.hidden && image.getAttribute('src')!==images[0]) image.src=images[0];
    if(campaignForm) {
      campaignForm.querySelector('[data-fixed]').hidden=value('schedule_mode')!=='fixed';
      campaignForm.querySelector('[data-random]').hidden=value('schedule_mode')!=='random';
      text('drop-selection-count',`${campaignForm.querySelectorAll('input[name="channels"]:checked').length} individual channels selected`);
    }
    if(variantForm) {
      fields.forEach(field=>{
        const override=checked('override_'+field);
        input(field+'_override').disabled=!override;
        variantForm.querySelector(`[data-override-hint="${field}"]`).textContent=override?'This override will be saved.':`Inheriting: ${campaign[field] || '(empty)'}`;
      });
      variantForm.querySelector('[data-variant-images]').hidden=['inherit','none'].includes(value('image_mode'));
      probabilities(draft);
    }
  }
  form?.addEventListener('input',preview);
  picker.addEventListener('change',preview);
  document.getElementById('drop-channel-search')?.addEventListener('input',event=>{
    const query=event.target.value.toLowerCase();
    campaignForm.querySelectorAll('.drop-channel-option').forEach(el=>{el.hidden=!el.dataset.search.includes(query);});
  });
  input('images')?.addEventListener('change',event=>{
    uploadUrls.forEach(url=>URL.revokeObjectURL(url));
    uploadUrls=[...event.target.files].map(file=>URL.createObjectURL(file));preview();
  });
  window.addEventListener('beforeunload',()=>uploadUrls.forEach(url=>URL.revokeObjectURL(url)));
  preview();
})();
