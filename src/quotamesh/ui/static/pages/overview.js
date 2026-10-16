function compactNumber(value) {
  if (value == null) return 'Unknown';
  if (value >= 1e6) return (value/1e6).toFixed(value >= 1e7 ? 0 : 1)+'M';
  if (value >= 1e4) return Math.round(value/1e3)+'K';
  return value.toLocaleString('en-US');
}

function renderToday(today) {
  // With no attempts the gateway reports tokens as unknown; nothing was used, so show 0.
  const tokens = value => today.attempts ? compactNumber(value) : '0';
  $('#tile-requests').textContent = compactNumber(today.routed_requests);
  $('#tile-input').textContent = tokens(today.input_tokens);
  $('#tile-output').textContent = tokens(today.output_tokens);
  $('#metric-spend').textContent = dollars(today.known_cost_usd);
  $('#tile-unknown').textContent = String(today.unknown_cost);
}

// ---- 14-day stacked bar chart (inline SVG; colours come from classes, never style attributes) ----
const SVG_NS = 'http://www.w3.org/2000/svg';
let usageDaily = null;

function svgNode(tag, attributes = {}, text) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (text !== undefined) element.textContent = text;
  return element;
}

// Colour follows the provider in registry order; slots 1-3 are validated as a set (all pairs,
// colour-vision deficiency included), so a fourth provider and beyond fold into "Other".
function chartSeries(daily) {
  const used = new Set(daily.flatMap(day => Object.entries(day.by_provider || {}).filter(([, v]) => v.routed_requests > 0).map(([id]) => id)));
  const ordered = [...providers.filter(id => used.has(id)), ...[...used].filter(id => !providers.includes(id)).sort()];
  const series = ordered.slice(0, 3).map((id, index) => ({label: id, className: 'series-'+(index+1), members: [id]}));
  if (ordered.length > 3) series.push({label: 'Other ('+ordered.slice(3).join(', ')+')', className: 'series-other', members: ordered.slice(3)});
  return series;
}

function seriesValue(day, entry, field = 'routed_requests') {
  return entry.members.reduce((sum, id) => sum+(day.by_provider?.[id]?.[field] || 0), 0);
}

// Clean, whole-number tick steps (1, 2, 5 × 10ⁿ) giving at most about four intervals.
function niceStep(max) {
  const rough = Math.max(1, max)/4; const power = 10 ** Math.floor(Math.log10(rough));
  return Math.max(1, [1, 2, 5, 10].map(f => f*power).find(step => step >= rough) || power*10);
}

function dayLabel(day) { return new Date(day+'T00:00:00Z').toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'}); }

function renderUsageChart(daily) {
  const card = $('#usage-chart');
  if (!Array.isArray(daily) || !daily.length) { card.hidden = true; return; } // older gateway: no series
  usageDaily = daily; card.hidden = false;
  const series = chartSeries(daily);
  const totals = daily.map(day => day.routed_requests);
  // One series needs no legend box: the title names it.
  $('#chart-title').textContent = 'Routed requests per day, last 14 days'+(series.length === 1 ? ' · '+series[0].label : '');
  $('#chart-legend').replaceChildren(...(series.length > 1 ? series.map(entry => { const item = node('li'); item.append(node('span', undefined, 'swatch '+entry.className), entry.label); return item; }) : []));
  $('#chart-legend').hidden = series.length < 2;
  renderChartTable(daily, series);
  const empty = !totals.some(Boolean);
  $('#chart-empty').hidden = !empty;
  const frame = $('#chart-frame'); frame.hidden = empty; if (empty) { frame.replaceChildren(); return; }

  const width = Math.max(300, Math.round(frame.clientWidth || 640)); const height = 260;
  const margin = {top: 26, right: 12, bottom: 48, left: 52};
  const plotWidth = width-margin.left-margin.right; const plotHeight = height-margin.top-margin.bottom;
  const step = niceStep(Math.max(...totals)); const yMax = Math.ceil(Math.max(...totals)/step)*step;
  const y = value => margin.top+plotHeight-(value/yMax)*plotHeight;
  const band = plotWidth/daily.length; const barWidth = Math.min(24, Math.max(6, band*0.6));
  const chart = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, width, height, role: 'group', 'aria-labelledby': 'chart-title'});
  for (let value = 0; value <= yMax; value += step) {
    chart.append(svgNode('line', {x1: margin.left, x2: width-margin.right, y1: y(value), y2: y(value), class: value ? 'gridline' : 'baseline'}));
    chart.append(svgNode('text', {x: margin.left-8, y: y(value)+4, 'text-anchor': 'end', class: 'tick'}, compactNumber(value)));
  }
  chart.append(svgNode('text', {x: 14, y: margin.top+plotHeight/2, transform: `rotate(-90 14 ${margin.top+plotHeight/2})`, 'text-anchor': 'middle', class: 'axis-title'}, 'Requests'));
  chart.append(svgNode('text', {x: margin.left+plotWidth/2, y: height-6, 'text-anchor': 'middle', class: 'axis-title'}, 'Day (UTC)'));
  const labelEvery = Math.max(1, Math.ceil(52/band));
  const tip = svgNode('g', {class: 'chart-tip', visibility: 'hidden', 'aria-hidden': 'true'});
  daily.forEach((day, index) => {
    const x = margin.left+index*band+(band-barWidth)/2; const group = svgNode('g', {class: 'day'});
    let base = y(0); const drawn = series.map(entry => ({entry, value: seriesValue(day, entry)})).filter(part => part.value > 0);
    drawn.forEach((part, position) => {
      const top = y(0)-(part.value+drawn.slice(0, position).reduce((s, p) => s+p.value, 0))/yMax*plotHeight;
      const bottom = position ? base-2 : base; // 2px surface gap between stacked segments
      if (bottom-top < 1) { base = top; return; }
      const radius = position === drawn.length-1 ? Math.min(4, (bottom-top)/2) : 0; // rounded data end only
      const path = `M${x},${bottom}V${top+radius}Q${x},${top} ${x+radius},${top}H${x+barWidth-radius}Q${x+barWidth},${top} ${x+barWidth},${top+radius}V${bottom}Z`;
      group.append(svgNode('path', {d: path, class: 'segment '+part.entry.className}));
      base = top;
    });
    if ((daily.length-1-index) % labelEvery === 0) chart.append(svgNode('text', {x: x+barWidth/2, y: margin.top+plotHeight+18, 'text-anchor': 'middle', class: 'tick'}, index === daily.length-1 ? 'Today' : dayLabel(day.day)));
    if (index === daily.length-1 && day.routed_requests) chart.append(svgNode('text', {x: x+barWidth/2, y: y(day.routed_requests)-8, 'text-anchor': 'middle', class: 'value-label'}, compactNumber(day.routed_requests)));
    const parts = series.map(entry => `${entry.label} ${seriesValue(day, entry)}`).join(', ');
    const summary = `${dayLabel(day.day)}: ${day.routed_requests} request(s)${series.length ? ' ('+parts+')' : ''}, known cost ${dollars(day.known_cost_usd)}${day.unknown_cost ? ', '+day.unknown_cost+' with unknown cost' : ''}`;
    const hit = svgNode('rect', {x: margin.left+index*band, y: margin.top, width: band, height: plotHeight, class: 'hit', tabindex: '0', role: 'img', 'aria-label': summary});
    const show = () => { group.classList.add('active'); showTip(tip, day, series, x+barWidth/2, width); };
    const hide = () => { group.classList.remove('active'); tip.setAttribute('visibility', 'hidden'); };
    hit.addEventListener('pointerenter', show); hit.addEventListener('focus', show);
    hit.addEventListener('pointerleave', hide); hit.addEventListener('blur', hide);
    group.append(hit); chart.append(group);
  });
  chart.append(tip); frame.replaceChildren(chart);
}

