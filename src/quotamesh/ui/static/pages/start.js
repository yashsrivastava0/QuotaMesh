// Get started: add a key, check it, choose paid use, create qm/default, send a first message.
// .env text and key values live only in this page until they are imported; nothing is stored
// in the browser.
const START_STEPS = ['key', 'check', 'paid', 'profile', 'message', 'connect'];
const PLAN_OPTIONS = [['FREE', 'Free'], ['TRIAL_CREDIT', 'Trial & credit'], ['PAID', 'Paid'], ['UNKNOWN', 'Unknown (treated as paid)']];
const ENV_STATUS = {
  new: 'Ready to import', configured: 'Already saved', duplicate: 'Same key as an earlier line',
  placeholder: 'Placeholder, not a real key', unsupported: 'Not a provider key QuotaMesh knows', invalid: 'Not a usable key value',
};
let startData = null;
let envText = null;
const newKeys = new Set();
const chosenModels = new Map();
const checkFailures = new Map();
let checking = false;
let firstReplyOk = false;

function startProgress() {
  const routing = startData?.routing; const credentials = routing?.credentials || [];
  const configured = Boolean(routing?.profile && routing.targets.length);
  return {
    key: credentials.length > 0,
    check: diagnosticData.some(c => c.outcome === 'listing_ok' && credentials.some(k => k.id === c.credential_id)),
    paid: configured,
    profile: configured,
    message: firstReplyOk || (startData?.recent_attempts || []).some(a => a.outcome === 'OK'),
    connect: false,
  };
}

function renderProgress() {
  const done = startProgress(); const current = START_STEPS.find(step => !done[step]);
  START_STEPS.forEach((step, index) => {
    const status = done[step] ? 'done' : step === current ? 'current' : 'todo';
    for (const element of document.querySelectorAll(`[data-step="${step}"]`)) {
      element.classList.toggle('done', status === 'done'); element.classList.toggle('current', status === 'current');
      const label = element.querySelector('.step-state'); if (label) label.textContent = {done: 'Done', current: 'Next', todo: 'To do'}[status];
      const mark = element.querySelector('.step-mark'); if (mark) mark.textContent = status === 'done' ? '✓' : String(index+1);
    }
  });
}

// Models a chat test can use come first, with small and fast variants at the top.
function modelRank(id) {
  const name = id.toLowerCase();
  if (/(embed|tts|whisper|transcri|audio|realtime|image|imagen|dall-e|moderation|search|computer-use|veo|aqa|davinci|babbage|rerank|lyria)/.test(name)) return 3;
  if (/(lite|nano|mini|flash|small|haiku|instant|8b)/.test(name)) return 0;
  if (/(gpt|gemini|gemma|llama|claude|mistral|mixtral|qwen|deepseek|grok|command|chat|instruct)/.test(name)) return 1;
  return 2;
}
function sortModels(models) { return [...models].sort((a, b) => modelRank(a)-modelRank(b) || a.localeCompare(b)); }

function listing(credentialId) { return diagnosticData.find(c => c.credential_id === credentialId && c.mode === 'models'); }

function renderStart() {
  const credentials = startData.routing.credentials;
  $('#saved-keys').textContent = credentials.length ? `Saved API keys: ${credentials.map(c => `${c.label} (${planName(c.plan_type)})`).join(', ')}.` : 'No API keys saved yet.';
  renderKeyChecks(); renderSetupForm(); renderProgress();
}

// Step 1: .env paste or file, parsed by the local server (values never come back).
function envSource() { return envText ?? $('#env-content').value; }

$('#env-file').onchange = () => {
  const file = $('#env-file').files[0]; if (!file) return;
  if (file.size > 65536) { $('#env-status').textContent = 'This file is larger than 64 KiB.'; $('#env-file').value = ''; return; }
  const reader = new FileReader();
  reader.onload = () => { envText = String(reader.result); $('#env-content').value = ''; $('#env-status').textContent = `Loaded ${file.name}. Its contents are not shown. Choose Find keys.`; };
  reader.onerror = () => { $('#env-status').textContent = 'The file could not be read.'; };
  reader.readAsText(file);
};
$('#env-content').addEventListener('input', () => { envText = null; $('#env-file').value = ''; });

