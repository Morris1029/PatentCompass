const test = require('node:test');
const assert = require('node:assert/strict');
const { create, storageKey, previousKey, legacyKey, scopeKeys, titleFor } = require('../assets/workspace-state.js');
const memoryStorage = (initial = {}) => {
  const values = new Map(Object.entries(initial));
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)) };
};
const queryFor = (project, conversation) => '?' + new URLSearchParams({ project, conversation });
const selected = (storage, project, conversation) => create({ storage, search: queryFor(project, conversation) });
const scope = { industry: '无人机', goal: 'rd', need: '降落技术', focus: '军用除外', start: '2018', end: '2025', type: 'invention' };
const previousProject = () => ({ id: 'old-project', title: '旧项目', lastConversationId: 'recent', lastVisitedAt: 123,
  conversations: [
    { id: 'first', title: '第一轮', version: 1, scope: { industry: '机器人', focus: '仅民用', start: '2000' }, messages: [{ text: '第一轮消息' }], reportVersions: [] },
    { id: 'recent', title: '最近研究', version: 2, scope, messages: [{ text: '最近消息', reply: '已整理' }], reportVersions: [] }
  ] });

test('v2 migration takes the whole recent scope and preserves original data and message history', () => {
  const original = JSON.stringify({ version: 2, projects: [previousProject()] });
  const storage = memoryStorage({ [previousKey]: original });
  const workspace = selected(storage, 'old-project', 'recent');
  assert.deepEqual(workspace.draft.scope, scope);
  assert.deepEqual(workspace.conversation.messages, [{ text: '最近消息', reply: '已整理' }]);
  assert.equal(workspace.conversation.scope, undefined);
  assert.equal(workspace.conversation.version, undefined);
  assert.deepEqual(workspace.project.reportVersions, []);
  assert.equal(workspace.project.currentReportId, null);
  assert.equal(storage.getItem(previousKey), original);
  assert.equal(JSON.parse(storage.getItem(storageKey)).version, 3);
  workspace.updateScope({ need: '新需求' });
  const again = selected(storage, 'old-project', 'first');
  assert.equal(again.projects.length, 1);
  assert.equal(again.project.conversations.length, 2);
  assert.equal(again.draft.scope.need, '新需求');
  assert.deepEqual(again.conversation.messages, [{ text: '第一轮消息' }]);
  assert.equal(storage.getItem(previousKey), original);
});

test('migration does not combine scope fields and falls back to first conversation for invalid recent ID', () => {
  const project = previousProject();
  project.conversations[1].scope = { industry: '电池' };
  const storage = memoryStorage({ [previousKey]: JSON.stringify({ version: 2, projects: [project] }) });
  assert.deepEqual(selected(storage, project.id, 'recent').draft.scope, { industry: '电池' });
  project.lastConversationId = 'missing';
  const fallback = create({ storage: memoryStorage({ [previousKey]: JSON.stringify({ version: 2, projects: [project] }) }), search: '?project=old-project' });
  assert.equal(fallback.conversation.id, 'first');
  assert.deepEqual(fallback.draft.scope, project.conversations[0].scope);
});

test('v1 migration is idempotent, keeps original storage and supports research links', () => {
  const legacy = [{ id: 'old-research', title: '无人机研究', scope, messages: [{ text: '旧消息' }] }];
  const original = JSON.stringify(legacy);
  const storage = memoryStorage({ [legacyKey]: original });
  const workspace = create({ storage, search: '?research=old-research' });
  assert.equal(workspace.project.id, 'project-old-research');
  assert.deepEqual(workspace.draft.scope, scope);
  assert.deepEqual(workspace.conversation.messages, legacy[0].messages);
  workspace.updateScope({ start: '2020' });
  const again = create({ storage, search: '?research=old-research' });
  assert.equal(again.projects.length, 1);
  assert.equal(again.project.conversations.length, 1);
  assert.equal(again.draft.scope.start, '2020');
  assert.equal(storage.getItem(legacyKey), original);
});

