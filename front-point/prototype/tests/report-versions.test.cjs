const test = require('node:test');
const assert = require('node:assert/strict');
const Reports = require('../assets/report-versions.js');
const Insights = require('../assets/decision-insights.js');
const strategyIds = ['opportunity-risk', 'patent-layout', 'implementation-roadmap'];
const legacyState = () => {
  const state = Reports.createPreview().serialize();
  state.versions[0].snapshot.sections = state.versions[0].snapshot.sections.filter((section) => !strategyIds.includes(section.id));
  state.versions[0].snapshot.order = state.versions[0].snapshot.order.filter((id) => !strategyIds.includes(id));
  delete state.versions[0].snapshot.decisionInsights;
  return state;
};

test('preview starts with only V3 and has no generated change messages', () => {
  const preview = Reports.createPreview();
  assert.equal(preview.current.number, 3);
  assert.equal(preview.versions.length, 1);
  assert.deepEqual(preview.messages, []);
  assert.equal(preview.current.snapshot.data.status, 'empty');
  assert.equal(preview.current.snapshot.maps.length, 4);
  assert.ok(preview.current.snapshot.maps.every((map) => map.version === null && map.status === 'empty' && map.items.length === 0));
});

test('fixed edit creates V4 with one title change, two rewrites and one order change', () => {
  const preview = Reports.createPreview({ now: () => '2026-10-18T03:00:00.000Z' });
  const original = preview.current;
  const reference = { versionId: original.id, sectionId: 'technology', kind: 'section', text: '技术路线' };
  const result = preview.generate(Reports.examplePrompt, reference);
  assert.equal(result.ok, true);
  assert.equal(result.version.number, 4);
  assert.equal(preview.currentReportId, result.version.id);
  assert.equal(result.version.parentId, original.id);
  assert.deepEqual(result.version.reference, reference);
  assert.equal(result.version.createdAt, '2026-10-18T03:00:00.000Z');
  assert.deepEqual(preview.getVersion(original.id), original);
  const diff = preview.diff(result.version.id);
  assert.equal(diff.entries.filter((entry) => entry.kind === 'title').length, 1);
  assert.equal(diff.entries.filter((entry) => entry.kind === 'paragraph').length, 2);
  assert.equal(diff.entries.filter((entry) => entry.kind === 'order').length, 1);
  assert.equal(result.message.change.summary, result.version.summary);
  assert.equal(result.message.change.versionId, result.version.id);
  assert.deepEqual(result.version.snapshot.scope, original.snapshot.scope);
  assert.deepEqual(result.version.snapshot.data, original.snapshot.data);
  assert.deepEqual(result.version.snapshot.maps, original.snapshot.maps);
});

test('reading historical versions does not adopt them; rollback preserves all versions', () => {
  const preview = Reports.createPreview();
  const v4 = preview.generate(Reports.examplePrompt).version;
  preview.getVersion('preview-v3');
  preview.diff(v4.id);
  assert.equal(preview.currentReportId, v4.id);
  assert.equal(preview.adoptVersion('preview-v3'), true);
  assert.equal(preview.current.number, 3);
  assert.equal(preview.versions.length, 2);
  assert.equal(preview.adoptVersion('does-not-exist'), false);
  assert.equal(preview.current.number, 3);
});

test('editing after rollback creates V5 from V3 and retains V4', () => {
  const preview = Reports.createPreview();
  const v4 = preview.generate(Reports.examplePrompt).version;
  preview.adoptVersion('preview-v3');
  const result = preview.generate(Reports.examplePrompt);
  assert.equal(result.version.number, 5);
  assert.equal(result.version.parentId, 'preview-v3');
  assert.deepEqual(preview.getVersion(v4.id), v4);
  assert.equal(preview.diff(result.version.id).baseVersion.number, 3);
  assert.equal(preview.messages.length, 2);
});

test('unsupported commands and repeated edits never fabricate a new report', () => {
  const preview = Reports.createPreview();
  const unsupported = preview.generate('预测无人机市场规模');
  assert.equal(unsupported.ok, false);
  assert.match(unsupported.error, /仅支持示例指令/);
  assert.equal(preview.versions.length, 1);
  assert.equal(preview.messages.length, 0);
  preview.generate(Reports.examplePrompt);
  const unchanged = preview.generate(Reports.examplePrompt);
  assert.equal(unchanged.ok, false);
  assert.equal(preview.versions.length, 2);
  assert.equal(preview.messages.length, 1);
});

