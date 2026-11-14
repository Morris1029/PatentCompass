---
name: patent-research-strategy
description: 基于已有统计或语义分析与核验原文补齐候选方向、布局建议、风险、阶段交付物和验收条件，保存可展示的新分析版本；不要求重新完成全量语义分类。
---

# 候选方向与布局路线

用于“完善候选方向、布局与路线图、补齐研究结果”等请求。沿用已有范围、数据与统计，用原文形成研究建议，不把缺少法律状态等同于无法提出核查计划。

1. 内置宿主通过 `prepare_agent_action` 选择 action=`analyze`、semantic_mode=`off`、research_intent=`continue`，完成原系统确认。这里不重新运行统计或全量分类；直接保留已有分析。
2. 读取 `get_agent_context` 和 `read_analysis` 的 overview、technology、landscape、opportunities、roadmap。记录当前 research_id、analysis_id、context.revision。已有方案可结合原文修订，不能只复述“数据不足”。
3. 用 `search_patents` 在固定 dataset_version_ids 和 scope 内检索相关原文，必要时 `get_patent`；每次只读少量记录，多种方法／场景分开检索，不把样本支持数当成全行业结论。
4. 依照 [strategy.md](references/strategy.md) 组织 2–4 个互有区别的方向。每项包括标题、研究假设、逐字 evidence（真实 application_id 与 quote）、风险、推进条件，以及至少两阶段的 actions、deliverable、acceptance。
5. 调用 `save_research_strategy`，传 project_id、research_id、expected_revision、analysis_id、当前宿主模型名 agent_model 和 directions。真实方向 basis=`evidence`；缺少材料时可以提出待验证假设。仅用户明确允许模拟时传 allow_mock=true，模拟项 basis=`mock` 且 evidence=[]，明确称为模拟方案；不能伪造专利、指标或检索结果。
6. 跟踪返回的 run_id 至 succeeded，确认新的 analysis_id。工作台会展示候选方向和阶段路线。这个流程不声称已完成全量技术分类；不要覆盖原标题、统计或报告历史。
7. 仅用户要求完善／生成报告时，继续使用报告技能，以新的 analysis_id 生成新报告版本并实际导出。报告冻结方案、风险与引用，模拟内容保留标识。
