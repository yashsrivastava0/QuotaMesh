'use strict';
const $ = (selector) => document.querySelector(selector);
let state = JSON.parse($('#initial-state').textContent);
const providers = JSON.parse($('#provider-options').textContent);
let controller = null;
let currentSlug = 'default';
let creatingProfile = false;
let historyCursor = null;
let refreshVersion = 0;
let policyRefreshPending = false;
let historyVersion = 0;
let integrationData = null;
let diagnosticData = [];
let doctorController = null;
const credentialDeleteConfirmations = new Set();
const reasons = {
  no_credentials: 'No credential matches this target provider',
  disabled: 'Disabled by you or the route', expired: 'Expiry date has passed', invalid: 'Key rejected — replace or rotate it',
  unusable: 'Account, billing, or regional access blocked', paid_blocked: 'Paid use is off for this route', trial_blocked: 'Trial use is off',
  missing_secret: 'Environment variable is unavailable in the running process', cooldown: 'Temporarily rate limited', exhausted: 'Explicit daily quota/reset evidence',
  degraded: 'Temporary provider/model failure (30-second block)', cap_reached: 'Observed paid cap reached',
  unknown_price: 'Cap requires both token prices; add a manual override below', unknown_spend: 'Some paid usage has unknown cost; cap cannot be measured',
  accounting_unavailable: 'Paid accounting failed; paid requests are blocked',
};
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function notice(message, failed = false) {
  $('#notice').textContent = message; $('#notice').hidden = false;
  $('#notice').classList.toggle('success', !failed);
}
async function api(path, body, method = 'POST') {
  const response = await fetch(path, body === undefined ? {} : {
    method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  let result;
  try { result = await response.json(); } catch { throw new Error(`Local request failed (HTTP ${response.status}). Refresh or reopen the startup link.`); }
  if (!response.ok) throw new Error(result.error?.message || 'Request failed');
  return result;
}
function numberOrNull(value) { return value === '' ? null : Number(value); }
function select(options, value) {
  const element = node('select');
  for (const [key, label] of options) { const option = node('option', label); option.value = key; element.append(option); }
  element.value = String(value ?? ''); return element;
}
function field(label, input) { const element = node('label', label); element.append(input); return element; }
function input(value, type = 'text') {
  const element = node('input'); element.type = type; element.value = value ?? ''; return element;
}
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
function renderCredentials() {
  $('#credential-list').replaceChildren();
  if (!state.credentials.length) $('#credential-list').append(node('p', 'Start with one credential, then add a target below.', 'empty'));
  for (const credential of state.credentials) {
    const card = node('div', undefined, 'credential');
    card.append(node('strong', credential.label), node('p', `${credential.provider_id} · ${credential.plan_type} · ${credential.enabled ? credential.status : 'DISABLED'}`));
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
function renderDecisions(decisions) {
  $('#decisions').replaceChildren();
  let first = true;
  for (const decision of decisions) {
    const row = node('div', undefined, 'decision '+(decision.eligible ? 'eligible' : 'blocked'));
    const badge = decision.eligible ? (first ? 'NEXT' : 'BACKUP') : 'SKIP';
    if (decision.eligible) first = false;
    row.append(node('span', badge, 'decision-badge'));
    const text = node('div'); text.append(node('strong', `${decision.position}. ${decision.label} / ${decision.model}`));
    text.append(node('small', decision.eligible ? `Eligible · ${decision.plan_type} · no live quota probe` : reasons[decision.skip_reason] || decision.skip_reason));
    if (decision.recovery_at) text.append(node('small', 'Recovery (UTC): '+decision.recovery_at));
    row.append(text); $('#decisions').append(row);
  }
  if (!decisions.length) $('#decisions').append(node('p', 'Save a credential and route target to see decisions.', 'empty'));
  $('#metric-eligible').textContent = decisions.filter(d => d.eligible).length;
}
function renderAttempts(attempts) {
  $('#attempts').replaceChildren();
  if (!attempts.length) { const row = node('tr'); const cell = node('td', 'No requests yet. Run a dry run or send an explicit test above.'); cell.colSpan = 4; row.append(cell); $('#attempts').append(row); }
  for (const attempt of attempts) {
    const row = node('tr');
    const time = node('td', attempt.ts.slice(0,19).replace('T',' ')); time.append(node('small', attempt.request_id.slice(0,10), 'request-id'));
    const target = node('td', `#${attempt.attempt_idx} · ${attempt.provider_id} / ${attempt.model}`);
    const result = node('td', `${attempt.outcome} ${attempt.http_status || ''}`); result.append(node('small', attempt.error_class || '', 'request-id'));
    if (attempt.skipped_json) {
      const skips = JSON.parse(attempt.skipped_json);
      if (skips.length) { const details = node('details'); details.append(node('summary', `${skips.length} initial skip(s)`)); for (const skip of skips) details.append(node('p', `${skip.label} / ${skip.model}: ${reasons[skip.skip_reason] || skip.skip_reason}`, 'fine')); result.append(details); }
    }
    const cost = attempt.provider_cost_usd ?? attempt.estimated_cost_usd;
    const latency = node('td', `${attempt.latency_ms || 0} ms`);
    latency.append(node('small', cost == null ? 'Cost unknown' : `$${cost.toFixed(6)} · ${attempt.provider_cost_usd != null ? 'provider' : 'local estimate'}`, 'request-id'));
    row.append(time, target, result, latency); $('#attempts').append(row);
  }
}
async function refresh(editPolicy = false) {
  policyRefreshPending ||= editPolicy;
  const version = ++refreshVersion; const slug = currentSlug;
  const results = await Promise.all([
    api('/api/status?profile='+encodeURIComponent(slug)), api('/api/wallet'),
    api('/api/explain?profile='+encodeURIComponent(slug)),
    api('/api/integrations?profile='+encodeURIComponent(slug)+'&shell='+$('#integration-shell').value),
    api('/api/doctor'), api('/api/catalog'),
  ]).catch(error => { if (version !== refreshVersion || slug !== currentSlug) return null; throw error; });
  if (!results) return;
  const [status, wallet, explanation, integrations, diagnostics, catalog] = results;
  if (version !== refreshVersion || slug !== currentSlug) return;
  state = status.routing; integrationData = integrations; diagnosticData = diagnostics.checks;
  renderWallet(wallet);
  const profileSelect = $('#profile-select'); profileSelect.replaceChildren();
  if (!wallet.profiles.length) { const option = node('option','Default · configure qm/default'); option.value = 'default'; profileSelect.append(option); }
  for (const profile of wallet.profiles) { const option = node('option', profile.name+' · qm/'+profile.slug+(profile.enabled ? '' : ' · disabled')); option.value = profile.slug; profileSelect.append(option); }
  profileSelect.value = currentSlug;
  if ($('#delete-profile').dataset.slug !== slug) {
    $('#delete-profile').dataset.slug = slug; $('#delete-profile').dataset.confirm = ''; $('#delete-profile').textContent = 'Delete profile';
  }
  $('#profile-alias').textContent = creatingProfile ? 'UNSAVED PROFILE' : 'qm/'+currentSlug;
  $('#delete-profile').hidden = currentSlug === 'default' || creatingProfile;
  await loadHistory();
  if (version !== refreshVersion || slug !== currentSlug) return;
  renderCredentials(); renderDecisions(explanation.decisions); renderAttempts(status.recent_attempts);
  renderExplanation(explanation); renderIntegration(); renderDoctor(); renderCatalog(catalog);
  $('#metric-keys').textContent = state.credentials.length;
  $('#metric-spend').textContent = '$'+state.usage.daily.toFixed(4);
  $('#spend-note').textContent = `qm/${currentSlug}: ${status.observed_today.routed_requests} routed request(s), ${status.observed_today.attempts} upstream attempt(s) today [LOCAL]. Paid spend: $${state.usage.daily.toFixed(4)} today / $${state.usage.monthly.toFixed(4)} this UTC month. `+(state.usage.unknown ? 'Some costs are unknown; totals are incomplete. ' : '')+(status.diagnostic_write_failures ? `Metadata write failures: ${status.diagnostic_write_failures}. ` : '')+'Only gateway traffic is visible.';
  if (policyRefreshPending && !creatingProfile) {
    fillPolicy();
    $('#profile-name').value = state.profile?.name || 'Default';
    $('#profile-slug').value = currentSlug;
    $('#profile-slug').readOnly = true;
    $('#profile-template').value = 'advanced';
    policyRefreshPending = false;
  }
  // New credentials must become selectable without discarding unsaved target edits.
  for (const row of $('#targets').children) {
    const provider = row.querySelector('[data-field="provider_id"]').value;
    const pool = row.querySelector('[data-field="credential_id"]'); const selected = pool.value;
    pool.replaceChildren();
    for (const [id, label] of [['', 'Provider pool (by priority)'], ...state.credentials.filter(c => c.provider_id === provider).map(c => [c.id,c.label])]) {
      const option = node('option', label); option.value = id; pool.append(option);
    }
    if (selected && ![...pool.options].some(option => option.value === selected)) {
      const option = node('option', 'Unavailable key #'+selected+' — choose a replacement'); option.value = selected; pool.append(option);
    }
    pool.value = selected;
  }
  $('#connection-alias').textContent = 'qm/'+currentSlug;
}
$('#add-target').onclick = () => targetRow();
$('#refresh').onclick = () => refresh().catch(error => notice(error.message, true));
$('#cancel-edit').onclick = () => {
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
    $('#cancel-edit').onclick(); await refresh();
    notice(identifier ? 'Credential updated. Secret rotation keeps shared cooldowns and usage.' : 'Credential saved as untested. Choose it in a target and save your profile.');
    if (shouldCheck) {
      $('#doctor-status').textContent = 'Checking listing access…';
      const result = await api('/api/doctor', {credential_id:Number(identifier || saved.credential_id), mode:'models'});
      $('#doctor-status').textContent = checkText(result); await refresh();
      $('#doctor').scrollIntoView({behavior:'smooth',block:'start'});
    }
  } catch (error) { notice(error.message, true); }
  finally { delete form.dataset.busy; for (const button of form.querySelectorAll('button')) button.disabled = false; }
};
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
  form.dataset.busy = 'yes'; form.querySelector('[type="submit"]').disabled = true;
  try {
    await api(creatingProfile ? '/api/profiles' : '/api/profiles/'+currentSlug, values, creatingProfile ? 'POST' : 'PUT');
    currentSlug = slug; creatingProfile = false; await refresh(true); notice('Profile saved. The dry run shows the active policy.');
  } catch (error) { notice(error.message, true); }
  finally { delete form.dataset.busy; form.querySelector('[type="submit"]').disabled = false; }
};
if ($('#demo-form')) $('#demo-form').onsubmit = async event => {
  event.preventDefault(); try { await api('/api/demo', Object.fromEntries(new FormData(event.currentTarget))); currentSlug = 'default'; creatingProfile = false; await refresh(true); $('#test-output').textContent = 'Scenario loaded. Send a request to observe it.'; $('#test-summary').textContent = 'No test sent in this scenario yet.'; notice('Demo reset. Use stream mode for the streaming scenarios.'); } catch (error) { notice(error.message, true); }
};
$('#cancel-test').onclick = () => controller?.abort();
$('#test-form').onsubmit = async event => {
  event.preventDefault(); if (controller) return;
  controller = new AbortController(); const form = event.currentTarget;
  form.querySelector('[type="submit"]').disabled = true; $('#cancel-test').hidden = false;
  $('#test-output').textContent = ''; $('#test-summary').textContent = 'Routing request…';
  try {
    const response = await fetch('/api/test', {method: 'POST', signal: controller.signal,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({model: 'qm/'+currentSlug, messages: [{role: 'user', content: form.elements.prompt.value}], stream: form.elements.stream.checked})});
    $('#test-summary').textContent = `HTTP ${response.status} · ${response.headers.get('X-QuotaMesh-Attempts') || '0'} upstream attempt(s) · ${response.headers.get('X-QuotaMesh-Provider') || 'no provider'} · fallback ${response.headers.get('X-QuotaMesh-Fallback') || 'false'}`;
    if (!response.headers.get('content-type')?.includes('text/event-stream')) {
      const text = await response.text();
      try { const body = JSON.parse(text); $('#test-output').textContent = body?.choices?.[0]?.message?.content || JSON.stringify(body, null, 2); }
      catch { $('#test-output').textContent = text || 'Empty upstream response.'; }
    } else {
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const {value, done} = await reader.read(); if (done) break;
        buffer += decoder.decode(value, {stream: true}).replace(/\r\n/g, '\n');
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const eventText = buffer.slice(0,end); buffer = buffer.slice(end+2);
          const data = eventText.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (!data || data === '[DONE]') continue;
          try { const payload = JSON.parse(data); if (payload.error) { $('#test-output').textContent += '\nStream error: '+(payload.error.message || 'Upstream error'); $('#test-summary').textContent += ' · interrupted after commitment'; } else { $('#test-output').textContent += payload.choices?.[0]?.delta?.content || ''; } } catch { $('#test-output').textContent += '\n[Unrecognized upstream event]'; }
        }
      }
    }
  } catch (error) { $('#test-summary').textContent = error.name === 'AbortError' ? 'Request stopped. Upstream cancellation recorded when detected.' : error.message; }
  finally { controller = null; $('#cancel-test').hidden = true; await refresh().catch(error => notice(error.message, true)); form.querySelector('[type="submit"]').disabled = false; }
};