test('historical references require explicit adoption of the source version', () => {
  const preview = Reports.createPreview();
  preview.generate(Reports.examplePrompt);
  const reference = { versionId: 'preview-v3', sectionId: 'technology', kind: 'section', text: '技术路线' };
  const result = preview.generate(Reports.examplePrompt, reference);
  assert.equal(result.ok, false);
  assert.match(result.error, /先切回该版本/);
  assert.equal(preview.versions.length, 2);
  preview.adoptVersion('preview-v3');
  assert.equal(preview.generate(Reports.examplePrompt, reference).version.number, 5);
});

test('preview instances and returned snapshots cannot change each other', () => {
  const first = Reports.createPreview();
  const second = Reports.createPreview();
  const current = first.current;
  current.snapshot.title = 'changed outside';
  const versions = first.versions;
  versions.push({ id: 'external' });
  first.generate(Reports.examplePrompt);
  assert.notEqual(first.getVersion('preview-v3').snapshot.title, 'changed outside');
  assert.equal(first.versions.length, 2);
  assert.equal(second.versions.length, 1);
  assert.equal(second.current.number, 3);
  assert.equal(first.getVersion('missing'), null);
  assert.equal(first.diff('missing'), null);
});

test('example sessions hydrate complete version history, current pointer and conversation source', () => {
  const original = Reports.createPreview();
  const result = original.generate(Reports.examplePrompt, null, { conversationId: 'actual-project-chat' });
  original.adoptVersion('preview-v3');
  const saved = original.serialize();
  const restored = Reports.createPreview({ state: saved });
  assert.deepEqual(restored.serialize(), saved);
  assert.equal(restored.getVersion(result.version.id).conversationId, 'actual-project-chat');
  const next = restored.generate(Reports.examplePrompt, null, { conversationId: 'another-chat' });
  assert.equal(next.version.number, 5);
  assert.equal(next.version.parentId, 'preview-v3');
  saved.versions[0].snapshot.title = 'external change';
  assert.notEqual(restored.getVersion('preview-v3').snapshot.title, 'external change');
});

test('legacy single-map snapshots hydrate into multiple map slots and preserve map snapshots', () => {
  const state = Reports.createPreview().serialize();
  const legacyMap = { id: 'legacy-map', title: '旧技术路线图', version: null, status: 'empty', items: [] };
  state.versions[0].snapshot.map = legacyMap;
  delete state.versions[0].snapshot.maps;
  const restored = Reports.createPreview({ state });
  assert.equal(restored.current.snapshot.maps.length, 4);
  assert.deepEqual(restored.current.snapshot.maps[0], legacyMap);
  assert.equal(restored.current.snapshot.map, undefined);
  const edited = restored.generate(Reports.examplePrompt);
  assert.deepEqual(edited.version.snapshot.maps, restored.getVersion('preview-v3').snapshot.maps);
});

test('new sample reports snapshot shared opportunities, risks, priorities and roadmap', () => {
  const preview = Reports.createPreview();
  const original = preview.current.snapshot;
  assert.equal(Reports.needsLayout(original), false);
  assert.deepEqual(original.decisionInsights, Insights.getExample());
  assert.deepEqual(original.sections.filter((section) => strategyIds.includes(section.id)), Insights.reportSections(original.decisionInsights));
  assert.deepEqual(original.order, ['technology', 'scope', ...strategyIds, 'follow-up']);
  preview.generate(Reports.examplePrompt);
  assert.deepEqual(preview.current.snapshot.decisionInsights, original.decisionInsights);
  assert.deepEqual(preview.getVersion('preview-v3').snapshot, original);
  const external = preview.current;
  external.snapshot.decisionInsights.priorities[0].title = '外部修改';
  assert.deepEqual(preview.current.snapshot.decisionInsights, original.decisionInsights);
});

