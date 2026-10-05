'use strict';
const $ = (selector) => document.querySelector(selector);
let state = JSON.parse($('#initial-state').textContent);
const providers = JSON.parse($('#provider-options').textContent);
let controller = null;
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
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const result = await response.json();
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
  const status = await api('/api/status'); state = status.routing;
  renderCredentials(); renderDecisions(status.decisions); renderAttempts(status.recent_attempts);
  $('#metric-keys').textContent = state.credentials.length;
  $('#metric-spend').textContent = '$'+state.usage.daily.toFixed(4);
  $('#spend-note').textContent = `Paid spend: $${state.usage.daily.toFixed(4)} today / $${state.usage.monthly.toFixed(4)} this UTC month. `+(state.usage.unknown ? 'Some costs are unknown; totals are incomplete. ' : '')+(status.diagnostic_write_failures ? `Metadata write failures: ${status.diagnostic_write_failures}. ` : '')+'Only gateway traffic is visible.';
  if (editPolicy) fillPolicy();
  // New credentials must become selectable without discarding unsaved target edits.
  for (const row of $('#targets').children) {
    const provider = row.querySelector('[data-field="provider_id"]').value;
    const pool = row.querySelector('[data-field="credential_id"]'); const selected = pool.value;
    pool.replaceChildren();
    for (const [id, label] of [['', 'Provider pool (by priority)'], ...state.credentials.filter(c => c.provider_id === provider).map(c => [c.id,c.label])]) {
      const option = node('option', label); option.value = id; pool.append(option);
    }
    pool.value = selected;
  }
}
$('#add-target').onclick = () => targetRow();
$('#refresh').onclick = () => refresh().catch(error => notice(error.message, true));
$('#credential-form').onsubmit = async event => {
  event.preventDefault(); const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form)); values.model = 'configured-in-route';
  try { await api('/api/credentials', values); form.reset(); await refresh(); notice('Credential saved as untested. Choose it in a target and save your route.'); } catch (error) { notice(error.message, true); }
};
$('#policy-form').onsubmit = async event => {
  event.preventDefault(); const form = event.currentTarget; const values = {};
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
  try { await api('/api/policy', values); await refresh(); notice('Route rules saved. The dry run now shows the active policy.'); } catch (error) { notice(error.message, true); }
};
if ($('#demo-form')) $('#demo-form').onsubmit = async event => {
  event.preventDefault(); try { await api('/api/demo', Object.fromEntries(new FormData(event.currentTarget))); await refresh(true); $('#test-output').textContent = 'Scenario loaded. Send a request to observe it.'; $('#test-summary').textContent = 'No test sent in this scenario yet.'; notice('Demo reset. Use stream mode for the streaming scenarios.'); } catch (error) { notice(error.message, true); }
};
$('#cancel-test').onclick = () => controller?.abort();
$('#test-form').onsubmit = async event => {
  event.preventDefault(); if (controller) return;
  controller = new AbortController(); const form = event.currentTarget;
  form.querySelector('[type="submit"]').disabled = true; $('#cancel-test').hidden = false;
  $('#test-output').textContent = ''; $('#test-summary').textContent = 'Routing request…';
  try {
    const response = await fetch('/api/test', {method: 'POST', signal: controller.signal,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({model: 'qm/default', messages: [{role: 'user', content: form.elements.prompt.value}], stream: form.elements.stream.checked})});
    $('#test-summary').textContent = `HTTP ${response.status} · ${response.headers.get('X-QuotaMesh-Attempts') || '0'} upstream attempt(s) · ${response.headers.get('X-QuotaMesh-Provider') || 'no provider'} · fallback ${response.headers.get('X-QuotaMesh-Fallback') || 'false'}`;
    if (!response.headers.get('content-type')?.includes('text/event-stream')) {
      const body = await response.json(); $('#test-output').textContent = body.choices?.[0]?.message?.content || JSON.stringify(body, null, 2);
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
  finally { controller = null; form.querySelector('[type="submit"]').disabled = false; $('#cancel-test').hidden = true; await refresh().catch(error => notice(error.message, true)); }
};
fillPolicy();
refresh().catch(error => notice(error.message, true));
