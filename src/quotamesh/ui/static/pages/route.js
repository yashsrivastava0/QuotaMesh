function renderDecisions(decisions) {
  $('#decisions').replaceChildren();
  let first = true;
  for (const decision of decisions) {
    const row = node('div', undefined, 'decision '+(decision.eligible ? 'eligible' : 'blocked'));
    const badge = decision.eligible ? (first ? 'First' : 'Backup') : 'Skipped';
    if (decision.eligible) first = false;
    row.append(node('span', badge, 'decision-badge'));
    const text = node('div'); text.append(node('strong', `${decision.position}. ${decision.label} / ${decision.model}`));
    text.append(node('small', decision.eligible ? `Allowed by saved rules · ${planName(decision.plan_type)} · provider quota not checked` : decision.explanation || reasons[decision.skip_reason] || decision.skip_reason));
    if (decision.recovery_at) text.append(node('small', 'Recovery (UTC): '+decision.recovery_at));
    if (decision.recovery_action) {
      const action=decision.recovery_action;
      const access=['add_access','rotate_access','review_expiry','review_environment'];
      const destination=access.includes(action.code) ? '/access' : ['wait','diagnose_access'].includes(action.code) ? '/help/doctor' : action.code === 'repair_accounting' ? '/help' : '/profiles';
      const link=node('a',action.label+' →','text-link');link.href=profileUrl(destination);
      if(destination==='/access' && action.credential_id)link.href+='&credential='+action.credential_id;
      text.append(link);
    }
    row.append(text); $('#decisions').append(row);
  }
  if (!decisions.length) $('#decisions').append(node('p', 'Save a credential and route target to see decisions.', 'empty'));
}

$('#refresh').onclick = () => refresh().catch(error => notice(error.message, true));

$('#cancel-test').onclick = () => controller?.abort();

$('#test-form').onsubmit = async event => {
  event.preventDefault(); if (controller) return;
  controller = new AbortController(); const form = event.currentTarget;
  form.querySelector('[type="submit"]').disabled = true; $('#cancel-test').hidden = false;
  $('#test-output').textContent = ''; $('#test-summary').textContent = 'Routing request…';
  try {
    const response = await fetch('/api/test', {method: 'POST', signal: controller.signal,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({model: 'qm/'+currentSlug, messages: [{role: 'user', content: form.elements.prompt.value}], stream: form.elements.stream.checked})});
    $('#test-summary').textContent = `HTTP ${response.status} · ${response.headers.get('X-QuotaMesh-Attempts') || '0'} upstream attempt(s) · ${response.headers.get('X-QuotaMesh-Provider') || 'no provider'} · ${response.headers.get('X-QuotaMesh-Fallback') === 'true' ? 'Backup used' : 'No fallback'}`;
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

