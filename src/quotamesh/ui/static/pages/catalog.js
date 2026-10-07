function renderCatalog(data) {
  $('#catalog-entries').replaceChildren();
  for (const entry of data.entries) {
    const card = node('article', undefined, 'catalog-entry');
    card.append(node('h3',entry.name), node('p',entry.access_note),node('p',`${entry.configured ? 'Configured' : 'Not configured'} · ${entry.compatibility} · reviewed ${entry.last_verified}`,'fine'));
    const links = node('div',undefined,'actions');
    for (const [title,href] of [['Official docs',entry.docs_url],['Get access',entry.signup_url]]) {
      const link = node('a',title,'text-link'); link.href=href; link.target='_blank'; link.rel='noopener noreferrer'; links.append(link);
    }
    const add=node('a','Configure access →','text-link'); add.href=profileUrl('/access')+'&provider='+encodeURIComponent(entry.preset ? entry.id : 'custom'); links.append(add);
    card.append(links); $('#catalog-entries').append(card);
  }
}