test('old report snapshots hydrate unchanged and gain three chapters only through an explicit new version', () => {
  const state = legacyState();
  const original = JSON.parse(JSON.stringify(state));
  const preview = Reports.createPreview({ state, now: () => '2026-10-18T04:00:00.000Z' });
  assert.deepEqual(preview.serialize(), original);
  assert.equal(Reports.needsLayout(preview.current.snapshot), true);
  const result = preview.generate(Reports.layoutPrompt, null, { conversationId: 'layout-edit-chat' });
  assert.equal(result.ok, true);
  assert.equal(result.version.number, 4);
  assert.equal(result.version.parentId, 'preview-v3');
  assert.equal(result.version.instruction, Reports.layoutPrompt);
  assert.equal(result.version.conversationId, 'layout-edit-chat');
  assert.equal(result.version.createdAt, '2026-10-18T04:00:00.000Z');
  assert.deepEqual(preview.getVersion('preview-v3'), original.versions[0]);
  assert.deepEqual(state, original);
  assert.deepEqual(result.version.snapshot.scope, original.versions[0].snapshot.scope);
  assert.deepEqual(result.version.snapshot.data, original.versions[0].snapshot.data);
  assert.deepEqual(result.version.snapshot.maps, original.versions[0].snapshot.maps);
  assert.deepEqual(result.version.snapshot.decisionInsights, Insights.getExample());
  assert.deepEqual(result.version.snapshot.sections.filter((section) => strategyIds.includes(section.id)), Insights.reportSections(result.version.snapshot.decisionInsights));
  assert.equal(Reports.needsLayout(result.version.snapshot), false);
  const diff = preview.diff(result.version.id);
  assert.deepEqual(diff.entries.filter((entry) => entry.kind === 'section').map((entry) => entry.sectionId), strategyIds);
  assert.equal(diff.entries.filter((entry) => ['title', 'paragraph'].includes(entry.kind)).length, 0);
  assert.match(result.version.summary, /3 个章节/);
  assert.doesNotMatch(result.version.summary, /调整标题|重写正文/);
  assert.equal(result.message.change.summary, result.version.summary);
  assert.equal(result.message.text, Reports.layoutPrompt);
});

test('layout upgrade fills missing chapters without duplicating or replacing a partial snapshot', () => {
  const state = legacyState();
  const source = Insights.getExample();
  source.priorities[0].title = '快照中的原始研究方向';
  state.versions[0].snapshot.decisionInsights = source;
  const existing = Insights.reportSections(source)[0];
  existing.paragraphs[0].text = '已经评审并保留的旧章节文字。';
  state.versions[0].snapshot.sections.push(existing);
  state.versions[0].snapshot.order.splice(2, 0, existing.id);
  const preview = Reports.createPreview({ state });
  const before = preview.current;
  const result = preview.generate(Reports.layoutPrompt);
  assert.equal(result.ok, true);
  assert.deepEqual(result.version.snapshot.sections.find((section) => section.id === existing.id), existing);
  strategyIds.forEach((id) => {
    assert.equal(result.version.snapshot.sections.filter((section) => section.id === id).length, 1);
    assert.equal(result.version.snapshot.order.filter((item) => item === id).length, 1);
  });
  assert.match(result.version.snapshot.sections.find((section) => section.id === 'patent-layout').paragraphs[1].text, /快照中的原始研究方向/);
  assert.match(result.version.summary, /2 个章节/);
  assert.deepEqual(preview.diff(result.version.id).entries.filter((entry) => entry.kind === 'section').map((entry) => entry.sectionId), strategyIds.slice(1));
  assert.deepEqual(preview.getVersion(before.id), before);
  assert.deepEqual(result.version.snapshot.decisionInsights, source);
  const unchanged = preview.generate(Reports.layoutPrompt);
  assert.equal(unchanged.ok, false);
  assert.match(unchanged.error, /无需重复生成/);
  assert.equal(preview.versions.length, 2);
  assert.equal(preview.messages.length, 1);
});

