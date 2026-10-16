'use strict';

const $ = (selector) => document.querySelector(selector);

// Captured once, before any history.replaceState, so one-shot hints such as
// ?model=…&credential=… are still readable when asynchronous panels apply them.
const bootParams = new URLSearchParams(location.search);

let state = JSON.parse($('#initial-state').textContent);

const providers = JSON.parse($('#provider-options').textContent);

let controller = null;

// Pages without the header picker (Playground has its own) read the server-rendered slug.
let currentSlug = $('#profile-select')?.value || document.body.dataset.profile || 'default';
let dirty = false;
let readSignal;
const page = document.body.dataset.page;

let creatingProfile = false;

let historyCursor = null;

let refreshVersion = 0;


let historyVersion = 0;

let integrationData = null;

let diagnosticData = [];

let catalogPromise = null;

const integrationCache = new Map();

function cachedCatalog() {
  if (!catalogPromise) catalogPromise = api('/api/catalog').catch(error => { catalogPromise = null; throw error; });
  return catalogPromise;
}

function cachedIntegration(slug, shell) {
  const key = slug+'|'+shell;
  if (!integrationCache.has(key)) integrationCache.set(key, api('/api/integrations?profile='+encodeURIComponent(slug)+'&shell='+shell).catch(error => { integrationCache.delete(key); throw error; }));
  return integrationCache.get(key);
}

let doctorController = null;

const credentialDeleteConfirmations = new Set();

const reasons = {
  no_credentials: 'No API key is saved for this target provider',
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
  const form = document.activeElement?.closest('form');
  if (form) {
    let status = form.querySelector('.form-status');
    if (!status) { status = node('div', undefined, 'form-status alert'); status.setAttribute('role','status'); form.append(status); }
    status.textContent = message; status.classList.toggle('success', !failed);
  }
}

async function api(path, body, method = 'POST') {
  let response;
  try { response = await fetch(path, body === undefined ? {signal:readSignal} : {
    method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  }); } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('Cannot reach the local gateway. Check that QuotaMesh is running, then retry.');
  }
  let result;
  try { result = await response.json(); } catch { throw requestError(`Local request failed (HTTP ${response.status}). Refresh or reopen the startup link.`, response.status); }
  if (!response.ok) {
    if (response.status === 401) throw requestError('Your dashboard session expired. Reopen the startup link printed by QuotaMesh.', 401, 'unauthorized');
    showFieldErrors(result.error?.fields || []);
    throw requestError(result.error?.message || 'Request failed', response.status, result.error?.code);
  }
  return result;
}

// Errors keep the HTTP status so callers can degrade gracefully (for example a 404
// from an endpoint that this gateway version does not provide yet).
function requestError(message, status, code) {
  const error = new Error(message); error.status = status; error.code = code; return error;
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

function tagged(label, value, source) {
  const names = {MANUAL:'you entered', PROVIDER:'provider reported', LOCAL:'gateway observed / estimated', UNKNOWN:'unknown'};
  return node('p', `${label}: ${value} (${names[source] || source.replaceAll('LOCAL','gateway observed').replaceAll('PROVIDER','provider reported')})`, 'fine');
}

function dollars(value) { return value == null ? 'Unknown' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:value && Math.abs(value)<.01 ? 6 : 2}).format(value); }
function planName(plan) {return {FREE:'Free',TRIAL_CREDIT:'Trial / credit',PAID:'Paid',UNKNOWN:'Unknown plan'}[plan] || plan;}
function statusName(status) {return {ACTIVE:'Enabled',DISABLED:'Disabled',INVALID:'Key rejected',UNUSABLE:'Account unavailable',OK:'Success observed',EXPIRED:'Expired',COOLDOWN:'Temporarily rate limited',EXHAUSTED:'Quota exhausted'}[status] || status;}

function checkText(check) {
  return `${check.mode === 'models' ? 'Listing' : 'Generation'}: ${check.outcome.replaceAll('_',' ')} · ${check.http_status == null ? 'no upstream response' : 'HTTP '+check.http_status} · ${check.latency_ms ?? 'unknown'} ms · ${check.checked_at}${check.stale ? ' · stale (over 24 hours)' : ''}`;
}