// Value-led readout for one day: the number is strong, the series name follows.
function showTip(tip, day, series, centre, width) {
  const lines = [[`${day.routed_requests} request(s)`, 'tip-value'], [dayLabel(day.day)+' (UTC)', '']];
  for (const entry of series) lines.push([`${seriesValue(day, entry)}  ${entry.label}`, '', entry.className]);
  lines.push([`Known cost ${dollars(day.known_cost_usd)}${day.unknown_cost ? ' · '+day.unknown_cost+' unknown' : ''}`, '']);
  lines.push([`Tokens ${compactNumber(day.input_tokens)} in · ${compactNumber(day.output_tokens)} out`, '']);
  tip.replaceChildren();
  const box = svgNode('rect', {rx: 6, ry: 6}); tip.append(box);
  let widest = 0;
  lines.forEach(([text, className, key], index) => {
    const offset = key ? 22 : 10;
    if (key) tip.append(svgNode('line', {x1: 10, x2: 18, y1: 18+index*18-4, y2: 18+index*18-4, class: 'tip-key '+key}));
    const label = svgNode('text', {x: offset, y: 18+index*18, class: className}, text); tip.append(label);
    tip.setAttribute('visibility', 'visible');
    widest = Math.max(widest, offset+label.getComputedTextLength());
  });
  const boxWidth = widest+12; const boxHeight = lines.length*18+10;
  box.setAttribute('width', boxWidth); box.setAttribute('height', boxHeight);
  const left = Math.min(Math.max(4, centre-boxWidth/2), width-boxWidth-4);
  tip.setAttribute('transform', `translate(${left},4)`);
}

function renderChartTable(daily, series) {
  $('#chart-table-head').replaceChildren(...['Day (UTC)', 'Requests', ...series.map(s => s.label), 'Known cost', 'Unknown-cost attempts'].map(text => node('th', text)));
  $('#chart-table-body').replaceChildren(...daily.map(day => {
    const row = node('tr');
    for (const value of [day.day, day.routed_requests, ...series.map(entry => seriesValue(day, entry)), dollars(day.known_cost_usd), day.unknown_cost]) row.append(node('td', String(value)));
    return row;
  }));
}

let chartResize;
window.addEventListener('resize', () => { clearTimeout(chartResize); chartResize = setTimeout(() => { if (usageDaily) renderUsageChart(usageDaily); }, 150); });

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
      for (const rate of source.rate_observations || []) evidence.append(tagged(`${rate.model} / ${rate.dimension} (${rate.window})`, `${rate.remaining} reported at ${rate.observed_at}${rate.stale ? ' · stale' : ' · recent snapshot'}; other traffic may have consumed it`, 'PROVIDER'));
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
