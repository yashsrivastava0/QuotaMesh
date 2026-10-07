function renderIntegration() {
  if (!integrationData) return;
  const kind = $('#integration-client').value;
  $('#integration-alias').textContent = integrationData.model;
  $('#integration-code').textContent = integrationData.snippets[kind];
  $('#integration-instructions').textContent = integrationData.instructions[kind];
  $('#integration-notice').textContent = integrationData.notice+(integrationData.enabled ? '' : ' This saved profile is disabled.')+(creatingProfile ? ' Your new profile is unsaved; these instructions use the selected saved profile.' : '');
  $('#copy-status').textContent = '';
}

$('#integration-client').onchange = renderIntegration;

$('#integration-shell').onchange = () => refresh().catch(error => notice(error.message,true));

$('#copy-integration').onclick = async () => {
  try { await navigator.clipboard.writeText($('#integration-code').textContent); $('#copy-status').textContent = 'Copied.'; }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($('#integration-code')); selection.removeAllRanges(); selection.addRange(range); $('#integration-code').focus(); $('#copy-status').textContent = 'Text selected. Press Ctrl+C or Command+C to copy.'; }
};
