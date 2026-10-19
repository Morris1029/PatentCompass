(function (root) {
  'use strict';
  const insights = root.PatentDecisionInsights || (typeof require === 'function' ? require('./decision-insights.js') : null);
  const examplePrompt = '优化报告标题，并调整技术路线部分的行文';
  const layoutPrompt = '完善布局与路线图';
  const summary = '调整标题 1 处；重写正文 2 段；调整章节顺序。';
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const strategyIds = ['opportunity-risk', 'patent-layout', 'implementation-roadmap'];
  const decisionExample = insights.getExample();
  const strategySections = (data) => copy(insights.reportSections(data));
  const needsLayout = (snapshot) => strategyIds.some((id) => !snapshot?.sections?.some((section) => section.id === id));
  const emptyMaps = () => [
    { id: 'technology-routes', title: '技术路线图', description: '展示技术分支、关联与演进方向。' },
    { id: 'patent-timeline', title: '专利申请趋势图', description: '查看不同年份的申请活动与变化。' },
    { id: 'applicant-landscape', title: '创新主体布局图', description: '比较申请主体与技术领域的布局。' },
    { id: 'geographic-landscape', title: '地域分布图', description: '查看专利申请与技术布局的地域分布。' }
  ].map((map) => ({ ...map, version: null, status: 'empty', items: [] }));
  const snapshotMaps = (snapshot) => snapshot?.maps?.length ? snapshot.maps : snapshot?.map ? [snapshot.map, ...emptyMaps().slice(1)] : emptyMaps();
  const initialSnapshot = {
    title: '项目研究报告',
    order: ['technology', 'scope', ...strategyIds, 'follow-up'],
    sections: [
      { id: 'scope', title: '研究范围与方法', paragraphs: [
        { id: 'scope-1', text: '本报告先确认研究问题、时间边界和检索口径，再组织分析与复核。具体范围以项目研究底稿为准。' }
      ] },
      { id: 'technology', title: '技术路线', paragraphs: [
        { id: 'technology-1', text: '技术路线部分需要整理技术分支，展示每个分支的内容，并说明后续如何分析。' },
        { id: 'technology-2', text: '分析时还需要检查资料，补充相关依据，然后整理到报告中。' }
      ] },
      ...strategySections(decisionExample),
      { id: 'follow-up', title: '后续研究安排', paragraphs: [
        { id: 'follow-up-1', text: '确认范围后，整理可复核的数据来源，再开展技术、主体与地域分析。尚无数据的部分保留待研究状态。' }
      ] }
    ],
    scope: { industry: '', goal: '', need: '', focus: '', start: '', end: '', type: '' },
    data: { status: 'empty', sources: [] },
    maps: emptyMaps(),
    decisionInsights: copy(decisionExample)
  };
  const orderedSections = (snapshot) => [...new Set([...(snapshot?.order || []), ...(snapshot?.sections || []).map((section) => section.id)])]
    .map((id) => snapshot.sections.find((section) => section.id === id)).filter(Boolean);
  const optimizedSnapshot = (base = initialSnapshot) => {
    const snapshot = copy(base);
    snapshot.title = '项目专利导航研究报告：范围、方法与技术路线';
    snapshot.order = orderedSections(snapshot).map((section) => section.id).filter((id) => id !== 'scope');
    snapshot.order.splice(snapshot.order.indexOf('technology'), 0, 'scope');
    snapshot.sections.find((section) => section.id === 'technology').paragraphs = [
      { id: 'technology-1', text: '按“研究问题—技术分支—验证任务”组织技术路线：先说明需要回答的问题，再明确各分支的分析内容与边界。' },
      { id: 'technology-2', text: '每个技术分支均记录所需证据、复核方法和待确认事项。完成数据核查后，再形成判断并关联来源，方便读者回查。' }
    ];
    return snapshot;
  };
  const layoutSnapshot = (base) => {
    const snapshot = copy(base);
    snapshot.decisionInsights = copy(base.decisionInsights || insights.getExample());
    const added = strategySections(snapshot.decisionInsights).filter((section) => !snapshot.sections.some((item) => item.id === section.id));
    snapshot.sections.push(...added);
    const existingOrder = orderedSections(base).map((section) => section.id);
    const insertion = existingOrder.indexOf('follow-up');
    snapshot.order = [...existingOrder];
    snapshot.order.splice(insertion < 0 ? snapshot.order.length : insertion, 0, ...added.map((section) => section.id));
    const additions = { 'opportunity-risk': '机会线索、风险关注与建议行动', 'patent-layout': '布局优先级', 'implementation-roadmap': '阶段行动与推进条件' };
    return { snapshot, summary: '新增' + added.map((section) => '“' + section.title + '”').join('、') + ' ' + added.length + ' 个章节；补充' + added.map((section) => additions[section.id]).join('；') + '。' };
  };
  const differences = (before, after) => {
    if (!before || !after) return [];
    const entries = [];
    if (before.title !== after.title) entries.push({ kind: 'title', sectionId: 'title', label: '报告标题', before: before.title, after: after.title });
    orderedSections(after).forEach((section) => {
      const previous = before.sections.find((item) => item.id === section.id);
      if (!previous) {
        entries.push({ kind: 'section', sectionId: section.id, label: '新增章节：' + section.title,
          before: '此版本尚无该章节。', after: section.paragraphs.map((paragraph) => paragraph.text).join('\n\n') });
        return;
      }
      section.paragraphs.forEach((paragraph) => {
        const oldText = previous?.paragraphs.find((item) => item.id === paragraph.id)?.text || '';
        if (oldText !== paragraph.text) entries.push({ kind: 'paragraph', sectionId: section.id, paragraphId: paragraph.id, label: section.title, before: oldText, after: paragraph.text });
      });
    });
    if (JSON.stringify(before.order) !== JSON.stringify(after.order)) entries.push({
      kind: 'order', sectionId: 'scope', label: '章节顺序',
      before: orderedSections(before).map((section) => section.title).join(' → '),
      after: orderedSections(after).map((section) => section.title).join(' → ')
    });
    snapshotMaps(after).forEach((map) => {
      const previous = snapshotMaps(before).find((item) => item.id === map.id);
      if (previous && JSON.stringify(previous) !== JSON.stringify(map)) entries.push({ kind: 'map', sectionId: 'navigation-map', mapId: map.id,
        label: map.title, before: previous.version ? '图表版本 ' + previous.version : '暂无图表版本', after: map.version ? '图表版本 ' + map.version : '暂无图表版本' });
    });
    return entries;
  };

  const createPreview = ({ now = () => new Date().toISOString(), state } = {}) => {
    const versions = state?.versions?.length ? copy(state.versions) : [{ id: 'preview-v3', number: 3, parentId: null, instruction: '', summary: '报告编辑流程的初始版本。', conversationId: 'preview-conversation', createdAt: '2026-10-18T02:00:00.000Z', snapshot: copy(initialSnapshot) }];
    versions.forEach((version) => { version.snapshot.maps = copy(snapshotMaps(version.snapshot)); delete version.snapshot.map; });
    const messages = copy(state?.messages || []);
    let currentReportId = versions.some((version) => version.id === state?.currentReportId) ? state.currentReportId : versions[0].id;
    const getVersion = (id) => versions.find((version) => version.id === id);
    return {
      get versions() { return copy(versions); },
      get currentReportId() { return currentReportId; },
      get current() { return copy(getVersion(currentReportId)); },
      get messages() { return copy(messages); },
      serialize() { return copy({ versions, currentReportId, messages }); },
      getVersion(id) { const version = getVersion(id); return version ? copy(version) : null; },
      generate(instruction, reference = null, context = {}) {
        const prompt = String(instruction).trim().replace(/[。！!]+$/, '');
        if (![examplePrompt, layoutPrompt].includes(prompt)) return { ok: false, error: '当前仅支持示例指令，请使用“' + examplePrompt + '”或“' + layoutPrompt + '”。' };
        if (reference?.versionId && reference.versionId !== currentReportId) return { ok: false, error: '引用来自历史版本，请先切回该版本再修改。' };
        const parent = getVersion(currentReportId);
        if (prompt === layoutPrompt && !needsLayout(parent.snapshot)) return { ok: false, error: '当前版本已包含机会与风险、专利布局建议和实施路线图，无需重复生成。' };
        const result = prompt === layoutPrompt ? layoutSnapshot(parent.snapshot) : { snapshot: optimizedSnapshot(parent.snapshot), summary };
        const { snapshot } = result;
        if (!differences(parent.snapshot, snapshot).length) return { ok: false, error: '当前版本已包含这组示例调整。可切回 V3 后继续体验。' };
        const number = Math.max(...versions.map((version) => version.number)) + 1;
        const version = { id: 'preview-v' + number, number, parentId: parent.id, instruction: prompt, summary: result.summary,
          conversationId: context.conversationId || 'preview-conversation', createdAt: now(), snapshot, reference: reference ? copy(reference) : null };
        versions.push(version);
        currentReportId = version.id;
        const message = { id: 'preview-message-' + number, text: prompt, reply: '已完成本次示例修改，原版本已保留。',
          reference: reference ? copy(reference) : null, change: { versionId: version.id, number, summary: result.summary } };
        messages.push(message);
        return { ok: true, version: copy(version), message: copy(message) };
      },
      adoptVersion(id) {
        if (!getVersion(id)) return false;
        currentReportId = id;
        return true;
      },
      diff(id) {
        const version = getVersion(id);
        if (!version) return null;
        const baseVersion = getVersion(version.parentId);
        return { version: copy(version), baseVersion: baseVersion ? copy(baseVersion) : null,
          entries: baseVersion ? differences(baseVersion.snapshot, version.snapshot) : [] };
      }
    };
  };

  let renderCount = 0;
  const element = (doc, tag, attributes = {}, children = []) => {
    const node = doc.createElement(tag);
    Object.entries(attributes).forEach(([key, value]) => {
      if (value === false || value == null) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value === true ? '' : value);
    });
    children.forEach((child) => { if (child) node.append(child); });
    return node;
  };
  const selections = new WeakMap();
  const dispose = (container) => { selections.get(container)?.(); selections.delete(container); };
  const attachSelection = (container, documentNode, version, options) => {
    const doc = container.ownerDocument;
    const win = doc.defaultView;
    let popover = null;
    let reference = null;
    let selectedRange = null;
    const close = () => { popover?.remove(); popover = null; reference = null; selectedRange = null; };
    const el = (tag, attributes, children) => element(doc, tag, attributes, children);
    const position = () => {
      if (!popover || !selectedRange) return;
      const rect = selectedRange.getBoundingClientRect?.() || documentNode.getBoundingClientRect();
      const width = Math.min(360, Math.max(240, (win.innerWidth || 1024) - 24));
      const height = popover.getBoundingClientRect().height || 230;
      const left = Math.max(12, Math.min(rect.left, (win.innerWidth || 1024) - width - 12));
      const below = rect.bottom + 10;
      const top = below + height <= (win.innerHeight || 768) - 12 ? below : Math.max(12, rect.top - height - 10);
      popover.style.width = width + 'px';
      popover.style.left = left + 'px';
      popover.style.top = top + 'px';
    };
    const readSelection = () => {
      if (popover?.contains(doc.activeElement)) return;
      const selection = win.getSelection?.();
      if (!selection?.rangeCount || selection.isCollapsed || !selection.toString().trim()) { close(); return; }
      const range = selection.getRangeAt(0);
      if (!documentNode.contains(range.startContainer) || !documentNode.contains(range.endContainer)) { close(); return; }
      const ancestor = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
      if (ancestor?.closest('[data-report-no-select]')) { close(); return; }
      const target = ancestor?.closest('[data-report-selectable]');
      if (!target || !documentNode.contains(target)) { close(); return; }
      const text = selection.toString().trim();
      if (reference?.text === text && reference.sectionId === target.dataset.sectionId) { selectedRange = range.cloneRange(); position(); return; }
      close();
      reference = { versionId: version.id, sectionId: target.dataset.sectionId, kind: target.dataset.kind,
        label: target.dataset.label, text };
      if (target.dataset.paragraphId) reference.paragraphId = target.dataset.paragraphId;
      selectedRange = range.cloneRange();
      const inputId = 'report-selection-instruction-' + renderCount;
      const status = el('p', { class: 'selection-edit-status', role: 'status' });
      const input = el('textarea', { id: inputId, name: 'instruction', rows: '2', placeholder: options.example || options.preview ? examplePrompt : '输入修改要求', required: true });
      const form = el('form', { class: 'selection-edit-form', onsubmit: (event) => {
        event.preventDefault();
        const instruction = input.value.trim();
        if (!instruction) { input.focus(); return; }
        const result = options.onEdit?.(instruction, { ...reference });
        if (result?.ok === false) { status.textContent = result.error || '暂未完成修改。'; position(); }
        else close();
      } }, [el('label', { for: inputId, text: '修改选中文字' }),
        el('p', { class: 'selection-excerpt', text: text.length > 100 ? text.slice(0, 100) + '…' : text }), input,
        options.example || options.preview ? el('button', { type: 'button', class: 'text-button selection-example', text: '填入示例指令', onclick: () => { input.value = examplePrompt; input.focus(); } }) : null,
        el('div', { class: 'selection-actions' }, [el('button', { type: 'button', class: 'text-button', text: '引用到对话', onclick: () => {
          const quoted = { ...reference };
          close();
          options.onQuote?.(quoted, input.value.trim());
        } }), el('button', { type: 'submit', class: 'btn primary', text: '提交修改' })]), status]);
      popover = el('div', { class: 'report-selection-popover', role: 'dialog', 'aria-label': '修改选中文字' }, [
        el('button', { type: 'button', class: 'selection-close icon-button', 'aria-label': '关闭文字修改', text: '×', onclick: close }), form]);
      doc.body.append(popover);
      position();
    };
    const outside = (event) => { if (popover && !popover.contains(event.target)) close(); };
    const keydown = (event) => { if (event.key === 'Escape' && popover) { event.preventDefault(); close(); } };
    const selectionChange = () => { if (!popover?.contains(doc.activeElement)) readSelection(); };
    doc.addEventListener('selectionchange', selectionChange);
    documentNode.addEventListener('mouseup', readSelection);
    documentNode.addEventListener('keyup', readSelection);
    doc.addEventListener('pointerdown', outside);
    doc.addEventListener('keydown', keydown);
    win.addEventListener('resize', position);
    win.addEventListener('scroll', position, true);
    selections.set(container, () => {
      close();
      doc.removeEventListener('selectionchange', selectionChange);
      documentNode.removeEventListener('mouseup', readSelection);
      documentNode.removeEventListener('keyup', readSelection);
      doc.removeEventListener('pointerdown', outside);
      doc.removeEventListener('keydown', keydown);
      win.removeEventListener('resize', position);
      win.removeEventListener('scroll', position, true);
    });
  };
  const dateLabel = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
  };
  const render = (container, options = {}) => {
    dispose(container);
    const doc = container.ownerDocument;
    const el = (tag, attributes, children) => element(doc, tag, attributes, children);
    const versions = options.versions || [];
    const selected = versions.find((version) => version.id === options.selectedReportId) || versions.find((version) => version.id === options.currentReportId) || versions.at(-1);
    const current = versions.find((version) => version.id === options.currentReportId);
    const selectId = 'report-version-' + (++renderCount);
    const button = (text, callback, disabled = false, className = 'btn secondary', attributes = {}) => el('button', { type: 'button', class: className, text, disabled, onclick: callback || (() => {}), ...attributes });
    const details = (label, value) => el('div', {}, [el('dt', { text: label }), el('dd', { text: value })]);
    const select = el('select', { id: selectId, 'aria-label': '选择报告版本', disabled: !versions.length, onchange: (event) => options.onSelect?.(event.target.value) });
    if (!versions.length) select.append(el('option', { value: '', text: '暂无报告版本' }));
    [...versions].reverse().forEach((version) => select.append(el('option', { value: version.id, selected: version.id === selected?.id,
      text: '报告 V' + version.number + (version.id === options.currentReportId ? ' · 当前采用' : '') })));
    const panel = el('section', { class: 'report-version-panel', 'aria-label': '报告版本' }, [
      el('div', { class: 'section-head' }, [el('div', {}, [el('span', { class: 'label', text: '导航报告' }), el('h2', { text: '报告版本' })]),
        el('div', { class: 'actions' }, [
          (options.example || options.preview) && selected && needsLayout(selected.snapshot)
            ? button(layoutPrompt, () => options.onImproveLayout?.(selected.id), selected.id !== options.currentReportId, 'btn secondary',
              { 'data-improve-layout': '', title: selected.id !== options.currentReportId ? '先切回该版本后再完善布局与路线图' : '补充机会与风险、专利布局建议和实施路线图' }) : null,
          button('生成新版本', options.onGenerate, !(options.example || options.preview) || !selected, 'btn primary')])]),
      el('div', { class: 'report-version-controls' }, [el('div', { class: 'field' }, [el('label', { for: selectId, text: '选择报告版本' }), select]),
        el('div', { class: 'actions' }, [button('查看差异', () => options.onDiff?.(selected.id), !selected?.parentId),
          button('切回旧版', () => options.onAdopt?.(selected.id), !selected || selected.id === options.currentReportId)])]),
      el('dl', { class: 'report-version-details' }, [details('正在查看', selected ? '报告 V' + selected.number : '暂无版本'),
        details('当前采用', current ? '报告 V' + current.number : '暂无版本'), details('生成时间', selected ? dateLabel(selected.createdAt) : '—'),
        details('来源对话', selected ? selected.conversationTitle || (options.example || options.preview ? '示例编辑' : '项目对话') : '—')])
    ]);
    container.replaceChildren();
    container.append(panel);
    const mapVisual = (map) => {
      const gallery = doc.defaultView?.PatentCharts || root.PatentCharts;
      const picture = gallery?.createFigure(doc, map.id || 'technology-routes');
      if (picture) return el('div', { class: 'chart-plot chart-illustration' }, [picture]);
      return el('div', { class: 'chart-plot empty' }, [el('div', { class: 'empty-state compact' }, [
        el('span', { class: 'empty-mark', 'aria-hidden': 'true' }), el('h4', { text: '尚未生成' }),
        el('p', { text: map.description || '导入专利数据并完成分析后生成。' })
      ])]);
    };
    const mapsSection = (snapshot) => el('section', { class: 'report-section report-map', id: 'report-section-navigation-map', 'data-report-no-select': '' }, [
      el('div', { class: 'section-head' }, [el('div', {}, [el('h2', { text: '导航地图' }), el('p', { class: 'muted', text: '技术、趋势、主体与地域的可视化示意。当前使用模拟数据，项目分析结果待数据导入后生成。' })])]),
      el('div', { class: 'report-map-grid' }, snapshotMaps(snapshot).map((map) => el('section', { class: 'report-map-card', 'data-map-id': map.id || 'navigation-map' }, [
        el('div', { class: 'section-head' }, [el('h3', { text: map.title }), el('span', { class: 'status-chip', text: map.version ? '图表版本 ' + map.version : '暂无图表版本' })]),
        mapVisual(map),
        button('修改要求', null, true, 'text-button')
      ])))
    ]);
    if (!selected) {
      container.append(el('section', { class: 'report-empty empty-state' }, [el('span', { class: 'empty-mark', 'aria-hidden': 'true' }),
        el('h2', { text: '暂无报告版本' }), el('p', { text: '确认项目研究范围、准备专利数据并完成分析后，报告将在这里形成版本记录。' }),
        el('div', { class: 'actions' }, [button('返回对话', options.onOpenChat), options.onStartExample ? button('体验报告编辑', options.onStartExample, !options.hasProject) : null])]));
      container.append(mapsSection(null));
      return;
    }
    const snapshot = selected.snapshot;
    const selectable = (reference) => ({ 'data-report-selectable': '', 'data-section-id': reference.sectionId, 'data-kind': reference.kind,
      'data-label': reference.label, 'data-paragraph-id': reference.paragraphId });
    const title = el('h1', { text: snapshot.title, ...selectable({ sectionId: 'title', kind: 'title', label: '报告标题' }) });
    const documentNode = el('article', { class: 'report-document', 'aria-label': '报告 V' + selected.number,
      ...selectable({ sectionId: 'document', kind: 'section', label: '报告内容' }) }, [
      el('header', { class: 'report-document-header', id: 'report-section-title' }, [el('span', { class: 'label', text: '报告 V' + selected.number }), title])
    ]);
    orderedSections(snapshot).forEach((section) => {
      const sectionNode = el('section', { class: 'report-section' + (strategyIds.includes(section.id) ? ' report-strategy-section' : ''), id: 'report-section-' + section.id, 'data-report-section': section.id,
        ...selectable({ sectionId: section.id, kind: 'section', label: section.title }) });
      sectionNode.append(el('h2', { text: section.title }));
      section.paragraphs.forEach((paragraph) => {
        const paragraphClass = section.id === 'patent-layout' ? ' report-priority-paragraph' : section.id === 'implementation-roadmap' ? ' report-roadmap-paragraph' : '';
        sectionNode.append(el('p', { class: 'report-paragraph' + paragraphClass, text: paragraph.text, id: 'report-paragraph-' + paragraph.id,
          ...selectable({ sectionId: section.id, paragraphId: paragraph.id, kind: 'paragraph', label: section.title }) }));
      });
      documentNode.append(sectionNode);
    });
    documentNode.append(mapsSection(snapshot));
    documentNode.append(el('p', { class: 'report-snapshot-note', text: '本报告版本保留生成时的研究范围、数据依据与地图快照。当前版本的数据状态：未导入专利数据。' }));
    container.append(documentNode);
    attachSelection(container, documentNode, selected, options);
  };

  const renderDiff = (container, options = {}) => {
    dispose(container);
    const doc = container.ownerDocument;
    const el = (tag, attributes, children) => element(doc, tag, attributes, children);
    const { version, baseVersion } = options;
    container.replaceChildren();
    const returnButton = el('button', { type: 'button', class: 'btn secondary', text: '返回报告', onclick: () => options.onClose?.() });
    if (!version || !baseVersion) {
      container.append(el('div', { class: 'report-diff-heading section-head' }, [el('h2', { text: '版本差异' }), returnButton]),
        el('p', { class: 'muted', text: '此版本没有可对比的上一版。' }));
      return;
    }
    container.append(el('div', { class: 'report-diff-heading section-head' }, [el('div', {}, [
      el('h2', { text: '报告 V' + baseVersion.number + ' → V' + version.number }), el('p', { text: version.summary })]), returnButton]));
    const list = el('div', { class: 'report-diff-list' });
    differences(baseVersion.snapshot, version.snapshot).forEach((entry) => {
      list.append(el('section', { class: 'report-diff-entry' }, [el('div', { class: 'section-head' }, [el('h3', { text: entry.label }),
        el('button', { type: 'button', class: 'text-button', text: '定位到报告', onclick: () => options.onLocate?.(entry.sectionId, entry.paragraphId) })]),
        el('div', { class: 'report-diff-columns' }, [
          el('div', { class: 'report-diff-before' }, [el('small', { text: 'V' + baseVersion.number + ' · 修改前' }), el('p', { text: entry.before })]),
          el('div', { class: 'report-diff-after' }, [el('small', { text: 'V' + version.number + ' · 修改后' }), el('p', { text: entry.after })])
        ])]));
    });
    container.append(list);
  };
  const api = { examplePrompt, layoutPrompt, needsLayout, createPreview, differences, render, renderDiff, dispose };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PatentReports = api;
})(globalThis);
