// Playground: a multi-turn chat with one saved profile. The conversation lives only in this
// array while the page is open; nothing is written to browser storage or kept by QuotaMesh.
const conversation = [];
let playgroundController = null;
let retryTimer = null;

function pageLink(label, path) {
  const link = node('a', label, 'text-link'); link.href = linkTo(path); return link;
}

function renderRouteHint(data) {
  const hint = $('#playground-route');
  if (!data.configured) { hint.replaceChildren(`qm/${currentSlug} is not set up yet. `, pageLink('Get started →', '/start')); return; }
  if (!data.selected) { hint.replaceChildren(`No target in qm/${currentSlug} can answer right now. `, pageLink('See why in Route preview →', '/route')); return; }
  const next = data.selected; const backups = data.decisions.filter(d => d.eligible).length-1;
  hint.textContent = `Next answer from ${next.provider_id} / ${next.model} using ${next.label} (${planName(next.plan_type)}) · ${backups > 0 ? backups+' backup target(s)' : 'no backup target'}. Remaining provider quota is only known after you send.`;
}

function addMessage(role, text) {
  $('#transcript-empty').hidden = true;
  const item = node('div', undefined, 'message '+role);
  const bubble = node('div', text, 'bubble');
  item.append(node('div', role === 'user' ? 'You' : 'Assistant', 'message-role'), bubble);
  $('#transcript').append(item);
  $('#transcript').scrollTop = $('#transcript').scrollHeight;
  return {item, bubble};
}

function addNote(item, text) { item.append(node('p', text, 'note')); }

function headerMeta(headers) {
  const get = name => headers.get('X-QuotaMesh-'+name);
  return {provider: get('Provider'), model: get('Model'), key: get('Key'), attempts: get('Attempts'), fallback: get('Fallback') === 'true', requestId: get('Request-Id')};
}

function formatMs(ms) {
  if (!Number.isFinite(ms)) return 'unknown';
  return ms < 1000 ? Math.round(ms)+' ms' : (ms/1000).toFixed(ms < 10000 ? 2 : 1)+' s';
}

function metaItem(label, value) {
  const item = node('span', label+' '); item.append(node('strong', value)); return item;
}

const costSources = {LOCAL: 'gateway estimate', PROVIDER: 'provider reported', MANUAL: 'you entered', UNKNOWN: 'unknown'};

// Metadata strip from the X-QuotaMesh-* headers and the reply's own usage; the server trace
// (when this gateway provides one) later fills in cost and server-side timing.
function renderMeta(item, meta, result, timing) {
  const strip = node('div', undefined, 'meta-strip');
  if (meta.provider) strip.append(metaItem('Provider', meta.provider));
  if (meta.model) strip.append(metaItem('Model', meta.model+(result.model && result.model !== meta.model ? ' (reported '+result.model+')' : '')));
  // Header values are ASCII-only; the trace below replaces the key label with the saved one.
  const key = meta.key ? metaItem('Key', meta.key) : null;
  if (key) strip.append(key);
  if (meta.attempts) strip.append(metaItem('Attempts', meta.attempts+(meta.fallback ? ' · backup used' : ' · no fallback')));
  strip.append(metaItem(timing.firstLabel, formatMs(timing.first)), metaItem('Total', formatMs(timing.total)));
  const counts = usageCounts(result.usage);
  strip.append(metaItem('Tokens', counts ? `${counts.input ?? '?'} in · ${counts.output ?? '?'} out (provider reported)` : 'not reported'));
  const reported = Number(result.usage?.cost_usd);
  const cost = metaItem('Cost', Number.isFinite(reported) ? dollars(reported)+' (provider reported)' : 'checking…');
  strip.append(cost);
  if (result.finishReason) strip.append(metaItem('Finish', result.finishReason));
  if (meta.requestId) { const link = node('a', 'View in Activity →'); link.href = linkTo('/activity?request='+encodeURIComponent(meta.requestId)); strip.append(link); }
  item.append(strip);
  return {item, strip, key, cost, reportedCost: Number.isFinite(reported)};
}

async function getJson(path) {
  const response = await fetch(path, {headers: {Accept: 'application/json'}});
  let body = null; try { body = await response.json(); } catch { /* not JSON */ }
  return {status: response.status, body};
}

