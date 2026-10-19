(function (root) {
  'use strict';
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const example = {
    id: 'uav-landing-decision-example', topic: '无人机自主降落', example: true,
    opportunities: [
      { id: 'robust-localization', title: '弱纹理环境下的融合定位', question: '视觉信息不足时，怎样保持近地定位与着陆引导稳定？',
        basis: '从感知定位路线进一步比较视觉、惯性与测距信息的互补方式，值得开展针对性检索。',
        evidenceGap: '尚缺同一检索口径下的专利样本、权利要求对照和弱纹理工况验证记录。',
        existingLayout: '示意技术树已列出多源感知分支；实际申请人、专利覆盖及法律状态待核实。', validationStatus: '待补检', analysis: 'technology', priorityId: 'robust-localization', actionIds: ['supplementary-search', 'direction-comparison'] },
      { id: 'landing-assessment', title: '动态场景中的落点安全评估', question: '怎样将障碍变化、地面条件与落点选择结合起来？',
        basis: '落点判断与路径规划之间存在可比较的方案组合，需要结合应用场景复核其研究价值。',
        evidenceGap: '尚缺落点评估相关权利要求、场景边界和不同方案的性能对照。',
        existingLayout: '示意技术树包含规划决策分支；相关专利的保护主题与重叠程度待复核。', validationStatus: '待复核', analysis: 'branches', priorityId: 'landing-assessment', actionIds: ['patent-review', 'direction-comparison'] },
      { id: 'failure-recovery', title: '异常识别与复飞协同控制', question: '定位失效或降落条件变化时，怎样形成可验证的退出与复飞策略？',
        basis: '异常感知、控制切换与复飞触发可作为组合研究问题，先验证安全边界再判断布局价值。',
        evidenceGap: '尚缺异常工况定义、试验记录及控制切换相关专利的完整检索。',
        existingLayout: '示意技术树包含控制执行分支；异常处理方案的实际布局情况待调查。', validationStatus: '待技术验证', analysis: 'technology', priorityId: 'failure-recovery', actionIds: ['technical-validation', 'direction-comparison'] }
    ],
    risks: [
      { id: 'competition-concentration', title: '竞争布局可能集中', impact: '影响拟进入方向的比较与差异化研究。',
        reason: '需要核对重点申请主体是否在同一技术分支持续布局，当前示意矩阵不能用于判断集中度。',
        evidenceGap: '缺少经归一化的申请人、专利族与时间序列数据。', attentionLevel: '优先关注', validationStatus: '待补检', analysis: 'competition', actionIds: ['supplementary-search', 'direction-comparison'] },
      { id: 'patent-coverage', title: '相关权利要求覆盖待核实', impact: '影响具体方案的保护主题选择与研发评审。',
        reason: '标题或摘要相近不能直接说明保护范围，需要结合权利要求、专利族及法律状态复核。',
        evidenceGap: '缺少重点专利全文、法律状态与拟采用方案的要素对照。', attentionLevel: '优先关注', validationStatus: '待复核', analysis: 'branches', actionIds: ['patent-review'] },
      { id: 'validation-difficulty', title: '复杂工况的验证成本待评估', impact: '影响方案可行性、研发节奏及验证资源安排。',
        reason: '弱纹理、动态障碍与定位异常需要分别定义测试条件，再比较系统表现。',
        evidenceGap: '缺少统一工况、基线方案、测试指标和可重复的验证记录。', attentionLevel: '持续关注', validationStatus: '待技术验证', analysis: 'technology', actionIds: ['technical-validation'] }
    ],
    actions: [
      { id: 'supplementary-search', title: '补充检索', sequence: 1, relationIds: ['robust-localization', 'competition-concentration'],
        materials: '技术关键词、分类号、研究时间与地域边界。', output: '检索式、检索记录及待复核专利清单。',
        completion: '记录检索口径与来源，完成同义词、分类号和申请主体交叉检查。',
        prompt: '针对示例线索“弱纹理融合定位”，请补充检索并核查竞争布局，列出关键词、分类号、检索口径、所需材料与完成判据；尚无数据的判断请标记待验证。', stageId: 'search-review' },
      { id: 'patent-review', title: '重点专利复核', sequence: 2, relationIds: ['landing-assessment', 'patent-coverage'],
        materials: '重点专利全文、专利族、法律状态及拟采用方案说明。', output: '保护主题与方案要素对照表、待确认事项。',
        completion: '每个关注要素可追溯到原文，区分已确认信息与待进一步核查事项。',
        prompt: '针对示例线索“动态落点安全评估”，请制定重点专利复核清单，比较权利要求、专利族和法律状态，整理与拟采用方案的要素对照、所需材料与完成判据。', stageId: 'search-review' },
      { id: 'technical-validation', title: '技术验证', sequence: 3, relationIds: ['failure-recovery', 'validation-difficulty'],
        materials: '候选方案、异常工况、基线方案与可用测试资源。', output: '验证计划、测试指标和验证记录模板。',
        completion: '明确工况、基线、判定标准及退出条件，结果可以重复检查。',
        prompt: '针对示例线索“异常识别与复飞协同控制”，请拟定技术验证计划，明确复杂工况、对照基线、测试指标、所需材料与完成判据，先保留未验证结论。', stageId: 'compare-validate' },
      { id: 'direction-comparison', title: '方向比较', sequence: 4, relationIds: ['robust-localization', 'landing-assessment', 'failure-recovery', 'competition-concentration'],
        materials: '检索记录、重点专利复核结果及技术验证记录。', output: '候选方向比较表、研究优先级与布局评审材料。',
        completion: '各方向使用相同维度比较，优先级说明依据、约束及调整条件。',
        prompt: '针对示例线索“融合定位、落点安全评估、异常识别与复飞协同控制”，请按证据充分度、相关专利覆盖、技术验证成本和应用价值比较方向，列出所需材料、优先级、推进条件与完成判据。', stageId: 'compare-validate' }
    ],
    priorities: [
      { id: 'robust-localization', level: 'P1', label: '优先研究与验证', title: '弱纹理融合定位', topic: '多源感知协同与近地定位方法。',
        scenario: '视觉纹理不足或光照变化的自主降落。', reason: '研究问题与感知定位路线直接相关，先补检可明确后续验证边界。',
        conditions: '补齐相关专利样本，明确拟采用方案，并具备可重复的弱纹理测试工况。', action: '先执行补充检索，再形成多源融合方案对照与验证任务。', validationStatus: '待补检', opportunityId: 'robust-localization', actionIds: ['supplementary-search', 'direction-comparison'], stageId: 'search-review' },
      { id: 'landing-assessment', level: 'P2', label: '补证后推进', title: '动态落点安全评估', topic: '落点条件评估与路径决策的协同方法。',
        scenario: '存在动态障碍或地面条件变化的降落区域。', reason: '需先厘清相关保护主题及场景约束，再比较拟研究方案。',
        conditions: '完成重点专利要素复核，明确场景与评价指标，并形成候选方案比较。', action: '建立权利要求对照表，安排场景验证后提交布局评审。', validationStatus: '待复核', opportunityId: 'landing-assessment', actionIds: ['patent-review', 'direction-comparison'], stageId: 'compare-validate' },
      { id: 'failure-recovery', level: 'P3', label: '跟踪与储备', title: '异常识别与复飞控制', topic: '异常检测、控制切换及复飞触发策略。',
        scenario: '定位异常、落点失效或降落条件突然变化。', reason: '安全边界与验证资源尚待明确，先建立跟踪与试验任务。',
        conditions: '形成异常工况清单、控制切换测试记录及相关专利检索结果。', action: '建立技术与专利跟踪事项，验证可行性后再调整研究优先级。', validationStatus: '待技术验证', opportunityId: 'failure-recovery', actionIds: ['technical-validation'], stageId: 'review-monitor' }
    ],
    stages: [
      { id: 'search-review', number: 1, title: '补检与复核', time: '阶段一', owner: '待分配',
        actions: ['完善关键词、分类号与检索边界。', '归一化申请主体并核对重点专利全文及法律状态。'],
        deliverables: ['检索记录与证据清单。', '重点专利要素对照表及待确认事项。'],
        gates: ['来源与检索口径可追溯。', '重点专利和证据缺口已标识，可进入候选方案比较。'], actionIds: ['supplementary-search', 'patent-review'] },
      { id: 'compare-validate', number: 2, title: '方案比较与技术验证', time: '阶段二', owner: '待分配',
        actions: ['按同一维度比较三个候选方向。', '开展工况、基线及判定标准明确的技术验证。'],
        deliverables: ['候选方案比较表。', '技术验证记录与方向选择依据。'],
        gates: ['主要证据缺口已有处理结论。', '验证记录支持继续研究、调整方向或暂缓推进的判断。'], actionIds: ['technical-validation', 'direction-comparison'] },
      { id: 'review-monitor', number: 3, title: '布局评审与持续跟踪', time: '阶段三', owner: '待分配',
        actions: ['整理拟研究的保护主题、应用场景及方案差异。', '组织布局评审，并建立后续检索与技术监测事项。'],
        deliverables: ['布局评审材料与阶段行动清单。', '专利、技术变化及责任分配待办。'],
        gates: ['评审记录明确推进事项、所需补证及责任人。', '后续跟踪频率与优先级调整条件已确定。'], actionIds: ['direction-comparison'] }
    ]
  };
  const getExample = () => copy(example);
  const reportSections = (data = getExample()) => [
    { id: 'opportunity-risk', title: '机会与风险', paragraphs: [
      { id: 'opportunity-risk-note', text: '示例主题：' + data.topic + '。以下机会、风险与建议行动均为待验证的研究安排，不代表当前项目的实际分析结果。' },
      ...data.opportunities.map((item) => ({ id: 'opportunity-' + item.id, text: '机会线索：' + item.title + '。研究问题：' + item.question + ' 机会依据：' + item.basis + ' 已有布局：' + item.existingLayout + ' 证据缺口：' + item.evidenceGap + ' 验证状态：' + item.validationStatus + '。' })),
      ...data.risks.map((item) => ({ id: 'risk-' + item.id, text: '风险关注：' + item.title + '（' + item.attentionLevel + '；' + item.validationStatus + '）。影响对象：' + item.impact + ' 关注原因：' + item.reason + ' 证据缺口：' + item.evidenceGap + ' 对应行动：' + data.actions.filter((action) => item.actionIds.includes(action.id)).map((action) => action.title).join('、') + '。' })),
      ...data.actions.map((item) => ({ id: 'action-' + item.id, text: '建议行动 ' + item.sequence + '：' + item.title + '。关联事项：' + item.relationIds.map((id) => [...data.opportunities, ...data.risks].find((relation) => relation.id === id)?.title).filter(Boolean).join('、') + '。所需材料：' + item.materials + ' 预期产出：' + item.output + ' 完成判据：' + item.completion }))
    ] },
    { id: 'patent-layout', title: '专利布局建议', paragraphs: [
      { id: 'patent-layout-note', text: '示例主题：' + data.topic + '。P1、P2、P3 表示建议研究顺序，均待证据与技术验证后评审，不代表已确认的专利空白或申请结论。' },
      ...data.priorities.map((item) => ({ id: 'priority-' + item.id, text: item.level + ' ' + item.label + '：' + item.title + '（' + item.validationStatus + '）。拟研究保护主题：' + item.topic + ' 目标场景：' + item.scenario + ' 排序理由：' + item.reason + ' 推进条件：' + item.conditions + ' 具体行动：' + item.action }))
    ] },
    { id: 'implementation-roadmap', title: '实施路线图', paragraphs: [
      { id: 'implementation-roadmap-note', text: '示例主题：' + data.topic + '。路线图以相对阶段组织，具体时间与责任分配在评审后确认；各阶段均待验证。' },
      ...data.stages.map((item) => ({ id: 'stage-' + item.id, text: item.time + '：' + item.title + '。责任人：' + item.owner + '。具体行动：' + item.actions.join(' ') + ' 交付物：' + item.deliverables.join(' ') + ' 进入下一阶段或完成条件：' + item.gates.join(' ') }))
    ] }
  ];

  const node = (doc, tag, className, text) => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  };
  const button = (doc, text, callback, className = 'btn btn-small btn-outline') => {
    const element = node(doc, 'button', className, text);
    element.type = 'button';
    element.addEventListener('click', callback);
    return element;
  };
  const facts = (doc, entries, className = 'insight-facts') => {
    const list = node(doc, 'dl', className);
    entries.forEach(([label, value]) => { list.append(node(doc, 'dt', '', label), node(doc, 'dd', '', value)); });
    return list;
  };
  const note = (doc, data) => node(doc, 'p', 'decision-example-note', '示例主题：' + data.topic + ' · 待验证。以下内容展示研究线索与行动安排，不代表当前项目的分析结果。');
  const navigate = (options, target) => () => options.onNavigate?.(target);
  const analysisTarget = (analysis) => ({ view: 'workbench', tab: 'analysis', analysis });
  const detailTarget = (id) => ({ view: 'workbench', tab: 'analysis', analysis: 'opportunity', anchor: 'opportunity-detail-' + id });
  const layoutTarget = (id) => ({ view: 'workbench', tab: 'layout', anchor: 'layout-priority-' + id });
  const relationTarget = (data, id) => data.opportunities.some((item) => item.id === id) ? detailTarget(id) : { view: 'workbench', tab: 'overview', anchor: 'overview-risk-' + id };
  const actionTarget = (id) => ({ view: 'workbench', tab: 'overview', anchor: 'overview-action-' + id });
  const stageTarget = (id) => ({ view: 'workbench', tab: 'layout', anchor: 'layout-stage-' + id });
  const relatedActions = (doc, data, actionIds, options) => {
    const links = node(doc, 'div', 'action-relations');
    links.append(node(doc, 'span', 'small muted', '对应行动'));
    actionIds.forEach((id) => { const action = data.actions.find((item) => item.id === id); if (action) links.append(button(doc, action.title, navigate(options, actionTarget(id)), 'link-button')); });
    return links;
  };
  const opportunityCard = (doc, data, item, options, detail = false) => {
    const article = node(doc, 'article', detail ? 'opportunity-detail-card opportunity-card' : 'opportunity-card');
    article.id = (detail ? 'opportunity-detail-' : 'overview-opportunity-') + item.id;
    article.dataset.insightId = item.id;
    const head = node(doc, 'div', 'insight-card-head');
    head.append(node(doc, detail ? 'h3' : 'h4', '', item.title), node(doc, 'span', 'insight-status', item.validationStatus));
    article.append(head, node(doc, 'p', 'insight-question', item.question), facts(doc, [['机会依据', item.basis], ['已有布局', item.existingLayout], ['尚缺证据', item.evidenceGap]]));
    const links = node(doc, 'div', 'insight-links');
    links.append(button(doc, '查看相关分析', navigate(options, analysisTarget(item.analysis))), button(doc, '查看布局建议', navigate(options, layoutTarget(item.priorityId))));
    if (!detail) links.append(button(doc, '查看线索详情', navigate(options, detailTarget(item.id)), 'link-button'));
    article.append(relatedActions(doc, data, item.actionIds, options), links);
    return article;
  };
  const renderOverview = (container, options = {}) => {
    const data = getExample(), doc = container.ownerDocument;
    container.replaceChildren(note(doc, data));
    const grid = node(doc, 'div', 'decision-overview-grid');
    const opportunities = node(doc, 'section', 'insight-panel');
    const opportunityHead = node(doc, 'div', 'insight-panel-head');
    opportunityHead.append(node(doc, 'h2', '', '机会线索'), node(doc, 'p', 'muted', '哪些方向值得进一步研究？'));
    opportunities.append(opportunityHead, node(doc, 'p', 'small muted', '以下是值得进一步验证的机会，先补齐证据，再判断研究价值。'));
    const opportunityList = node(doc, 'div', 'insight-list');
    data.opportunities.forEach((item) => opportunityList.append(opportunityCard(doc, data, item, options)));
    opportunities.append(opportunityList);
    const risks = node(doc, 'section', 'insight-panel');
    const riskHead = node(doc, 'div', 'insight-panel-head');
    riskHead.append(node(doc, 'h2', '', '风险关注'), node(doc, 'p', 'muted', '哪些因素可能影响进入或研发？'));
    risks.append(riskHead, node(doc, 'p', 'small muted', '关注级别表示复核顺序；验证状态表示当前证据是否充分。'));
    const riskList = node(doc, 'div', 'insight-list');
    data.risks.forEach((item) => {
      const article = node(doc, 'article', 'risk-card');
      article.id = 'overview-risk-' + item.id;
      article.dataset.insightId = item.id;
      const head = node(doc, 'div', 'insight-card-head');
      head.append(node(doc, 'h4', '', item.title));
      const badges = node(doc, 'div', 'insight-badges');
      badges.append(node(doc, 'span', 'risk-attention', item.attentionLevel), node(doc, 'span', 'insight-status', item.validationStatus));
      article.append(head, badges, facts(doc, [['影响对象', item.impact], ['关注原因', item.reason], ['证据缺口', item.evidenceGap]]), relatedActions(doc, data, item.actionIds, options));
      const links = node(doc, 'div', 'insight-links');
      links.append(button(doc, '查看相关分析', navigate(options, analysisTarget(item.analysis))));
      article.append(links);
      riskList.append(article);
    });
    risks.append(riskList);
    grid.append(opportunities, risks);
    container.append(grid);
    const actions = node(doc, 'section', 'insight-panel decision-actions');
    const actionHead = node(doc, 'div', 'insight-panel-head');
    actionHead.append(node(doc, 'h2', '', '建议行动'), node(doc, 'p', 'muted', '接下来应该做什么？'));
    actions.append(actionHead);
    const actionList = node(doc, 'div', 'action-plan-list');
    data.actions.forEach((item) => {
      const article = node(doc, 'article', 'action-plan-card');
      article.id = 'overview-action-' + item.id;
      article.dataset.actionId = item.id;
      const head = node(doc, 'div', 'insight-card-head');
      head.append(node(doc, 'span', 'action-sequence', String(item.sequence).padStart(2, '0')), node(doc, 'h3', '', item.title));
      const relations = node(doc, 'div', 'action-relations');
      relations.append(node(doc, 'span', 'small muted', '关联事项'));
      item.relationIds.forEach((id) => {
        const relation = [...data.opportunities, ...data.risks].find((entry) => entry.id === id);
        relations.append(button(doc, relation.title, navigate(options, relationTarget(data, id)), 'link-button'));
      });
      article.append(head, relations, facts(doc, [['所需材料', item.materials], ['预期产出', item.output], ['完成判据', item.completion]]));
      const links = node(doc, 'div', 'insight-links');
      links.append(button(doc, '带入对话', () => options.onPrompt?.(item.prompt)), button(doc, '查看阶段安排', navigate(options, stageTarget(item.stageId)), 'link-button'));
      article.append(links);
      actionList.append(article);
    });
    actions.append(actionList);
    container.append(actions);
  };
  const renderLayout = (container, options = {}) => {
    const data = getExample(), doc = container.ownerDocument;
    container.replaceChildren(note(doc, data));
    const priorities = node(doc, 'section', 'insight-panel');
    const heading = node(doc, 'div', 'insight-panel-head');
    heading.append(node(doc, 'h2', '', '布局优先级'), node(doc, 'p', 'muted', '先明确研究顺序，再依据补证与验证结果调整。'));
    priorities.append(heading);
    const list = node(doc, 'div', 'layout-priority-list');
    data.priorities.forEach((item) => {
      const article = node(doc, 'article', 'layout-priority-card');
      article.id = 'layout-priority-' + item.id;
      article.dataset.priorityId = item.id;
      const head = node(doc, 'div', 'insight-card-head');
      head.append(node(doc, 'span', 'priority-badge level-' + item.level.toLowerCase(), item.level + ' · ' + item.label), node(doc, 'span', 'insight-status', item.validationStatus));
      article.append(head, node(doc, 'h3', '', item.title), facts(doc, [['拟研究保护主题', item.topic], ['目标场景', item.scenario], ['排序理由', item.reason], ['推进条件', item.conditions], ['具体行动', item.action]]));
      const links = node(doc, 'div', 'insight-links');
      links.append(button(doc, '查看机会依据', navigate(options, detailTarget(item.opportunityId))), button(doc, '查看阶段安排', navigate(options, stageTarget(item.stageId)), 'link-button'));
      article.append(relatedActions(doc, data, item.actionIds, options), links);
      list.append(article);
    });
    priorities.append(list);
    const roadmap = node(doc, 'section', 'insight-panel');
    const roadmapHead = node(doc, 'div', 'insight-panel-head');
    roadmapHead.append(node(doc, 'h2', '', '阶段路线图'), node(doc, 'p', 'muted', '按交付物与推进条件组织研究，不预设尚未验证的结论。'));
    roadmap.append(roadmapHead);
    const stages = node(doc, 'ol', 'layout-roadmap');
    data.stages.forEach((item) => {
      const article = node(doc, 'li', 'roadmap-stage');
      article.id = 'layout-stage-' + item.id;
      article.dataset.stageId = item.id;
      const head = node(doc, 'div', 'roadmap-stage-head');
      head.append(node(doc, 'span', 'action-sequence', String(item.number).padStart(2, '0')), node(doc, 'h3', '', item.title), node(doc, 'span', 'insight-status', '待验证'));
      article.append(head, facts(doc, [['阶段安排', item.time], ['责任人', item.owner], ['具体行动', item.actions.join(' ')], ['交付物', item.deliverables.join(' ')], ['推进条件', item.gates.join(' ')]], 'insight-facts roadmap-facts'), relatedActions(doc, data, item.actionIds, options));
      stages.append(article);
    });
    roadmap.append(stages);
    container.append(priorities, roadmap);
    if (options.onOpenReport) {
      const links = node(doc, 'div', 'insight-links layout-report-link');
      links.append(button(doc, '前往导航报告', () => options.onOpenReport()));
      container.append(links);
    }
  };
  const renderOpportunities = (container, options = {}) => {
    const data = getExample(), doc = container.ownerDocument;
    container.replaceChildren(note(doc, data));
    const list = node(doc, 'div', 'opportunity-detail-list');
    data.opportunities.forEach((item) => list.append(opportunityCard(doc, data, item, options, true)));
    container.append(list);
  };
  const api = { getExample, reportSections, renderOverview, renderLayout, renderOpportunities };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PatentDecisionInsights = api;
})(globalThis);