const checkOutcomes = {
  listing_ok: 'The provider listed its models. Generation access and remaining quota are not verified yet.',
  listing_unsupported: 'This provider does not offer a model list. Type the exact model ID from its documentation.',
  auth: 'The provider rejected this API key. Check that you copied the whole key and chose the matching provider.',
  invalid: 'The provider rejected this API key. Check that you copied the whole key and chose the matching provider.',
  funds_or_account: 'The provider reported a billing, account, or region problem for this API key.',
  rate_limit: 'The provider rate-limited this check. Wait a moment, then try again.',
  daily_quota: 'The provider reported that the daily quota is used up. Try again after it resets.',
  timeout: 'The provider did not answer within 10 seconds. Try again.',
  network_error: 'QuotaMesh could not reach the provider. Check your connection or the base URL.',
  redirect_blocked: 'The endpoint answered with a redirect, which QuotaMesh never follows. Check the base URL.',
  protocol_error: 'The provider answered in an unexpected format. Check the base URL and provider.',
  missing_secret: 'The environment variable for this API key is not set in the running QuotaMesh process.',
  disabled: 'This API key is disabled. Enable it on the API keys page first.',
  expired: 'The expiry date of this API key has passed.',
  storage_error: 'QuotaMesh could not save the check result locally.',
  model_access: 'The provider refused access to this model or endpoint.',
  provider_failure: 'The provider reported a temporary failure. Try again shortly.',
  request_error: 'The provider rejected the request.',
  generation_failed: 'The test request failed.',
  ok: 'The test request succeeded.',
};

// Plain-language explanation for Doctor/listing outcomes; unknown codes stay visible.
function checkOutcomeText(outcome) {
  return checkOutcomes[outcome] || reasons[outcome] || 'Result: '+String(outcome || 'unknown').replaceAll('_',' ')+'.';
}

function profileUrl(path, slug = currentSlug) { return path+'?profile='+encodeURIComponent(slug); }

// Same-origin path with ?profile set; other parameters and the hash are preserved.
function linkTo(href, {keepProfile = false} = {}) {
  const url = new URL(href, location.origin);
  if (url.origin !== location.origin) return profileUrl('/');
  if (!keepProfile || !url.searchParams.has('profile')) url.searchParams.set('profile', currentSlug);
  return url.pathname+url.search+url.hash;
}

function updateProfileUrl() {
  history.replaceState(history.state,'',linkTo(location.href));
  for (const link of document.querySelectorAll('[data-profile-link]')) link.href = linkTo(link.href);
}

// Remove one-shot hints after they were applied, so a reload does not apply them twice.
function dropParams(...names) {
  const url = new URL(location.href);
  for (const name of names) url.searchParams.delete(name);
  history.replaceState(history.state,'',url.pathname+url.search+url.hash);
}

// Keep the picker label in step with the saved profile after create or rename.
function syncProfilePicker() {
  const picker = $('#profile-select');
  if (!picker || creatingProfile) return;
  if (state.profile && state.profile.slug === currentSlug) {
    let option = [...picker.options].find(o => o.value === currentSlug);
    if (!option) { option = node('option'); option.value = currentSlug; picker.append(option); }
    option.textContent = state.profile.name+' · qm/'+currentSlug;
  }
  picker.value = currentSlug;
}

// Link to the page that fixes a blocked target. Prefer the server-provided relative href.
function recoveryHref(action, decision = {}) {
  if (typeof action?.href === 'string' && /^\/(?![/\\])/.test(action.href)) return linkTo(action.href, {keepProfile: true});
  const credential = action?.credential_id ?? decision.credential_id;
  const withKey = path => profileUrl(path)+(credential ? '&credential='+encodeURIComponent(credential) : '');
  switch (action?.code) {
    case 'add_access': case 'rotate_access': case 'review_expiry': case 'review_environment': case 'review_enabled': return withKey('/access');
    case 'diagnose_access': return withKey('/help/doctor');
    case 'wait': return profileUrl('/route');
    case 'review_cost': case 'repair_accounting': return profileUrl('/route')+'#spend';
    default: return profileUrl('/profiles');
  }
}

