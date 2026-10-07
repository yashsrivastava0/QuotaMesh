function renderSetup() {
  const pending = currentSlug === 'default' && !state.targets.length && !creatingProfile;
  $('#setup').hidden = !pending;
  const chosen = $('#setup-credential').value;
  $('#setup-credential').replaceChildren();
  for (const credential of state.credentials) {
    const option = node('option', credential.label+' · '+planName(credential.plan_type)); option.value = credential.id; $('#setup-credential').append(option);
  }
  if ([...$('#setup-credential').options].some(o => o.value === chosen)) $('#setup-credential').value = chosen;
  $('#setup-form button').disabled = !state.credentials.length;
  $('#setup-models').replaceChildren();
  for (const check of diagnosticData.filter(c => c.credential_id === Number($('#setup-credential').value))) for (const model of check.models || []) {
    const option = node('option'); option.value = model; $('#setup-models').append(option);
  }
}

$('#setup-credential').onchange = renderSetup;

$('#setup-form').onsubmit = async event => {
  event.preventDefault(); const button = event.currentTarget.querySelector('button'); button.disabled = true;
  try {
    await api('/api/setup', {credential_id: Number($('#setup-credential').value), model: $('#setup-model').value});
    dirty=false;await refresh(true); notice('Default profile created. Paid use is off. Inspect the saved route.'); location.href = profileUrl('/route');
  } catch (error) { $('#setup-status').textContent = error.message; }
  finally { button.disabled = !state.credentials.length; }
};

function renderCredentials() {
  $('#credential-list').replaceChildren();
  if (!state.credentials.length) $('#credential-list').append(node('p', 'Start with one credential, then add a target below.', 'empty'));
  for (const credential of state.credentials) {
    const card = node('div', undefined, 'credential');card.dataset.credentialId=credential.id;
    card.append(node('strong', credential.label), node('p', `${credential.provider_id} · ${planName(credential.plan_type)} · ${statusName(credential.enabled ? credential.status : 'DISABLED')}`));
    card.append(node('small', `Group: ${credential.quota_group || (credential.provider_id === 'groq' ? 'individual key' : 'provider default')} · priority ${credential.priority}${credential.trial_expires_at ? ' · expiry '+credential.trial_expires_at.slice(0,10) : ''}`));
    const actions = node('div', undefined, 'actions');
    const edit = node('button', 'Edit / rotate', 'compact secondary'); edit.type = 'button';
    edit.onclick = () => {
      const form = $('#credential-form'); form.reset();
      form.elements.credential_id.value = credential.id;
      for (const control of form.elements) {
        if (!control.name || ['secret_value','credential_id'].includes(control.name)) continue;
        control.value = control.name === 'trial_expires_at' ? (credential[control.name] || '').slice(0,10) : credential[control.name] ?? '';
      }
      // Rotation is explicit: never resubmit the current environment reference as a secret edit.
      form.elements.env_name.value = '';
      $('#credential-editor-title').textContent = 'Edit '+credential.label+' — leave secret fields blank to keep access';
      $('#cancel-edit').hidden = false; form.closest('details').open = true; form.scrollIntoView({behavior:'smooth',block:'center'});
    }; actions.append(edit);
    const remove = node('button', 'Delete', 'compact danger secondary'); remove.type = 'button';
    if (credentialDeleteConfirmations.has(credential.id)) remove.textContent = 'Confirm deletion';
    remove.onclick = async () => {
      if (!credentialDeleteConfirmations.has(credential.id)) { credentialDeleteConfirmations.add(credential.id); remove.textContent = 'Confirm deletion'; return; }
      try { await api(`/api/credentials/${credential.id}`, {}, 'DELETE'); credentialDeleteConfirmations.delete(credential.id); await refresh(); notice('Secret deleted. Historical usage retained.'); } catch (error) { notice(error.message, true); }
    }; actions.append(remove);
    for (const [action, label] of [[credential.enabled ? 'disable' : 'enable', credential.enabled ? 'Disable' : 'Enable'], ['reset', 'Reset shared state']]) {
      const button = node('button', label, 'compact secondary'); button.type = 'button';
      button.onclick = async () => { try { await api(`/api/credentials/${credential.id}/action`, {action}); await refresh(); notice(action === 'reset' ? 'Credential state and its shared quota group reset. This does not restore provider quota.' : 'Credential updated.'); } catch (error) { notice(error.message, true); } };
      actions.append(button);
    }
    card.append(actions); $('#credential-list').append(card);
  }
}

