function targetRow(target = {}) {
  const row = node('div', undefined, 'target-row');
  const top = node('div', undefined, 'target-top');
  top.append(node('span', 'TARGET', 'eyebrow'));
  for (const [text, label, action] of [['↑', 'Move target up', -1], ['↓', 'Move target down', 1], ['×', 'Remove target', 0]]) {
    const button = node('button', text, 'icon-button'); button.type = 'button'; button.setAttribute('aria-label', label);
    button.onclick = () => {
      if (action === -1 && row.previousElementSibling) row.previousElementSibling.before(row);
      if (action === 1 && row.nextElementSibling) row.nextElementSibling.after(row);
      if (action === 0) row.remove();
      dirty = true;
    }; top.append(button);
  }
  row.append(top);
  const grid = node('div', undefined, 'policy-grid');
  const provider = select(providers.map(p => [p, p]), target.provider_id || 'custom'); provider.dataset.field = 'provider_id';
  const model = input(target.model); model.dataset.field = 'model'; model.required = true; model.maxLength = 200; model.placeholder = 'Exact upstream model ID';
  const credential = select([], ''); credential.dataset.field = 'credential_id';
  function fillPool(value) {
    credential.replaceChildren();
    for (const [id, label] of [['', 'Provider pool (by priority)'], ...state.credentials.filter(c => c.provider_id === provider.value).map(c => [c.id, c.label])]) {
      const option = node('option', label); option.value = id; credential.append(option);
    }
    credential.value = String(value ?? '');
  }
  fillPool(target.credential_id); provider.onchange = () => fillPool(null);
  grid.append(field('Provider', provider), field('Upstream model', model), field('Key choice', credential)); row.append(grid);
  const details = node('details'); details.append(node('summary', 'Optional manual token prices (USD / million tokens)'));
  details.append(node('p', 'Use your provider’s current model price. Both prices are required for capped paid routing. This is a local estimate; missing usage stays unknown.', 'fine'));
  const prices = node('div', undefined, 'two-col');
  for (const name of ['input_price', 'output_price']) {
    const value = input(target[name], 'number'); value.min = '0'; value.max = '1000000'; value.step = 'any'; value.dataset.field = name;
    prices.append(field(name === 'input_price' ? 'Input tokens' : 'Output tokens', value));
  }
  details.append(prices); row.append(details); $('#targets').append(row);
}

function fillPolicy() {
  const profile = state.profile;
  $('#targets').replaceChildren();
  for (const target of state.targets) targetRow(target);
  if (!state.targets.length) targetRow();
  if (!profile) return;
  for (const control of $('#policy-form').elements) {
    if (!control.name || !(control.name in profile)) continue;
    if (control.type === 'checkbox') control.checked = !!profile[control.name];
    else control.value = profile[control.name] ?? '';
  }
}

$('#add-target').onclick = () => {if($('#targets').children.length>=30){notice('A profile supports at most 30 targets.',true);return;}targetRow();dirty=true;};

$('#policy-form').onsubmit = async event => {
  event.preventDefault(); const form = event.currentTarget; const values = {};
  if (form.dataset.busy === 'yes') return;
  if (!$('#profile-name').checkValidity() || !$('#profile-slug').checkValidity()) {
    $('#profile-name').reportValidity(); $('#profile-slug').reportValidity(); return;
  }
  for (const control of form.elements) {
    if (!control.name) continue;
    values[control.name] = control.type === 'checkbox' ? control.checked : numberOrNull(control.value);
  }
  values.targets = [...$('#targets').children].map(row => {
    const target = {}; for (const control of row.querySelectorAll('[data-field]')) {
      const key = control.dataset.field;
      target[key] = ['credential_id','input_price','output_price'].includes(key) ? numberOrNull(control.value) : control.value;
    } return target;
  });
  const slug = $('#profile-slug').value; const name = $('#profile-name').value;
  if (!slug || !name.trim()) { notice('Enter a profile name and a lowercase slug.', true); return; }
  values.slug = slug; values.name = name;
  if (!values.targets.length) {notice('Add at least one target before saving.',true);return;}
  form.dataset.busy = 'yes'; form.querySelector('[type="submit"]').disabled = true;
  try {
    await api(creatingProfile ? '/api/profiles' : '/api/profiles/'+currentSlug, values, creatingProfile ? 'POST' : 'PUT');
    if(pendingNavigation){const destination=new URL(pendingNavigation);if(destination.searchParams.get('profile')===currentSlug){destination.searchParams.set('profile',slug);pendingNavigation=destination.href;}}
    currentSlug = slug; creatingProfile = false; dirty = false; updateProfileUrl(); await refresh(true); notice('Profile saved. Inspect Route & test to see the active policy.');
    document.dispatchEvent(new Event('editor-saved'));
  } catch (error) { pendingNavigation=null;notice(error.message, true); }
  finally { delete form.dataset.busy; form.querySelector('[type="submit"]').disabled = false; }
};