function recoveryLabel(action, decision = {}) {
  const until = decision.recovery_at && action?.code === 'wait' ? ' (after '+new Date(decision.recovery_at).toLocaleTimeString()+')' : '';
  return (action?.label || 'Review saved rules')+until+' →';
}
// ---- Chat requests shared by Route preview, Playground and Get started ----

// Read a text/event-stream body and call onEvent({event, data}) once per event. Handles
// LF, CRLF and CR line endings (also when a CRLF is split across chunks), comments and
// multi-line data fields.
async function readEventStream(response, onEvent) {
  const reader = response.body.getReader(); const decoder = new TextDecoder();
  let buffer = ''; let carry = '';
  const dispatch = block => {
    let event = 'message'; const data = [];
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const name = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (name === 'event') event = value; else if (name === 'data') data.push(value);
    }
    if (data.length) onEvent({event, data: data.join('\n')});
  };
  const feed = text => {
    text = carry+text; carry = '';
    if (text.endsWith('\r')) { carry = '\r'; text = text.slice(0, -1); }
    buffer += text.replace(/\r\n?/g, '\n');
    let end;
    while ((end = buffer.indexOf('\n\n')) >= 0) { dispatch(buffer.slice(0, end)); buffer = buffer.slice(end+2); }
  };
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    feed(decoder.decode(value, {stream: true}));
  }
  feed(decoder.decode());
  // A stream may end without a final blank line; a held-back CR is a line ending.
  for (const block of (buffer+(carry ? '\n' : '')).split('\n\n')) if (block.trim()) dispatch(block);
}

// Merge one streamed tool-call delta. OpenAI sends an index on every fragment; Gemini sends
// a complete call without an index, so an id without an index starts a new call.
function mergeToolCall(calls, call) {
  if (!call || typeof call !== 'object') return;
  const indexed = Number.isInteger(call.index);
  let target = indexed ? calls.find(c => c.index === call.index) : call.id ? null : calls.at(-1);
  if (!target) { target = {index: indexed ? call.index : null, id: null, name: '', arguments: ''}; calls.push(target); }
  if (call.id) target.id = call.id;
  const name = call.function?.name;
  if (typeof name === 'string' && name) target.name = target.name && !name.startsWith(target.name) ? target.name+name : name;
  if (typeof call.function?.arguments === 'string') target.arguments += call.function.arguments;
}

// Accumulate an OpenAI-style chat stream. Usage can arrive on a choices-less final chunk
// (OpenAI) or on every content chunk (Gemini); the last one seen wins. A data error object or
// an `event: error` after output started is reported as result.error, never thrown.
async function consumeChatStream(response, onUpdate) {
  const result = {content: '', usage: null, finishReason: null, model: null, toolCalls: [], error: null, done: false, firstTokenAt: null, unrecognized: 0};
  await readEventStream(response, ({event, data}) => {
    if (data === '[DONE]') { result.done = true; return; }
    let payload;
    try { payload = JSON.parse(data); } catch { result.unrecognized += 1; return; }
    if (event === 'error' || payload?.error) {
      result.error = payload?.error && typeof payload.error === 'object' ? payload.error : {message: String(payload?.error || 'Upstream stream error')};
      onUpdate?.(result); return;
    }
    if (!payload || typeof payload !== 'object') { result.unrecognized += 1; return; }
    if (payload.usage && typeof payload.usage === 'object') result.usage = payload.usage;
    if (typeof payload.model === 'string') result.model = payload.model;
    for (const choice of Array.isArray(payload.choices) ? payload.choices : []) {
      const delta = choice?.delta || {};
      if (typeof delta.content === 'string' && delta.content) { result.content += delta.content; result.firstTokenAt ??= performance.now(); }
      if (Array.isArray(delta.tool_calls)) { for (const call of delta.tool_calls) mergeToolCall(result.toolCalls, call); result.firstTokenAt ??= performance.now(); }
      if (choice?.finish_reason) result.finishReason = choice.finish_reason;
    }
    onUpdate?.(result);
  });
  return result;
}

