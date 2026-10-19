(function (root) {
  const charts = [
    { id: 'technology-routes', title: '技术路线图', file: 'technology-routes.svg', description: '无人机自主降落的感知定位、规划决策与控制执行技术结构示意。' },
    { id: 'patent-timeline', title: '专利申请趋势图', file: 'patent-timeline.svg', description: '使用模拟数量绘制的年度申请活动趋势，不代表实际检索结果。' },
    { id: 'applicant-landscape', title: '创新主体布局图', file: 'applicant-landscape.svg', description: '使用虚构研究主体和模拟数值绘制的技术布局矩阵。' },
    { id: 'geographic-landscape', title: '地域分布图', file: 'geographic-landscape.svg', description: '使用模拟数量绘制的申请人所在地分布，不代表实际地域排名。' }
  ];
  const get = (id) => charts.find((chart) => chart.id === id);
  const createFigure = (doc, id) => {
    const chart = get(id);
    if (!chart) return null;
    const figure = doc.createElement('figure');
    figure.className = 'sample-chart';
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'chart-image-button';
    button.dataset.chartZoom = chart.id;
    button.setAttribute('aria-label', '放大查看' + chart.title + '（示意图）');
    const picture = doc.createElement('img');
    picture.src = 'assets/charts/' + chart.file;
    picture.alt = chart.description;
    picture.width = 960;
    picture.height = 600;
    picture.loading = 'lazy';
    button.append(picture);
    const zoom = doc.createElement('span');
    zoom.className = 'chart-zoom-hint';
    zoom.setAttribute('aria-hidden', 'true');
    zoom.textContent = '放大查看 ↗';
    button.append(zoom);
    const caption = doc.createElement('figcaption');
    caption.className = 'sample-chart-caption';
    caption.textContent = '示意图 · 非项目分析结果';
    figure.append(button, caption);
    return figure;
  };
  const mounted = new WeakSet();
  const mount = (doc) => {
    if (mounted.has(doc)) return;
    mounted.add(doc);
    const dialog = doc.createElement('dialog');
    dialog.className = 'chart-preview-dialog';
    dialog.setAttribute('aria-labelledby', 'chart-preview-title');
    dialog.innerHTML = '<div class="chart-preview-head"><div><span class="label">可视化示意</span><h2 id="chart-preview-title"></h2></div><button class="chart-preview-close" type="button" aria-label="关闭图表预览">×</button></div><div class="chart-preview-content"><img width="960" height="600"><p>示例主题：无人机自主降落。使用模拟数据展示图表样式，不代表当前项目的分析结果。</p></div>';
    doc.body.append(dialog);
    let opener;
    doc.addEventListener('click', (event) => {
      const trigger = event.target.closest('[data-chart-zoom]');
      if (!trigger) return;
      const chart = get(trigger.dataset.chartZoom);
      if (!chart) return;
      opener = trigger;
      dialog.querySelector('h2').textContent = chart.title;
      const picture = dialog.querySelector('img');
      picture.src = 'assets/charts/' + chart.file;
      picture.alt = chart.description;
      dialog.showModal();
      doc.body.classList.add('chart-preview-open');
      dialog.querySelector('button').focus();
    });
    dialog.querySelector('button').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => {
      doc.body.classList.remove('chart-preview-open');
      if (opener?.isConnected) opener.focus();
    });
    doc.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && dialog.open) { event.preventDefault(); dialog.close(); }
    });
    [
      ['.plot-tree', 'technology-routes'],
      ['.plot-trend', 'patent-timeline'],
      ['[data-analysis-panel="competition"] figure.chart-plot', 'applicant-landscape'],
      ['.geo-origin', 'geographic-landscape']
    ].forEach(([selector, id]) => {
      const stage = doc.querySelector(selector);
      if (!stage) return;
      const picture = createFigure(doc, id);
      if (!picture) return;
      stage.classList.add('has-chart-example');
      stage.prepend(picture);
    });
  };
  const api = { charts, get, createFigure, mount };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else {
    root.PatentCharts = api;
    mount(root.document);
  }
})(globalThis);
