---
name: patent-evidence-qa
description: 根据固定专利数据、资料、分析或报告版本检索证据并回答问题；用于研究追问和结果解释，不隐式重算或修改成果。
---

# 证据问答

读取 [qa.md](references/qa.md) 和 [statistics.md](references/statistics.md) 的专业约束。

- 先确定回答依据。报告追问用 `get_report_version` 读取冻结来源；工作分析用 `get_agent_context`、`read_analysis`。保留 scope / dataset / analysis / report version。
- 使用 `search_patents`，传明确 `dataset_version_ids` 与该版本范围，或已授权 records。分页用 offset / limit / next_offset。多个同义词用 queries，不能把整句问题当关键词或把 AND 零命中写成所有词都不存在。
- `get_patent` 核对标题、摘要和公开记录；`search_materials` 检索项目 Word 资料，引用实际段落/单元格定位；`get_evidence` 核对证据。附件不计入专利数量，不虚构页码。
- `read_analysis` 的 view 可为 overview / technology / landscape / opportunities / roadmap / geography。主体与技术交叉结论须读取对应矩阵或原文。
- 命中词项与技术主题归属分别说明；区分直接技术证据和相近场景。采用真实返回的申请号引用。
- 工具不可用、字段缺失、数据不覆盖或检索不足时保留限制。未执行外部补检就不能声称已经验证技术空白。

直接由宿主 Agent 整合回答，无需调用后端聊天模型。证据已经足够时停止检索；预算不足时采用 [budget.md](references/budget.md) 完成有边界的回答。