// Normalise a non-stream chat completion into the same shape as consumeChatStream.
function completionResult(payload) {
  const choice = Array.isArray(payload?.choices) ? payload.choices[0] : null;
  const message = choice?.message || {};
  const toolCalls = [];
  for (const call of Array.isArray(message.tool_calls) ? message.tool_calls : []) mergeToolCall(toolCalls, {...call, index: undefined});
  return {content: typeof message.content === 'string' ? message.content : '', usage: payload?.usage && typeof payload.usage === 'object' ? payload.usage : null,
    finishReason: choice?.finish_reason || null, model: typeof payload?.model === 'string' ? payload.model : null, toolCalls, error: null, done: true, firstTokenAt: null, unrecognized: choice ? 0 : 1};
}

// Token counts from a usage object; providers may report hidden thinking only in the total.
function usageCounts(usage) {
  if (!usage) return null;
  const input = Number.isFinite(usage.prompt_tokens) ? usage.prompt_tokens : null;
  let output = Number.isFinite(usage.completion_tokens) ? usage.completion_tokens : null;
  if (Number.isFinite(usage.total_tokens) && input != null) output = Math.max(output ?? 0, usage.total_tokens-input);
  return input == null && output == null ? null : {input, output};
}

// Error bodies are {"error": {...}} or, from Gemini, a root array [{"error": {...}}].
function errorDetails(body) {
  const item = Array.isArray(body) ? body[0] : body;
  const error = item && typeof item === 'object' ? item.error : null;
  if (!error || typeof error !== 'object') return {message: typeof error === 'string' ? error : '', code: null, type: null, param: null, status: null};
  return {message: typeof error.message === 'string' ? error.message : '', code: error.code ?? null, type: error.type ?? null, param: error.param ?? null, status: error.status ?? null};
}

// Turn a failed chat response into a plain explanation with links to the page that fixes it.
function explainFailure(status, body, headers) {
  const error = errorDetails(body);
  const code = typeof error.code === 'string' ? error.code : typeof error.type === 'string' ? error.type : '';
  const text = [error.message, error.status, code, error.type].filter(Boolean).join(' ').toLowerCase();
  const slug = headers?.get?.('X-QuotaMesh-Profile') || currentSlug;
  // The provider's own words first, as a sentence, then our guidance.
  const said = error.message.trim() ? error.message.trim()+(/[.!?]$/.test(error.message.trim()) ? ' ' : '. ') : '';
  const retry = Number(headers?.get?.('Retry-After'));
  const out = {title: '', detail: said.trim(), links: [], candidates: null, retryAfter: Number.isFinite(retry) && retry > 0 ? retry : null, tone: 'error'};
  const link = (label, path) => out.links.push({label, href: linkTo(path)});
  if (status === 401 && code === 'unauthorized') {
    out.title = 'Your dashboard session expired.'; out.detail = 'Run quotamesh open in a terminal, or reopen the dashboard link printed when QuotaMesh started.';
  } else if (code === 'paid_blocked') {
    out.title = `qm/${slug} only has paid or Unknown-plan keys, and paid use is off.`; out.detail = 'Allow paid use (ideally with a daily cap) in the profile, or add a Free target.';
    link('Review paid permission', '/profiles');
  } else if (code === 'no_candidates') {
    out.title = `No target in qm/${slug} can answer right now.`; out.detail = 'Each target was skipped for the reason below. Nothing was sent to a provider.';
    out.candidates = Array.isArray(body?.quotamesh?.candidates) ? body.quotamesh.candidates : [];
    out.tone = out.retryAfter ? 'warn' : 'error'; link('Open Route preview', '/route');
  } else if (code === 'not_configured') {
    out.title = 'qm/default is not set up yet.'; out.detail = 'Add an API key and create the default profile first.'; link('Get started', '/start');
  } else if (code === 'profile_not_found') {
    out.title = `qm/${slug} does not exist.`; out.detail = 'It may have been deleted. Choose a saved profile.'; link('Open Profiles', '/profiles');
  } else if (code === 'deadline_exceeded') {
    const info = body?.quotamesh || {};
    out.title = 'No provider answered before the profile deadline.';
    out.detail = `${info.attempts ? info.attempts+' attempt(s) were made. ' : ''}Streaming usually shows the first words sooner. You can also raise the deadline in the profile.`;
    link('Raise the profile deadline', '/profiles');
  } else if (code === 'invalid_model') {
    out.title = 'Use a model name like qm/<profile>.';
  } else if (code === 'request_too_large') {
    out.title = 'This conversation is too large to send.'; out.detail = 'Clear the conversation and start again.';
  } else if (code === 'invalid_api_key' || code === 'access_token_type_unsupported' || /please pass a valid api key|incorrect api key|invalid api key|api key not valid|api_key_invalid|access_token_type_unsupported/.test(text) || (status === 401 && headers?.get?.('X-QuotaMesh-Provider'))) {
    out.title = 'The provider rejected the API key.'; out.detail = said+'Check that the whole key was saved and belongs to this provider.';
    link('Replace the API key', '/access');
  } else if (code === 'unsupported_parameter' || /unsupported parameter|unsupported_parameter/.test(text)) {
    out.title = 'The model rejected a request setting.';
    out.detail = said+(error.param === 'max_tokens' || /max_tokens/.test(text) ? 'Newer OpenAI models need max_completion_tokens instead of max_tokens. The Playground sends max_completion_tokens; update your app to do the same.' : '');
  } else if (code === 'model_not_found' || (status === 404 && /not found|not_found|does not exist/.test(text))) {
    out.title = 'The provider does not offer this model to this key.'; out.detail = said+'Check the exact model ID, or list the models this key can use.';
    link('Edit the profile', '/profiles'); link('List models', '/help/doctor');
  } else if (code === 'insufficient_quota' || /insufficient_quota|billing/.test(text)) {
    out.title = 'The provider reports no remaining credit or quota for this key.'; out.detail = said.trim(); link('Review API keys', '/access');
  } else if (status === 429 || /rate.?limit|resource_exhausted|quota/.test(text)) {
    out.title = 'The provider rate-limited this request.'; out.detail = said+'Wait and retry, or add a backup target.'; out.tone = 'warn';
    link('Add a backup target', '/profiles');
  } else if (status === 503 && /high demand|overloaded|unavailable/.test(text)) {
    out.title = 'The provider is overloaded right now.'; out.detail = said+'Try again shortly, or add a backup target.'; out.tone = 'warn';
  } else {
    out.title = `The request failed (HTTP ${status}).`;
  }
  return out;
}

