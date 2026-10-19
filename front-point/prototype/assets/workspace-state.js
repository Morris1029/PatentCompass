(function (root) {
  const storageKey = 'patent-navigation-projects-v3';
  const previousKey = 'patent-navigation-projects-v2';
  const legacyKey = 'patent-navigation-researches-v1';
  const scopeKeys = ['industry', 'goal', 'need', 'focus', 'start', 'end', 'type'];
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const newId = () => root.crypto?.randomUUID?.() || Date.now() + '-' + Math.random().toString(36).slice(2);
  const titleFor = (text, industry) => industry ? industry + '专利导航' : text.replace(/\s+/g, ' ').slice(0, 22) || '新对话';
  const validRecord = (item) => item && typeof item.id === 'string' && typeof item.title === 'string';
  const emptyDraft = (scope = {}) => ({ scope: copy(scope), dataStatus: 'empty', analysisStatus: 'empty' });
  const sharedScope = (scope = {}) => ({ industry: scope.industry || '', start: scope.start || '', end: scope.end || '' });
  const normalizeConversation = (item, summaryOnly = false) => ({
    id: item.id, title: item.title,
    messages: summaryOnly ? [] : copy(Array.isArray(item.messages) ? item.messages : []),
    ...(summaryOnly || item.summaryOnly ? { summaryOnly: true } : {})
  });
  const normalizeProjects = (items, summaryOnly = false) => Array.isArray(items) ? items.filter((item) => validRecord(item) && Array.isArray(item.conversations)).map((project) => ({
    id: project.id, title: project.title, lastConversationId: project.lastConversationId,
    lastVisitedAt: project.lastVisitedAt || 0,
    draft: summaryOnly ? emptyDraft(sharedScope(project.draft?.scope))
      : { ...emptyDraft(), ...copy(project.draft || {}), scope: copy(project.draft?.scope || {}) },
    reportVersions: summaryOnly ? [] : copy(Array.isArray(project.reportVersions) ? project.reportVersions : []),
    currentReportId: summaryOnly ? null : project.currentReportId || null,
    conversations: project.conversations.filter(validRecord).map((item) => normalizeConversation(item, summaryOnly)),
    ...(summaryOnly || project.summaryOnly ? { summaryOnly: true } : {})
  })) : [];
  const fromPrevious = (items, summaryOnly = false) => Array.isArray(items) ? items.filter((item) => validRecord(item) && Array.isArray(item.conversations)).map((project) => {
    const conversations = project.conversations.filter(validRecord);
    const recent = conversations.find((item) => item.id === project.lastConversationId) || conversations[0];
    return {
      id: project.id, title: project.title,
      lastConversationId: recent?.id, lastVisitedAt: project.lastVisitedAt || 0,
      draft: emptyDraft(summaryOnly ? sharedScope(recent?.scope) : recent?.scope || {}), reportVersions: [], currentReportId: null,
      conversations: conversations.map((item) => normalizeConversation(item, summaryOnly)),
      ...(summaryOnly || (conversations.length && conversations.every((item) => item.summaryOnly)) ? { summaryOnly: true } : {})
    };
  }) : [];
  const fromLegacy = (items, summaryOnly = false) => fromPrevious(Array.isArray(items) ? items.filter(validRecord).map((item) => ({
    id: 'project-' + item.id, title: item.title, lastConversationId: item.id,
    conversations: [item]
  })) : [], summaryOnly);
  // Full stored drafts take priority over link summaries; visit metadata and missing conversations are retained.
  const mergeProjects = (preferred, fallback) => {
    const merged = copy(preferred);
    fallback.forEach((project) => {
      let target = merged.find((item) => item.id === project.id);
      if (!target) { merged.push(copy(project)); return; }
      if (target.summaryOnly && !project.summaryOnly) {
        const summaries = target;
        target = copy(project);
        merged[merged.indexOf(summaries)] = target;
        summaries.conversations.forEach((conversation) => {
          if (!target.conversations.some((item) => item.id === conversation.id)) target.conversations.push(copy(conversation));
        });
        if ((summaries.lastVisitedAt || 0) > target.lastVisitedAt) {
          target.lastConversationId = summaries.lastConversationId;
          target.lastVisitedAt = summaries.lastVisitedAt;
        }
      }
      if ((project.lastVisitedAt || 0) > (target.lastVisitedAt || 0)) {
        target.lastConversationId = project.lastConversationId;
        target.lastVisitedAt = project.lastVisitedAt;
      }
      project.conversations.forEach((conversation) => {
        const index = target.conversations.findIndex((item) => item.id === conversation.id);
        if (index < 0) target.conversations.push(copy(conversation));
        else if (target.conversations[index].summaryOnly && !conversation.summaryOnly) target.conversations[index] = copy(conversation);
      });
    });
    return merged;
  };
  const attachmentMetadata = (items) => Array.isArray(items) ? items.filter((item) => item && typeof item.name === 'string' && /\.docx?$/i.test(item.name)).map((item) => ({
    id: typeof item.id === 'string' ? item.id : newId(), name: item.name,
    size: Number.isFinite(item.size) && item.size >= 0 ? item.size : 0,
    type: typeof item.type === 'string' ? item.type : '',
    lastModified: Number.isFinite(item.lastModified) ? item.lastModified : 0
  })) : [];
  const messageMetadata = (metadata = {}) => {
    const result = {};
    if (metadata.reference && typeof metadata.reference === 'object') {
      const reference = {};
      ['versionId', 'kind', 'label', 'sectionId', 'paragraphId', 'text', 'mapId'].forEach((key) => {
        if (typeof metadata.reference[key] === 'string') reference[key] = metadata.reference[key];
      });
      if (Object.keys(reference).length) result.reference = reference;
    }
    if (metadata.change && typeof metadata.change.versionId === 'string' && Number.isFinite(metadata.change.number)) {
      const change = metadata.change;
      result.change = { versionId: change.versionId, number: change.number,
        summary: Array.isArray(change.summary) ? change.summary.filter((item) => typeof item === 'string') : String(change.summary || ''),
        ...(change.example === true ? { example: true } : {}) };
    }
    return result;
  };

  const create = ({ storage = null, search = '' } = {}) => {
    const read = (key) => {
      try { return JSON.parse(storage?.getItem(key) || 'null'); } catch { return null; }
    };
    const write = (projects) => {
      try { storage?.setItem(storageKey, JSON.stringify({ version: 3, projects })); } catch { /* Keep the workspace usable if storage is blocked. */ }
    };
    const readStored = () => normalizeProjects(read(storageKey)?.projects);
    const stored = readStored();
    let projects = mergeProjects(mergeProjects(stored, fromPrevious(read(previousKey)?.projects)), fromLegacy(read(legacyKey)));
    if (JSON.stringify(projects) !== JSON.stringify(stored)) write(projects);
    let projectId = null;
    let conversationId = null;
    let pendingConversation = null;
    let pendingProjectId = null;
    let missingSelection = false;
    const findProject = () => projects.find((project) => project.id === projectId);
    const findConversation = () => pendingProjectId === projectId && pendingConversation?.id === conversationId
      ? pendingConversation : findProject()?.conversations.find((conversation) => conversation.id === conversationId);
    const refresh = () => { projects = mergeProjects(readStored(), projects); };
    const persist = () => write(projects);
    const remember = () => {
      const project = findProject();
      if (!project || !findConversation() || findConversation().pending) return;
      project.lastConversationId = conversationId;
      project.lastVisitedAt = Math.max(Date.now(), (project.lastVisitedAt || 0) + 1);
      persist();
    };
    const startPending = (project) => {
      if (pendingProjectId !== project.id || !pendingConversation) {
        pendingConversation = { id: newId(), title: '新对话', messages: [], pending: true };
        pendingProjectId = project.id;
      }
      conversationId = pendingConversation.id;
      return pendingConversation;
    };
    const restore = (query) => {
      refresh();
      const params = new URLSearchParams(query);
      try {
        const shared = JSON.parse(params.get('groups') || 'null');
        const summaries = Array.isArray(shared) ? fromLegacy(shared, true)
          : shared?.version === 3 ? normalizeProjects(shared.projects, true) : fromPrevious(shared?.projects, true);
        projects = mergeProjects(projects, summaries);
      } catch { /* Malformed link summaries do not replace local records. */ }
      projectId = params.get('project');
      conversationId = params.get('conversation');
      const oldId = params.get('research');
      if (!projectId && oldId) {
        const project = projects.find((item) => item.conversations.some((conversation) => conversation.id === oldId));
        projectId = project?.id || 'project-' + oldId;
        conversationId = oldId;
      }
      const project = findProject();
      if (project && params.get('draft') === '1' && !conversationId) startPending(project);
      else {
        pendingConversation = null;
        pendingProjectId = null;
        if (project && !conversationId) conversationId = project.lastConversationId || project.conversations[0]?.id;
      }
      missingSelection = Boolean((projectId && !project) || (conversationId && !findConversation()));
      remember();
    };
    const select = (nextProjectId, nextConversationId) => {
      refresh();
      projectId = nextProjectId;
      pendingConversation = null;
      pendingProjectId = null;
      const project = findProject();
      conversationId = nextConversationId || project?.lastConversationId || project?.conversations[0]?.id;
      missingSelection = Boolean(!project || (conversationId && !findConversation()));
      remember();
      return findConversation();
    };
    const createProject = (title, { scope = {}, messages = [] } = {}) => {
      refresh();
      const conversation = messages.length ? { id: newId(), title: titleFor(String(messages[0].text || messages[0]), scope.industry), messages: copy(messages) } : null;
      const project = { id: newId(), title: title.trim(), lastConversationId: conversation?.id, lastVisitedAt: Date.now(),
        draft: emptyDraft(scope), reportVersions: [], currentReportId: null, conversations: conversation ? [conversation] : [] };
      projects.unshift(project);
      projectId = project.id;
      pendingConversation = null;
      pendingProjectId = null;
      conversationId = conversation?.id;
      if (!conversation) startPending(project);
      missingSelection = false;
      persist();
      return findConversation();
    };
    const createConversation = (targetProjectId) => {
      refresh();
      const project = projects.find((item) => item.id === targetProjectId);
      if (!project) return null;
      projectId = project.id;
      pendingConversation = null;
      pendingProjectId = null;
      missingSelection = false;
      return startPending(project);
    };
    const appendMessage = (text, reply, scopeChanges = {}, attachments = [], metadata = {}) => {
      refresh();
      const project = findProject();
      const conversation = findConversation();
      const files = attachmentMetadata(attachments);
      const message = String(text || '');
      if (!project || !conversation || conversation.summaryOnly || (project.summaryOnly && !conversation.pending) || (!message.trim() && !files.length)) return null;
      if (conversation.pending) {
        delete conversation.pending;
        delete project.summaryOnly;
        project.conversations.unshift(conversation);
        project.lastConversationId = conversation.id;
        project.lastVisitedAt = Math.max(Date.now(), (project.lastVisitedAt || 0) + 1);
        pendingConversation = null;
        pendingProjectId = null;
      }
      if (!conversation.messages.length) conversation.title = titleFor(message.trim() || files[0].name, message.trim() ? scopeChanges.industry || project.draft.scope.industry : '');
      scopeKeys.forEach((key) => { if (Object.hasOwn(scopeChanges, key)) project.draft.scope[key] = scopeChanges[key]; });
      conversation.messages.push({ id: newId(), text: message, reply, ...(files.length ? { attachments: files } : {}), ...messageMetadata(metadata) });
      persist();
      return conversation;
    };
    const updateScope = (changes) => {
      refresh();
      const project = findProject();
      const conversation = findConversation();
      if (!project || project.summaryOnly || missingSelection || conversation?.summaryOnly) return null;
      scopeKeys.forEach((key) => { if (Object.hasOwn(changes, key)) project.draft.scope[key] = changes[key]; });
      persist();
      return conversation || project;
    };
    const snapshot = () => JSON.stringify({ version: 3, projects: projects.map((project) => ({
      id: project.id, title: project.title, lastConversationId: project.lastConversationId, lastVisitedAt: project.lastVisitedAt || 0,
      draft: { scope: sharedScope(project.draft.scope) },
      conversations: project.conversations.map(({ id, title }) => ({ id, title }))
    })) });
    const link = (path, targetProjectId = projectId, targetConversationId = conversationId, hash = '') => {
      const query = new URLSearchParams();
      if (targetProjectId) query.set('project', targetProjectId);
      if (targetProjectId === pendingProjectId && targetConversationId === pendingConversation?.id) query.set('draft', '1');
      else if (targetConversationId) query.set('conversation', targetConversationId);
      if (projects.length) query.set('groups', snapshot());
      return path + (query.size ? '?' + query : '') + (hash ? '#' + hash : '');
    };
    restore(search);
    return {
      get projects() { return projects; }, get project() { return findProject(); },
      get draft() { return findProject()?.draft; }, get conversation() { return findConversation(); },
      get missingSelection() { return missingSelection; },
      restore, refresh, select, createProject, createConversation, appendMessage, updateScope, snapshot, link
    };
  };
  const api = { create, storageKey, previousKey, legacyKey, scopeKeys, titleFor };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PatentWorkspace = api;
})(globalThis);
