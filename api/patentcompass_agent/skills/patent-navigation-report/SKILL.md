---
name: patent-navigation-report
description: 从固定专利分析和证据组织行业专利导航报告并保存项目版本、导出 Word；用于明确的报告交付需求。
---

# 导航报告

1. 明确研究归属和目标。读取 `get_agent_context`，需要独立主题时先 `create_agent_research`，不能自动覆写已有研究。
2. 确认范围和数据。需要语义技术分类时使用语义分析技能；明确只要统计时 `start_agent_task` action=`analyze`，不额外执行模型分类。跟踪任务至 succeeded，重新取得 context.revision 和 analysis_id。
3. 通过 `read_analysis` 读取所需工作台视图。数字采用程序统计，解释基于真实原文和矩阵。报告覆盖范围、技术体系与路线、主体格局、可支持的地域比较、候选方向及分阶段行动；资料不足的章节明确缺口。
4. 当前 Agent 按 [summary.md](references/summary.md) 撰写专业摘要。调用 `start_agent_task`，传 project_id、research_id、expected_revision、action=`generate_report`、analysis_id、expected_current_version_id（研究尚无报告时为 null）、expected_adopted_version_id（get_agent_context 返回的 adopted_report_version_id）和 summary_text。后端依照原模板装配统计、语义章节、图表、证据与摘要，不调用生成模型。
5. 等待 succeeded，从 result 获取 report_id / report_version_id。`get_report_version` 核对实际保存内容与冻结来源。若需要更完整的行业论证，使用修改技能以结构化正文操作补充段落，不捏造产业或法律事实。
6. 导出使用 `start_agent_task` action=`export`，传目标 report_version_id 与最新上下文 revision，等待文件 download_url。历史版可导出，不能用当前图表代替报告已冻结图表。

新研究首次报告沿用现有业务的发布与采用规则；已发布研究的后续版本保留历史。没有报告版本与文件结果时不能声称已生成/导出。独立 `build_report` 仅构建内容；需要保留项目业务关系时使用上述项目任务。