$('#new-profile').onclick = () => {
  if (dirty && !confirm('Discard the current draft and create a new profile?')) return;
  $('#policy-form').reset(); $('#targets').replaceChildren(); targetRow(); dirty = true;
  creatingProfile = true; $('#profile-name').value = ''; $('#profile-slug').value = ''; $('#profile-slug').readOnly = false;
  $('#profile-alias').textContent = 'UNSAVED PROFILE'; $('#delete-profile').hidden = true;
  $('#policy-form').elements.allow_paid.checked = false;
  $('#policy-form').elements.allow_unknown_price.checked = false;
  $('#policy-form').elements.enabled.checked = true;
  $('#policy-form').elements.allow_trial.checked = true;
  notice('New profile starts with empty targets and paid off. Enter a unique slug, choose exact models, then save. Requests still use the selected saved profile.');
  $('#profile-name').focus();
};

$('#delete-profile').onclick = async event => {
  const button = event.currentTarget;
  if (button.dataset.confirm !== 'yes') { button.dataset.confirm = 'yes'; button.textContent = 'Confirm deletion'; return; }
  try {
    await api('/api/profiles/'+currentSlug, {}, 'DELETE'); currentSlug = 'default'; creatingProfile = false;dirty=false;
    [...$('#profile-select').options].filter(o=>o.value!==currentSlug && o.value===$('#delete-profile').dataset.slug).forEach(o=>o.remove());
    button.dataset.confirm = ''; button.textContent = 'Delete profile'; await refresh(true);
    notice('Profile archived. Its slug and historical usage remain reserved.');
  } catch (error) { notice(error.message,true); }
};

$('#profile-template').onchange = event => {
  dirty = true;
  const mode = event.target.value; if (mode === 'advanced') return;
  const form = $('#policy-form');
  const rank = plan => plan === 'FREE' ? 0 : plan === 'TRIAL_CREDIT' ? 1 : 2;
  let targets = [...$('#targets').children].map(row => Object.fromEntries([...row.querySelectorAll('[data-field]')].map(c => [c.dataset.field, ['credential_id','input_price','output_price'].includes(c.dataset.field) ? numberOrNull(c.value) : c.value])));
  targets = targets.flatMap(target => {
    const keys = state.credentials.filter(c => c.provider_id === target.provider_id && (target.credential_id == null || c.id === target.credential_id));
    if (new Set(keys.map(k => k.plan_type)).size > 1) return keys.map(key => ({...target,credential_id:key.id,rank:rank(key.plan_type)}));
    return [{...target,rank:keys.length ? rank(keys[0].plan_type) : 3}];
  }).filter(t => mode === 'paid-backup' || t.rank <= (mode === 'free-only' ? 0 : 1)).sort((a,b) => a.rank-b.rank);
  if (!targets.length || targets.length > 30) { notice('Template needs 1–30 matching targets. Add access or use advanced ordered mode.',true); event.target.value = 'advanced'; return; }
  form.elements.allow_trial.checked = mode !== 'free-only'; form.elements.allow_paid.checked = mode === 'paid-backup';
  if (mode === 'paid-backup') { if (!form.elements.paid_daily_cap_usd.value) form.elements.paid_daily_cap_usd.value = 2; if (!form.elements.paid_monthly_cap_usd.value) form.elements.paid_monthly_cap_usd.value = 15; }
  $('#targets').replaceChildren(); for (const target of targets) targetRow(target);
  notice('Template applied to this form. Inspect the explicit order and permissions, then save.');
};

$('#cancel-profile').onclick = () => {dirty=false;creatingProfile=false;refresh(true);};
