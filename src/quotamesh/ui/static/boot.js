'use strict';
let reads = null;
let pendingNavigation = null;
let credentialSignature = '';
let modelHintApplied = false;
let accessHintApplied = false;
const legacy = {wallet:'/',access:'/access',policy:'/profiles',test:'/route',connect:'/connect',doctor:'/help/doctor',activity:'/activity',catalog:'/help/catalog'};
if (location.hash && legacy[location.hash.slice(1)]) location.replace(profileUrl(legacy[location.hash.slice(1)]));

const panelStates = new Map();

// The status box sits just before its panel, never inside it: panels can be CSS grids,
// and renderers may replace their children.
function panelState(id, area) {
  let status = panelStates.get(id);
  if (!status?.isConnected) {
    status=node('div',undefined,'panel-state');status.setAttribute('role','status');status.dataset.for=id;
    area.before(status);panelStates.set(id,status);
  }
  return status;
}

async function panel(id, work, version) {
  const area = $(id);
  if (!area) return;
  const status = panelState(id, area);
  status.textContent='Updating local observations…'; status.classList.remove('failed'); status.hidden=false;
  area.setAttribute('aria-busy','true');
  try {
    const apply = await work();
    if (version !== refreshVersion) return;
    apply(); status.hidden=true; area.dataset.stale='false';
  } catch(error) {
    if (version !== refreshVersion || error.name === 'AbortError') return;
    status.replaceChildren(node('span',error.message+' Displayed observations may be stale.'));
    const retry=node('button','Retry','secondary compact');retry.type='button';retry.onclick=()=>refresh();status.append(retry);
    status.classList.add('failed');area.dataset.stale='true';
  } finally { if (version === refreshVersion) area.removeAttribute('aria-busy'); }
}

