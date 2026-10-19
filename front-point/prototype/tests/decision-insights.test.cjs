const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');
const Insights = require('../assets/decision-insights.js');

test('decision fixture has complete opportunity, risk, priority and stage relationships', () => {
  const data = Insights.getExample();
  assert.equal(data.topic, '无人机自主降落');
  assert.equal(data.example, true);
  for (const [key, size] of Object.entries({ opportunities: 3, risks: 3, actions: 4, priorities: 3, stages: 3 })) assert.equal(data[key].length, size);
  const actions = new Set(data.actions.map((item) => item.id));
  const relations = new Set([...data.opportunities, ...data.risks].map((item) => item.id));
  for (const item of [...data.opportunities, ...data.risks, ...data.priorities, ...data.stages]) {
    assert.ok(item.actionIds.every((id) => actions.has(id)));
  }
  for (const item of data.actions) {
    assert.ok(item.relationIds.every((id) => relations.has(id)));
    assert.ok(data.stages.some((stage) => stage.id === item.stageId));
    for (const key of ['materials', 'output', 'completion', 'prompt']) assert.ok(item[key]);
  }
  assert.ok(data.priorities.every((item) => data.opportunities.some((opportunity) => opportunity.id === item.opportunityId)));
  assert.ok(data.stages.every((item) => item.owner === '待分配' && item.actions.length && item.deliverables.length && item.gates.length));
});

test('example is copied so a report snapshot cannot change the shared fixture', () => {
  const snapshot = Insights.getExample();
  snapshot.opportunities[0].title = 'changed';
  snapshot.stages[0].actions.push('changed');
  assert.notEqual(Insights.getExample().opportunities[0].title, 'changed');
  assert.equal(Insights.getExample().stages[0].actions.length, 2);
});

test('example action prompts do not silently replace a project research scope when sent', () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../assets/scope-parser.js'), 'utf8'), context);
  for (const action of Insights.getExample().actions) {
    assert.equal(JSON.stringify(context.PatentScope.extract(action.prompt)), '{}', action.id + ' prompt changed the project scope');
  }
});

test('report sections reuse the supplied snapshot and include opportunities, risks, actions and gates', () => {
  const data = Insights.getExample();
  data.opportunities[0].title = 'snapshot-specific-title';
  const sections = Insights.reportSections(data);
  assert.deepEqual(sections.map((section) => section.id), ['opportunity-risk', 'patent-layout', 'implementation-roadmap']);
  for (const section of sections) {
    assert.equal(new Set(section.paragraphs.map((paragraph) => paragraph.id)).size, section.paragraphs.length);
    assert.match(section.paragraphs[0].text, /示例主题：无人机自主降落/);
    assert.match(section.paragraphs[0].text, /待验证|待证据与技术验证/);
  }
  assert.match(sections[0].paragraphs.map((item) => item.text).join(''), /snapshot-specific-title/);
  for (const action of data.actions) assert.ok(sections[0].paragraphs.some((item) => item.text.includes(action.completion)));
  for (const item of data.priorities) assert.ok(sections[1].paragraphs.some((paragraph) => [item.level, item.topic, item.scenario, item.conditions, item.action].every((value) => paragraph.text.includes(value))));
  for (const stage of data.stages) assert.ok(sections[2].paragraphs.some((paragraph) => [...stage.actions, ...stage.deliverables, ...stage.gates, stage.owner].every((value) => paragraph.text.includes(value))));
});

let JSDOM;
try { ({ JSDOM } = require(process.env.PATENT_JSDOM_PATH || path.join(process.env.TEMP || '', 'patent-navigation-dom-check/node_modules/jsdom'))); } catch { /* Optional local DOM test dependency. */ }