// GET /api/requests/{id}: server timing, cost and the attempt trace with skip reasons. A gateway
// without this endpoint answers 404 without an error object; the header metadata then stands.
async function loadTrace(meta, place, streamed) {
  const fallbackCost = () => { if (!place.reportedCost) place.cost.replaceChildren('Cost ', node('strong', 'see Activity')); };
  if (!/^[a-f0-9]{32}$/.test(meta.requestId || '')) { fallbackCost(); return; }
  let missingEndpoint = false;
  // A streamed attempt is recorded when the stream closes, which can be just after the client finished.
  for (let attempt = 0; attempt < (streamed ? 4 : 2); attempt++) {
    let result;
    try { result = await getJson('/api/requests/'+meta.requestId); } catch { fallbackCost(); return; }
    if (result.status === 200 && result.body) { renderTrace(result.body, place); return; }
    if (result.status === 404 && !result.body?.error) { missingEndpoint = true; break; }
    if (result.status !== 404) break;
    await new Promise(resolve => setTimeout(resolve, 300*(attempt+1)));
  }
  fallbackCost();
  addNote(place.item, (missingEndpoint ? 'This gateway version has no attempt trace' : 'The attempt trace is not available')+'; the details above come from the response headers.');
}

function renderTrace(trace, place) {
  const totals = trace.totals || {};
  const known = totals.cost_source !== 'UNKNOWN' && totals.known_cost_usd != null;
  place.cost.replaceChildren('Cost ', node('strong', known ? `${dollars(totals.known_cost_usd)} (${costSources[totals.cost_source] || totals.cost_source})` : 'unknown'));
  const attempts = Array.isArray(trace.attempts) ? trace.attempts : [];
  const served = [...attempts].reverse().find(a => a.outcome === 'OK');
  if (served) place.strip.append(metaItem('Server', `first byte ${served.ttfb_ms ?? '?'} ms · total ${served.latency_ms ?? '?'} ms`));
  if (served?.credential_label && place.key) place.key.replaceChildren('Key ', node('strong', served.credential_label));
  const details = node('details', undefined, 'trace');
  details.append(node('summary', `Attempt trace · ${attempts.length} attempt(s)`));
  const list = node('ol');
  for (const attempt of attempts) {
    const cost = attempt.provider_cost_usd ?? attempt.estimated_cost_usd;
    const entry = node('li', `${attempt.provider_id} / ${attempt.model} · ${attempt.credential_label || 'key'} (${planName(attempt.plan_type)}) · ${attempt.outcome}${attempt.http_status ? ' HTTP '+attempt.http_status : ''}${attempt.error_class ? ' · '+attempt.error_class : ''} · ${attempt.latency_ms ?? '?'} ms · ${attempt.input_tokens ?? '?'} in / ${attempt.output_tokens ?? '?'} out · ${cost == null ? 'cost unknown' : dollars(cost)}`);
    for (const skip of Array.isArray(attempt.skipped) ? attempt.skipped : []) {
      const line = node('div', `Skipped ${skip.label || 'a target'}: ${skip.explanation || reasons[skip.skip_reason] || skip.skip_reason}`, 'fine');
      if (skip.recovery_action) { line.append(' '); const link = node('a', recoveryLabel(skip.recovery_action, skip)); link.href = recoveryHref(skip.recovery_action, skip); line.append(link); }
      entry.append(line);
    }
    list.append(entry);
  }
  details.append(list); place.item.append(details);
}

function startCountdown(element, seconds) {
  clearInterval(retryTimer);
  const until = Date.now()+seconds*1000;
  const tick = () => {
    const left = Math.ceil((until-Date.now())/1000);
    element.textContent = left > 0 ? `You can retry in ${left} s.` : 'You can retry now.';
    if (left <= 0) clearInterval(retryTimer);
  };
  tick(); retryTimer = setInterval(tick, 1000);
}

function renderFailure(reply, failure) {
  const card = failureCard(failure);
  if (failure.retryAfter) {
    const countdown = node('p'); countdown.dataset.countdown = '';
    const links = card.querySelector('.fix-links'); if (links) links.before(countdown); else card.append(countdown);
    startCountdown(countdown, failure.retryAfter);
  }
  reply.bubble.replaceWith(card);
}

// Returns true when the reply has text that can be part of the next turn.
function showReply(reply, result, meta, timing) {
  reply.bubble.classList.remove('pending');
  reply.bubble.textContent = result.content;
  for (const call of result.toolCalls) reply.item.append(node('div', `Tool call: ${call.name || 'unnamed'}(${call.arguments})`, 'tool-call'));
  if (result.error) addNote(reply.item, `The reply was interrupted after it started: ${result.error.message || 'upstream error'}. QuotaMesh does not switch providers once output has started.`);
  if (!result.content) {
    reply.bubble.classList.add('pending');
    reply.bubble.textContent = result.toolCalls.length ? '(tool call only)' : '(empty reply)';
    if (result.finishReason === 'length') addNote(reply.item, 'The model used the whole output limit before writing any text. Thinking models spend tokens on reasoning first: raise Max output tokens (for example to 1024) and send again.');
  }
  if (result.unrecognized) addNote(reply.item, 'Some upstream events were not in the expected format and were skipped.');
  const first = timing.stream ? (result.firstTokenAt ?? timing.headersAt) : timing.headersAt;
  const place = renderMeta(reply.item, meta, result, {firstLabel: timing.stream ? 'First words' : 'Response', first: first-timing.started, total: timing.finished-timing.started});
  loadTrace(meta, place, timing.stream);
  return Boolean(result.content);
}

