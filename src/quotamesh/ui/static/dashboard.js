'use strict';

const $ = (selector) => document.querySelector(selector);

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
  try { result = await response.json(); } catch { throw new Error(`Local request failed (HTTP ${response.status}). Refresh or reopen the startup link.`); }
  if (!response.ok) {
    if (response.status === 401) throw new Error('Your dashboard session expired. Reopen the startup link printed by QuotaMesh.');
    showFieldErrors(result.error?.fields || []);
    throw new Error(result.error?.message || 'Request failed');
  }
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

function profileUrl(path, slug = currentSlug) { return path+'?profile='+encodeURIComponent(slug); }
function updateProfileUrl() {
  history.replaceState(null,'',profileUrl(location.pathname));
  for (const link of document.querySelectorAll('[data-profile-link]')) link.href = profileUrl(new URL(link.href).pathname);
  if (!creatingProfile && state.profile) {
    const select = $('#profile-select');
    if (![...select.options].some(o => o.value === currentSlug)) {
      const option = node('option',state.profile.name+' · qm/'+currentSlug); option.value = currentSlug; select.append(option);
    }
    select.value = currentSlug;
  }
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
