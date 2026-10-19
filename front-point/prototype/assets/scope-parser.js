(function (root) {
  // Only phrases explicitly stated by the user are copied into the draft scope.
  const extractExplicitScope = (message) => {
    const clauses = message.split(/[，,。；;、\n]/).map((part) => part.trim()).filter(Boolean);
    const negativeIntent = /(?:不打算|不希望|不需要|不想|不准备|不要|不考虑|不研究|不分析|不调研|不看|不使用|不做|不进入|不选择|不关注|不聚焦|不重点关注|暂不|无需)/;
    const positive = clauses.filter((part) => !negativeIntent.test(part) && !/(?:不排除|排除|剔除|不包括|不包含)/.test(part));
    const extracted = {};
    const industries = new Set();
    let ambiguousIndustry = /(?:研究|分析|调研|进入|关注|了解|梳理)[^，,。；;\n]{0,60}、[^，,。；;\n]{0,30}(?:行业|产业|领域|赛道)/.test(message);
    positive.forEach((part) => {
      if (/(?:行业|产业|领域|赛道)(?:和|与|及)/.test(part)) ambiguousIndustry = true;
      const match = part.match(/(?:目标行业|研究行业|行业|研究对象)(?:是|为|：|:)\s*(.{2,30})$/)
        || part.match(/(?:研究|分析|调研|进入)(?:一下|关于|对)?\s*(.{2,30}?)(?:行业|产业|领域|赛道)(?=的|$|\s|和|与|及)/)
        || part.match(/以(.{2,30}?)为(?:研究|分析)对象/)
        || part.match(/(?:行业|领域|研究对象)(?:改成|改为|换成|是|为)\s*(.{2,30})$/)
        || (!/专利|用于|希望|目标/.test(part) && part.match(/(?:研究|分析|调研)\s*(?:一下)?\s*(.{2,30})$/));
      const value = match?.[1]?.replace(/^(?:一下|关于|对)\s*/, '').replace(/(?:19|20)\d{2}.*$/, '').trim();
      if (value && /(?:和|与|及)/.test(value)) ambiguousIndustry = true;
      if (value && !/^(?:这个|该|某个|专利|技术|行业)$/.test(value)) industries.add(value);
    });
    if (!industries.size) {
      positive.forEach((part) => {
        const match = part.match(/(?:关注|了解|梳理)\s*(.{2,30}?)(?:行业|产业|领域|赛道)(?=的|$|\s)/)
          || part.match(/^(.{2,30}?)(?:行业|产业|领域|赛道)$/);
        if (match) {
          if (/(?:和|与|及)/.test(match[1])) ambiguousIndustry = true;
          industries.add(match[1].trim());
        }
      });
    }
    if (industries.size === 1 && !ambiguousIndustry) extracted.industry = [...industries][0];
    if (industries.size > 1 || ambiguousIndustry) extracted.industry = '';

    const matchedGoals = new Set();
    positive.forEach((part) => {
      if (/进入.{0,16}(?:行业|产业|市场)|行业进入|进入新行业/.test(part)) matchedGoals.add('entry');
      if (/研发立项|立项评估|是否立项/.test(part)) matchedGoals.add('rd');
      if (/选择.{0,8}(?:技术方向|技术路线)|(?:技术方向|技术路线)选择/.test(part)) matchedGoals.add('direction');
    });
    if (matchedGoals.size === 1) extracted.goal = [...matchedGoals][0];
    if (matchedGoals.size > 1) extracted.goal = '';

    for (const part of positive) {
      const range = part.match(/((?:19|20)\d{2})\s*年?\s*(?:—|–|-|~|～|至|到)\s*((?:19|20)\d{2})\s*年?/);
      if (range && Number(range[1]) <= Number(range[2])) {
        extracted.start = range[1];
        extracted.end = range[2];
        break;
      }
      const start = part.match(/(?:从|自)\s*((?:19|20)\d{2})\s*年?\s*(?:起|以来)/);
      const end = part.match(/截至\s*((?:19|20)\d{2})\s*年?/);
      if (start) extracted.start = start[1];
      if (end) extracted.end = end[1];
    }

    const typeText = positive.join('；');
    const invention = /发明专利/.test(typeText) || /发明(?:和|与|及|、)实用新型/.test(typeText);
    const utility = /实用新型/.test(typeText);
    if (invention && utility) extracted.type = 'both';
    else if (invention) extracted.type = 'invention';
    else if (utility) extracted.type = 'utility';

    const needs = positive.map((part) => part.match(/(?:希望解决|要解决|解决|应用场景(?:是|为|：|:)|应用需求(?:是|为|：|:))\s*(.{2,50})/)?.[1]?.trim()
      || part.match(/用于(?!研发立项|行业进入|选择技术方向)(.{2,50})/)?.[1]?.trim()).filter(Boolean);
    if (needs.length === 1) extracted.need = needs[0];
    const focuses = clauses.map((part) => {
      if (/(?:不排除|无需排除|不要排除)/.test(part)) return part;
      if (negativeIntent.test(part)) return;
      return part.match(/(?:重点关注|重点看|聚焦|仅看|排除|不包括|剔除).{2,50}/)?.[0];
    }).filter(Boolean);
    if (focuses.length) extracted.focus = focuses.join('；');
    return extracted;
  };

  root.PatentScope = { extract: extractExplicitScope };
})(globalThis);
