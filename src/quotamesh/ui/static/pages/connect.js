function renderIntegration() {
  if (!integrationData) return;
  const kind = $('#integration-client').value;
  $('#integration-alias').textContent = integrationData.model;
  $('#integration-code').textContent = integrationData.snippets[kind];
  $('#integration-instructions').textContent = integrationData.instructions[kind];
  // An unsaved default is not "disabled": it needs to be saved before any request works.
  const saved = integrationData.configured === false ? ' Save your default profile first.' : integrationData.enabled ? '' : ' This saved profile is disabled.';
  $('#integration-notice').textContent = integrationData.notice+saved+(creatingProfile ? ' Your new profile is unsaved; these instructions use the selected saved profile.' : '');
  $('#copy-status').textContent = '';
  $('#copy-integration').disabled = false;
}

$('#integration-client').onchange = renderIntegration;

// Copy stays disabled until the snippet for the chosen shell has loaded.
$('#integration-shell').onchange = () => { $('#copy-integration').disabled = true; refresh().catch(error => notice(error.message,true)); };

$('#copy-integration').onclick = async () => {
  if (!integrationData) return;
  try { await navigator.clipboard.writeText($('#integration-code').textContent); $('#copy-status').textContent = 'Copied.'; }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($('#integration-code')); selection.removeAllRanges(); selection.addRange(range); $('#integration-code').focus(); $('#copy-status').textContent = 'Text selected. Press Ctrl+C or Command+C to copy.'; }
};
