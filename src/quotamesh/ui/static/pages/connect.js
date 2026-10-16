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

// The local key goes from POST /api/local-key/reveal straight to the clipboard. Only when the
// clipboard is denied is it shown, once, in a read-only field the user can hide.
$('#copy-local-key').onclick = async () => {
  const button = $('#copy-local-key'); button.disabled = true; $('#local-key-status').textContent = 'Copying…';
  let key = null;
  try {
    const result = await api('/api/local-key/reveal', {});
    if (typeof result?.key !== 'string' || !result.key) throw requestError('The key could not be read.', 500);
    key = result.key;
  } catch (error) {
    $('#local-key-status').textContent = error.status === 404 ? 'This QuotaMesh version cannot copy the key from the browser. Run quotamesh key in a terminal instead.' : error.message;
    button.disabled = false; return;
  }
  try {
    await navigator.clipboard.writeText(key);
    $('#local-key-status').textContent = 'Local key copied. Paste it as QUOTAMESH_KEY in your app.';
  } catch {
    $('#local-key-value').value = key; $('#local-key-fallback').hidden = false;
    $('#local-key-value').focus(); $('#local-key-value').select();
    $('#local-key-status').textContent = 'Clipboard access was denied; the key is shown once below.';
  } finally { key = null; button.disabled = false; }
};
$('#hide-local-key').onclick = () => { $('#local-key-value').value = ''; $('#local-key-fallback').hidden = true; $('#local-key-status').textContent = 'Key hidden.'; };
window.addEventListener('pagehide', () => { $('#local-key-value').value = ''; });

$('#copy-integration').onclick = async () => {
  if (!integrationData) return;
  try { await navigator.clipboard.writeText($('#integration-code').textContent); $('#copy-status').textContent = 'Copied.'; }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($('#integration-code')); selection.removeAllRanges(); selection.addRange(range); $('#integration-code').focus(); $('#copy-status').textContent = 'Text selected. Press Ctrl+C or Command+C to copy.'; }
};
