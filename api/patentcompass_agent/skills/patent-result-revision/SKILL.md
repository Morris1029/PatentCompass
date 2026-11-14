---
name: patent-result-revision
description: 基于明确版本修改专利报告文字、结构或图表解读，恢复历史并切换采用研究；保留统计、证据和版本关系。
---

# 成果修改

先读取目标研究的 `get_agent_context` 与 `get_report_version`；图表使用 `get_agent_chart`，可以指定 chart_version_id。目标有歧义时只澄清会改变修改对象的信息。

宿主提供 prepare_agent_action 时，准备操作也应包含已明确的 operations / block_ids。仅改标题就提供 set_title 和目标文字，不请求正文选段；只有正文目标不明确时才明确请求 select_report_target。

## 报告

- 按 [rewrite.md](references/rewrite.md) 在当前宿主生成替换文字。选段要求还可参考 [selection.md](references/selection.md)。
- 使用 `start_agent_task` action=`revise_report`，传 project_id / research_id / expected_revision / base_version_id / expected_current_version_id / operations。
- base_version_id 必须是 get_report_version 返回的实际版本 id；expected_current_version_id 仅用于并发校验，不能替代 base_version_id。不要只传 expected_adopted_version_id 或报告容器 report_id。
- operations 使用工具 Schema 定义的操作，如 set_title、replace_text、insert_block、remove_block、move_block、append_roadmap、insert_chart、update_chart、set_layout、set_chart_layout。正文块 ID 必须来自目标报告。replace_text 的 text 是 Agent 已完成的文字。
- 图表插入/更新必须明确 chart_id 和 chart_version_id；后端读取授权冻结快照。修改文字不能冒充重新统计，不能自行修改来源、计数和证据快照。
- 等待任务完成后读取新报告版本。冲突时重新核对版本和用户目标，不覆盖并发改动。

## 图表

- `start_agent_task` action=`revise_chart`，传 chart_id、base_version_id、expected_current_version_id 和 chart_mode。
- style：chart_option 仅提供允许的呈现样式补丁。data：chart_filters 表达用户明确要求的局部筛选，后端重新计算。insight：按 [chart.md](references/chart.md) 基于冻结数据和原文撰写 interpretation，由后端保存。
- 样式和解读修改不改变数据；筛选后旧解读失效。图表新版不自动替换历史报告里的冻结图表，需要明确报告图表更新操作。

## 恢复与采用

- 恢复本研究历史报告：`start_agent_task` action=`restore_report`，提供 report_version_id、expected_current_version_id 和 expected_adopted_version_id（当前采用报告），产生保留历史的新版本。
- 切换当前采用研究：`adopt_agent_research`，提供 research_id 和 expected_current_version_id（当前采用报告，可为 null）。仅采用已经发布的研究。
- 导出指定版本沿用报告技能。所有写工具使用稳定 idempotency_key，不能把恢复解释成删除历史。