async function refresh(editPolicy = false) {
  reads?.abort(); reads=new AbortController(); readSignal=reads.signal;
  const version=++refreshVersion; const slug=currentSlug;
  if(page==='activity')historyVersion++;
  if (editPolicy) integrationCache.clear();
  const tasks=[];
  const query='?profile='+encodeURIComponent(slug);
  const statusTask = async () => {
    const data=await api('/api/status'+query);
    if (version !== refreshVersion) return null;
    state=data.routing;syncProfilePicker();
    return data;
  };
  if (page === 'overview') {
    tasks.push(panel('#wallet',async()=>{const data=await api('/api/wallet');return ()=>{
      renderWallet(data); $('#metric-keys').textContent=Object.values(data.buckets).flat().reduce((n,s)=>n+s.keys.length,0);
      $('#metric-spend').textContent=dollars(data.today.known_cost_usd);
      document.querySelector('#metric-spend').previousElementSibling.textContent='Known observed cost today';
    };},version));
    tasks.push(panel('.next-steps',async()=>{const data=await api('/api/explain'+query);return ()=>{
      $('#metric-eligible').textContent=data.decisions.filter(d=>d.eligible).length;
      $('#journey-status').textContent=data.configured ? `qm/${slug} is saved. ${data.selected ? 'Inspect its route or connect your client.' : 'No target is eligible. Inspect the route to see what needs attention.'}` : 'Start with API access. Save an exact model in a profile, then connect your client.';
    };},version));
  }
  if (page === 'access') tasks.push(panel('#access',async()=>{const data=await statusTask();return ()=>{
    if (!data) return;
    const signature=JSON.stringify(state.credentials);
    if (signature !== credentialSignature) {renderCredentials();credentialSignature=signature;}
    $('#connection-alias').textContent='qm/'+slug;renderSetup();
    if(!accessHintApplied){const id=Number(bootParams.get('credential'));if(Number.isInteger(id) && id>0)$('#credential-list [data-credential-id="'+id+'"] button')?.click();accessHintApplied=true;dropParams('credential');}
  };},version));
  if (page === 'profiles') tasks.push(panel('#policy',async()=>{
    // Saved Doctor listings only feed model suggestions; the form still works without them.
    const [data,checks]=await Promise.all([statusTask(),api('/api/doctor').catch(error=>{if(error.name==='AbortError')throw error;return null;})]);
    return ()=>{
    if (!data) return;
    if (checks) diagnosticData=checks.checks;
    if ((editPolicy || !modelHintApplied) && !dirty) {
      fillPolicy(); $('#profile-name').value=state.profile?.name || 'Default'; $('#profile-slug').value=slug;
      $('#profile-slug').readOnly=true; $('#profile-template').value='advanced';
      const key=state.credentials.find(c=>c.id===Number(bootParams.get('credential')));
      if (!modelHintApplied && key && bootParams.get('model')) {targetRow({provider_id:key.provider_id,credential_id:key.id,model:bootParams.get('model')});dirty=true;notice('Model added to the draft. Review and save your profile.');}
      if (!modelHintApplied) dropParams('credential','model');
      modelHintApplied=true;
    }
    $('#profile-alias').textContent=creatingProfile ? 'Unsaved draft' : 'qm/'+slug;
    $('#delete-profile').hidden=slug==='default' || creatingProfile;
    $('#delete-profile').dataset.slug=slug;
    for (const row of $('#targets').children) row.refreshChoices?.();
    updateProfileUrl();
  };},version));
  if (page === 'route') {
    tasks.push(panel('#decisions',async()=>{const data=await api('/api/explain'+query);return ()=>{renderDecisions(data.decisions);renderExplanation(data);};},version));
    tasks.push(panel('#spend-note',async()=>{const data=await statusTask();return ()=>{
      if(data) $('#spend-note').textContent=`qm/${slug}: ${data.observed_today.routed_requests} requests and ${data.observed_today.attempts} attempts today. Observed paid spend: ${dollars(state.usage.daily)} today / ${dollars(state.usage.monthly)} this UTC month.${state.usage.unknown ? ' Some costs are unknown; totals are incomplete.' : ''}`;
    };},version));
  }
  if (page === 'connect') tasks.push(panel('#connect',async()=>{const data=await cachedIntegration(slug,$('#integration-shell').value);return ()=>{integrationData=data;renderIntegration();};},version));
  if (page === 'doctor') {
    tasks.push(panel('#doctor-results',async()=>{const [data,checks]=await Promise.all([statusTask(),api('/api/doctor')]);return ()=>{if(data){diagnosticData=checks.checks;renderDoctor();}};},version));
  }
  if (page === 'activity') {
    tasks.push(panel('#request-history',async()=>{const profile=$('#history-filter').value==='selected'?'&profile='+encodeURIComponent(slug):'';const data=await api('/api/activity?limit=20'+profile);return ()=>renderHistory(data,false);},version));
    tasks.push(panel('.table-wrap',async()=>{const data=await statusTask();return ()=>{if(data)renderAttempts(data.recent_attempts);};},version));
  }
  if (page === 'catalog') tasks.push(panel('#catalog-entries',async()=>{const data=await cachedCatalog();return ()=>renderCatalog(data);},version));
  if (page === 'playground') tasks.push(panel('#playground-route',async()=>{const data=await api('/api/explain'+query);return ()=>renderRouteHint(data);},version));
  // Get started always works on qm/default, whatever profile the address carries.
  if (page === 'start') tasks.push(panel('#setup-progress',async()=>{const [data,checks]=await Promise.all([api('/api/status?profile=default'),api('/api/doctor')]);return ()=>{startData=data;diagnosticData=checks.checks;renderStart();};},version));
  await Promise.all(tasks);
  if(version===refreshVersion) $('#page-load-status').textContent=page==='help' ? 'Built-in guidance. No provider calls.' : document.querySelector('[data-stale=true]') ? 'Some observations could not be updated. Retry the affected panel.' : `Local view updated ${new Date().toLocaleTimeString()}. Provider quota is not probed.`;
}