test('a project starts empty and the first message names its conversation once', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  const conversation = workspace.createProject('  无人机项目  ');
  assert.equal(workspace.project.title, '无人机项目');
  assert.equal(conversation.title, '新对话');
  assert.deepEqual(conversation.messages, []);
  assert.equal(conversation.pending, true);
  assert.deepEqual(workspace.project.conversations, []);
  assert.deepEqual(JSON.parse(storage.getItem(storageKey)).projects[0].conversations, []);
  assert.equal(conversation.version, undefined);
  assert.deepEqual(workspace.project.reportVersions, []);
  assert.equal(workspace.draft.dataStatus, 'empty');
  assert.equal(workspace.draft.analysisStatus, 'empty');
  workspace.appendMessage('我想研究无人机行业', '已整理', { industry: '无人机' });
  assert.equal(workspace.conversation.id, conversation.id);
  assert.equal(workspace.conversation.pending, undefined);
  assert.equal(workspace.project.conversations.length, 1);
  workspace.appendMessage('补充降落技术', '已更新', { need: '降落技术' });
  assert.equal(workspace.conversation.title, '无人机专利导航');
  assert.deepEqual(workspace.conversation.messages.map((message) => message.text), ['我想研究无人机行业', '补充降落技术']);
  assert.equal(titleFor('  一个\n新研究  ', ''), ' 一个 新研究 ');
});

test('same-project conversations share one draft but keep independent messages', () => {
  const workspace = create({ storage: memoryStorage() });
  const first = workspace.createProject('项目 A', { scope, messages: [{ text: '首轮消息' }] });
  const projectId = workspace.project.id;
  const second = workspace.createConversation(projectId);
  assert.deepEqual(workspace.draft.scope, scope);
  assert.deepEqual(second.messages, []);
  assert.equal(second.scope, undefined);
  workspace.updateScope({ start: '2020', focus: '仅民用' });
  workspace.appendMessage('次轮消息', '已更新');
  workspace.select(projectId, first.id);
  assert.equal(workspace.draft.scope.start, '2020');
  assert.equal(workspace.draft.scope.focus, '仅民用');
  assert.deepEqual(workspace.conversation.messages, [{ text: '首轮消息' }]);
  workspace.appendMessage('补充目标', '已更新', { goal: 'direction' });
  workspace.select(projectId, second.id);
  assert.equal(workspace.draft.scope.goal, 'direction');
  assert.deepEqual(workspace.conversation.messages.map((item) => item.text), ['次轮消息']);
  assert.deepEqual(workspace.project.reportVersions, []);
});

test('different projects keep draft, messages and reports separate', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  const a = workspace.createProject('项目 A', { scope: { industry: '无人机' } });
  const projectA = workspace.project.id;
  workspace.appendMessage('消息 A', '回复 A', { need: '降落' });
  const b = workspace.createProject('项目 B', { scope: { industry: '机器人' } });
  const projectB = workspace.project.id;
  workspace.appendMessage('消息 B', '回复 B', { need: '抓取' });
  const restoredA = selected(storage, projectA, a.id);
  const restoredB = selected(storage, projectB, b.id);
  assert.equal(restoredA.draft.scope.need, '降落');
  assert.equal(restoredB.draft.scope.need, '抓取');
  assert.deepEqual(restoredA.conversation.messages.map((item) => item.text), ['消息 A']);
  assert.deepEqual(restoredB.conversation.messages.map((item) => item.text), ['消息 B']);
  assert.deepEqual(restoredA.project.reportVersions, []);
  assert.deepEqual(restoredB.project.reportVersions, []);
});

test('attachment-only messages save Word metadata and discard file content or arbitrary properties', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  workspace.createProject('资料项目', { scope });
  const file = { id: 'file-id', name: '研究需求.docx', size: 1024, type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', lastModified: 1234, content: 'PRIVATE CONTENT', localPath: 'C:/private/file.docx' };
  workspace.appendMessage('', '已添加附件', {}, [file]);
  assert.equal(workspace.conversation.title, file.name);
  assert.deepEqual(workspace.conversation.messages[0].attachments, [{ id: file.id, name: file.name, size: file.size, type: file.type, lastModified: file.lastModified }]);
  assert.equal(storage.getItem(storageKey).includes('PRIVATE CONTENT'), false);
  assert.equal(storage.getItem(storageKey).includes('C:/private'), false);
  assert.equal(workspace.draft.dataStatus, 'empty');
  assert.equal(workspace.appendMessage(' ', '空消息', {}, [{ name: '不支持.pdf' }]), null);
  assert.equal(workspace.conversation.messages.length, 1);
});