// Error card for an explained failure: plain title and detail, blocked candidates with their
// reasons and recovery links, then links to the pages that fix the problem.
function failureCard(failure) {
  const card = node('div', undefined, 'error-card'+(failure.tone === 'warn' ? ' warn' : ''));
  card.setAttribute('role', 'alert');
  card.append(node('strong', failure.title));
  if (failure.detail) card.append(node('p', failure.detail));
  if (failure.candidates?.length) {
    const list = node('ul');
    for (const candidate of failure.candidates) {
      const entry = node('li', `${candidate.label || 'Target '+candidate.position} / ${candidate.model}: ${candidate.explanation || reasons[candidate.skip_reason] || candidate.skip_reason || 'not eligible'}`);
      if (candidate.recovery_action) { entry.append(' '); const link = node('a', recoveryLabel(candidate.recovery_action, candidate)); link.href = recoveryHref(candidate.recovery_action, candidate); entry.append(link); }
      list.append(entry);
    }
    card.append(list);
  }
  if (failure.links.length) {
    const links = node('p', undefined, 'fix-links');
    for (const link of failure.links) { const anchor = node('a', link.label+' →'); anchor.href = link.href; links.append(anchor, ' '); }
    card.append(links);
  }
  return card;
}

function showFieldErrors(fields) {
  document.querySelectorAll('.field-error').forEach(e => e.remove());
  document.querySelectorAll('[aria-invalid]').forEach(e => {e.removeAttribute('aria-invalid');e.removeAttribute('aria-describedby');});
  for (const [index, field] of fields.entries()) {
    const path = field.path;
    let control;
    if (path[0] === 'targets') control = [...($('#targets')?.children[path[1]]?.querySelectorAll('[data-field]') || [])].find(e=>e.dataset.field===path[2]);
    else control = document.getElementsByName(path[0])[0] || document.getElementById('profile-'+path[0]);
    if (!control) continue;
    const error = node('span', field.message, 'field-error'); error.id = 'field-error-'+index;
    control.setAttribute('aria-invalid','true'); control.setAttribute('aria-describedby',error.id); control.after(error);
  }
  if (fields.length) document.querySelector('[aria-invalid=true]')?.focus();
}