function useManualKey(message) {
  $('#env-import').hidden = true; $('#manual-key').open = true;
  $('#manual-note').textContent = message; $('#manual-note').hidden = false;
}

$('#env-parse').onclick = async () => {
  const content = envSource();
  if (!content.trim()) { $('#env-status').textContent = 'Paste your .env text or choose a file first.'; return; }
  $('#env-parse').disabled = true; $('#env-status').textContent = 'Looking for provider keys…';
  try {
    renderEnvRows(await api('/api/environment/parse', {content}));
  } catch (error) {
    if (error.status === 404) useManualKey('This QuotaMesh version cannot read .env files. Enter the key here instead.');
    else $('#env-status').textContent = error.message;
  } finally { $('#env-parse').disabled = false; }
};

function renderEnvRows(data) {
  const rows = $('#env-rows'); rows.replaceChildren();
  for (const entry of data.entries) {
    const ready = entry.status === 'new';
    const row = node('tr'); row.dataset.name = entry.name;
    const pick = node('input'); pick.type = 'checkbox'; pick.checked = ready; pick.disabled = !ready; pick.setAttribute('aria-label', 'Import '+entry.name);
    const plan = select(PLAN_OPTIONS, entry.suggested_plan || 'UNKNOWN'); plan.disabled = !ready; plan.setAttribute('aria-label', 'Plan for '+entry.name);
    const cells = [[pick], [node('code', entry.name)], [entry.provider_id || '—'], [entry.fingerprint ? `fp ${entry.fingerprint} · ${entry.length} chars` : '—'], [ENV_STATUS[entry.status] || entry.status], [plan]];
    for (const content of cells) { const cell = node('td'); cell.append(...content); row.append(cell); }
    rows.append(row);
  }
  $('#env-warnings').replaceChildren(...data.warnings.map(w => node('li', `Line ${w.line}: ${w.message}`)));
  const ready = data.entries.filter(e => e.status === 'new').length;
  $('#env-results').hidden = !data.entries.length;
  $('#env-import-button').disabled = !ready;
  $('#env-status').textContent = data.entries.length ? `Found ${data.entries.length} entr${data.entries.length === 1 ? 'y' : 'ies'}; ${ready} ready to import.` : 'No provider keys found. Names look like GEMINI_API_KEY=… or OPENAI_API_KEY=….';
}

$('#env-import-button').onclick = async () => {
  const items = [...$('#env-rows').children].filter(row => row.querySelector('input').checked).map(row => ({name: row.dataset.name, plan_type: row.querySelector('select').value}));
  if (!items.length) { $('#env-status').textContent = 'Tick at least one key to import.'; return; }
  $('#env-import-button').disabled = true;
  try {
    const result = await api('/api/environment/import-file', {content: envSource(), store: 'value', items});
    for (const item of result.imported) newKeys.add(item.credential_id);
    // The pasted text held secrets: drop it as soon as the keys are saved.
    envText = null; $('#env-content').value = ''; $('#env-file').value = ''; $('#env-results').hidden = true; $('#env-rows').replaceChildren();
    const skipped = result.skipped.map(s => `${s.name} (${ENV_STATUS[s.reason] || s.reason})`).join(', ');
    $('#env-status').textContent = `Imported ${result.imported.length} API key(s).${skipped ? ' Skipped: '+skipped+'.' : ''}${result.warnings.length ? ' '+result.warnings.map(w => w.message).join(' ') : ''}`;
    await refresh();
  } catch (error) { $('#env-status').textContent = error.message; $('#env-import-button').disabled = false; }
};