function tagged(label, value, source) { return node('p', `${label}: ${value} [${source}]`, 'fine'); }
function dollars(value) { return value == null ? 'Unknown' : '$'+value.toFixed(4); }
function renderWallet(wallet) {
  $('#wallet-summary').textContent = `${wallet.today.routed_requests} routed requests today · ${wallet.today.attempts} upstream attempts · ${wallet.today.failures} failed attempts [LOCAL / UTC]. Known observed cost ${dollars(wallet.today.known_cost_usd)}; ${wallet.today.unknown_cost} attempt(s) with unknown cost. Provider and local estimates are listed separately below.`;
  $('#coverage-note').textContent = 'Coverage: '+wallet.coverage+'. Daily usage survives history pruning. Local paid caps can be exceeded by requests already in flight.';
  $('#wallet-buckets').replaceChildren();
  for (const [plan, title] of [['FREE','FREE'],['TRIAL_CREDIT','TRIAL / CREDITS'],['PAID','PAID / UNKNOWN']]) {
    const section = node('section', undefined, 'wallet-bucket '+plan.toLowerCase());
    section.append(node('h3', title));
    if (!wallet.buckets[plan].length) section.append(node('p', 'No access configured in this group.', 'fine'));
    for (const source of wallet.buckets[plan]) {
      const card = node('article', undefined, 'capacity-source');
      card.append(node('strong', source.provider_id+' / '+(source.keys[0].account_label || source.group.split(':').slice(1).join(':'))));
      card.append(tagged('Access', `${source.keys.length} key(s)${source.shared ? ' · shared quota' : ''} · ${source.plan_types.join(', ')}`, 'MANUAL'));
      card.append(tagged('Routed requests / attempts today', `${source.today.routed_requests} / ${source.today.attempts}`, 'LOCAL / UTC'));
      card.append(tagged('Lifetime observed attempts', source.usage.attempts, 'LOCAL'));
      card.append(tagged('Last success', source.usage.last_success_at || 'Not observed', 'LOCAL'));
      card.append(tagged('Last error', source.usage.last_error_at ? `${source.usage.last_error_at} · ${source.usage.last_error_class}` : 'Not observed', 'LOCAL'));
      card.append(tagged('Last attempt latency', source.usage.last_latency_ms == null ? 'Unknown' : source.usage.last_latency_ms+' ms', source.usage.last_latency_ms == null ? 'UNKNOWN' : 'LOCAL'));
      card.append(tagged('Provider-reported USD', source.usage.provider_cost_count ? dollars(source.usage.provider_cost_usd) : 'Not reported', source.usage.sources.provider_cost_usd));
      card.append(tagged('Locally estimated USD', source.usage.estimated_cost_count ? dollars(source.usage.estimated_cost_usd) : 'Not estimated', source.usage.sources.estimated_cost_usd));
      card.append(tagged('Unknown costs', source.usage.unknown_cost+' attempt(s)', 'UNKNOWN'));
      const partial = source.usage.input_missing || source.usage.output_missing;
      card.append(tagged('Observed input / output tokens', `${source.usage.input_tokens ?? 'Unknown'} / ${source.usage.output_tokens ?? 'Unknown'}${partial ? ' · incomplete' : ''}`, source.usage.sources.tokens));
      if (plan === 'TRIAL_CREDIT' || source.starting_credit_usd != null || source.conflicting_credit) {
        card.append(tagged('Starting credit', source.conflicting_credit ? 'Conflicting amounts — edit keys to agree' : dollars(source.starting_credit_usd), source.starting_credit_usd == null ? 'UNKNOWN' : 'MANUAL'));
        card.append(tagged('Estimated remaining USD', dollars(source.estimated_remaining_usd), source.sources.estimated_remaining_usd));
      }
      card.append(tagged('Remaining provider quota', 'Unknown', 'UNKNOWN'));
      for (const key of source.keys) {
        const observed = key.usage.last_success_at ? 'success observed' : 'untested';
        card.append(tagged(key.label, `${key.enabled ? (key.expired ? 'EXPIRED' : key.status) : 'DISABLED'} · ${observed}${!key.secret_available ? ' · environment reference unavailable' : ''}`, 'LOCAL'));
        if (key.trial_expires_at || key.plan_type === 'TRIAL_CREDIT') card.append(tagged('Expiry', key.trial_expires_at ? `${key.trial_expires_at.slice(0,10)} · ${key.expired ? 'expired' : Math.max(0, Math.ceil((Date.parse(key.trial_expires_at)-Date.now())/86400000))+' days left'}` : 'Unknown', key.trial_expires_at ? 'MANUAL' : 'UNKNOWN'));
      }
      for (const model of source.models) card.append(tagged(model.model, `${model.usage.attempts} attempt(s) · last success ${model.usage.last_success_at || 'unknown'}`, 'LOCAL'));
      for (const status of source.quota_states) card.append(tagged(status.model, `${status.status}${status.active ? ' · active block' : ' · historical observation'}${status.until ? ' · retry/reset '+status.until : ''}`, 'LOCAL'));
      card.append(tagged('Allocated profiles', source.allocated_profiles.map(p => p.slug+(p.enabled ? '' : ' (disabled)')).join(', ') || 'None', 'MANUAL'));
      if (plan === 'PAID') card.append(node('p', 'Blocked unless the selected profile explicitly allows paid use.', 'fine'));
      section.append(card);
    }
    $('#wallet-buckets').append(section);
  }
}
async function loadHistory(append = false) {
  const version = ++historyVersion; const slug = currentSlug;
  const profile = $('#history-filter').value === 'selected' ? '&profile='+encodeURIComponent(currentSlug) : '';
  const cursor = append && historyCursor ? '&before='+historyCursor : '';
  const data = await api('/api/activity?limit=20'+profile+cursor);
  if (version !== historyVersion || slug !== currentSlug) return;
  if (!append) $('#request-history').replaceChildren();
  if (!append && !data.requests.length) $('#request-history').append(node('p','No routed requests in this view yet.','fine'));
  for (const request of data.requests) {
    const details = node('details', undefined, 'request-trace');
    details.append(node('summary', `${request.ts.slice(0,19)} UTC · qm/${request.profile_slug} · ${request.outcome} · ${request.attempt_count} attempt(s) · ${request.latency_ms} ms [LOCAL]`));
    details.append(node('p', 'Request '+request.request_id, 'fine'));
    for (const attempt of request.attempts) {
      details.append(node('p', `#${attempt.attempt_idx} · ${attempt.credential_label} · ${attempt.provider_id} / ${attempt.model} · ${attempt.outcome} ${attempt.http_status ?? ''} · ${attempt.error_class || 'success'} · ${attempt.latency_ms ?? 'unknown'} ms [LOCAL]`, 'fine'));
      details.append(tagged('Input / output tokens', `${attempt.input_tokens ?? 'Unknown'} / ${attempt.output_tokens ?? 'Unknown'}`, attempt.input_tokens == null && attempt.output_tokens == null ? 'UNKNOWN' : 'PROVIDER'));
      const cost = attempt.provider_cost_usd ?? attempt.estimated_cost_usd;
      details.append(tagged('Attempt cost', dollars(cost), cost == null ? 'UNKNOWN' : attempt.provider_cost_usd != null ? 'PROVIDER' : 'LOCAL'));
      for (const skip of JSON.parse(attempt.skipped_json || '[]')) details.append(node('p', `Skipped ${skip.label} / ${skip.model}: ${reasons[skip.skip_reason] || skip.skip_reason}`, 'fine'));
    }
    $('#request-history').append(details);
  }
  historyCursor = data.next_before; $('#more-history').hidden = !historyCursor;
}
$('#more-history').onclick = () => loadHistory(true).catch(error => notice(error.message,true));
$('#history-filter').onchange = () => loadHistory().catch(error => notice(error.message,true));
$('#profile-select').onchange = async event => {
  currentSlug = event.target.value; creatingProfile = false;
  $('#doctor-consent').checked = false;
  await refresh(true).catch(error => notice(error.message,true));
};
$('#new-profile').onclick = () => {
  creatingProfile = true; $('#profile-name').value = ''; $('#profile-slug').value = ''; $('#profile-slug').readOnly = false;
  $('#profile-alias').textContent = 'UNSAVED PROFILE'; $('#delete-profile').hidden = true;
  $('#policy-form').elements.allow_paid.checked = false;
  $('#policy-form').elements.allow_unknown_price.checked = false;
  $('#policy-form').elements.enabled.checked = true;
  notice('New profile starts with paid off. Enter a unique slug, choose your targets and save. The test panel uses the selected saved profile until then.');
  $('#profile-name').focus();
};
$('#delete-profile').onclick = async event => {
  const button = event.currentTarget;
  if (button.dataset.confirm !== 'yes') { button.dataset.confirm = 'yes'; button.textContent = 'Confirm deletion'; return; }
  try {
    await api('/api/profiles/'+currentSlug, {}, 'DELETE'); currentSlug = 'default'; creatingProfile = false;
    button.dataset.confirm = ''; button.textContent = 'Delete profile'; await refresh(true);
    notice('Profile archived. Its slug and historical usage remain reserved.');
  } catch (error) { notice(error.message,true); }
};
$('#profile-template').onchange = event => {
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

function renderExplanation(data) {
  const eligible = data.decisions.filter(d => d.eligible).length;
  $('#explain-summary').textContent = data.configured ? `${data.selected ? 'Next: '+data.selected.label+' / '+data.selected.model : 'No eligible candidate'} · ${eligible} eligible · evaluated ${new Date(data.evaluated_at).toLocaleTimeString()}. Saved policy: qm/${currentSlug}.` : 'Add access, choose an exact model, and save your default profile. No upstream request has been made.';
  $('#cap-explanation').replaceChildren();
  for (const [period, cap] of Object.entries(data.caps)) {
    $('#cap-explanation').append(node('p', `${period === 'daily' ? 'Daily' : 'Monthly'} paid cap: ${cap.cap_usd == null ? 'none' : dollars(cap.cap_usd)} · observed ${dollars(cap.observed_usd)}${cap.incomplete ? ' + unknown costs' : ''} · headroom ${cap.headroom_usd == null ? 'unknown / not capped' : dollars(cap.headroom_usd)} [LOCAL / UTC]`, 'fine'));
  }
  $('#cap-explanation').append(node('p', `Paid: ${data.profile?.allow_paid ? 'allowed' : 'blocked'} · trials: ${data.profile?.allow_trial ? 'allowed' : 'blocked'}. Earliest known recovery: ${data.earliest_recovery_at || 'unknown / no timed block'}. Recovery may not remove other policy blocks.`, 'fine'));
  $('#unallocated').replaceChildren(node('p', 'Other owned access outside this profile:', 'fine'));
  if (!data.unallocated.length) $('#unallocated').append(node('p','All credentials are listed in this profile.','fine'));
  for (const credential of data.unallocated) $('#unallocated').append(node('p', credential.label+' · '+credential.reason,'fine'));
}
function renderIntegration() {
  if (!integrationData) return;
  const kind = $('#integration-client').value;
  $('#integration-alias').textContent = integrationData.model;
  $('#integration-code').textContent = integrationData.snippets[kind];
  $('#integration-instructions').textContent = integrationData.instructions[kind];
  $('#integration-notice').textContent = integrationData.notice+(integrationData.enabled ? '' : ' This saved profile is disabled.')+(creatingProfile ? ' Your new profile is unsaved; these instructions use the selected saved profile.' : '');
  $('#copy-status').textContent = '';
}
$('#integration-client').onchange = renderIntegration;
$('#integration-shell').onchange = () => refresh().catch(error => notice(error.message,true));
$('#copy-integration').onclick = async () => {
  try { await navigator.clipboard.writeText($('#integration-code').textContent); $('#copy-status').textContent = 'Copied.'; }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($('#integration-code')); selection.removeAllRanges(); selection.addRange(range); $('#integration-code').focus(); $('#copy-status').textContent = 'Text selected. Press Ctrl+C or Command+C to copy.'; }
};
function checkText(check) {
  return `${check.mode === 'models' ? 'Listing' : 'Generation'}: ${check.outcome.replaceAll('_',' ')} · ${check.http_status == null ? 'no upstream response' : 'HTTP '+check.http_status} · ${check.latency_ms ?? 'unknown'} ms · ${check.checked_at}${check.stale ? ' · stale (over 24 hours)' : ''}`;
}
function doctorTargets() {
  const generation = $('#doctor-mode').value === 'generation';
  $('#doctor-target-label').hidden = !generation; $('#doctor-consent-label').hidden = !generation;
  const selected = $('#doctor-target').value; $('#doctor-target').replaceChildren();
  const credential = state.credentials.find(c => c.id === Number($('#doctor-credential').value));
  for (const target of state.targets.filter(t => credential && t.provider_id === credential.provider_id && (t.credential_id == null || t.credential_id === credential.id))) {
    const option = node('option', `${target.position}. ${target.provider_id} / ${target.model}`); option.value = target.position; $('#doctor-target').append(option);
  }
  if ([...$('#doctor-target').options].some(o => o.value === selected)) $('#doctor-target').value = selected;
  $('#doctor-form button[type=submit]').disabled = !!doctorController || !credential || (generation && !$('#doctor-target').options.length);
}
function renderDoctor() {
  const selected = $('#doctor-credential').value; $('#doctor-credential').replaceChildren();
  for (const credential of state.credentials) { const option = node('option', credential.label+' · '+credential.provider_id); option.value = credential.id; $('#doctor-credential').append(option); }
  if ([...$('#doctor-credential').options].some(o => o.value === selected)) $('#doctor-credential').value = selected;
  doctorTargets(); $('#doctor-results').replaceChildren();
  if (!diagnosticData.length) $('#doctor-results').append(node('p','No checks recorded. Saved access remains untested until you run a check or route traffic.','fine'));
  for (const check of diagnosticData) {
    const credential = state.credentials.find(c => c.id === check.credential_id); if (!credential) continue;
    const card = node('article',undefined,'credential'); card.append(node('strong',credential.label),node('p',checkText(check),'fine'));
    card.append(node('p',check.mode === 'models' ? `Check time and latency [LOCAL]. Model IDs [${check.models_source}]. Generation permissions and quota remain unverified.` : 'Generation test [LOCAL]; any attempt cost is included in usage and paid caps.','fine'));
    if (check.models.length) {
      const models = select(check.models.map(id => [id,id]),check.models[0]); models.setAttribute('aria-label','Discovered model for '+credential.label); card.append(models);
      const use = node('button','Use model in profile form','compact secondary'); use.type = 'button';
      use.onclick = () => {
        for (const row of [...$('#targets').children]) if (!row.querySelector('[data-field="model"]').value) row.remove();
        if ($('#targets').children.length >= 30) { notice('Profiles allow at most 30 targets.',true); return; }
        targetRow({provider_id:credential.provider_id, model:models.value, credential_id:credential.id});
        notice('Model added to the profile form. Inspect permissions and save; discovery never saves a target automatically.'); $('#policy').scrollIntoView({behavior:'smooth'});
      }; card.append(use);
    }
    $('#doctor-results').append(card);
  }
}
$('#doctor-mode').onchange = () => { $('#doctor-consent').checked = false; doctorTargets(); };
$('#doctor-credential').onchange = () => { $('#doctor-consent').checked = false; doctorTargets(); };
$('#refresh-doctor').onclick = () => refresh().catch(error => notice(error.message,true));
$('#cancel-doctor').onclick = () => doctorController?.abort();
$('#doctor-form').onsubmit = async event => {
  event.preventDefault(); if (doctorController) return;
  const generation = $('#doctor-mode').value === 'generation';
  if (generation && !$('#doctor-consent').checked) { $('#doctor-status').textContent = 'Authorize quota use before running generation.'; $('#doctor-consent').focus(); return; }
  const values = {credential_id:Number($('#doctor-credential').value), mode:$('#doctor-mode').value, profile:currentSlug, position:generation ? Number($('#doctor-target').value) : null, consent:generation && $('#doctor-consent').checked};
  doctorController = new AbortController(); doctorTargets(); $('#cancel-doctor').hidden = false;
  for (const selector of ['#doctor-mode','#doctor-credential','#doctor-target']) $(selector).disabled = true;
  $('#doctor-status').textContent = 'Running explicit check…';
  try {
    const response = await fetch('/api/doctor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values),signal:doctorController.signal});
    const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || 'Check failed');
    $('#doctor-status').textContent = checkText(result)+(result.saved ? '' : ' · result not saved')+' · '+result.notice;
    await refresh();
  } catch(error) { $('#doctor-status').textContent = error.name === 'AbortError' ? 'Check stopped.' : error.message; }
  finally { doctorController = null; $('#cancel-doctor').hidden = true; for (const selector of ['#doctor-mode','#doctor-credential','#doctor-target']) $(selector).disabled = false; $('#doctor-consent').checked = false; doctorTargets(); }
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
function renderCatalog(data) {
  $('#catalog-entries').replaceChildren();
  for (const entry of data.entries) {
    const card = node('article',undefined,'catalog-entry'); card.append(node('h3',entry.name),node('p',entry.access_note,'fine'),node('p',`${entry.configured ? 'Configured' : 'Not configured as a preset'} · ${entry.compatibility} · verified ${entry.last_verified}`,'fine'));
    const links = node('div',undefined,'actions');
    for (const [title,href] of [['Official docs',entry.docs_url],['Get access',entry.signup_url]]) { const link = node('a',title,'text-link'); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; links.append(link); }
    const add = node('a',entry.preset ? 'Configure access' : 'Custom setup','text-link'); add.href = '#access';
    add.onclick = () => { if ($('#credential-form').elements.credential_id.value) { notice('Finish or cancel your credential edit before adding access.',true); return; } $('#credential-form').elements.provider_id.value = entry.preset ? entry.id : 'custom'; $('#credential-form').closest('details').open = true; };
    links.append(add); card.append(links); $('#catalog-entries').append(card);
  }
}
if (/Windows/i.test(navigator.userAgent)) $('#integration-shell').value = 'powershell';
fillPolicy(); refresh(true).catch(error => notice(error.message,true));
const activityFeed = new EventSource('/events'); let activityTimer = null;
function activityRefresh() {
  clearTimeout(activityTimer); activityTimer = setTimeout(() => {
    if (!controller && !doctorController && document.visibilityState === 'visible') refresh().catch(error => notice(error.message,true));
  },300);
}
activityFeed.addEventListener('activity',activityRefresh);
activityFeed.addEventListener('refresh',() => { $('#activity-status').textContent = 'Local activity connected.'; });
activityFeed.onopen = () => { $('#activity-status').textContent = 'Local activity connected.'; activityRefresh(); };
activityFeed.onerror = () => { $('#activity-status').textContent = 'Activity reconnecting. Refresh dry run to update manually.'; };
window.addEventListener('pagehide',() => activityFeed.close());
document.addEventListener('visibilitychange',() => { if (document.visibilityState === 'visible') activityRefresh(); });
const sectionObserver = new IntersectionObserver(entries => {
  for (const entry of entries) if (entry.isIntersecting) {
    for (const link of document.querySelectorAll('.section-nav a')) {
      if (link.hash === '#'+entry.target.id) link.setAttribute('aria-current','location'); else link.removeAttribute('aria-current');
    }
  }
},{rootMargin:'-10% 0px -65% 0px'});
for (const section of document.querySelectorAll('main > [id]')) sectionObserver.observe(section);