test('v1 and v2 summary links stay read only until the first new local message', () => {
  const old = [{ id: 'linked-old', title: '旧链接项目', scope: { industry: '电池', start: '2015', end: '2020' } }];
  const v2 = { version: 2, projects: [previousProject()] };
  for (const [groups, selection, industry] of [[old, { research: 'linked-old' }, '电池'], [v2, { project: 'old-project', conversation: 'recent' }, '无人机']]) {
    const workspace = create({ storage: memoryStorage(), search: '?' + new URLSearchParams({ ...selection, groups: JSON.stringify(groups) }) });
    assert.equal(workspace.missingSelection, false);
    assert.equal(workspace.project.summaryOnly, true);
    assert.equal(workspace.conversation.summaryOnly, true);
    assert.equal(workspace.draft.scope.industry, industry);
    assert.deepEqual(workspace.conversation.messages, []);
    assert.equal(workspace.appendMessage('摘要不能修改', '回复'), null);
    assert.equal(workspace.updateScope({ industry: '改动' }), null);
    const oldConversationId = workspace.conversation.id;
    workspace.createConversation(workspace.project.id);
    assert.equal(workspace.project.summaryOnly, true);
    assert.equal(workspace.conversation.pending, true);
    assert.equal(workspace.updateScope({ industry: '不得提前修改' }), null);
    assert.equal(workspace.draft.scope.industry, industry);
    assert.equal(workspace.conversation.messages.length, 0);
    workspace.appendMessage('新的本地消息', '回复', { need: '新需求' });
    assert.notEqual(workspace.project.summaryOnly, true);
    assert.equal(workspace.conversation.messages.length, 1);
    workspace.select(workspace.project.id, oldConversationId);
    assert.equal(workspace.updateScope({ need: '摘要不可修改' }), null);
    assert.equal(workspace.draft.scope.need, '新需求');
  }
});

test('snapshot shares project hierarchy and limited scope, never private fields, attachments or reports', () => {
  const workspace = create({ storage: memoryStorage() });
  workspace.createProject('项目标题', { scope });
  workspace.appendMessage('PRIVATE MESSAGE', 'PRIVATE REPLY', {}, [{ name: 'PRIVATE FILE.doc', size: 2 }]);
  const data = JSON.parse(workspace.snapshot());
  assert.equal(data.version, 3);
  assert.deepEqual(Object.keys(data.projects[0].conversations[0]).sort(), ['id', 'title']);
  assert.deepEqual(data.projects[0].draft.scope, { industry: '无人机', start: '2018', end: '2025' });
  for (const field of scopeKeys.filter((key) => !['industry', 'start', 'end'].includes(key))) assert.equal(Object.hasOwn(data.projects[0].draft.scope, field), false);
  for (const privateText of ['PRIVATE MESSAGE', 'PRIVATE REPLY', 'PRIVATE FILE', 'reportVersions']) assert.equal(workspace.snapshot().includes(privateText), false);
  const url = new URL(workspace.link('report.html', workspace.project.id, workspace.conversation.id, 'versions'), 'https://example.test/');
  assert.equal(url.pathname, '/report.html');
  assert.equal(url.hash, '#versions');
  const linked = create({ search: url.search });
  assert.equal(linked.conversation.title, workspace.conversation.title);
  assert.equal(linked.project.summaryOnly, true);
  assert.equal(linked.conversation.summaryOnly, true);
  assert.deepEqual(linked.project.reportVersions, []);
});

test('full local draft and messages take priority over stale shared summaries', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  workspace.createProject('本地项目', { scope, messages: [{ text: '完整消息' }] });
  const projectId = workspace.project.id;
  const conversationId = workspace.conversation.id;
  const shared = JSON.parse(workspace.snapshot());
  shared.projects[0].title = '旧摘要标题';
  shared.projects[0].draft.scope.industry = '旧摘要行业';
  workspace.restore('?' + new URLSearchParams({ project: projectId, conversation: conversationId, groups: JSON.stringify(shared) }));
  assert.equal(workspace.project.title, '本地项目');
  assert.deepEqual(workspace.draft.scope, scope);
  assert.deepEqual(workspace.conversation.messages, [{ text: '完整消息' }]);
  assert.notEqual(workspace.project.summaryOnly, true);
});