function navigate(url) {
  if (!dirty) {location.href=url;return;}
  pendingNavigation=url;$('#leave-save').hidden=!document.querySelector('form[data-editor]');
  $('#leave-dialog').returnValue='';$('#leave-dialog').showModal();
}
// Escape, Stay, or any close other than "Save and continue" cancels the pending navigation.
$('#leave-dialog').addEventListener('close',()=>{if($('#leave-dialog').returnValue!=='save')pendingNavigation=null;});
document.addEventListener('click',event=>{
  const link=event.target.closest('a');
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || link.target==='_blank') return;
  const url=new URL(link.href);
  if(url.origin===location.origin && (url.pathname!==location.pathname || url.search!==location.search)){event.preventDefault();navigate(url.href);}
});
if($('#profile-select'))$('#profile-select').onchange=event=>{
  const slug=event.target.value;event.target.value=currentSlug;navigate(profileUrl(location.pathname,slug));
};
$('#leave-discard').onclick=()=>{dirty=false;location.href=pendingNavigation;};
$('#leave-stay').onclick=()=>{$('#leave-dialog').close('stay');pendingNavigation=null;};
$('#leave-save').onclick=()=>{const form=document.querySelector('form[data-editor]');$('#leave-dialog').close('save');if(form.reportValidity() && (!$('#profile-name') || ($('#profile-name').reportValidity() && $('#profile-slug').reportValidity())))form.requestSubmit();else pendingNavigation=null;};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
document.addEventListener('input',event=>{if(event.target.closest('[data-editor]') || ['profile-name','profile-slug'].includes(event.target.id)) dirty=true;});
document.addEventListener('change',event=>{if(event.target.closest('[data-editor]'))dirty=true;});
document.addEventListener('submit',event=>{
  event.target.querySelectorAll('.field-error').forEach(e=>e.remove());
  event.target.querySelectorAll('[aria-invalid]').forEach(e=>e.removeAttribute('aria-invalid'));
});
document.addEventListener('editor-saved',()=>{dirty=false;if(pendingNavigation){location.href=pendingNavigation;pendingNavigation=null;}});

if($('#demo-form')) {
  const descriptions=JSON.parse($('#demo-guides').textContent);
  function describe(){const key=$('#demo-form').elements.scenario.value;$('#demo-instructions').textContent=descriptions[key] || 'Load this scenario, inspect the route, then send a simulated request. '+(document.body.dataset.scenarioNote || '');}
  $('#demo-form').elements.scenario.onchange=describe;
  $('#demo-form').onsubmit=async event=>{
    event.preventDefault();if(dirty && !confirm('Discard unsaved changes and reset this demo?'))return;
    const button=event.target.querySelector('button');button.disabled=true;
    try{const result=await api('/api/demo',Object.fromEntries(new FormData(event.target)));dirty=false;location.href=profileUrl('/route')+'&scenario='+encodeURIComponent(result.scenario);}
    catch(error){notice(error.message,true);button.disabled=false;}
  };
  describe();
  if($('#test-form') && ['error-first','midstream'].includes($('#demo-form').elements.scenario.value))$('#test-form').elements.stream.checked=true;
}
if(page==='access') {
  const provider=bootParams.get('provider');if(providers.includes(provider)){$('#credential-form').elements.provider_id.value=provider;$('#credential-form').closest('details').open=true;}
}
if(page==='connect' && /Windows/i.test(navigator.userAgent))$('#integration-shell').value='powershell';
updateProfileUrl();syncProfilePicker();refresh(true);

if(['overview','access','route','activity','doctor','profiles'].includes(page)) {
  const feed=new EventSource('/events');let timer;
  function observed(){clearTimeout(timer);timer=setTimeout(()=>{
    if(!controller && !doctorController && !dirty && document.visibilityState==='visible' && !document.activeElement?.matches('input,select,textarea,button'))refresh();
  },300);}
  // Both the navigation note and the Activity page show the live-update state.
  function feedStatus(text){for(const id of ['#activity-status','#activity-feed-status'])if($(id))$(id).textContent=text;}
  feed.addEventListener('activity',observed);
  feed.onopen=()=>feedStatus('Local updates connected.');
  feed.onerror=()=>feedStatus('Updates reconnecting. Retry to refresh observations.');
  window.addEventListener('pagehide',()=>{feed.close();reads?.abort();});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')observed();});
}
