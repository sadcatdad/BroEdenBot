(() => {
  const config = document.getElementById('drop-preview-config');
  if (!config) return;
  const campaignForm = document.getElementById('drop-editor');
  const variantForm = document.getElementById('variant-editor');
  const form = campaignForm || variantForm;
  const base = JSON.parse(config.dataset.campaign);
  const variants = JSON.parse(config.dataset.variants);
  const fields = ['emoji','title','description','color','thumbnail','button_label','button_emoji','button_style'];
  const picker = document.getElementById('drop-preview-variant');
  const colors = {primary:'#5865f2',secondary:'#4e5058',success:'#248046',danger:'#da373c'};
  const input = name => form?.elements.namedItem(name);
  const value = name => input(name)?.value || '';
  const checked = name => Boolean(input(name)?.checked);
  const text = (id, content) => { const el=document.getElementById(id); if(el) el.textContent=content; };
  const substitute = (source, values) => (source || '').replace(/\{(campaign|variant|points|currency|total)\}/g,(token,key)=>String(values[key] ?? token));
  let uploadUrls=[];
  function rewardFields() {
    if(!form) return;
    const mode=value('reward_mode') || 'static';
    [['[data-static-reward]',mode==='static'],['[data-random-reward]',mode==='random']].forEach(([selector,show])=>{
      const section=form.querySelector(selector); if(!section) return;
      section.hidden=!show;
      section.querySelectorAll('input').forEach(el=>{el.disabled=!show;});
    });
    const reply=form.querySelector('[data-empty-reply]');
    if(reply) reply.hidden=!(mode==='empty' || (mode==='static' && Number(value('points'))===0));
  }
  function currentCampaign() {
    const campaign={...base};
    if(campaignForm) {
      [...fields,'name','points','points_min','points_max','reward_mode','singular','plural','claim_minutes','message_text','ping_role_id'].forEach(key=>{campaign[key]=value(key);});
      const removed=[...campaignForm.querySelectorAll('[name="remove_assets"]:checked')].map(el=>el.value);
      campaign.image_urls=[...uploadUrls,...base.image_urls.filter(url=>!removed.includes(url.split('/').pop()))];
    }
    return campaign;
  }
  function currentVariant() {
    const item={id:config.dataset.editingId || 'editing',name:value('name') || 'New variant',weight:Number(value('weight')),
      points:Number(value('points')),points_min:Number(value('points_min')),points_max:Number(value('points_max')),reward_mode:value('reward_mode'),
      rarity:value('rarity'),enabled:checked('enabled'),show_reward:checked('show_reward'),show_rarity:checked('show_rarity'),image_mode:value('image_mode'),empty_claim_message:value('empty_claim_message')};
    fields.forEach(field=>{item[field+'_override']=checked('override_'+field)?value(field+'_override'):null;});
    item.message_text_override=checked('override_message_text')?value('message_text_override'):null;
    item.image_urls=[...uploadUrls,...variantForm.querySelectorAll('[name="asset_ids"]:checked')].map(item=>typeof item==='string'?item:item.dataset.imageUrl);
    return item;
  }
  function probabilities(draft) {
    const rows=variants.filter(v=>String(v.id)!==config.dataset.editingId).concat([draft]);
    const valid=rows.every(v=>!v.enabled || (Number.isFinite(Number(v.weight)) && Number(v.weight)>0));
    const total=rows.reduce((sum,v)=>sum+(v.enabled?Number(v.weight):0),0);
    const chance=v=>v.enabled && valid && total>0?Number(v.weight)/total*100:0;
    text('variant-estimate',valid?`${chance(draft).toFixed(2)}% base chance${draft.enabled?'':' · Disabled'}${base.variants_enabled?'':' · Campaign mode OFF'}`:'Enabled variants require a positive, finite weight.');
    const summary=document.getElementById('variant-probabilities');summary.replaceChildren();
    rows.forEach(v=>{const line=document.createElement('p');line.textContent=`${v.name} · ${chance(v).toFixed(2)}%${v.enabled?'':' · Disabled'}`;summary.append(line);});
    text('variant-enabled-label',draft.enabled?'Enabled · included when variant mode is on':'Disabled · excluded from draws');
  }
  function preview() {
    rewardFields();
    const campaign=currentCampaign();
    const draft=variantForm?currentVariant():null;
    const selected=picker.value==='editing'?draft:variants.find(v=>String(v.id)===picker.value);
    const appearance={...campaign,show_reward:false,show_rarity:false};
    let images=campaign.image_urls;
    const reward=selected || campaign;
    if(selected) {
      fields.forEach(field=>{if(selected[field+'_override']!==null && selected[field+'_override']!==undefined) appearance[field]=selected[field+'_override'];});
      appearance.show_reward=selected.show_reward;appearance.show_rarity=selected.show_rarity;
      if(selected.image_mode==='none') images=[]; else if(selected.image_mode!=='inherit') images=selected.image_urls;
    }
    const random=reward.reward_mode==='random';
    const points=reward.reward_mode==='empty'?0:(random?Math.floor((Number(reward.points_min)+Number(reward.points_max))/2):Number(reward.points));
    const noun=points===1?appearance.singular:appearance.plural;
    const tokens={campaign:campaign.name,variant:selected?.name || 'Standard Drop',points,currency:noun,total:0};
    const message=selected?.message_text_override ?? campaign.message_text;
    const role=campaignForm?input('ping_role_id')?.selectedOptions[0]?.textContent:(campaign.ping_role_id?'Selected role':'');
    const content=[campaign.ping_role_id?`@${role}`:'',substitute(message,tokens)].filter(Boolean).join('\n');
    const post=document.getElementById('drop-preview-message');post.hidden=!content;post.textContent=content;
    text('drop-preview-title',appearance.title);text('drop-preview-body',appearance.description);
    text('drop-preview-expires',`${appearance.emoji} Expires in ${appearance.claim_minutes || 10} minutes.`);
    text('drop-preview-button',`${appearance.button_emoji} ${appearance.button_label}`);
    const rewardLine=document.getElementById('drop-preview-reward');rewardLine.hidden=!appearance.show_reward;
    rewardLine.textContent=`Worth: ${points} ${noun}${random?' (example roll)':''}`;
    const rarity=document.getElementById('drop-preview-rarity');rarity.hidden=!(appearance.show_rarity && selected?.rarity);rarity.textContent=selected?.rarity?`Rarity: ${selected.rarity}`:'';
    const empty=document.getElementById('drop-preview-empty');empty.hidden=points!==0;
    empty.textContent='Private claim reply: '+substitute(selected?.empty_claim_message || 'Gotcha! This drop is empty. No {currency} this time.',tokens);
    let note=random?`Random reward: ${reward.points_min}–${reward.points_max}. Preview shows one example.`:'Preview of saved or edited appearance.';
    if(selected && (!base.variants_enabled || !selected.enabled)) note+=' This variant is currently OFF and will not appear automatically.';
    text('drop-preview-note',note);
    document.getElementById('drop-preview-embed').style.borderLeftColor=appearance.color;
    document.getElementById('drop-preview-button').style.backgroundColor=colors[appearance.button_style] || colors.primary;
    const thumb=document.getElementById('drop-preview-thumb');thumb.hidden=!appearance.thumbnail?.startsWith('https://');
    if(!thumb.hidden && thumb.getAttribute('src')!==appearance.thumbnail){thumb.src=appearance.thumbnail;thumb.referrerPolicy='no-referrer';}
    const image=document.getElementById('drop-preview-image');image.hidden=!images?.length;if(!image.hidden && image.getAttribute('src')!==images[0]) image.src=images[0];
    if(campaignForm) {
      campaignForm.querySelector('[data-fixed]').hidden=value('schedule_mode')!=='fixed';
      campaignForm.querySelector('[data-random]').hidden=value('schedule_mode')!=='random';
      text('drop-selection-count',`${campaignForm.querySelectorAll('input[name="channels"]:checked').length} individual channels selected`);
    }
    if(variantForm) {
      fields.forEach(field=>{const override=checked('override_'+field);input(field+'_override').disabled=!override;variantForm.querySelector(`[data-override-hint="${field}"]`).textContent=override?'Custom value.':`Inheriting: ${campaign[field] || '(empty)'}`;});
      input('message_text_override').disabled=!checked('override_message_text');
      variantForm.querySelector('[data-variant-images]').hidden=['inherit','none'].includes(value('image_mode'));
      probabilities(draft);
    }
  }
  if(!form && base.variants_enabled) { const selected=variants.find(v=>v.enabled); if(selected) picker.value=String(selected.id); }
  form?.addEventListener('input',preview);form?.addEventListener('change',preview);picker.addEventListener('change',preview);
  document.getElementById('drop-channel-search')?.addEventListener('input',event=>{const query=event.target.value.toLowerCase();campaignForm.querySelectorAll('.drop-channel-option').forEach(el=>{el.hidden=!el.dataset.search.includes(query);});});
  input('images')?.addEventListener('change',event=>{uploadUrls.forEach(url=>URL.revokeObjectURL(url));uploadUrls=[...event.target.files].map(file=>URL.createObjectURL(file));preview();});
  // Reveal required fields inside collapsed sections when browser validation fails.
  form?.addEventListener('invalid',event=>{let node=event.target.parentElement;while(node){if(node.tagName==='DETAILS') node.open=true;node=node.parentElement;}},true);
  window.addEventListener('beforeunload',()=>uploadUrls.forEach(url=>URL.revokeObjectURL(url)));
  preview();
})();