test('legacy V4 upgrades to V5 and historical layout references require explicit adoption', () => {
  const legacy = Reports.createPreview({ state: legacyState() });
  const v4 = legacy.generate(Reports.examplePrompt).version;
  const restored = Reports.createPreview({ state: legacy.serialize() });
  const reference = { versionId: 'preview-v3', sectionId: 'scope', kind: 'section', text: '研究范围与方法' };
  assert.equal(restored.generate(Reports.layoutPrompt, reference).ok, false);
  assert.equal(restored.versions.length, 2);
  const upgraded = restored.generate(Reports.layoutPrompt);
  assert.equal(upgraded.version.number, 5);
  assert.equal(upgraded.version.parentId, v4.id);
  assert.deepEqual(restored.getVersion(v4.id), v4);
  restored.adoptVersion('preview-v3');
  const branch = restored.generate(Reports.layoutPrompt, reference);
  assert.equal(branch.version.number, 6);
  assert.equal(branch.version.parentId, 'preview-v3');
  assert.deepEqual(branch.version.reference, reference);
  assert.deepEqual(restored.getVersion(upgraded.version.id), upgraded.version);
});

test('title and technical edits preserve all new and unknown sections in the document order', () => {
  const state = Reports.createPreview().serialize();
  const extra = { id: 'additional-review', title: '补充复核', paragraphs: [{ id: 'additional-review-1', text: '保留此补充章节及其顺序。' }] };
  state.versions[0].snapshot.sections.push(extra);
  state.versions[0].snapshot.order.splice(2, 0, extra.id);
  const preview = Reports.createPreview({ state });
  const result = preview.generate(Reports.examplePrompt);
  assert.deepEqual(result.version.snapshot.order, ['scope', 'technology', extra.id, ...strategyIds, 'follow-up']);
  assert.deepEqual(result.version.snapshot.sections.find((section) => section.id === extra.id), extra);
  assert.deepEqual(result.version.snapshot.sections.filter((section) => strategyIds.includes(section.id)), Insights.reportSections(state.versions[0].snapshot.decisionInsights));
  const diff = preview.diff(result.version.id);
  assert.equal(diff.entries.filter((entry) => entry.kind === 'section').length, 0);
  assert.equal(diff.entries.filter((entry) => entry.kind === 'paragraph').length, 2);
});