$('#manual-key-form').elements.provider_id.onchange = event => { $('#manual-base-url').hidden = event.target.value !== 'custom'; };
$('#manual-key-form').onsubmit = async event => {
  event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('button'); button.disabled = true;
  const values = {provider_id: form.elements.provider_id.value, plan_type: form.elements.plan_type.value, secret_value: form.elements.secret_value.value.trim()};
  if (form.elements.label.value.trim()) values.label = form.elements.label.value.trim();
  if (values.provider_id === 'custom') values.base_url = form.elements.base_url.value.trim();
  try {
    const saved = await api('/api/credentials', values); newKeys.add(saved.credential_id);
    form.reset(); $('#manual-base-url').hidden = true; notice('API key saved. Next, check it.'); await refresh();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
};

// Step 2: explicit model listings; they contact the provider but never generate.
function renderKeyChecks() {
  const credentials = startData.routing.credentials; const list = $('#key-checks'); list.replaceChildren();
  $('#check-keys').disabled = checking || !credentials.length;
  if (!credentials.length) { list.append(node('p', 'Add an API key first.', 'empty')); return; }
  for (const credential of credentials) {
    const card = node('div', undefined, 'key-check'); card.dataset.credentialId = credential.id;
    card.append(node('strong', credential.label), node('span', ` · ${credential.provider_id} · ${planName(credential.plan_type)}`, 'fine'));
    const check = listing(credential.id);
    if (checkFailures.has(credential.id)) card.append(node('p', checkFailures.get(credential.id), 'fine'));
    else if (!check) card.append(node('p', 'Not checked yet.', 'fine'));
    else if (check.outcome === 'listing_ok') {
      const models = sortModels(check.models || []);
      card.append(node('p', `Found ${models.length} model(s).`, 'fine'));
      if (models.length) {
        if (!models.includes(chosenModels.get(credential.id))) chosenModels.set(credential.id, models[0]);
        const choice = select(models.map(m => [m, m]), chosenModels.get(credential.id)); choice.setAttribute('aria-label', 'Model for '+credential.label);
        choice.onchange = () => { chosenModels.set(credential.id, choice.value); if ($('#setup-key').value === String(credential.id)) $('#setup-model-id').value = choice.value; };
        card.append(choice);
      }
    } else card.append(node('p', checkOutcomeText(check.outcome), 'fine'));
    list.append(card);
  }
}

$('#check-keys').onclick = async () => {
  const pending = startData.routing.credentials.filter(c => newKeys.has(c.id) || listing(c.id)?.outcome !== 'listing_ok');
  if (!pending.length) { $('#check-status').textContent = 'Every key already has a successful model listing.'; return; }
  checking = true; renderKeyChecks();
  for (const credential of pending) {
    $('#check-status').textContent = `Listing models for ${credential.label}…`;
    try {
      const result = await api('/api/doctor', {credential_id: credential.id, mode: 'models'});
      checkFailures.delete(credential.id); newKeys.delete(credential.id);
      diagnosticData = [...diagnosticData.filter(c => !(c.credential_id === credential.id && c.mode === 'models')), result];
    } catch (error) { checkFailures.set(credential.id, error.message); }
    renderKeyChecks();
  }
  checking = false; $('#check-status').textContent = 'Checks finished.'; renderStart(); renderSetupForm(true);
};

// Steps 3 and 4: paid permission feeds the first-run setup request.
function paidMode() { return document.querySelector('[name=paid-mode]:checked').value; }
for (const radio of document.querySelectorAll('[name=paid-mode]')) radio.onchange = () => { $('#paid-options').hidden = paidMode() !== 'paid'; renderSetupForm(); };

function renderSetupForm(preferListed = false) {
  const routing = startData.routing; const configured = Boolean(routing.profile && routing.targets.length);
  $('#setup-done').hidden = !configured; $('#setup-default-form').hidden = configured;
  if (configured) {
    const targets = routing.targets.map(t => `${t.provider_id} / ${t.model}`).join(', ');
    $('#setup-summary').textContent = `qm/default is set up with ${targets}. Paid use is ${routing.profile.allow_paid ? 'allowed' : 'off'}.`;
    return;
  }
  const keySelect = $('#setup-key'); const previous = keySelect.value;
  keySelect.replaceChildren(...routing.credentials.map(c => { const option = node('option', `${c.label} · ${planName(c.plan_type)}`); option.value = c.id; return option; }));
  const free = routing.credentials.find(c => c.plan_type === 'FREE' && chosenModels.has(c.id)) || routing.credentials.find(c => chosenModels.has(c.id));
  if (previous && !preferListed && [...keySelect.options].some(o => o.value === previous)) keySelect.value = previous;
  else if (free) keySelect.value = String(free.id);
  const id = Number(keySelect.value); const listed = listing(id)?.models || [];
  $('#setup-model-list').replaceChildren(...sortModels(listed).map(m => { const option = node('option'); option.value = m; return option; }));
  if (chosenModels.has(id) && (preferListed || !$('#setup-model-id').value)) $('#setup-model-id').value = chosenModels.get(id);
  const key = routing.credentials.find(c => c.id === id);
  const blocked = key && ['PAID', 'UNKNOWN'].includes(key.plan_type) && paidMode() === 'free';
  $('#setup-warning').hidden = !blocked;
  $('#setup-warning').textContent = blocked ? `${key.label} has plan ${planName(key.plan_type)}. With Free only, qm/default cannot use it: choose a Free key, or allow paid backup in step 3.` : '';
  $('#setup-default-form button').disabled = !routing.credentials.length;
}
$('#setup-key').onchange = () => { $('#setup-model-id').value = ''; renderSetupForm(); };

$('#setup-default-form').onsubmit = async event => {
  event.preventDefault(); const button = event.currentTarget.querySelector('button'); button.disabled = true;
  const body = {credential_id: Number($('#setup-key').value), model: $('#setup-model-id').value.trim()};
  if (paidMode() === 'paid') Object.assign(body, {allow_paid: true, paid_daily_cap_usd: numberOrNull($('#paid-cap').value), input_price: numberOrNull($('#paid-input-price').value), output_price: numberOrNull($('#paid-output-price').value)});
  try {
    const result = await api('/api/setup', body);
    notice(`${result.alias} created. Paid use is ${result.allow_paid ? 'allowed up to your cap' : 'off'}. Next, send a first message.`);
    await refresh(); $('#step-message').scrollIntoView({block: 'start'}); $('#first-message').focus();
  } catch (error) {
    notice(error.status === 409 ? 'qm/default already exists. Edit it on the Profiles page.' : error.message, true);
    if (error.status === 409) await refresh();
  } finally { button.disabled = false; }
};

// Step 5: one streamed message through the real gateway path.
$('#first-message-form').onsubmit = async event => {
  event.preventDefault(); const text = $('#first-message').value.trim(); if (!text) return;
  const button = event.currentTarget.querySelector('button'); button.disabled = true;
  const out = $('#first-reply'); out.replaceChildren(node('p', 'Waiting for the first words…', 'fine'));
  const started = performance.now();
  try {
    const response = await fetch('/api/test', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: 'qm/default', messages: [{role: 'user', content: text}], stream: true, stream_options: {include_usage: true}, max_completion_tokens: 256})});
    const header = name => response.headers.get('X-QuotaMesh-'+name);
    if (!response.ok) {
      let payload = null; try { payload = JSON.parse(await response.text()); } catch { /* plain-text error */ }
      out.replaceChildren(failureCard(explainFailure(response.status, payload, response.headers))); return;
    }
    const reply = node('div', '', 'bubble first-reply'); out.replaceChildren(reply);
    const result = response.headers.get('content-type')?.includes('text/event-stream')
      ? await consumeChatStream(response, partial => { reply.textContent = partial.content; })
      : completionResult(await response.json().catch(() => null));
    reply.textContent = result.content || '(empty reply)';
    const counts = usageCounts(result.usage); const seconds = ((performance.now()-started)/1000).toFixed(1);
    out.append(node('p', `Answered by ${header('Provider')} / ${header('Model')} using ${header('Key')} · ${header('Attempts')} attempt(s)${header('Fallback') === 'true' ? ', backup used' : ''} · ${seconds} s${counts ? ` · ${counts.input ?? '?'} in / ${counts.output ?? '?'} out tokens` : ''}.`, 'fine'));
    if (result.error) out.append(node('p', 'The reply was interrupted: '+(result.error.message || 'upstream error'), 'fine'));
    if (!result.content && result.finishReason === 'length') out.append(node('p', 'The model used the whole output limit before writing text. Try the Playground with a larger Max output tokens.', 'fine'));
    if (result.content) { firstReplyOk = true; renderProgress(); }
  } catch {
    out.replaceChildren(failureCard({title: 'Cannot reach the local gateway.', detail: 'Check that QuotaMesh is still running, then send again.', links: []}));
  } finally { button.disabled = false; }
};