test('unknown IDs report missing selection without overwriting existing projects', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  workspace.createProject('已有项目');
  const projectId = workspace.project.id;
  for (const search of [queryFor('missing-project', 'missing-conversation'), queryFor(projectId, 'missing-conversation')]) {
    workspace.restore(search);
    assert.equal(workspace.missingSelection, true);
    assert.equal(workspace.conversation, undefined);
    assert.equal(workspace.projects.length, 1);
    assert.equal(workspace.updateScope({ industry: '不得覆盖' }), null);
  }
});

test('restore supports history, recent conversation fallback and blank home', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  const first = workspace.createProject('项目', { scope });
  workspace.appendMessage('第一条消息', '已整理');
  const projectId = workspace.project.id;
  const second = workspace.createConversation(projectId);
  workspace.appendMessage('第二条消息', '已整理');
  workspace.restore(queryFor(projectId, first.id));
  assert.equal(workspace.conversation.id, first.id);
  workspace.restore(queryFor(projectId, second.id));
  assert.equal(workspace.conversation.id, second.id);
  const reloaded = create({ storage, search: '?' + new URLSearchParams({ project: projectId }) });
  assert.equal(reloaded.conversation.id, second.id);
  reloaded.restore('');
  assert.equal(reloaded.project, undefined);
  assert.equal(reloaded.conversation, undefined);
  assert.equal(reloaded.missingSelection, false);
  assert.equal(reloaded.projects.length, 1);
});

test('two stale tabs creating projects and conversations preserve all records', () => {
  const storage = memoryStorage();
  const a = create({ storage });
  const b = create({ storage });
  const first = a.createProject('项目 A');
  a.appendMessage('原对话', '回复');
  const projectA = a.project.id;
  b.createProject('项目 B');
  const c = selected(storage, projectA, first.id);
  const second = a.createConversation(projectA);
  const third = c.createConversation(projectA);
  a.appendMessage('第二个对话', '回复');
  c.appendMessage('第三个对话', '回复');
  const restored = selected(storage, projectA, third.id);
  assert.deepEqual(restored.projects.map((item) => item.title).sort(), ['项目 A', '项目 B']);
  assert.equal(restored.project.conversations.length, 3);
  assert.equal(new Set(restored.project.conversations.map((item) => item.id)).size, 3);
  assert.equal(restored.project.conversations.some((item) => item.id === second.id), true);
  assert.equal(restored.project.lastConversationId, third.id);
});

test('interleaved saves merge shared draft fields and messages across conversations', () => {
  const storage = memoryStorage();
  const a = create({ storage });
  const first = a.createProject('项目', { scope: { industry: '无人机' } });
  a.appendMessage('首条消息', '已整理');
  const projectId = a.project.id;
  const second = a.createConversation(projectId);
  a.appendMessage('次轮首条消息', '已整理');
  a.select(projectId, first.id);
  const b = selected(storage, projectId, second.id);
  a.updateScope({ focus: '仅民用' });
  b.updateScope({ need: '精准降落' });
  a.appendMessage('消息 A', '回复 A', { start: '2020' });
  b.appendMessage('消息 B', '回复 B', { end: '2025' });
  const restored = selected(storage, projectId, first.id);
  assert.deepEqual(restored.draft.scope, { industry: '无人机', focus: '仅民用', need: '精准降落', start: '2020', end: '2025' });
  assert.deepEqual(restored.conversation.messages.map(({ text, reply }) => ({ text, reply })), [{ text: '首条消息', reply: '已整理' }, { text: '消息 A', reply: '回复 A' }]);
  restored.select(projectId, second.id);
  assert.deepEqual(restored.conversation.messages.map(({ text, reply }) => ({ text, reply })), [{ text: '次轮首条消息', reply: '已整理' }, { text: '消息 B', reply: '回复 B' }]);
});