let JSDOM;
try { ({ JSDOM } = require(process.env.PATENT_JSDOM_PATH || require('node:path').join(process.env.TEMP || '', 'patent-navigation-dom-check/node_modules/jsdom'))); } catch { /* Optional local DOM test dependency. */ }
if (JSDOM) {
  test('layout controls require the current report and added chapters retain native selection and diff locations', () => {
    const dom = new JSDOM('<main id="report"></main><aside id="diff"></aside>', { pretendToBeVisual: true });
    const { window } = dom;
    const container = window.document.querySelector('#report');
    const diffContainer = window.document.querySelector('#diff');
    const model = Reports.createPreview({ state: legacyState() });
    let improved;
    Reports.render(container, { versions: model.versions, currentReportId: model.currentReportId, example: true, onImproveLayout: (id) => { improved = id; } });
    const improve = container.querySelector('[data-improve-layout]');
    assert.equal(improve.textContent, Reports.layoutPrompt);
    assert.equal(improve.disabled, false);
    improve.click();
    assert.equal(improved, 'preview-v3');
    const upgraded = model.generate(Reports.layoutPrompt).version;
    Reports.render(container, { versions: model.versions, currentReportId: model.currentReportId, selectedReportId: 'preview-v3', example: true });
    assert.equal(container.querySelector('[data-improve-layout]').disabled, true);
    assert.match(container.querySelector('[data-improve-layout]').title, /先切回/);
    let quoted;
    Reports.render(container, { versions: model.versions, currentReportId: model.currentReportId, example: true, onQuote: (reference) => { quoted = reference; } });
    assert.equal(container.querySelector('[data-improve-layout]'), null);
    assert.equal(container.querySelectorAll('.report-strategy-section').length, 3);
    assert.ok(container.querySelector('p.report-priority-paragraph'));
    assert.ok(container.querySelector('p.report-roadmap-paragraph'));
    const paragraph = container.querySelector('#report-section-patent-layout p[data-paragraph-id^="priority-"]');
    const range = window.document.createRange();
    range.selectNodeContents(paragraph);
    window.getSelection().addRange(range);
    paragraph.dispatchEvent(new window.MouseEvent('mouseup', { bubbles: true }));
    const popover = window.document.querySelector('.report-selection-popover');
    assert.ok(popover);
    [...popover.querySelectorAll('button')].find((button) => button.textContent === '引用到对话').click();
    assert.equal(quoted.sectionId, 'patent-layout');
    assert.equal(quoted.versionId, upgraded.id);
    assert.equal(quoted.text, paragraph.textContent);
    const comparison = model.diff(upgraded.id);
    let located;
    Reports.renderDiff(diffContainer, { ...comparison, onLocate: (sectionId) => { located = sectionId; } });
    assert.equal(diffContainer.querySelectorAll('.report-diff-entry').length, 4);
    diffContainer.querySelector('.report-diff-entry button').click();
    assert.equal(located, 'opportunity-risk');
    assert.match(diffContainer.textContent, /新增章节：实施路线图/);
    Reports.dispose(container);
    dom.window.close();
  });

  test('report rendering uses native text selection for adjacent editing and quotation, with listener cleanup', () => {
    const dom = new JSDOM('<main id="report"></main>', { pretendToBeVisual: true, runScripts: 'outside-only' });
    const { window } = dom;
    window.eval(require('node:fs').readFileSync(require('node:path').join(__dirname, '../assets/chart-gallery.js'), 'utf8'));
    const container = window.document.querySelector('#report');
    const model = Reports.createPreview();
    let edited;
    let quoted;
    const options = { versions: model.versions, currentReportId: model.currentReportId, example: true,
      onEdit: (instruction, reference) => { edited = { instruction, reference }; return { ok: false, error: '仅支持示例指令' }; },
      onQuote: (reference) => { quoted = reference; } };
    Reports.render(container, options);
    assert.equal(container.querySelectorAll('.report-quote').length, 0);
    assert.equal(container.querySelectorAll('.report-map-card').length, 4);
    assert.equal(container.querySelectorAll('.report-map-card .sample-chart img').length, 4);
    const selectParagraph = () => {
      const paragraph = container.querySelector('#report-paragraph-technology-1');
      const range = window.document.createRange();
      range.selectNodeContents(paragraph);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      paragraph.dispatchEvent(new window.MouseEvent('mouseup', { bubbles: true }));
      return paragraph.textContent;
    };
    const selectedText = selectParagraph();
    let popover = window.document.querySelector('.report-selection-popover');
    assert.ok(popover);
    assert.equal(popover.getAttribute('aria-label'), '修改选中文字');
    assert.equal(popover.querySelector('.selection-excerpt').textContent, selectedText);
    const input = popover.querySelector('textarea');
    input.value = '让这段表述更简洁';
    input.focus();
    window.getSelection().removeAllRanges();
    window.document.dispatchEvent(new window.Event('selectionchange'));
    assert.ok(window.document.querySelector('.report-selection-popover'));
    popover.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
    assert.equal(edited.instruction, '让这段表述更简洁');
    assert.equal(edited.reference.text, selectedText);
    assert.equal(edited.reference.paragraphId, 'technology-1');
    assert.equal(popover.querySelector('[role="status"]').textContent, '仅支持示例指令');
    [...popover.querySelectorAll('button')].find((button) => button.textContent === '引用到对话').click();
    assert.equal(quoted.versionId, 'preview-v3');
    assert.equal(quoted.text, selectedText);
    assert.equal(window.document.querySelector('.report-selection-popover'), null);
    selectParagraph();
    window.document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert.equal(window.document.querySelector('.report-selection-popover'), null);
    Reports.dispose(container);
    selectParagraph();
    assert.equal(window.document.querySelector('.report-selection-popover'), null);
    Reports.render(container, { versions: [], hasProject: true, onStartExample() {} });
    assert.equal(container.querySelectorAll('.report-map-card').length, 4);
    assert.equal(container.querySelectorAll('.report-map-card .sample-chart img').length, 4);
    assert.ok([...container.querySelectorAll('.sample-chart-caption')].every((node) => node.textContent === '示意图 · 非项目分析结果'));
    assert.equal(container.querySelector('.report-document'), null);
    assert.ok([...container.querySelectorAll('button')].some((button) => button.textContent === '体验报告编辑' && !button.disabled));
    dom.window.close();
  });
} else {
  test('report text selection DOM check', { skip: 'Set PATENT_JSDOM_PATH to an existing jsdom installation.' }, () => {});
}
