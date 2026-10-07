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
        location.href = profileUrl('/profiles')+'&credential='+credential.id+'&model='+encodeURIComponent(models.value);
      }; card.append(use);
    }
    $('#doctor-results').append(card);
  }
}

$('#doctor-mode').onchange = () => { $('#doctor-consent').checked = false; doctorTargets(); };

$('#doctor-credential').onchange = () => { $('#doctor-consent').checked = false; doctorTargets(); };

$('#cancel-doctor').onclick = () => doctorController?.abort();

$('#refresh-doctor').onclick = () => refresh();

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
