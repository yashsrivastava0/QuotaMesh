'use strict';

const $ = (selector) => document.querySelector(selector);

// Captured once, before any history.replaceState, so one-shot hints such as
// ?model=…&credential=… are still readable when asynchronous panels apply them.
const bootParams = new URLSearchParams(location.search);

let state = JSON.parse($('#initial-state').textContent);

const providers = JSON.parse($('#provider-options').textContent);

let controller = null;

let currentSlug = $('#profile-select').value;
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