if (!JSDOM) {
  test('decision DOM checks', { skip: 'jsdom is not installed' }, () => {});
} else {
  const setup = () => {
    const dom = new JSDOM('<main><div id="overview"></div><div id="layout"></div><div id="opportunities"></div></main>');
    const doc = dom.window.document;
    return { dom, doc, overview: doc.querySelector('#overview'), layout: doc.querySelector('#layout'), opportunities: doc.querySelector('#opportunities') };
  };
  test('overview renders three opportunities, three risks and four complete actions with sample labels', () => {
    const { dom, overview } = setup();
    Insights.renderOverview(overview);
    assert.equal(overview.querySelectorAll('.opportunity-card').length, 3);
    assert.equal(overview.querySelectorAll('.risk-card').length, 3);
    assert.equal(overview.querySelectorAll('.action-plan-card').length, 4);
    assert.match(overview.querySelector('.decision-example-note').textContent, /示例主题：无人机自主降落.*待验证/);
    overview.querySelectorAll('.risk-card').forEach((card) => {
      assert.ok(card.querySelector('.risk-attention'));
      assert.ok(card.querySelector('.insight-status'));
    });
    dom.window.close();
  });
  test('action buttons only pass a concrete prompt to the host', () => {
    const { dom, overview } = setup();
    const prompts = [], routes = [];
    Insights.renderOverview(overview, { onPrompt: (text) => prompts.push(text), onNavigate: (target) => routes.push(target) });
    const before = Insights.getExample();
    [...overview.querySelectorAll('button')].find((button) => button.textContent === '带入对话').click();
    assert.deepEqual(prompts, [before.actions[0].prompt]);
    assert.deepEqual(routes, []);
    assert.deepEqual(Insights.getExample(), before);
    dom.window.close();
  });
  test('all crosslinks point to shared opportunity, risk, action, priority and stage anchors', () => {
    const { dom, doc, overview, layout, opportunities } = setup();
    const routes = [], options = { onNavigate: (target) => routes.push(target) };
    Insights.renderOverview(overview, options);
    Insights.renderLayout(layout, options);
    Insights.renderOpportunities(opportunities, options);
    doc.querySelectorAll('button').forEach((button) => button.click());
    assert.ok(routes.length > 30);
    routes.filter((target) => target.anchor).forEach((target) => {
      assert.ok(doc.getElementById(target.anchor), 'missing navigation anchor ' + target.anchor);
      assert.equal(target.view, 'workbench');
    });
    const ids = [...doc.querySelectorAll('[id]')].map((item) => item.id);
    assert.equal(new Set(ids).size, ids.length);
    assert.ok(routes.some((target) => target.analysis === 'opportunity'));
    assert.ok(routes.some((target) => target.tab === 'layout'));
    dom.window.close();
  });
  test('layout separates priority from validation state and includes three stage gates', () => {
    const { dom, layout } = setup();
    let opens = 0;
    Insights.renderLayout(layout, { onOpenReport: () => opens++ });
    const priorities = [...layout.querySelectorAll('.layout-priority-card')];
    assert.equal(priorities.length, 3);
    assert.deepEqual(priorities.map((card) => card.querySelector('.priority-badge').textContent.slice(0, 2)), ['P1', 'P2', 'P3']);
    assert.ok(priorities.every((card) => card.querySelector('.insight-status')));
    assert.equal(layout.querySelectorAll('.roadmap-stage').length, 3);
    layout.querySelectorAll('.roadmap-stage').forEach((card) => {
      assert.match(card.textContent, /责任人待分配/);
      assert.match(card.textContent, /交付物/);
      assert.match(card.textContent, /推进条件/);
    });
    [...layout.querySelectorAll('button')].find((button) => button.textContent === '前往导航报告').click();
    assert.equal(opens, 1);
    dom.window.close();
  });
  test('renderers can remount without duplicating content or event responses', () => {
    const { dom, overview } = setup();
    const prompts = [];
    Insights.renderOverview(overview, { onPrompt: (value) => prompts.push(value) });
    Insights.renderOverview(overview, { onPrompt: (value) => prompts.push(value) });
    assert.equal(overview.querySelectorAll('.action-plan-card').length, 4);
    [...overview.querySelectorAll('button')].find((button) => button.textContent === '带入对话').click();
    assert.equal(prompts.length, 1);
    dom.window.close();
  });
}