function setBusy(busy) {
  $('#playground-send').disabled = busy; $('#playground-stop').hidden = !busy; $('#playground-profile').disabled = busy;
}

$('#playground-form').onsubmit = async event => {
  event.preventDefault();
  if (playgroundController) return;
  const text = $('#playground-message').value;
  if (!text.trim()) { $('#playground-message').focus(); return; }
  if (!$('#playground-max-tokens').reportValidity()) return;
  const maxTokens = Number($('#playground-max-tokens').value);
  const stream = $('#playground-stream').checked; const system = $('#playground-system').value.trim();
  clearInterval(retryTimer);
  const turn = {role: 'user', content: text}; conversation.push(turn);
  $('#playground-message').value = '';
  const user = addMessage('user', text);
  const reply = addMessage('assistant', stream ? 'Waiting for the first words…' : 'Waiting for the reply…');
  reply.bubble.classList.add('pending');
  const body = {model: 'qm/'+currentSlug, messages: [...(system ? [{role: 'system', content: system}] : []), ...conversation], stream, max_completion_tokens: maxTokens};
  if (stream) body.stream_options = {include_usage: true};
  $('#playground-status').textContent = `Sending to qm/${currentSlug}…`;
  playgroundController = new AbortController(); setBusy(true);
  const started = performance.now(); let partial = null; let answered = false; let meta = {};
  try {
    const response = await fetch('/api/test', {method: 'POST', signal: playgroundController.signal, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const headersAt = performance.now(); meta = headerMeta(response.headers);
    if (!response.ok) {
      let payload = null; try { payload = JSON.parse(await response.text()); } catch { /* plain-text error */ }
      renderFailure(reply, explainFailure(response.status, payload, response.headers));
      $('#playground-status').textContent = `Request failed (HTTP ${response.status}).`;
      return;
    }
    let result;
    if (response.headers.get('content-type')?.includes('text/event-stream')) {
      result = await consumeChatStream(response, update => {
        partial = update;
        if (update.content) { reply.bubble.classList.remove('pending'); reply.bubble.textContent = update.content; }
      });
    } else {
      let payload = null; try { payload = await response.json(); } catch { /* reported as unrecognized */ }
      result = completionResult(payload);
    }
    answered = showReply(reply, result, meta, {started, headersAt, finished: performance.now(), stream});
    if (answered) conversation.push({role: 'assistant', content: result.content});
    $('#playground-status').textContent = answered ? `Reply from ${meta.provider || 'the provider'} in ${formatMs(performance.now()-started)}.` : 'The reply had no text.';
  } catch (error) {
    if (error.name === 'AbortError') {
      if (partial?.content) {
        answered = true; conversation.push({role: 'assistant', content: partial.content});
        addNote(reply.item, 'Stopped. The partial reply stays in the conversation.');
      } else { reply.bubble.textContent = 'Stopped before any reply arrived.'; }
      $('#playground-status').textContent = 'Stopped.';
    } else {
      renderFailure(reply, {title: 'Cannot reach the local gateway.', detail: 'Check that QuotaMesh is still running, then send again.', links: [], tone: 'error'});
      $('#playground-status').textContent = 'Request failed.';
    }
  } finally {
    if (!answered) {
      // An unanswered message is not part of the conversation; put it back for a retry.
      const index = conversation.lastIndexOf(turn); if (index >= 0) conversation.splice(index, 1);
      user.item.classList.add('unanswered'); addNote(user.item, 'Not answered, so it is not part of the conversation.');
      if (!$('#playground-message').value) $('#playground-message').value = text;
    }
    playgroundController = null; setBusy(false);
  }
};

$('#playground-message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('#playground-form').requestSubmit(); }
});

$('#playground-stop').onclick = () => playgroundController?.abort();

$('#playground-clear').onclick = () => {
  playgroundController?.abort(); clearInterval(retryTimer); conversation.length = 0;
  for (const item of $('#transcript').querySelectorAll('.message')) item.remove();
  $('#transcript-empty').hidden = false; $('#playground-status').textContent = 'Conversation cleared.';
  $('#playground-message').focus();
};

$('#playground-profile').onchange = event => {
  currentSlug = event.target.value; updateProfileUrl(); refresh();
};
