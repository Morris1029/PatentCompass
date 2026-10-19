const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { create, storageKey } = require('../assets/workspace-state.js');
let JSDOM;
try { ({ JSDOM } = require(process.env.PATENT_JSDOM_PATH || path.join(process.env.TEMP || '', 'patent-navigation-dom-check/node_modules/jsdom'))); } catch { /* Optional local DOM test dependency. */ }

if (!JSDOM) {
  test('workspace DOM regression checks (set PATENT_JSDOM_PATH to an existing jsdom installation)', { skip: 'jsdom is not installed; state tests remain available without dependencies' }, () => {});
} else {
  const prototype = path.resolve(__dirname, '..');
  const html = fs.readFileSync(path.join(prototype, 'index.html'), 'utf8');
  const scripts = ['workspace-state.js', 'chart-gallery.js', 'decision-insights.js', 'report-versions.js', 'scope-parser.js', 'ui.js', 'chart-editor.js'];
  const storage = (initial = {}) => {
    const values = new Map(Object.entries(initial));
    return { values, getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: (key) => values.delete(key), clear: () => values.clear() };
  };
  const open = ({ store = storage(), query = '', mobile = false } = {}) => {
    const dom = new JSDOM(html, { url: 'https://example.test/index.html' + query, runScripts: 'outside-only', pretendToBeVisual: true });
    const { window } = dom;
    Object.defineProperty(window, 'localStorage', { value: store });
    const media = { matches: mobile, addEventListener() {} };
    window.matchMedia = () => media;
    window.HTMLElement.prototype.scrollIntoView = function () {};
    Object.defineProperty(window.HTMLElement.prototype, 'offsetParent', { get() {
      if (this.closest('[hidden]') || (this.classList.contains('sidebar-mobile-toggle') && window.document.body.classList.contains('sidebar-mobile-open'))) return null;
      if (this.closest('.project-conversations') && !this.closest('.project-group.is-open')) return null;
      return this.parentElement;
    } });
    window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new window.Event('close')); };
    window.HTMLFormElement.prototype.requestSubmit = function () { this.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })); };
    const errors = [];
    window.addEventListener('error', (event) => errors.push(event.error));
    scripts.forEach((file) => window.eval(fs.readFileSync(path.join(prototype, 'assets', file), 'utf8')));
    assert.deepEqual(errors, [], 'scripts initialize without runtime errors');
    const document = window.document;
    const submit = (form) => form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
    const click = (selector, scope = document) => { const node = scope.querySelector(selector); assert.ok(node, 'missing ' + selector); node.click(); return node; };
    const send = (text) => { document.querySelector('#chat-message').value = text; submit(document.querySelector('[data-chat-form]')); };
    const createProject = (title) => {
      click('[data-new-project]');
      document.querySelector('#project-name').value = title;
      submit(document.querySelector('.project-dialog form'));
      return JSON.parse(store.getItem(storageKey)).projects.find((item) => item.title === title);
    };
    const selectText = (selector, length = 4) => {
      const target = document.querySelector(selector);
      assert.ok(target, 'missing selectable text ' + selector);
      const range = document.createRange();
      range.setStart(target.firstChild, 0); range.setEnd(target.firstChild, Math.min(length, target.firstChild.textContent.length));
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      target.dispatchEvent(new window.MouseEvent('mouseup', { bubbles: true }));
      return target;
    };
    const startReportExample = (title = '报告项目') => {
      const project = createProject(title);
      click('[data-project-resource="' + project.id + '"][data-view="report"]');
      buttonText(document.querySelector('[data-report-container]'), '体验报告编辑').click();
      return project;
    };
    const editSelected = () => {
      selectText('.report-document h1');
      document.querySelector('.selection-edit-form textarea').value = window.PatentReports.examplePrompt;
      submit(document.querySelector('.selection-edit-form'));
    };
    const state = () => JSON.parse(store.getItem(storageKey));
    const route = () => new URLSearchParams(window.location.hash.slice(11));
    const dispose = () => { assert.deepEqual(errors, [], 'no interaction runtime errors'); window.close(); };
    return { window, document, store, submit, click, send, createProject, selectText, startReportExample, editSelected, state, route, dispose };
  };
  const buttonText = (root, text) => [...root.querySelectorAll('button')].find((node) => node.textContent.trim() === text);

  test('UI: two conversations share the project draft and project resources have no conversation version labels', () => {
    const ui = open();
    const project = ui.createProject('无人机研究');
    ui.send('研究行业：无人机；应用需求：精准降落');
    const original = ui.state().projects[0].conversations[0].id;
    ui.click('[data-new-conversation="' + project.id + '"]');
    assert.equal(ui.document.querySelector('#chat-message').value, '');
    assert.equal(ui.state().projects[0].conversations.length, 1);
    assert.equal(ui.route().get('draft'), '1');
    assert.equal(ui.route().has('conversation'), false);
    assert.equal(ui.state().projects[0].draft.scope.industry, '无人机');
    ui.click('[data-project-resource][data-view="workbench"]');
    assert.equal(ui.route().get('view'), 'workbench');
    ui.click('[data-workbench-tab="scope"]');
    const form = ui.document.querySelector('[data-workbench-scope] [data-scope-form]');
    ui.click('[data-scope-edit]', ui.document.querySelector('[data-workbench-scope]'));
    form.elements.namedItem('need').value = '自主降落';
    ui.submit(form);
    assert.equal(ui.state().projects[0].draft.scope.need, '自主降落');
    ui.click('[data-conversation-link="' + original + '"]');
    assert.equal(ui.document.querySelector('[data-chat-scope] [data-need]').textContent, '自主降落');
    assert.equal(ui.document.querySelectorAll('.project-resource-link').length, 2);
    assert.deepEqual([...ui.document.querySelectorAll('.project-resource-link')].map((node) => node.textContent), ['分析工作台', '导航报告']);
    const children = ui.document.querySelector('.project-conversations');
    assert.equal(children.firstElementChild.className, 'project-resources');
    assert.equal(ui.document.querySelector('.project-new-chat'), null);
    assert.ok(ui.document.querySelector('.project-group-head [data-new-conversation]'));
    assert.equal([...ui.document.querySelectorAll('.project-chat-link')].some((node) => /^V\d/.test(node.textContent)), false);
    ui.dispose();
  });

  test('UI: project switching and real report empty state stay isolated', () => {
    const ui = open();
    const a = ui.createProject('项目 A');
    ui.send('研究行业：无人机');
    const aConversation = ui.state().projects.find((item) => item.id === a.id).conversations[0].id;
    ui.createProject('项目 B');
    ui.send('研究行业：机器人');
    ui.click('[data-conversation-link="' + aConversation + '"]');
    assert.equal(ui.document.querySelector('[data-chat-scope] [data-industry]').textContent, '无人机');
    ui.click('[data-project-resource="' + a.id + '"][data-view="report"]');
    assert.equal(ui.document.querySelector('[data-report-container] select').disabled, true);
    assert.equal(ui.document.querySelector('[data-report-container] select').textContent, '暂无报告版本');
    assert.equal(buttonText(ui.document.querySelector('[data-report-container]'), '生成新版本').disabled, true);
    assert.equal(ui.document.querySelector('.report-document'), null);
    assert.ok(ui.state().projects.every((item) => item.reportVersions.length === 0));
    ui.dispose();
  });

  test('UI: Word attachment selection, removal, attachment-only send and data material listing', () => {
    const ui = open();
    ui.createProject('文档项目');
    const input = ui.document.querySelector('#chat-files');
    const first = new ui.window.File(['private text'], '需求.docx', { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', lastModified: 10 });
    const second = new ui.window.File(['legacy'], '说明.doc', { lastModified: 20 });
    const invalid = new ui.window.File(['pdf'], '不支持.pdf');
    Object.defineProperty(input, 'files', { configurable: true, value: [first, second, invalid] });
    input.dispatchEvent(new ui.window.Event('change'));
    assert.equal(ui.document.querySelectorAll('.attachment-chip').length, 2);
    assert.match(ui.document.querySelector('[data-attachment-status]').textContent, /请选择 Word/);
    ui.click('[aria-label="移除附件：说明.doc"]');
    assert.equal(ui.document.querySelectorAll('.attachment-chip').length, 1);
    ui.send('');
    const message = ui.state().projects[0].conversations[0].messages[0];
    assert.equal(message.text, '');
    assert.deepEqual(Object.keys(message.attachments[0]).sort(), ['id', 'lastModified', 'name', 'size', 'type']);
    assert.equal(message.attachments[0].name, '需求.docx');
    assert.equal(ui.store.getItem(storageKey).includes('private text'), false);
    assert.equal(ui.document.querySelectorAll('.message-attachments .attachment-card').length, 1);
    assert.equal(ui.document.querySelectorAll('.attachment-chip').length, 0);
    ui.click('[data-project-resource][data-view="workbench"]');
    ui.click('[data-workbench-tab="data"]');
    assert.match(ui.document.querySelector('[data-project-materials]').textContent, /需求.docx/);
    assert.equal(ui.state().projects[0].draft.dataStatus, 'empty');
    ui.dispose();
  });

  test('UI: workbench tab keyboard navigation and analysis links stay in one shell', () => {
    const ui = open();
    ui.createProject('页签项目');
    ui.click('[data-project-resource][data-view="workbench"]');
    const scopeTab = ui.document.querySelector('[data-workbench-tab="scope"]');
    scopeTab.click(); scopeTab.focus();
    scopeTab.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    assert.equal(ui.route().get('tab'), 'data');
    assert.equal(ui.document.activeElement.dataset.workbenchTab, 'data');
    ui.click('[data-workbench-tab="analysis"]');
    ui.click('[data-analysis-tab="competition"]');
    assert.equal(ui.route().get('analysis'), 'competition');
    assert.equal(ui.document.querySelector('[data-analysis-panel="competition"]').hidden, false);
    assert.equal(ui.document.querySelector('[data-analysis-panel="technology"]').hidden, true);
    assert.equal(ui.window.location.pathname, '/index.html');
    ui.dispose();
  });

  test('UI: branch subviews use shell route anchors and restore the selected panel after reload', () => {
    const store = storage();
    const ui = open({ store });
    ui.createProject('技术分支项目');
    ui.click('[data-project-resource][data-view="workbench"]');
    ui.click('[data-workbench-tab="analysis"]');
    ui.click('[data-analysis-tab="branches"]');
    assert.equal(ui.document.querySelector('#branches-methods').hidden, false);
    assert.equal(ui.document.querySelector('#branches-elements').hidden, true);
    ui.click('.view-tabs a[data-base-href="landscape.html#elements"]');
    assert.equal(ui.route().get('anchor'), 'branches-elements');
    assert.equal(ui.document.querySelector('#branches-elements').hidden, false);
    assert.equal(ui.document.querySelector('#branches-methods').hidden, true);
    ui.click('.view-tabs a[data-base-href="landscape.html#applications"]');
    assert.equal(ui.route().get('anchor'), 'branches-applications');
    assert.equal(ui.document.querySelector('#branches-applications').hidden, false);
    assert.equal(ui.document.querySelector('#branches-elements').hidden, true);
    const destination = ui.window.location.hash;
    ui.dispose();
    const restored = open({ store, query: destination });
    assert.equal(restored.route().get('analysis'), 'branches');
    assert.equal(restored.document.querySelector('#branches-applications').hidden, false);
    assert.equal(restored.document.querySelector('#branches-methods').hidden, true);
    restored.dispose();
  });

  test('UI: workbench defaults to overview and restores explicit tabs, analysis and anchor routes', () => {
    const store = storage();
    const ui = open({ store });
    const project = ui.createProject('概览入口项目');
    ui.click('[data-project-resource][data-view="workbench"]');
    assert.equal(ui.route().get('tab'), 'overview');
    assert.deepEqual([...ui.document.querySelectorAll('[data-workbench-tab]')].map((node) => node.textContent), ['概览', '态势分析', '布局与路线图', '范围与目标', '数据与证据']);
    assert.equal(ui.document.querySelector('#panel-overview').hidden, false);
    const overviewRoute = ui.window.location.hash;
    ui.click('[data-workbench-tab="layout"]');
    const layoutRoute = ui.window.location.hash;
    ui.click('[data-workbench-tab="analysis"]');
    ui.click('[data-analysis-tab="competition"]');
    const analysisRoute = ui.window.location.hash;
    [overviewRoute, layoutRoute, analysisRoute].forEach((destination, index) => {
      ui.window.history.replaceState(null, '', destination);
      ui.window.dispatchEvent(new ui.window.PopStateEvent('popstate'));
      assert.equal(ui.route().get('tab'), ['overview', 'layout', 'analysis'][index]);
      assert.equal(ui.route().get('project'), project.id);
    });
    ui.click('[data-project-resource][data-view="workbench"]');
    assert.equal(ui.route().get('tab'), 'overview');
    const keyboardTab = ui.document.querySelector('[data-workbench-tab="overview"]');
    keyboardTab.focus();
    keyboardTab.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    assert.equal(ui.route().get('tab'), 'data');
    ui.document.activeElement.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
    assert.equal(ui.route().get('tab'), 'overview');
    ui.document.activeElement.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'End', bubbles: true }));
    assert.equal(ui.route().get('tab'), 'data');
    ui.dispose();
    for (const destination of [layoutRoute, analysisRoute]) {
      const restored = open({ store, query: destination });
      assert.equal(restored.document.querySelector('[data-workbench-panel="' + restored.route().get('tab') + '"]').hidden, false);
      if (restored.route().get('tab') === 'analysis') assert.equal(restored.document.querySelector('[data-analysis-panel="competition"]').hidden, false);
      restored.dispose();
    }
  });

  test('UI: overview and layout examples have complete linked decisions without changing project analysis', () => {
    const ui = open();
    ui.createProject('决策示例项目');
    ui.send('研究行业：机器人');
    ui.click('[data-project-resource][data-view="workbench"]');
    const saved = ui.store.getItem(storageKey);
    const overview = ui.document.querySelector('[data-decision-overview]');
    assert.equal(overview.querySelectorAll('.opportunity-card').length, 3);
    assert.equal(overview.querySelectorAll('.risk-card').length, 3);
    assert.equal(overview.querySelectorAll('.action-plan-card').length, 4);
    assert.match(overview.querySelector('.decision-example-note').textContent, /无人机自主降落.*待验证/);
    overview.querySelectorAll('.risk-card').forEach((card) => {
      assert.ok(card.querySelector('.risk-attention'));
      assert.match(card.querySelector('.insight-status').textContent, /^待/);
      assert.match(card.textContent, /影响对象.*关注原因.*证据缺口.*对应行动/);
    });
    buttonText(overview.querySelector('[data-insight-id="robust-localization"]'), '查看布局建议').click();
    assert.equal(ui.route().get('tab'), 'layout');
    assert.equal(ui.route().get('anchor'), 'layout-priority-robust-localization');
    assert.equal(ui.document.activeElement.id, ui.route().get('anchor'));
    const layout = ui.document.querySelector('[data-decision-layout]');
    assert.equal(layout.querySelectorAll('.layout-priority-card').length, 3);
    assert.equal(layout.querySelectorAll('.roadmap-stage').length, 3);
    assert.deepEqual([...layout.querySelectorAll('.priority-badge')].map((node) => node.textContent), ['P1 · 优先研究与验证', 'P2 · 补证后推进', 'P3 · 跟踪与储备']);
    layout.querySelectorAll('.roadmap-stage').forEach((card) => assert.match(card.textContent, /责任人待分配.*具体行动.*交付物.*推进条件/));
    buttonText(layout.querySelector('[data-priority-id="robust-localization"]'), '查看机会依据').click();
    assert.equal(ui.route().get('analysis'), 'opportunity');
    assert.equal(ui.route().get('anchor'), 'opportunity-detail-robust-localization');
    assert.equal(ui.document.querySelector('[data-analysis-panel="opportunity"]').hidden, false);
    buttonText(ui.document.getElementById(ui.route().get('anchor')), '补充检索').click();
    assert.equal(ui.route().get('anchor'), 'overview-action-supplementary-search');
    const action = ui.document.getElementById(ui.route().get('anchor'));
    buttonText(action, '竞争布局可能集中').click();
    assert.equal(ui.route().get('anchor'), 'overview-risk-competition-concentration');
    buttonText(ui.document.getElementById(ui.route().get('anchor')), '补充检索').click();
    buttonText(ui.document.getElementById(ui.route().get('anchor')), '查看阶段安排').click();
    assert.equal(ui.route().get('anchor'), 'layout-stage-search-review');
    assert.equal(ui.document.querySelector('#panel-layout').hidden, false);
    assert.equal(ui.store.getItem(storageKey), saved);
    assert.equal(ui.state().projects[0].draft.analysisStatus, 'empty');
    assert.deepEqual(ui.state().projects[0].reportVersions, []);
    ui.dispose();
  });

  test('UI: bring action into chat only prefills the current project and creates history after sending', () => {
    const ui = open();
    const a = ui.createProject('行动项目 A');
    ui.send('研究行业：机器人');
    const aConversation = ui.state().projects[0].conversations[0].id;
    const b = ui.createProject('行动项目 B');
    const before = ui.state();
    ui.click('[data-project-resource="' + b.id + '"][data-view="workbench"]');
    buttonText(ui.document.querySelector('#overview-action-supplementary-search'), '带入对话').click();
    const prompt = ui.window.PatentDecisionInsights.getExample().actions[0].prompt;
    assert.equal(ui.route().get('view'), 'chat');
    assert.equal(ui.route().get('project'), b.id);
    assert.equal(ui.document.querySelector('#chat-message').value, prompt);
    assert.equal(ui.document.activeElement.id, 'chat-message');
    assert.equal(ui.document.querySelectorAll('.project-chat-link').length, 1);
    assert.deepEqual(ui.state().projects.find((project) => project.id === b.id), before.projects.find((project) => project.id === b.id));
    assert.equal(decodeURIComponent(ui.window.location.hash).includes(prompt), false);
    ui.send(prompt);
    const bSaved = ui.state().projects.find((project) => project.id === b.id);
    assert.equal(bSaved.conversations.length, 1);
    assert.equal(bSaved.conversations[0].messages[0].text, prompt);
    assert.deepEqual(bSaved.draft.scope, {});
    ui.click('[data-conversation-link="' + aConversation + '"]');
    assert.equal(ui.state().projects.find((project) => project.id === a.id).draft.scope.industry, '机器人');
    assert.doesNotMatch(ui.document.querySelector('[data-chat-history]').textContent, /针对示例线索/);
    ui.dispose();
  });

  test('UI: layout opens the same-project report with shared example snapshots and native text quotation', () => {
    const ui = open();
    const project = ui.createProject('布局报告项目');
    ui.click('[data-project-resource][data-view="workbench"]');
    ui.click('[data-workbench-tab="layout"]');
    buttonText(ui.document.querySelector('[data-decision-layout]'), '前往导航报告').click();
    assert.equal(ui.route().get('project'), project.id);
    assert.equal(ui.route().get('view'), 'report');
    assert.equal(ui.route().get('example'), '1');
    assert.equal(ui.route().get('anchor'), 'report-section-patent-layout');
    const record = JSON.parse(ui.store.getItem('patent-navigation-report-example-' + project.id));
    assert.deepEqual(record.state.versions[0].snapshot.decisionInsights, JSON.parse(JSON.stringify(ui.window.PatentDecisionInsights.getExample())));
    assert.deepEqual([...ui.document.querySelectorAll('.report-strategy-section h2')].map((node) => node.textContent), ['机会与风险', '专利布局建议', '实施路线图']);
    ui.selectText('#report-paragraph-priority-robust-localization', 10);
    buttonText(ui.document.querySelector('.report-selection-popover'), '引用到对话').click();
    assert.equal(ui.route().get('view'), 'chat');
    assert.match(ui.document.querySelector('.composer-quote').textContent, /P1 优先研究与验证/);
    assert.equal(ui.state().projects[0].conversations.length, 0);
    assert.deepEqual(ui.state().projects[0].reportVersions, []);
    ui.dispose();
  });

  test('UI: legacy report layout upgrade creates V5, preserves V3/V4 and links a precise change card', () => {
    const store = storage();
    const first = open({ store });
    const project = first.startReportExample('旧报告项目');
    first.editSelected();
    const key = 'patent-navigation-report-example-' + project.id;
    const oldRecord = JSON.parse(store.getItem(key));
    const strategy = ['opportunity-risk', 'patent-layout', 'implementation-roadmap'];
    oldRecord.state.versions.forEach((version) => {
      version.snapshot.sections = version.snapshot.sections.filter((section) => !strategy.includes(section.id));
      version.snapshot.order = version.snapshot.order.filter((id) => !strategy.includes(id));
      delete version.snapshot.decisionInsights;
    });
    store.setItem(key, JSON.stringify(oldRecord));
    const destination = first.window.location.hash;
    first.dispose();
    const ui = open({ store, query: destination });
    assert.equal(ui.document.querySelectorAll('.report-strategy-section').length, 0);
    assert.deepEqual(JSON.parse(store.getItem(key)), oldRecord, 'opening the old report does not upgrade its content');
    buttonText(ui.document.querySelector('[data-report-container]'), '完善布局与路线图').click();
    const updated = JSON.parse(store.getItem(key)).state;
    assert.equal(updated.currentReportId, 'preview-v5');
    assert.deepEqual(updated.versions.slice(0, 2), oldRecord.state.versions);
    assert.equal(updated.versions[2].parentId, 'preview-v4');
    assert.equal(ui.document.querySelectorAll('.report-strategy-section').length, 3);
    const conversation = ui.state().projects[0].conversations[0];
    assert.equal(updated.versions[2].conversationId, conversation.id);
    ui.click('[data-conversation-link="' + conversation.id + '"]');
    const card = [...ui.document.querySelectorAll('.report-change-card')].at(-1);
    assert.match(card.textContent, /已生成报告 V5.*新增“机会与风险”、“专利布局建议”、“实施路线图” 3 个章节/);
    buttonText(card, '查看改动').click();
    assert.match(ui.document.querySelector('.report-diff-heading').textContent, /V4 → V5/);
    assert.equal(ui.document.querySelectorAll('.report-diff-entry').length, 4);
    const entry = [...ui.document.querySelectorAll('.report-diff-entry')].find((node) => node.textContent.includes('新增章节：专利布局建议'));
    assert.match(entry.textContent, /此版本尚无该章节.*P1 优先研究与验证/s);
    entry.querySelector('button').click();
    assert.equal(ui.route().get('anchor'), 'report-section-patent-layout');
    const select = ui.document.querySelector('[data-report-container] select');
    select.value = 'preview-v4'; select.dispatchEvent(new ui.window.Event('change'));
    assert.equal(ui.document.querySelectorAll('.report-strategy-section').length, 0);
    assert.equal(JSON.parse(store.getItem(key)).state.currentReportId, 'preview-v5');
    assert.equal(buttonText(ui.document.querySelector('[data-report-container]'), '完善布局与路线图').disabled, true);
    assert.deepEqual(ui.state().projects[0].reportVersions, []);
    ui.dispose();
  });

  test('UI: report editing starts from the project, creates V4 and a real conversation change card', () => {
    const ui = open();
    const project = ui.startReportExample();
    assert.equal(ui.route().get('project'), project.id);
    assert.equal(ui.route().get('example'), '1');
    assert.equal(ui.window.location.pathname, '/index.html');
    assert.equal(ui.document.querySelector('[data-preview-view]'), null);
    assert.equal(ui.document.querySelectorAll('.project-group').length, 1);
    assert.equal(ui.state().projects[0].conversations.length, 0);
    ui.editSelected();
    assert.equal(ui.route().get('view'), 'report');
    assert.equal(ui.route().get('report'), 'preview-v4');
    assert.match(ui.document.querySelector('.report-document h1').textContent, /项目专利导航研究报告/);
    const conversation = ui.state().projects[0].conversations[0];
    assert.equal(conversation.messages[0].change.versionId, 'preview-v4');
    const example = JSON.parse(ui.store.getItem('patent-navigation-report-example-' + project.id));
    assert.equal(example.state.versions.find((version) => version.number === 4).conversationId, conversation.id);
    assert.match(ui.document.querySelector('.report-version-details').textContent, new RegExp(conversation.title));
    ui.click('[data-conversation-link="' + conversation.id + '"]');
    assert.match(ui.document.querySelector('.report-change-card').textContent, /已生成报告 V4/);
    buttonText(ui.document.querySelector('.report-change-card'), '查看改动').click();
    assert.equal(ui.document.querySelectorAll('.report-diff-entry').length, 4);
    assert.match(ui.document.querySelector('.report-diff-heading').textContent, /V3 → V4/);
    ui.click('.report-diff-entry button');
    assert.equal(ui.route().get('anchor'), 'report-section-title');
    assert.ok(ui.document.querySelector('.report-document'));
    assert.equal(ui.state().projects[0].reportVersions.length, 0);
    assert.ok(ui.store.getItem('patent-navigation-report-example-' + project.id));
    ui.dispose();
  });

  test('UI: browsing history does not adopt it; rollback V3 then edit produces V5 retaining V4', () => {
    const ui = open();
    ui.startReportExample(); ui.editSelected();
    let select = ui.document.querySelector('[data-report-container] select');
    select.value = 'preview-v3'; select.dispatchEvent(new ui.window.Event('change'));
    assert.match(ui.document.querySelector('.report-version-details').textContent, /当前采用报告 V4/);
    buttonText(ui.document.querySelector('[data-report-container]'), '切回旧版').click();
    assert.match(ui.document.querySelector('.report-version-details').textContent, /当前采用报告 V3/);
    buttonText(ui.document.querySelector('[data-report-container]'), '生成新版本').click();
    assert.equal(ui.route().get('view'), 'chat');
    assert.equal(ui.document.querySelector('.report-edit-drawer'), null);
    assert.match(ui.document.querySelector('[data-composer-reference]').textContent, /引用报告 V3/);
    assert.equal(ui.document.querySelector('#chat-message').value, ui.window.PatentReports.examplePrompt);
    ui.send(ui.window.PatentReports.examplePrompt);
    assert.equal(ui.route().get('report'), 'preview-v5');
    select = ui.document.querySelector('[data-report-container] select');
    assert.deepEqual([...select.options].map((option) => option.value), ['preview-v5', 'preview-v4', 'preview-v3']);
    buttonText(ui.document.querySelector('[data-report-container]'), '查看差异').click();
    assert.match(ui.document.querySelector('.report-diff-heading').textContent, /V3 → V5/);
    ui.dispose();
  });

  test('UI: report quote uses its source version and rejects modification based on stale historical content', () => {
    const ui = open();
    ui.startReportExample(); ui.editSelected();
    const select = ui.document.querySelector('[data-report-container] select');
    select.value = 'preview-v3'; select.dispatchEvent(new ui.window.Event('change'));
    ui.selectText('.report-document h1');
    ui.document.querySelector('.selection-edit-form textarea').value = ui.window.PatentReports.examplePrompt;
    ui.submit(ui.document.querySelector('.selection-edit-form'));
    assert.match(ui.document.querySelector('.selection-edit-status').textContent, /引用来自历史版本/);
    buttonText(ui.document.querySelector('.report-selection-popover'), '引用到对话').click();
    assert.equal(ui.route().get('view'), 'chat');
    assert.match(ui.document.querySelector('[data-composer-reference]').textContent, /引用报告 V3/);
    ui.send(ui.window.PatentReports.examplePrompt);
    assert.equal(ui.route().get('view'), 'chat');
    assert.match(ui.document.querySelector('[data-attachment-status]').textContent, /引用来自历史版本/);
    ui.dispose();
  });

  test('UI: selected current report text is quoted and creates a version linked to that reference', () => {
    const ui = open();
    ui.startReportExample();
    const title = ui.selectText('.report-document h1');
    const quote = title.textContent.slice(0, 4);
    assert.equal(ui.document.querySelector('.selection-excerpt').textContent, quote);
    ui.document.querySelector('.selection-edit-form textarea').value = ui.window.PatentReports.examplePrompt;
    buttonText(ui.document.querySelector('.report-selection-popover'), '引用到对话').click();
    assert.equal(ui.route().get('view'), 'chat');
    assert.equal(ui.document.querySelector('[data-composer-reference] p').textContent, quote);
    assert.equal(ui.document.querySelector('#chat-message').value, ui.window.PatentReports.examplePrompt);
    assert.equal(ui.state().projects[0].conversations.length, 0);
    ui.send(ui.window.PatentReports.examplePrompt);
    assert.equal(ui.route().get('report'), 'preview-v4');
    const conversation = ui.state().projects[0].conversations[0];
    assert.equal(conversation.messages[0].reference.versionId, 'preview-v3');
    assert.equal(conversation.messages[0].reference.text, quote);
    ui.click('[data-conversation-link="' + conversation.id + '"]');
    assert.match(ui.document.querySelector('[data-chat-history] blockquote').textContent, /引用报告 V3/);
    assert.match(ui.document.querySelector('.report-change-card').textContent, /已生成报告 V4/);
    ui.dispose();
  });

  test('UI: project plus opens a draft and only its first valid send creates history after refresh', () => {
    const store = storage();
    const ui = open({ store });
    const project = ui.createProject('草稿项目');
    assert.deepEqual(project.conversations, []);
    assert.equal(ui.document.querySelectorAll('.project-chat-link').length, 0);
    ui.send('第一段研究');
    const saved = store.getItem(storageKey);
    const plus = '[data-new-conversation="' + project.id + '"]';
    ui.click(plus); ui.click(plus);
    assert.equal(store.getItem(storageKey), saved);
    assert.equal(ui.document.querySelectorAll('.project-chat-link').length, 1);
    assert.equal(ui.route().get('draft'), '1');
    assert.equal(ui.route().has('conversation'), false);
    ui.send('  ');
    assert.equal(store.getItem(storageKey), saved);
    const destination = ui.window.location.hash;
    ui.dispose();
    const restored = open({ store, query: destination });
    assert.equal(restored.document.querySelectorAll('.project-chat-link').length, 1);
    assert.equal(restored.document.querySelector('[data-chat-history]').textContent, '');
    assert.equal(restored.document.querySelector('#chat-message').disabled, false);
    restored.send('研究电池回收技术');
    assert.equal(restored.state().projects[0].conversations.length, 2);
    assert.equal(restored.document.querySelectorAll('.project-chat-link').length, 2);
    assert.equal(restored.route().has('draft'), false);
    assert.ok(restored.route().get('conversation'));
    assert.ok(restored.state().projects[0].conversations.every((item) => item.messages.length > 0));
    restored.dispose();
  });

  test('UI: report selection editing appears beside selected text and closes on Escape, outside and navigation', () => {
    const ui = open();
    ui.startReportExample();
    assert.equal(ui.document.querySelector('.report-quote'), null);
    assert.equal(ui.document.querySelector('.report-edit-drawer'), null);
    assert.equal(ui.document.querySelector('.report-selection-popover'), null);
    ui.selectText('.report-paragraph', 8);
    assert.ok(ui.document.querySelector('.report-selection-popover'));
    ui.document.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert.equal(ui.document.querySelector('.report-selection-popover'), null);
    ui.selectText('.report-paragraph', 7);
    ui.document.body.dispatchEvent(new ui.window.Event('pointerdown', { bubbles: true }));
    assert.equal(ui.document.querySelector('.report-selection-popover'), null);
    ui.selectText('.report-paragraph', 6);
    ui.click('[data-project-resource][data-view="workbench"]');
    assert.equal(ui.document.querySelector('.report-selection-popover'), null);
    ui.document.dispatchEvent(new ui.window.Event('selectionchange'));
    assert.equal(ui.document.querySelector('.report-selection-popover'), null);
    ui.click('[data-project-resource][data-view="report"]');
    ui.selectText('.report-paragraph', 5);
    assert.equal(ui.document.querySelectorAll('.report-selection-popover').length, 1);
    ui.click('[aria-label="关闭文字修改"]');
    assert.equal(ui.document.querySelector('.report-selection-popover'), null);
    ui.dispose();
  });

  test('UI: report maps embed four labelled local illustrations without generating project results', () => {
    const ui = open();
    ui.createProject('地图项目');
    ui.click('[data-project-resource][data-view="report"]');
    const expected = ['technology-routes', 'patent-timeline', 'applicant-landscape', 'geographic-landscape'];
    const checkMaps = () => {
      const maps = [...ui.document.querySelectorAll('.report-map-card')];
      assert.deepEqual(maps.map((map) => map.dataset.mapId), expected);
      maps.forEach((map, index) => {
        const image = map.querySelector('.sample-chart img');
        assert.equal(image.getAttribute('src'), 'assets/charts/' + expected[index] + '.svg');
        assert.ok(image.alt.length > 10);
        assert.equal(map.querySelector('.sample-chart-caption').textContent, '示意图 · 非项目分析结果');
        assert.match(map.textContent, /暂无图表版本/);
        assert.equal(map.querySelector('[data-chart-zoom]').disabled, false);
        assert.equal(buttonText(map, '修改要求').disabled, true);
      });
    };
    checkMaps();
    buttonText(ui.document.querySelector('[data-report-container]'), '体验报告编辑').click();
    checkMaps();
    ui.selectText('.report-map-card h3');
    assert.equal(ui.document.querySelector('.report-selection-popover'), null);
    assert.deepEqual(ui.state().projects[0].reportVersions, []);
    assert.equal(ui.state().projects[0].draft.dataStatus, 'empty');
    assert.equal(ui.state().projects[0].draft.analysisStatus, 'empty');
    assert.deepEqual(ui.state().projects[0].draft.scope, {});
    ui.dispose();
  });

  test('UI: workbench illustrations match analysis views and zoom dialog closes with focus restored', () => {
    const ui = open();
    const project = ui.createProject('图表示意项目');
    ui.send('研究行业：机器人');
    ui.click('[data-project-resource][data-view="workbench"]');
    ui.click('[data-workbench-tab="analysis"]');
    const saved = ui.store.getItem(storageKey);
    const locations = [
      ['.plot-tree', 'technology-routes'], ['.plot-trend', 'patent-timeline'],
      ['[data-analysis-panel="competition"]', 'applicant-landscape'],
      ['.geo-origin', 'geographic-landscape']
    ];
    locations.forEach(([selector, id]) => {
      const scope = ui.document.querySelector(selector);
      assert.equal(scope.querySelector('.sample-chart img').getAttribute('src'), 'assets/charts/' + id + '.svg');
      assert.equal(scope.querySelector('.sample-chart-caption').textContent, '示意图 · 非项目分析结果');
    });
    ui.window.PatentCharts.mount(ui.document);
    assert.equal(ui.document.querySelectorAll('[data-view-panel="workbench"] .sample-chart').length, 4);
    assert.equal(ui.document.querySelectorAll('.chart-preview-dialog').length, 1);
    const trigger = ui.document.querySelector('.plot-tree [data-chart-zoom]');
    assert.equal(trigger.tagName, 'BUTTON');
    assert.equal(trigger.type, 'button');
    assert.match(trigger.getAttribute('aria-label'), /放大查看技术路线图/);
    trigger.focus(); trigger.click();
    const dialog = ui.document.querySelector('.chart-preview-dialog');
    const close = dialog.querySelector('[aria-label="关闭图表预览"]');
    assert.equal(dialog.open, true);
    assert.equal(ui.document.activeElement, close);
    assert.equal(dialog.querySelector('h2').textContent, '技术路线图');
    assert.equal(dialog.querySelector('img').getAttribute('src'), 'assets/charts/technology-routes.svg');
    ui.document.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    assert.equal(dialog.open, false);
    assert.equal(ui.document.activeElement, trigger);
    trigger.click(); close.click();
    assert.equal(dialog.open, false);
    assert.equal(ui.document.activeElement, trigger);
    trigger.click(); dialog.click();
    assert.equal(dialog.open, false);
    assert.equal(ui.document.body.classList.contains('chart-preview-open'), false);
    const actual = ui.state().projects.find((item) => item.id === project.id);
    assert.equal(actual.draft.scope.industry, '机器人');
    assert.equal(actual.draft.dataStatus, 'empty');
    assert.equal(actual.draft.analysisStatus, 'empty');
    assert.deepEqual(actual.reportVersions, []);
    assert.equal(ui.store.getItem(storageKey), saved);
    ui.dispose();
  });

  test('UI: report example versions restore after refresh and remain separate between projects', () => {
    const store = storage();
    const ui = open({ store });
    const a = ui.startReportExample('报告 A');
    ui.editSelected();
    const destination = ui.window.location.hash;
    const aMessageId = ui.state().projects.find((project) => project.id === a.id).conversations[0].id;
    const b = ui.startReportExample('报告 B');
    assert.equal(ui.document.querySelector('[data-report-container] select').options.length, 1);
    assert.equal(ui.document.querySelector('[data-report-container] select').value, 'preview-v3');
    assert.equal(ui.state().projects.find((project) => project.id === b.id).conversations.length, 0);
    ui.dispose();
    const restored = open({ store, query: destination });
    assert.equal(restored.route().get('project'), a.id);
    assert.equal(restored.document.querySelector('[data-report-container] select').value, 'preview-v4');
    assert.equal(restored.document.querySelector('[data-report-container] select').options.length, 2);
    restored.click('[data-conversation-link="' + aMessageId + '"]');
    assert.match(restored.document.querySelector('.report-change-card').textContent, /已生成报告 V4/);
    assert.ok(restored.state().projects.every((project) => project.reportVersions.length === 0));
    restored.dispose();
  });

  test('UI: stale report tabs preserve V4 and create V5 from an explicitly adopted V3', () => {
    const store = storage();
    const a = open({ store });
    const project = a.startReportExample('跨标签报告');
    const exampleKey = 'patent-navigation-report-example-' + project.id;
    const b = open({ store, query: a.window.location.hash });
    b.selectText('.report-document h1');
    b.document.querySelector('.selection-edit-form textarea').value = b.window.PatentReports.examplePrompt;
    a.editSelected();
    const afterA = store.getItem(exampleKey);
    const v4 = JSON.parse(afterA).state.versions.find((version) => version.number === 4);
    b.submit(b.document.querySelector('.selection-edit-form'));
    assert.match(b.document.querySelector('.selection-edit-status').textContent, /引用来自历史版本/);
    assert.equal(store.getItem(exampleKey), afterA);
    assert.equal(JSON.parse(afterA).state.currentReportId, 'preview-v4');
    b.window.dispatchEvent(new b.window.StorageEvent('storage', { key: exampleKey }));
    assert.equal(store.getItem(exampleKey), afterA);
    assert.match(b.document.querySelector('.report-version-details').textContent, /当前采用报告 V4/);
    assert.equal(b.document.querySelector('[data-report-container] select').value, 'preview-v3');
    buttonText(b.document.querySelector('[data-report-container]'), '切回旧版').click();
    buttonText(b.document.querySelector('[data-report-container]'), '生成新版本').click();
    b.send(b.window.PatentReports.examplePrompt);
    const reportState = JSON.parse(store.getItem(exampleKey)).state;
    assert.equal(reportState.currentReportId, 'preview-v5');
    assert.deepEqual(reportState.versions.map((version) => version.number), [3, 4, 5]);
    const v5 = reportState.versions.find((version) => version.number === 5);
    assert.equal(v5.parentId, 'preview-v3');
    assert.equal(reportState.versions.find((version) => version.number === 4).conversationId, v4.conversationId);
    assert.notEqual(v5.conversationId, v4.conversationId);
    const savedProject = b.state().projects.find((item) => item.id === project.id);
    assert.equal(savedProject.conversations.length, 2);
    assert.equal(savedProject.conversations.find((conversation) => conversation.id === v4.conversationId).messages[0].change.versionId, 'preview-v4');
    assert.equal(savedProject.conversations.find((conversation) => conversation.id === v5.conversationId).messages[0].change.versionId, 'preview-v5');
    assert.deepEqual(savedProject.reportVersions, []);
    a.dispose(); b.dispose();
  });

  test('UI: hash route refresh and popstate restore conversation and workbench context', () => {
    const store = storage();
    const ui = open({ store });
    const project = ui.createProject('历史项目');
    ui.send('研究无人机行业');
    const firstConversation = ui.state().projects[0].conversations[0].id;
    const firstRoute = ui.window.location.hash;
    ui.click('[data-new-conversation="' + project.id + '"]');
    ui.send('第二次对话');
    const secondRoute = ui.window.location.hash;
    ui.window.history.replaceState(null, '', firstRoute);
    ui.window.dispatchEvent(new ui.window.PopStateEvent('popstate'));
    assert.equal(ui.route().get('conversation'), firstConversation);
    assert.match(ui.document.querySelector('[data-chat-history]').textContent, /研究无人机行业/);
    assert.doesNotMatch(ui.document.querySelector('[data-chat-history]').textContent, /第二次对话/);
    ui.window.history.replaceState(null, '', secondRoute);
    ui.window.dispatchEvent(new ui.window.PopStateEvent('popstate'));
    assert.match(ui.document.querySelector('[data-chat-history]').textContent, /第二次对话/);
    ui.click('[data-project-resource][data-view="workbench"]'); ui.click('[data-workbench-tab="data"]');
    const workbenchRoute = ui.window.location.hash;
    ui.dispose();
    const restored = open({ store, query: workbenchRoute });
    assert.equal(restored.route().get('project'), project.id);
    assert.equal(restored.route().get('view'), 'workbench');
    assert.equal(restored.document.querySelector('[data-workbench-panel="data"]').hidden, false);
    restored.dispose();
  });

  test('UI: a storage event updates the current draft without writing another storage event', () => {
    const store = storage();
    let writes = 0;
    const setItem = store.setItem;
    store.setItem = (key, value) => { writes += 1; setItem(key, value); };
    const ui = open({ store });
    ui.createProject('跨标签项目'); ui.send('研究无人机行业');
    const params = ui.route();
    const other = create({ storage: store, search: '?' + params });
    other.updateScope({ need: '其他标签的调整' });
    const beforeEvent = writes;
    ui.window.dispatchEvent(new ui.window.StorageEvent('storage', { key: storageKey }));
    assert.equal(ui.document.querySelector('[data-chat-scope] [data-need]').textContent, '其他标签的调整');
    assert.equal(writes, beforeEvent);
    ui.dispose();
  });

  test('UI: unknown route IDs disable composition; desktop collapse preference restores', () => {
    const store = storage();
    const ui = open({ store });
    ui.createProject('侧栏项目');
    ui.click('[data-sidebar-collapse]');
    assert.ok(ui.document.body.classList.contains('sidebar-collapsed'));
    assert.equal(ui.document.querySelector('[data-project-list]').inert, true);
    const href = ui.window.location.hash;
    ui.dispose();
    const reloaded = open({ store, query: href });
    assert.ok(reloaded.document.body.classList.contains('sidebar-collapsed'));
    reloaded.dispose();
    const missing = open({ store, query: '#workspace?project=unknown&conversation=unknown' });
    assert.equal(missing.document.querySelector('#chat-message').disabled, true);
    assert.equal(missing.document.querySelector('[data-workspace-notice]').hidden, false);
    assert.equal(missing.route().get('project'), 'unknown');
    assert.equal(missing.route().get('conversation'), 'unknown');
    const missingRoute = missing.window.location.hash;
    missing.dispose();
    const missingReload = open({ store, query: missingRoute });
    assert.equal(missingReload.route().get('project'), 'unknown');
    assert.equal(missingReload.route().get('conversation'), 'unknown');
    assert.equal(missingReload.document.querySelector('#chat-message').disabled, true);
    assert.equal(missingReload.document.querySelector('[data-workspace-notice]').hidden, false);
    missingReload.dispose();
  });

  test('UI: mobile drawer closes by Escape, backdrop and close control with focus restored', () => {
    const ui = open({ mobile: true });
    const toggle = ui.click('.sidebar-mobile-toggle');
    assert.ok(ui.document.body.classList.contains('sidebar-mobile-open'));
    assert.equal(ui.document.querySelector('main').inert, true);
    ui.document.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert.equal(ui.document.body.classList.contains('sidebar-mobile-open'), false);
    assert.equal(ui.document.activeElement, toggle);
    toggle.click(); ui.click('.sidebar-backdrop');
    assert.equal(ui.document.querySelector('main').inert, false);
    toggle.click(); ui.click('[data-sidebar-collapse]');
    assert.equal(ui.document.body.classList.contains('sidebar-mobile-open'), false);
    ui.dispose();
  });

  test('UI: mobile project drawer traps Tab and Shift+Tab between visible controls', () => {
    const ui = open({ mobile: true });
    ui.createProject('键盘项目');
    ui.click('.sidebar-mobile-toggle');
    const sidebar = ui.document.querySelector('.workspace-sidebar');
    const controls = [...sidebar.querySelectorAll('a[href], button:not(:disabled)')].filter((node) => node.offsetParent !== null);
    const first = controls[0]; const last = controls.at(-1);
    last.focus();
    last.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
    assert.equal(ui.document.activeElement, first);
    first.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
    assert.equal(ui.document.activeElement, last);
    ui.dispose();
  });

  test('UI: legacy redirects preserve identity and map anchors including the old preview URL', () => {
    const source = fs.readFileSync(path.join(prototype, 'assets', 'legacy-route.js'), 'utf8');
    for (const [file, expected] of [['report.html', { view: 'report' }], ['review.html', { view: 'workbench', tab: 'analysis', analysis: 'technology' }], ['evidence.html', { view: 'workbench', tab: 'data', anchor: 'data-records' }], ['scope.html', { view: 'workbench', tab: 'scope' }]]) {
      const dom = new JSDOM('<a data-redirect></a>', { url: 'https://example.test/' + file + '?project=p&conversation=c&groups=legacy#records', runScripts: 'outside-only' });
      dom.window.eval(source.replace('location.replace(target);', 'window.__redirect = target;'));
      const result = new URLSearchParams(dom.window.__redirect.split('#workspace?')[1]);
      assert.equal(result.get('project'), 'p'); assert.equal(result.get('conversation'), 'c'); assert.equal(result.get('groups'), 'legacy');
      Object.entries(expected).forEach(([key, value]) => assert.equal(result.get(key), value));
      dom.window.close();
    }
    const dom = new JSDOM('<a data-redirect></a>', { url: 'https://example.test/report-edit-preview.html?project=p&conversation=c', runScripts: 'outside-only' });
    dom.window.eval(source.replace('location.replace(target);', 'window.__redirect = target;'));
    const result = new URLSearchParams(dom.window.__redirect.split('#workspace?')[1]);
    assert.equal(result.get('project'), 'p');
    assert.equal(result.get('conversation'), 'c');
    assert.equal(result.get('view'), 'report');
    assert.equal(result.get('example'), '1');
    assert.equal(result.has('preview'), false);
    dom.window.close();
  });
}
