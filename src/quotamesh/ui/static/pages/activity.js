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

async function loadHistory(append = false) {
  const version = ++historyVersion; const slug = currentSlug;
  const profile = $('#history-filter').value === 'selected' ? '&profile='+encodeURIComponent(currentSlug) : '';
  const cursor = append && historyCursor ? '&before='+historyCursor : '';
  const data = await api('/api/activity?limit=20'+profile+cursor);
  if (version !== historyVersion || slug !== currentSlug) return;
  renderHistory(data, append);
}

function renderHistory(data, append) {
  const expanded = new Set([...document.querySelectorAll('#request-history details[open]')].map(item => item.dataset.requestId));
  if (!append) $('#request-history').replaceChildren();
  if (!append && !data.requests.length) $('#request-history').append(node('p','No routed requests in this view yet.','fine'));
  for (const request of data.requests) {
    const details = node('details', undefined, 'request-trace'); details.dataset.requestId = request.request_id; details.open = expanded.has(request.request_id);
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

$('#history-filter').onchange = () => refresh();