test('interleaved saves retain messages in the same conversation', () => {
  const storage = memoryStorage();
  const a = create({ storage });
  a.createProject('项目');
  const b = selected(storage, a.project.id, a.conversation.id);
  a.appendMessage('A', '回复 A');
  b.appendMessage('B', '回复 B');
  a.appendMessage('C', '回复 C');
  assert.deepEqual(selected(storage, a.project.id, a.conversation.id).conversation.messages.map((item) => item.text), ['A', 'B', 'C']);
});

test('malformed summaries and unavailable storage keep local in-memory editing usable', () => {
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const workspace = create({ storage: blocked });
  workspace.createProject('项目');
  workspace.restore('?' + new URLSearchParams({ project: workspace.project.id, draft: '1', groups: '{bad-json' }));
  assert.equal(workspace.missingSelection, false);
  workspace.appendMessage('仍可工作', '收到');
  assert.equal(workspace.conversation.messages.length, 1);
});

test('summary visit metadata crosses stores without replacing shared draft or messages', () => {
  const homeStorage = memoryStorage();
  const home = create({ storage: homeStorage });
  const first = home.createProject('项目 A', { scope, messages: [{ text: '完整对话' }] });
  const projectA = home.project.id;
  home.createConversation(projectA);
  home.updateScope({ industry: '机器人' });
  const other = home.createProject('项目 B');
  const projectB = home.project.id;
  const report = create({ storage: memoryStorage(), search: '?' + new URLSearchParams({ project: projectA, conversation: first.id, groups: home.snapshot() }) });
  report.select(projectB, other.id);
  home.restore('?' + new URLSearchParams({ project: projectB, conversation: other.id, groups: report.snapshot() }));
  assert.equal(home.projects.find((item) => item.id === projectA).lastConversationId, first.id);
  home.createConversation(projectA);
  assert.equal(home.draft.scope.industry, '机器人');
  assert.equal(home.draft.scope.need, '降落技术');
  home.select(projectA, first.id);
  assert.deepEqual(home.conversation.messages, [{ text: '完整对话' }]);
});

test('stale link recent metadata never replaces a newer visit choice', () => {
  const workspace = create({ storage: memoryStorage() });
  workspace.createProject('项目');
  workspace.appendMessage('首轮', '回复');
  const projectId = workspace.project.id;
  const stale = workspace.snapshot();
  const second = workspace.createConversation(projectId);
  workspace.appendMessage('次轮', '回复');
  workspace.restore('?' + new URLSearchParams({ groups: stale }));
  assert.equal(workspace.projects[0].lastConversationId, second.id);
});

test('read-only refresh receives other tab changes without writing visit metadata back', () => {
  const storage = memoryStorage();
  let writes = 0;
  const setItem = storage.setItem;
  storage.setItem = (key, value) => { writes += 1; setItem(key, value); };
  const a = create({ storage });
  a.createProject('共享项目');
  const b = selected(storage, a.project.id, a.conversation.id);
  a.updateScope({ need: '新需求' });
  a.appendMessage('新增消息', '回复');
  const beforeRefresh = writes;
  b.refresh();
  assert.equal(writes, beforeRefresh);
  assert.equal(b.draft.scope.need, '新需求');
  assert.equal(b.conversation.messages[0].text, '新增消息');
  assert.deepEqual(b.project.lastVisitedAt, a.project.lastVisitedAt);
});

test('starting and abandoning conversation drafts never saves history entries or visit metadata', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  const first = workspace.createProject('项目', { messages: [{ text: '已有对话' }] });
  const projectId = workspace.project.id;
  const saved = storage.getItem(storageKey);
  const a = workspace.createConversation(projectId);
  const b = workspace.createConversation(projectId);
  assert.notEqual(a.id, b.id);
  assert.equal(b.pending, true);
  assert.equal(workspace.project.conversations.length, 1);
  assert.equal(workspace.project.lastConversationId, first.id);
  assert.equal(storage.getItem(storageKey), saved);
  assert.equal(workspace.appendMessage('  ', '回复', {}, [{ name: '不支持.pdf' }]), null);
  assert.equal(storage.getItem(storageKey), saved);
  workspace.select(projectId, first.id);
  assert.equal(workspace.conversation.id, first.id);
  assert.equal(workspace.project.conversations.some((item) => item.id === a.id || item.id === b.id), false);
});