$('#cancel-edit').onclick = () => {
  dirty = false;
  $('#credential-form').reset(); $('#credential-form').elements.credential_id.value = '';
  $('#credential-editor-title').textContent = 'Add API access'; $('#cancel-edit').hidden = true;
};

$('#credential-form').onsubmit = async event => {
  event.preventDefault(); const form = event.currentTarget;
  if (form.dataset.busy === 'yes') return;
  form.dataset.busy = 'yes'; const shouldCheck = form.dataset.check === 'yes'; delete form.dataset.check;
  for (const button of form.querySelectorAll('button')) button.disabled = true;
  const values = Object.fromEntries(new FormData(form)); const identifier = values.credential_id; delete values.credential_id;
  values.priority = Number(values.priority || 0); values.starting_credit_usd = numberOrNull(values.starting_credit_usd);
  for (const name of ['quota_group','account_label','trial_expires_at']) values[name] = values[name] || null;
  for (const name of ['secret_value','env_name']) if (!values[name]) delete values[name];
  try {
    const saved = await api(identifier ? '/api/credentials/'+identifier : '/api/credentials', values, identifier ? 'PATCH' : 'POST');
    $('#cancel-edit').onclick(); document.dispatchEvent(new Event('editor-saved')); await refresh();
    notice(identifier ? 'Credential updated. Secret rotation keeps shared cooldowns and usage.' : 'Credential saved as untested. Choose it in a target and save your profile.');
    if (shouldCheck) {
      notice('Access saved. Checking model listing…');
      const result = await api('/api/doctor', {credential_id:Number(identifier || saved.credential_id), mode:'models'});
      diagnosticData = [result];
      notice('Access saved. '+checkText(result)); await refresh();
      
    }
  } catch (error) { pendingNavigation=null;notice(error.message, true); }
  finally { delete form.dataset.busy; for (const button of form.querySelectorAll('button')) button.disabled = false; }
};

$('#save-check').onclick = () => { const form = $('#credential-form'); if (form.reportValidity()) { form.dataset.check = 'yes'; form.requestSubmit(); } };

$('#preview-env').onclick = async () => {
  const button = $('#preview-env'); button.disabled = true;
  try {
    const data = await api('/api/environment'); const area = $('#environment-results'); area.replaceChildren();
    for (const variable of data.variables) {
      const label = node('label',undefined,'check'); const control = node('input'); control.type = 'checkbox'; control.value = variable.env_name; control.disabled = !variable.present || variable.configured;
      label.append(control,node('span',`${variable.env_name} · ${variable.configured ? 'already configured' : variable.present ? 'present' : 'not present'}`)); area.append(label);
    }
    const save = node('button','Import selected as UNKNOWN','secondary'); save.type = 'button';
    save.onclick = async () => {
      const names = [...area.querySelectorAll('input:checked')].map(control => control.value);
      if (!names.length) { notice('Select a present variable to import.',true); return; }
      save.disabled = true;
      try { const result = await api('/api/environment/import',{names}); await refresh(); notice(`${result.imported.length} environment reference(s) imported. Paid stays disabled; no targets were added.`); area.replaceChildren(); }
      catch(error) { notice(error.message,true); save.disabled = false; }
    }; area.append(save,node('p',data.notice,'fine'));
  } catch(error) { notice(error.message,true); } finally { button.disabled = false; }
};
