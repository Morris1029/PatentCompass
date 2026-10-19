(() => {
  const { scopeKeys, titleFor } = PatentWorkspace;
  const goals = { entry: '进入新行业', rd: '研发立项', direction: '选择技术方向' };
  const types = { invention: '发明专利', utility: '实用新型', both: '发明专利与实用新型' };
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    if (tag === 'button') node.type = 'button';
    return node;
  };
  const readRoute = () => {
    const query = new URLSearchParams(location.hash.startsWith('#workspace?') ? location.hash.slice(11) : location.search);
    if (location.hash === '#scope') { query.set('view', 'workbench'); query.set('tab', 'scope'); }
    return {
      search: '?' + query, view: ['chat', 'workbench', 'report'].includes(query.get('view')) ? query.get('view') : 'chat',
      tab: ['overview', 'analysis', 'layout', 'scope', 'data'].includes(query.get('tab')) ? query.get('tab') : 'overview',
      analysis: query.get('analysis') || 'technology', report: query.get('report') || '', diff: query.get('diff') || '',
      anchor: query.get('anchor') || '', example: query.get('example') === '1' || query.get('preview') === 'report-edit'
    };
  };
  let route = readRoute();
  let storage = null;
  try { storage = window.localStorage; } catch { /* The in-memory workspace remains available. */ }
  const workspace = PatentWorkspace.create({ storage, search: route.search });
  const exampleRecords = new Map();
  const exampleKey = (projectId) => 'patent-navigation-report-example-' + projectId;
  const exampleRecord = (create = false, projectId = workspace.project?.id, reload = false) => {
    if (!projectId) return null;
    if (reload || !exampleRecords.has(projectId)) {
      let saved;
      try { saved = JSON.parse(storage?.getItem(exampleKey(projectId)) || 'null'); } catch { /* Start with the empty report view. */ }
      if (saved?.state) exampleRecords.set(projectId, { active: saved.active, model: PatentReports.createPreview({ state: saved.state }) });
      else if (create && !exampleRecords.has(projectId)) exampleRecords.set(projectId, { active: true, model: PatentReports.createPreview() });
    }
    return exampleRecords.get(projectId) || null;
  };
  const saveExample = () => {
    const record = exampleRecord();
    if (!record) return;
    try { storage?.setItem(exampleKey(workspace.project.id), JSON.stringify({ active: record.active, state: record.model.serialize() })); } catch { /* Keep the current example in memory. */ }
  };
  const preferenceKey = 'patent-navigation-sidebar-collapsed';
  let savedPreference;
  try { savedPreference = storage?.getItem(preferenceKey); } catch { /* Use the linked preference. */ }
  const initialQuery = new URLSearchParams(route.search);
  let collapsed = initialQuery.has('sidebar') ? initialQuery.get('sidebar') === 'collapsed' : savedPreference === 'true';
  let lastLocation = '';
  let pendingAttachments = [];
  let pendingReference = null;
  const mobile = window.matchMedia('(max-width: 720px)');
  const sidebar = document.querySelector('.workspace-sidebar');
  const sidebarList = sidebar.querySelector('[data-project-list]');
  const main = document.querySelector('main');
  const chat = document.querySelector('[data-view-panel="chat"]');
  const welcome = chat.querySelector('.agent-welcome');
  const dialogue = chat.querySelector('.agent-dialogue');
  const historyList = chat.querySelector('[data-chat-history]');
  const response = chat.querySelector('[data-chat-response]');
  const chatForm = chat.querySelector('[data-chat-form]');
  const messageField = chatForm.elements.namedItem('message');
  const fileInput = document.querySelector('#chat-files');
  const attachmentStatus = document.querySelector('[data-attachment-status]');
  const reportContainer = document.querySelector('[data-report-container]');
  const context = document.querySelector('[data-context]');
  let mobileOpen = false;
  let mobileToggle;
  let backdrop;

  const hrefFor = (options = {}) => {
    const next = { ...route, ...options };
    const query = new URLSearchParams();
    const selected = new URLSearchParams(route.search);
    const projectId = Object.hasOwn(options, 'projectId') ? options.projectId : workspace.project?.id || (workspace.missingSelection ? selected.get('project') : null);
    const pending = workspace.conversation?.pending && projectId === workspace.project?.id;
    const conversationId = Object.hasOwn(options, 'conversationId') ? options.conversationId : pending ? null : workspace.conversation?.id || (workspace.missingSelection ? selected.get('conversation') : null);
    if (projectId) query.set('project', projectId);
    if (conversationId) query.set('conversation', conversationId);
    if (pending && !conversationId) query.set('draft', '1');
    if (workspace.projects.length) query.set('groups', workspace.snapshot());
    if (next.example && projectId) query.set('example', '1');
    query.set('view', next.view || 'chat');
    if (next.view === 'workbench') { query.set('tab', next.tab || 'overview'); if (next.tab === 'analysis') query.set('analysis', next.analysis || 'technology'); }
    if (next.report) query.set('report', next.report);
    if (next.diff) query.set('diff', next.diff);
    if (next.anchor) query.set('anchor', next.anchor);
    query.set('sidebar', collapsed ? 'collapsed' : 'expanded');
    return 'index.html#workspace?' + query;
  };
  const writeLocation = (replace = false) => {
    const href = hrefFor();
    const method = replace ? 'replaceState' : 'pushState';
    try { history[method](null, '', href); }
    catch {
      const fragment = href.slice(href.indexOf('#'));
      try { history[method](null, '', fragment); }
      catch { if (replace) location.replace(fragment); else location.hash = fragment; }
    }
    lastLocation = location.href;
    route.search = '?' + href.split('#workspace?')[1];
  };
  const clearComposer = () => {
    pendingAttachments = [];
    pendingReference = null;
    messageField.value = '';
    fileInput.value = '';
    attachmentStatus.hidden = true;
  };
  const applySidebar = () => {
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    document.body.classList.toggle('sidebar-mobile-open', mobileOpen);
    sidebar.classList.toggle('is-open', mobileOpen);
    sidebarList.inert = !mobile.matches && collapsed;
    const button = sidebar.querySelector('[data-sidebar-collapse]');
    button.setAttribute('aria-expanded', String(mobile.matches ? mobileOpen : !collapsed));
    button.setAttribute('aria-label', mobile.matches ? '关闭项目列表' : collapsed ? '展开侧栏' : '收起侧栏');
    button.title = button.getAttribute('aria-label');
    button.firstElementChild.textContent = mobile.matches ? '×' : '▥';
    mobileToggle?.setAttribute('aria-expanded', String(mobileOpen));
    if (backdrop) backdrop.hidden = !mobileOpen;
    main.inert = mobileOpen;
  };
  const closeMobile = (returnFocus = false) => { mobileOpen = false; applySidebar(); if (returnFocus) mobileToggle.focus(); };
  const navigate = (view, options = {}, replace = false) => {
    route = { ...route, view, anchor: '', diff: '', report: '', ...options };
    closeMobile();
    writeLocation(replace);
    renderAll();
    if (route.anchor) document.getElementById(route.anchor)?.scrollIntoView({ block: 'start' });
  };
  const selectConversation = (projectId, conversationId) => {
    workspace.select(projectId, conversationId);
    clearComposer();
    navigate('chat', { example: Boolean(exampleRecord()?.active) });
    messageField.focus();
  };
  const resourceLink = (label, view, project, conversationId) => {
    const link = element('a', 'project-resource-link', label);
    const targetConversationId = project.id === workspace.project?.id && workspace.conversation?.pending ? null : conversationId;
    link.href = hrefFor({ projectId: project.id, conversationId: targetConversationId, view, tab: 'overview', report: '', diff: '', anchor: '', example: Boolean(exampleRecord(false, project.id)?.active) });
    link.dataset.projectResource = project.id;
    link.dataset.view = view;
    if (workspace.project?.id === project.id && route.view === view) link.setAttribute('aria-current', 'page');
    return link;
  };
  const renderSidebar = () => {
    sidebarList.replaceChildren();
    sidebar.querySelector('.sidebar-brand').href = hrefFor({ example: false, projectId: null, conversationId: null, view: 'chat', report: '', diff: '', anchor: '' });
    if (!workspace.projects.length) sidebarList.append(element('p', 'research-list-empty', '创建项目，开始你的专利研究。'));
    workspace.projects.forEach((project) => {
      const selected = workspace.project?.id === project.id;
      const group = element('section', 'project-group' + (selected ? ' is-active is-open' : ''));
      const head = element('div', 'project-group-head');
      const toggle = element('button', 'project-group-toggle', '⌄');
      const childrenId = 'project-items-' + project.id;
      toggle.setAttribute('aria-label', '展开或收起项目：' + project.title);
      toggle.setAttribute('aria-expanded', String(selected));
      toggle.setAttribute('aria-controls', childrenId);
      toggle.addEventListener('click', () => toggle.setAttribute('aria-expanded', String(group.classList.toggle('is-open'))));
      const recent = project.conversations.find((item) => item.id === project.lastConversationId) || project.conversations[0];
      const title = element('a', 'project-group-title', project.title);
      title.href = hrefFor({ projectId: project.id, conversationId: recent?.id, view: 'chat', example: false, diff: '', report: '', anchor: '' });
      title.title = project.title;
      title.dataset.projectLink = project.id;
      title.dataset.conversationLink = recent?.id || '';
      const add = element('button', 'project-new-conversation', '＋');
      add.dataset.newConversation = project.id;
      add.setAttribute('aria-label', '在“' + project.title + '”中新建对话');
      add.title = '新建对话';
      head.append(toggle, title, add);
      const children = element('div', 'project-conversations');
      children.id = childrenId;
      const resources = element('div', 'project-resources');
      resources.setAttribute('role', 'group');
      resources.setAttribute('aria-label', '项目页面');
      resources.append(element('span', 'project-section-label', '项目页面'), resourceLink('分析工作台', 'workbench', project, recent?.id), resourceLink('导航报告', 'report', project, recent?.id));
      const conversationHistory = element('div', 'project-history');
      conversationHistory.setAttribute('role', 'group');
      conversationHistory.setAttribute('aria-label', '对话历史');
      conversationHistory.append(element('span', 'project-section-label', '对话历史'));
      project.conversations.filter((conversation) => conversation.messages.length > 0 || (conversation.summaryOnly && conversation.title !== '新对话')).forEach((conversation) => {
        const link = element('a', 'project-chat-link', conversation.title);
        link.title = conversation.title;
        link.href = hrefFor({ projectId: project.id, conversationId: conversation.id, view: 'chat', example: false, report: '', diff: '', anchor: '' });
        link.dataset.projectLink = project.id;
        link.dataset.conversationLink = conversation.id;
        if (selected && workspace.conversation?.id === conversation.id && route.view === 'chat') link.setAttribute('aria-current', 'page');
        conversationHistory.append(link);
      });
      if (!conversationHistory.querySelector('.project-chat-link')) conversationHistory.append(element('p', 'project-history-empty', '暂无对话，点击项目旁的＋开始'));
      children.append(resources, conversationHistory);
      group.append(head, children);
      sidebarList.append(group);
    });
  };
  const routeForLegacyLink = (raw) => {
    const [beforeHash, anchor = ''] = raw.split('#');
    const file = beforeHash.split('?')[0];
    const mapping = {
      'index.html': ['chat'], 'scope.html': ['workbench', 'scope'], 'upload.html': ['chat'],
      'review.html': ['workbench', 'analysis', 'technology'], 'landscape.html': ['workbench', 'analysis', 'branches'],
      'competition.html': ['workbench', 'analysis', 'competition'], 'geography.html': ['workbench', 'analysis', 'geography'],
      'opportunity.html': ['workbench', 'analysis', 'opportunity'], 'evidence.html': ['workbench', 'data'],
      'report.html': ['report'], 'report-print.html': ['report'], 'question.html': ['chat'],
      'citations.html': ['workbench', 'data'], 'contribution.html': ['workbench', 'data'], 'visual.html': ['report']
    };
    if (!mapping[file]) return null;
    let [view, tab, analysis] = mapping[file];
    if (file === 'index.html' && anchor === 'scope') { view = 'workbench'; tab = 'scope'; }
    const target = { view, tab, analysis, report: '', diff: '', anchor: '' };
    if (anchor && file === 'evidence.html') target.anchor = 'data-' + anchor;
    if (anchor && file === 'landscape.html') target.anchor = 'branches-' + anchor;
    if (anchor && file === 'geography.html') target.anchor = 'geography-' + anchor;
    return target;
  };
  const updateContentLinks = () => {
    main.querySelectorAll('a[href]').forEach((link) => {
      if (link.closest('[data-report-container]')) return;
      const raw = link.dataset.baseHref || link.getAttribute('href');
      const target = routeForLegacyLink(raw);
      if (!target) return;
      link.dataset.baseHref = raw;
      link.dataset.shellLink = '';
      link.href = hrefFor(target);
    });
  };
  const projectDialog = element('dialog', 'project-dialog');
  projectDialog.setAttribute('aria-labelledby', 'project-dialog-title');
  projectDialog.innerHTML = '<form><div class="section-head"><h2 id="project-dialog-title">新建项目</h2><button class="dialog-close" type="button" data-project-cancel aria-label="关闭新建项目">×</button></div><div class="field"><label for="project-name">项目名称</label><input id="project-name" name="title" type="text" maxlength="80" placeholder="为这项研究起个名字" required></div><div class="actions"><button class="btn secondary" type="button" data-project-cancel>取消</button><button class="btn primary" type="submit">创建项目</button></div></form>';
  document.body.append(projectDialog);
  let projectOpener;
  const openProjectDialog = (opener) => {
    closeMobile();
    projectOpener = opener;
    projectDialog.querySelector('form').reset();
    projectDialog.querySelector('input').setCustomValidity('');
    projectDialog.showModal();
    projectDialog.querySelector('input').focus();
  };
  projectDialog.querySelectorAll('[data-project-cancel]').forEach((button) => button.addEventListener('click', () => projectDialog.close()));
  projectDialog.addEventListener('close', () => { if (projectOpener?.isConnected) (mobile.matches ? mobileToggle : projectOpener).focus(); });
  projectDialog.querySelector('input').addEventListener('input', (event) => event.target.setCustomValidity(''));
  projectDialog.querySelector('form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = projectDialog.querySelector('input');
    if (!input.value.trim()) { input.setCustomValidity('请输入项目名称'); input.reportValidity(); return; }
    workspace.createProject(input.value.trim());
    projectOpener = null;
    projectDialog.close();
    clearComposer();
    navigate('chat', { example: Boolean(exampleRecord()?.active) });
    messageField.focus();
  });
  mobileToggle = element('button', 'sidebar-mobile-toggle', '项目');
  mobileToggle.setAttribute('aria-label', '打开项目列表');
  mobileToggle.setAttribute('aria-controls', 'workspace-navigation');
  mobileToggle.addEventListener('click', () => { mobileOpen = true; applySidebar(); sidebar.querySelector('[data-sidebar-collapse]').focus(); });
  sidebar.insertBefore(mobileToggle, sidebar.querySelector('.sidebar-nav'));
  backdrop = element('button', 'sidebar-backdrop');
  backdrop.setAttribute('aria-label', '关闭项目列表'); backdrop.hidden = true;
  backdrop.addEventListener('click', () => closeMobile(true));
  document.body.append(backdrop);
  sidebar.querySelector('[data-sidebar-collapse]').addEventListener('click', () => {
    if (mobile.matches) { closeMobile(true); return; }
    collapsed = !collapsed;
    try { storage?.setItem(preferenceKey, String(collapsed)); } catch { /* Keep the current preference in the route. */ }
    writeLocation(true); applySidebar(); renderSidebar(); updateContentLinks();
  });
  sidebar.classList.add('sidebar-ready');
  mobile.addEventListener('change', () => closeMobile());
  document.addEventListener('keydown', (event) => {
    if (!mobileOpen) return;
    if (event.key === 'Escape') { event.preventDefault(); closeMobile(true); }
    if (event.key === 'Tab') {
      const controls = [...sidebar.querySelectorAll('a[href], button:not(:disabled)')].filter((node) => node.offsetParent !== null);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  document.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const target = event.target.closest('button, a');
    if (!target) return;
    if (target.hasAttribute('data-new-project')) { event.preventDefault(); openProjectDialog(target); return; }
    if (target.hasAttribute('data-new-conversation')) {
      workspace.createConversation(target.dataset.newConversation); clearComposer(); navigate('chat', { example: Boolean(exampleRecord()?.active) }); messageField.focus(); return;
    }
    if (target.hasAttribute('data-conversation-link')) { event.preventDefault(); selectConversation(target.dataset.projectLink, target.dataset.conversationLink); return; }
    if (target.hasAttribute('data-project-resource')) {
      event.preventDefault();
      const id = target.dataset.projectResource;
      if (id !== workspace.project?.id || !workspace.conversation?.pending) workspace.select(id, id === workspace.project?.id ? workspace.conversation?.id : null);
      clearComposer(); navigate(target.dataset.view, { tab: 'overview', example: Boolean(exampleRecord()?.active) }); return;
    }
    if (target.hasAttribute('data-open-chat')) { navigate('chat'); messageField.focus(); return; }
    if (target.classList.contains('sidebar-brand')) {
      event.preventDefault();
      workspace.restore(''); clearComposer(); navigate('chat', { example: false }); return;
    }
    if (target.hasAttribute('data-shell-link')) {
      event.preventDefault(); const destination = routeForLegacyLink(target.dataset.baseHref); navigate(destination.view, destination); return;
    }
    if (target.getAttribute('href')?.startsWith('#') && ['#elements', '#methods', '#applications'].includes(target.getAttribute('href'))) {
      event.preventDefault(); navigate('workbench', { tab: 'analysis', analysis: 'branches', anchor: 'branches-' + target.getAttribute('href').slice(1) });
    }
  });

  const scopeEditors = [];
  document.querySelectorAll('[data-chat-scope], [data-workbench-scope]').forEach((host, index) => {
    const fragment = document.querySelector('#scope-template').content.cloneNode(true);
    fragment.querySelectorAll('[id]').forEach((node) => { node.id = (index ? 'workbench-' : 'chat-') + node.id; });
    fragment.querySelectorAll('[for]').forEach((node) => { node.htmlFor = (index ? 'workbench-' : 'chat-') + node.htmlFor; });
    fragment.querySelectorAll('[aria-labelledby]').forEach((node) => { node.setAttribute('aria-labelledby', (index ? 'workbench-' : 'chat-') + node.getAttribute('aria-labelledby')); });
    host.append(fragment);
    const section = host.querySelector('[data-scope-section]');
    const form = section.querySelector('[data-scope-form]');
    const button = section.querySelector('[data-scope-edit]');
    let baseline = {};
    const fill = () => { scopeKeys.forEach((key) => { form.elements.namedItem(key).value = baseline[key] || ''; }); form.elements.namedItem('end').setCustomValidity(''); };
    const render = (scope, disabled) => {
      const years = scope.start && scope.end ? scope.start + '—' + scope.end : scope.start ? scope.start + '年起' : scope.end ? '截至' + scope.end + '年' : '';
      const values = { ...scope, years, goal: goals[scope.goal], type: types[scope.type] };
      ['industry', 'goal', 'need', 'focus', 'years', 'type'].forEach((key) => { section.querySelector('[data-' + key + ']').textContent = values[key] || '待确认'; });
      baseline = Object.fromEntries(scopeKeys.map((key) => [key, scope[key] || '']));
      fill(); section.classList.remove('is-editing'); button.disabled = disabled;
    };
    button.addEventListener('click', () => { section.classList.add('is-editing'); form.elements.namedItem('industry').focus(); });
    section.querySelector('[data-scope-cancel]').addEventListener('click', () => { fill(); section.classList.remove('is-editing'); button.focus(); });
    for (const name of ['start', 'end']) form.elements.namedItem(name).addEventListener('input', () => form.elements.namedItem('end').setCustomValidity(''));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const start = form.elements.namedItem('start'), end = form.elements.namedItem('end');
      if (start.value && end.value && Number(start.value) > Number(end.value)) { end.setCustomValidity('结束年份不能早于开始年份'); end.reportValidity(); return; }
      const values = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value.trim()]));
      const changes = Object.fromEntries(scopeKeys.filter((key) => values[key] !== baseline[key]).map((key) => [key, values[key]]));
      if (workspace.updateScope(changes)) { writeLocation(true); renderAll(); button.focus(); }
    });
    scopeEditors.push(render);
  });
  document.querySelectorAll('[data-workbench-tab]').forEach((button) => button.addEventListener('click', () => navigate('workbench', { tab: button.dataset.workbenchTab })));
  document.querySelector('.workbench-tabs').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = [...document.querySelectorAll('[data-workbench-tab]')];
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].click(); tabs[next].focus();
  });
  const formatSize = (size) => size < 1024 ? size + ' B' : size < 1024 * 1024 ? Math.ceil(size / 1024) + ' KB' : (size / (1024 * 1024)).toFixed(1) + ' MB';
  const attachmentCard = (attachment, removable = false) => {
    const card = element('div', removable ? 'attachment-chip' : 'attachment-card');
    card.append(element('span', 'attachment-file-icon', 'W'), element('span', 'attachment-name', attachment.name), element('span', 'attachment-size', formatSize(attachment.size || 0)));
    if (removable) {
      const remove = element('button', 'attachment-remove', '×');
      remove.setAttribute('aria-label', '移除附件：' + attachment.name);
      remove.addEventListener('click', () => { pendingAttachments = pendingAttachments.filter((item) => item.id !== attachment.id); renderComposer(); document.querySelector('[data-attach-file]').focus(); });
      card.append(remove);
    }
    return card;
  };
  const showStatus = (text) => { attachmentStatus.textContent = text; attachmentStatus.hidden = !text; };
  const renderComposer = () => {
    const pending = document.querySelector('[data-pending-attachments]');
    pending.replaceChildren(...pendingAttachments.map((file) => attachmentCard(file, true)));
    const ref = document.querySelector('[data-composer-reference]');
    ref.hidden = !pendingReference;
    ref.replaceChildren();
    if (pendingReference) {
      ref.append(element('strong', '', referenceLabel(pendingReference)), element('p', '', pendingReference.text));
      const remove = element('button', 'attachment-remove', '×'); remove.setAttribute('aria-label', '取消引用');
      remove.addEventListener('click', () => { pendingReference = null; renderComposer(); }); ref.append(remove);
    }
    const disabled = workspace.missingSelection || Boolean(workspace.conversation?.summaryOnly);
    messageField.disabled = disabled;
    chatForm.querySelector('[type="submit"]').disabled = disabled;
    const attach = document.querySelector('[data-attach-file]'); attach.disabled = disabled;
    attach.title = '添加 Word 文档';
    document.querySelector('[data-composer-hint]').textContent = 'Word 附件 · Enter 发送';
  };
  document.querySelector('[data-attach-file]').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    const invalid = [];
    [...fileInput.files].forEach((file) => {
      if (!/\.(doc|docx)$/i.test(file.name)) { invalid.push(file.name); return; }
      if (pendingAttachments.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) return;
      pendingAttachments.push({ id: crypto.randomUUID?.() || Date.now() + '-' + Math.random().toString(36).slice(2), name: file.name, size: file.size, type: file.type, lastModified: file.lastModified });
    });
    fileInput.value = '';
    showStatus(invalid.length ? '请选择 Word 文档（.doc 或 .docx）：' + invalid.join('、') : '仅保存附件信息，文档正文尚未读取。');
    renderComposer();
  });
  const referenceLabel = (reference) => {
    const version = exampleRecord()?.model.getVersion(reference.versionId);
    return '引用报告 V' + (version?.number || reference.versionNumber || '—') + ' · ' + (reference.label || reference.sectionTitle || '报告内容');
  };
  const renderChangeCard = (change) => {
    const card = element('section', 'report-change-card');
    card.append(element('span', 'label', change.example ? '示例报告更新' : '报告更新'), element('h3', '', '已生成报告 V' + change.number), element('p', '', change.summary));
    const actions = element('div', 'report-change-actions');
    const diff = element('button', 'btn secondary', '查看改动'); diff.addEventListener('click', () => navigate('report', { example: Boolean(change.example), report: change.versionId, diff: change.versionId }));
    const open = element('button', 'btn primary', '打开报告 V' + change.number); open.addEventListener('click', () => navigate('report', { example: Boolean(change.example), report: change.versionId }));
    actions.append(diff, open); card.append(actions); return card;
  };
  const renderMessages = () => {
    const messages = workspace.conversation?.messages || [];
    historyList.replaceChildren();
    messages.forEach((message, index) => {
      const bubble = element('div', 'conversation-message user');
      const body = element('div', 'message-content');
      if (message.reference) { const quote = element('blockquote', 'message-quote'); quote.append(element('strong', '', referenceLabel(message.reference)), element('p', '', message.reference.text)); body.append(quote); }
      if (message.text || typeof message === 'string') body.append(element('p', '', message.text || message));
      if (message.attachments?.length) { const files = element('div', 'message-attachments'); files.append(...message.attachments.map((file) => attachmentCard(file))); body.append(files); }
      bubble.append(body); historyList.append(bubble);
      if (message.change || index < messages.length - 1) {
        const reply = element('div', 'conversation-message assistant history-reply');
        const avatar = element('span', 'assistant-avatar', '导'); avatar.setAttribute('aria-hidden', 'true');
        const content = element('div', 'assistant-content'); content.append(element('p', '', message.reply || '当前项目范围见下方表格。'));
        if (message.change) content.append(renderChangeCard(message.change));
        reply.append(avatar, content); historyList.append(reply);
      }
    });
  };
  const renderChat = () => {
    const conversation = workspace.conversation;
    const messages = conversation?.messages || [];
    const hasDialogue = !workspace.missingSelection && messages.length > 0;
    welcome.hidden = hasDialogue;
    dialogue.hidden = !hasDialogue;
    response.hidden = !hasDialogue || Boolean(messages.at(-1)?.change);
    document.body.classList.toggle('has-conversation', route.view === 'chat' && hasDialogue);
    if (!hasDialogue) {
      welcome.querySelector('h1').textContent = workspace.missingSelection ? '未找到这项研究对话' : conversation ? '继续这个项目的研究' : '从一个产业问题开始';
      welcome.querySelector('p').textContent = workspace.missingSelection ? '请从项目列表选择已有对话，或新建项目。' : conversation?.summaryOnly ? '当前只有链接摘要，可在项目下新建对话继续研究。' : conversation ? '新对话共用项目底稿。描述你的问题，逐步完善研究与报告。' : '描述研究目标，逐步完善项目底稿与导航报告。';
    }
    const scope = workspace.project?.draft.scope || {};
    const hasScope = scopeKeys.some((key) => scope[key]);
    chat.querySelector('[data-extraction-note]').textContent = !hasScope ? '暂未从文字中确定研究范围。可以继续补充，或直接修改下表。' : '已整理文字中明确的信息，未提及的项目保留待确认。以下是当前项目共用的研究范围。';
    if (messages.at(-1)?.attachments?.length) chat.querySelector('[data-extraction-note]').textContent = (messages.at(-1).text ? chat.querySelector('[data-extraction-note]').textContent + ' ' : '') + '附件已加入对话；目前仅保存文件信息，正文尚未读取。';
    renderMessages(); renderComposer();
  };
  let decisionWorkspaceMounted = false;
  const renderWorkbench = () => {
    const project = workspace.project;
    const enabled = Boolean(project && !workspace.missingSelection);
    document.querySelector('[data-workbench-empty]').hidden = enabled;
    document.querySelector('[data-workbench-content]').hidden = !enabled;
    if (!decisionWorkspaceMounted) {
      const onNavigate = (target) => {
        navigate(target.view || 'workbench', target);
        const destination = target.anchor && document.getElementById(target.anchor);
        if (destination) { destination.tabIndex = -1; destination.focus({ preventScroll: true }); }
      };
      const onPrompt = (text) => {
        if (!workspace.project || workspace.missingSelection) return;
        clearComposer();
        if (!workspace.conversation || workspace.conversation.summaryOnly) workspace.createConversation(workspace.project.id);
        navigate('chat', { example: false });
        messageField.value = text;
        messageField.focus();
      };
      const onOpenReport = () => {
        if (!workspace.project || workspace.project.summaryOnly) { navigate('report', { example: false }); return; }
        const record = exampleRecord(true);
        record.active = true;
        saveExample();
        const current = record.model.current;
        navigate('report', { example: true, report: current.id,
          anchor: PatentReports.needsLayout(current.snapshot) ? '' : 'report-section-patent-layout' });
      };
      PatentDecisionInsights.renderOverview(document.querySelector('[data-decision-overview]'), { onNavigate, onPrompt });
      PatentDecisionInsights.renderLayout(document.querySelector('[data-decision-layout]'), { onNavigate, onPrompt, onOpenReport });
      PatentDecisionInsights.renderOpportunities(document.querySelector('[data-decision-opportunities]'), { onNavigate, onPrompt });
      decisionWorkspaceMounted = true;
    }
    document.querySelectorAll('[data-workbench-tab]').forEach((button) => {
      const selected = button.dataset.workbenchTab === route.tab;
      button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll('[data-workbench-panel]').forEach((panel) => { panel.hidden = panel.dataset.workbenchPanel !== route.tab; });
    const analysis = ['technology', 'branches', 'competition', 'geography', 'opportunity'].includes(route.analysis) ? route.analysis : 'technology';
    document.querySelectorAll('[data-analysis-panel]').forEach((panel) => { panel.hidden = panel.dataset.analysisPanel !== analysis; });
    const branchId = ['branches-elements', 'branches-methods', 'branches-applications'].includes(route.anchor) ? route.anchor : 'branches-methods';
    document.querySelectorAll('.branch-panel').forEach((panel) => { panel.hidden = panel.id !== branchId; });
    document.querySelectorAll('[data-analysis-tab]').forEach((link) => {
      link.toggleAttribute('aria-current', link.dataset.analysisTab === analysis);
      if (link.hasAttribute('aria-current')) link.setAttribute('aria-current', 'page');
    });
    const materials = document.querySelector('[data-project-materials]'); materials.replaceChildren();
    const files = project?.conversations.flatMap((conversation) => conversation.messages.flatMap((message) => (message.attachments || []).map((attachment) => ({ ...attachment, conversationId: conversation.id, conversationTitle: conversation.title })))) || [];
    if (!files.length) materials.append(element('p', 'small muted', '暂无项目资料。在对话中添加 Word 文档后，附件信息会显示在这里。'));
    files.forEach((file) => {
      const row = element('div', 'project-material-row'); row.append(attachmentCard(file));
      const source = element('button', 'material-source', file.conversationTitle); source.addEventListener('click', () => selectConversation(project.id, file.conversationId));
      row.append(source); materials.append(row);
    });
    if (files.length) materials.append(element('p', 'attachment-note', '仅保存附件信息，尚未读取文档内容；专利数据集仍待建立。'));
  };

  const generateExample = (instruction, reference, attachments = []) => {
    if (!workspace.project || workspace.missingSelection || workspace.project.summaryOnly) return { ok: false, error: '请先创建或选择本机的完整项目。' };
    if (!workspace.conversation || workspace.conversation.summaryOnly) workspace.createConversation(workspace.project.id);
    const record = exampleRecord(true, workspace.project.id, true);
    const result = record.model.generate(instruction, reference, { conversationId: workspace.conversation.id });
    if (!result.ok) return result;
    workspace.appendMessage(instruction, result.message.reply, {}, attachments, {
      reference, change: { ...result.message.change, example: true }
    });
    record.active = true;
    saveExample();
    return result;
  };
  const quoteToChat = (reference, instruction = '') => {
    if (!workspace.conversation || workspace.conversation.summaryOnly) workspace.createConversation(workspace.project.id);
    pendingReference = { ...reference, example: route.example };
    if (instruction) messageField.value = instruction;
    navigate('chat');
    messageField.focus();
  };
  const renderReport = () => {
    const canEdit = Boolean(workspace.project && !workspace.missingSelection && !workspace.project.summaryOnly);
    const record = route.example && canEdit ? exampleRecord(true) : null;
    const model = record?.model;
    const versions = (model ? model.versions : workspace.project?.reportVersions || []).map((version) => ({
      ...version, conversationTitle: workspace.project?.conversations.find((conversation) => conversation.id === version.conversationId)?.title || (model ? '初始示例' : '项目对话')
    }));
    const currentReportId = model ? model.currentReportId : workspace.project?.currentReportId;
    if (model && route.diff) {
      const difference = model.diff(route.diff);
      if (difference) {
        PatentReports.renderDiff(reportContainer, { ...difference,
          onClose: () => navigate('report', { report: route.diff }),
          onLocate: (sectionId, paragraphId) => navigate('report', { report: route.diff, anchor: paragraphId ? 'report-paragraph-' + paragraphId : 'report-section-' + sectionId })
        });
      } else route.diff = '';
    }
    if (!model || !route.diff) PatentReports.render(reportContainer, {
      versions, currentReportId, selectedReportId: route.report, example: Boolean(model), hasProject: canEdit,
      onStartExample: () => {
        const example = exampleRecord(true);
        example.active = true;
        saveExample();
        navigate('report', { example: true, report: example.model.currentReportId });
      },
      onSelect: (id) => navigate('report', { report: id }),
      onAdopt: (id) => {
        if (model) {
          const latest = exampleRecord(true, workspace.project.id, true);
          latest.model.adoptVersion(id); saveExample(); navigate('report', { report: id });
        }
      },
      onDiff: (id) => navigate('report', { report: id, diff: id }),
      onImproveLayout: (id) => {
        const result = generateExample(PatentReports.layoutPrompt, {
          versionId: id, kind: 'section', sectionId: 'patent-layout', label: '布局与路线图',
          text: '补充机会与风险、专利布局建议及实施路线图。'
        });
        if (result.ok) {
          clearComposer();
          navigate('report', { example: true, report: result.version.id, anchor: 'report-section-patent-layout' });
        } else {
          const message = element('p', 'notice', result.error);
          message.setAttribute('role', 'status');
          reportContainer.prepend(message);
        }
      },
      onQuote: quoteToChat,
      onEdit: (instruction, reference) => {
        const result = generateExample(instruction, reference);
        if (result.ok) { clearComposer(); navigate('report', { example: true, report: result.version.id }); }
        return result;
      },
      onGenerate: () => {
        const version = model?.getVersion(route.report || currentReportId);
        if (!version) return;
        quoteToChat({ versionId: version.id, kind: 'report', label: '整份报告', text: version.snapshot.title });
        messageField.value = PatentReports.examplePrompt;
      },
      onOpenChat: () => { navigate('chat'); messageField.focus(); }
    });
    if (model) {
      const note = element('div', 'report-example-notice');
      note.append(element('p', '', '报告编辑体验 · 示例文字，无专利分析结论。选中文字即可提出修改要求。'));
      const finish = element('button', 'text-button', '结束体验');
      finish.addEventListener('click', () => { exampleRecord().active = false; saveExample(); navigate('report', { example: false }); });
      note.append(finish); reportContainer.prepend(note);
      if (route.report && !model.getVersion(route.report)) reportContainer.prepend(element('p', 'notice', '未找到指定的示例版本，当前显示已采用的版本。'));
    }
  };
  const renderAll = () => {
    document.querySelectorAll('[data-view-panel]').forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== route.view; });
    ['chat', 'workbench', 'report'].forEach((view) => document.body.classList.toggle('workspace-view-' + view, route.view === view));
    context.replaceChildren(); context.hidden = !workspace.project;
    if (workspace.project) {
      context.append(element('span', 'context-project', workspace.project.title), element('span', 'context-title', route.view === 'chat' ? workspace.conversation?.title || '新对话' : route.view === 'workbench' ? '分析工作台' : '导航报告'));
      if (workspace.project.summaryOnly) context.append(element('p', 'context-note', '当前只有链接摘要，完整记录仍保留在原浏览器中。可新建对话继续。'));
    }
    const notice = document.querySelector('[data-workspace-notice]');
    notice.hidden = !workspace.missingSelection;
    notice.textContent = '未找到这项项目或对话。请从项目列表重新选择，或新建项目。';
    scopeEditors.forEach((render) => render(workspace.project?.draft.scope || {}, !workspace.project || workspace.missingSelection || Boolean(workspace.project?.summaryOnly || workspace.conversation?.summaryOnly)));
    renderSidebar(); renderChat(); renderWorkbench();
    if (route.view === 'report') renderReport();
    else PatentReports.dispose?.(reportContainer);
    updateContentLinks(); applySidebar();
    document.title = (workspace.project?.title || '专利导航') + ' · ' + ({ chat: '研究对话', workbench: '分析工作台', report: '导航报告' }[route.view]);
  };
  chatForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const message = messageField.value.trim();
    if (messageField.disabled || (!message && !pendingAttachments.length)) { messageField.focus(); return; }
    if (pendingReference?.example || (route.example && [PatentReports.examplePrompt, PatentReports.layoutPrompt].includes(message))) {
      const result = generateExample(message, pendingReference, pendingAttachments);
      if (!result.ok) { showStatus(result.error); return; }
      clearComposer(); navigate('report', { example: true, report: result.version.id }); return;
    }
    const extracted = PatentScope.extract(message);
    if (!workspace.project) workspace.createProject(titleFor(message || pendingAttachments[0]?.name || '', extracted.industry));
    else if (!workspace.conversation) workspace.createConversation(workspace.project.id);
    const reply = pendingAttachments.length ? '已将附件信息加入对话，文档正文尚未读取。' : Object.keys(extracted).length ? '已根据文字更新项目研究范围。' : '未提取到新的范围字段，可以继续补充或修改表格。';
    workspace.appendMessage(message, reply, extracted, pendingAttachments);
    clearComposer(); writeLocation(true); renderAll(); response.scrollIntoView({ block: 'start' });
  });
  messageField.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); chatForm.requestSubmit(); } });
  const restoreLocation = () => {
    if (location.href === lastLocation) return;
    const next = readRoute();
    workspace.restore(next.search);
    route = next; lastLocation = location.href; clearComposer(); renderAll();
    if (route.anchor) document.getElementById(route.anchor)?.scrollIntoView({ block: 'start' });
  };
  window.addEventListener('popstate', restoreLocation);
  window.addEventListener('hashchange', restoreLocation);
  window.addEventListener('storage', (event) => {
    if (workspace.project && event.key === exampleKey(workspace.project.id)) {
      exampleRecords.delete(workspace.project.id); renderAll(); return;
    }
    if (event.key && event.key !== PatentWorkspace.storageKey) return;
    workspace.refresh(); renderAll();
  });
  {
    if (initialQuery.get('newProject')?.trim()) workspace.createProject(initialQuery.get('newProject').trim());
    else if (initialQuery.get('newConversation') === '1' && workspace.project && !workspace.missingSelection) workspace.createConversation(workspace.project.id);
    if (!workspace.conversation && !workspace.missingSelection && scopeKeys.some((key) => initialQuery.get(key))) {
      const scope = Object.fromEntries(scopeKeys.map((key) => [key, initialQuery.get(key) || '']));
      workspace.createProject(titleFor('', scope.industry), { scope });
    }
  }
  if (route.example && !workspace.project && !workspace.missingSelection && workspace.projects.length) {
    const recent = [...workspace.projects].sort((a, b) => b.lastVisitedAt - a.lastVisitedAt)[0];
    workspace.select(recent.id);
  }
  if (initialQuery.get('preview') === 'report-edit') route.view = 'report';
  writeLocation(true); renderAll();
  if (route.anchor) document.getElementById(route.anchor)?.scrollIntoView({ block: 'start' });
})();