test('draft links omit transient IDs and reload without persisting an empty conversation', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  const firstDraft = workspace.createProject('项目');
  const projectId = workspace.project.id;
  workspace.updateScope({ industry: '无人机' });
  const link = new URL(workspace.link('index.html'), 'https://example.test/');
  assert.equal(link.searchParams.get('draft'), '1');
  assert.equal(link.searchParams.has('conversation'), false);
  assert.equal(link.search.includes(firstDraft.id), false);
  const saved = storage.getItem(storageKey);
  const reloaded = create({ storage, search: link.search });
  assert.equal(reloaded.missingSelection, false);
  assert.equal(reloaded.conversation.pending, true);
  assert.notEqual(reloaded.conversation.id, firstDraft.id);
  assert.deepEqual(reloaded.project.conversations, []);
  assert.equal(storage.getItem(storageKey), saved);
  assert.equal(reloaded.draft.scope.industry, '无人机');
  reloaded.appendMessage('实际发送', '回复');
  assert.equal(reloaded.project.conversations.length, 1);
  assert.equal(reloaded.conversation.pending, undefined);
});

test('empty project resource pages allow scope edits without a saved conversation', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  workspace.createProject('项目');
  const reloaded = create({ storage, search: '?' + new URLSearchParams({ project: workspace.project.id }) });
  assert.equal(reloaded.missingSelection, false);
  assert.equal(reloaded.conversation, undefined);
  assert.ok(reloaded.updateScope({ need: '精准降落' }));
  assert.equal(reloaded.draft.scope.need, '精准降落');
  assert.deepEqual(reloaded.project.conversations, []);
  reloaded.restore('?' + new URLSearchParams({ project: workspace.project.id, draft: '1' }));
  assert.equal(reloaded.conversation.pending, true);
});

test('two stale pending tabs commit their own IDs while preserving shared scope updates', () => {
  const storage = memoryStorage();
  const a = create({ storage });
  const first = a.createProject('项目');
  const projectId = a.project.id;
  const b = create({ storage, search: '?' + new URLSearchParams({ project: projectId, draft: '1' }) });
  const second = b.conversation;
  a.updateScope({ industry: '无人机' });
  b.updateScope({ need: '降落' });
  b.appendMessage('', '附件已添加', {}, [{ name: '需求.docx', size: 1 }]);
  a.appendMessage('研究无人机', '已整理');
  const restored = selected(storage, projectId, first.id);
  assert.equal(restored.project.conversations.length, 2);
  assert.deepEqual(new Set(restored.project.conversations.map((item) => item.id)), new Set([first.id, second.id]));
  assert.deepEqual(restored.draft.scope, { industry: '无人机', need: '降落' });
  assert.equal(restored.conversation.messages.length, 1);
  restored.select(projectId, second.id);
  assert.equal(restored.conversation.messages[0].attachments[0].name, '需求.docx');
});

test('report references and change cards save only supported metadata without adding real report versions', () => {
  const storage = memoryStorage();
  const workspace = create({ storage });
  workspace.createProject('项目');
  workspace.appendMessage('修改这一段', '已生成', {}, [], {
    reference: { versionId: 'example-v3', kind: 'paragraph', label: '技术路线', sectionId: 'technical', paragraphId: 'p1', text: '引用内容', mapId: 'map-a', privatePath: 'SECRET PATH' },
    change: { versionId: 'example-v4', number: 4, summary: ['调整标题', '重写正文'], example: true, privatePath: 'SECRET PATH' },
    unrelated: 'SECRET DATA'
  });
  const message = workspace.conversation.messages[0];
  assert.deepEqual(message.reference, { versionId: 'example-v3', kind: 'paragraph', label: '技术路线', sectionId: 'technical', paragraphId: 'p1', text: '引用内容', mapId: 'map-a' });
  assert.deepEqual(message.change, { versionId: 'example-v4', number: 4, summary: ['调整标题', '重写正文'], example: true });
  assert.deepEqual(workspace.project.reportVersions, []);
  assert.equal(storage.getItem(storageKey).includes('SECRET'), false);
  assert.equal(workspace.snapshot().includes('引用内容'), false);
  assert.equal(selected(storage, workspace.project.id, workspace.conversation.id).conversation.messages[0].change.number, 4);
});
