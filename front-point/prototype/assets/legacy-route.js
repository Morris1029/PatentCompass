(function () {
  const file = location.pathname.split('/').pop();
  const routes = {
    'scope.html': ['workbench', 'scope'], 'upload.html': ['chat'],
    'review.html': ['workbench', 'analysis', 'technology'],
    'landscape.html': ['workbench', 'analysis', 'branches'],
    'competition.html': ['workbench', 'analysis', 'competition'],
    'geography.html': ['workbench', 'analysis', 'geography'],
    'opportunity.html': ['workbench', 'analysis', 'opportunity'],
    'evidence.html': ['workbench', 'data'], 'contribution.html': ['workbench', 'data'],
    'citations.html': ['workbench', 'data'], 'report.html': ['report'],
    'report-print.html': ['report'], 'question.html': ['chat'],
    'visual.html': ['report'], 'report-edit-preview.html': ['report']
  };
  const query = new URLSearchParams(location.hash.startsWith('#workspace?') ? location.hash.slice(11) : location.search);
  const [view, tab, analysis] = routes[file] || ['chat'];
  query.set('view', view);
  if (tab) query.set('tab', tab);
  if (analysis) query.set('analysis', analysis);
  if (file === 'report-edit-preview.html') {
    query.delete('preview');
    query.set('example', '1');
  } else if (location.hash && !location.hash.startsWith('#workspace?')) {
    const anchor = location.hash.slice(1);
    if (file === 'evidence.html') query.set('anchor', 'data-' + anchor);
    if (file === 'landscape.html') query.set('anchor', 'branches-' + anchor);
  }
  const target = 'index.html#workspace?' + query;
  document.querySelector('[data-redirect]').href = target;
  location.replace(target);
})();
