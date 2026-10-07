function renderWallet(wallet) {
  $('#wallet-summary').textContent = `${wallet.today.routed_requests} requests · ${wallet.today.attempts} upstream attempts · ${wallet.today.failures} failed attempts today (UTC). Known cost: ${dollars(wallet.today.known_cost_usd)}. ${wallet.today.unknown_cost} attempt(s) have unknown cost.`;
  $('#coverage-note').textContent = 'Coverage: '+wallet.coverage+'. Daily usage survives history pruning. Local paid caps can be exceeded by requests already in flight.';
  const expanded = new Set([...document.querySelectorAll('.capacity-evidence[open]')].map(item => item.dataset.group));
  $('#wallet-buckets').replaceChildren();
  for (const [plan, title] of [['FREE','Free access'],['TRIAL_CREDIT','Trials & credit'],['PAID','Paid & unknown plans']]) {
    const section = node('section', undefined, 'wallet-bucket '+plan.toLowerCase());
    section.append(node('h3', title));
    if (!wallet.buckets[plan].length) section.append(node('p', 'No access configured in this group.', 'fine'));
    for (const source of wallet.buckets[plan]) {
      const card = node('article', undefined, 'capacity-source');
      card.append(node('strong', source.keys[0].account_label || source.keys[0].label));
      card.append(tagged('Access', `${source.keys.length} key(s)${source.shared ? ' · one shared quota' : ''} · ${source.plan_types.map(planName).join(', ')}`, 'MANUAL'));
      card.append(tagged('Routed requests / attempts today', `${source.today.routed_requests} / ${source.today.attempts}`, 'LOCAL / UTC'));
      const evidence = node('details', undefined, 'capacity-evidence'); evidence.dataset.group = source.group; evidence.open = expanded.has(source.group); evidence.append(node('summary','Usage, cost, and timing evidence'));
      evidence.append(tagged('Provider / group', source.provider_id+' / '+source.group,'MANUAL'));
      evidence.append(tagged('Lifetime observed attempts', source.usage.attempts, 'LOCAL'));
      evidence.append(tagged('Last success', source.usage.last_success_at || 'Not observed', 'LOCAL'));
      evidence.append(tagged('Last error', source.usage.last_error_at ? `${source.usage.last_error_at} · ${source.usage.last_error_class}` : 'Not observed', 'LOCAL'));
      evidence.append(tagged('Last first-response time', source.usage.last_ttfb_ms == null ? 'Unknown' : source.usage.last_ttfb_ms+' ms', source.usage.last_ttfb_ms == null ? 'UNKNOWN' : 'LOCAL'));
      evidence.append(tagged('Last attempt latency', source.usage.last_latency_ms == null ? 'Unknown' : source.usage.last_latency_ms+' ms', source.usage.last_latency_ms == null ? 'UNKNOWN' : 'LOCAL'));
      evidence.append(tagged('Provider-reported USD', source.usage.provider_cost_count ? dollars(source.usage.provider_cost_usd) : 'Not reported', source.usage.sources.provider_cost_usd));
      evidence.append(tagged('Locally estimated USD', source.usage.estimated_cost_count ? dollars(source.usage.estimated_cost_usd) : 'Not estimated', source.usage.sources.estimated_cost_usd));
      evidence.append(tagged('Unknown costs', source.usage.unknown_cost+' attempt(s)', 'UNKNOWN'));
      const partial = source.usage.input_missing || source.usage.output_missing;
      evidence.append(tagged('Observed input / output tokens', `${source.usage.input_tokens ?? 'Unknown'} / ${source.usage.output_tokens ?? 'Unknown'}${partial ? ' · incomplete' : ''}`, source.usage.sources.tokens));
      if (plan === 'TRIAL_CREDIT' || source.starting_credit_usd != null || source.conflicting_credit) {
        card.append(tagged('Starting credit', source.conflicting_credit ? 'Conflicting amounts — edit keys to agree' : dollars(source.starting_credit_usd), source.starting_credit_usd == null ? 'UNKNOWN' : 'MANUAL'));
        card.append(tagged('Estimated remaining USD', dollars(source.estimated_remaining_usd), source.sources.estimated_remaining_usd));
      }
      evidence.append(tagged('Remaining provider quota', 'Unknown', 'UNKNOWN'));
      for (const rate of source.rate_observations || []) evidence.append(tagged(`${rate.model} / ${rate.dimension} (${rate.window})`, `${rate.remaining} reported at ${rate.observed_at}${rate.stale ? ' ? stale' : ' ? recent snapshot'}; other traffic may have consumed it`, 'PROVIDER'));
      card.append(evidence);
      for (const key of source.keys) {
        const observed = key.usage.last_success_at ? 'success observed' : 'untested';
        card.append(tagged(key.label, `${statusName(key.enabled ? (key.expired ? 'EXPIRED' : key.status) : 'DISABLED')} · ${observed}${!key.secret_available ? ' · environment reference unavailable' : ''}`, 'LOCAL'));
        if (key.trial_expires_at || key.plan_type === 'TRIAL_CREDIT') card.append(tagged('Expiry', key.trial_expires_at ? `${key.trial_expires_at.slice(0,10)} · ${key.expired ? 'expired' : Math.max(0, Math.ceil((Date.parse(key.trial_expires_at)-Date.now())/86400000))+' days left'}` : 'Unknown', key.trial_expires_at ? 'MANUAL' : 'UNKNOWN'));
      }
      for (const model of source.models) card.append(tagged(model.model, `${model.usage.attempts} attempt(s) · last success ${model.usage.last_success_at || 'unknown'}`, 'LOCAL'));
      for (const status of source.quota_states) card.append(tagged(status.model, `${status.status}${status.active ? ' · active block' : ' · historical observation'}${status.until ? ' · retry/reset '+status.until : ''}`, 'LOCAL'));
      card.append(tagged('Allocated profiles', source.allocated_profiles.map(p => p.slug+(p.enabled ? '' : ' (disabled)')).join(', ') || 'None', 'MANUAL'));
      if (plan === 'PAID') card.append(node('p', 'Blocked unless the selected profile explicitly allows paid use.', 'fine'));
      section.append(card);
    }
    $('#wallet-buckets').append(section);
  }
}
