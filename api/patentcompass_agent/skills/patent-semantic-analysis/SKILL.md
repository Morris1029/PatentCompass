---
name: patent-semantic-analysis
description: 由宿主模型逐条抽取专利基础要素、技术方法和应用场景并归并主题，提交后端核验和持久化；用于全量语义分析与可恢复批处理。
---

# 外部语义分析

专业推理由当前 Agent 完成。使用 `start_semantic_analysis` 等外部工具，不使用后端 `classify_patents` 代替推理。

1. `get_agent_context` 取得确认范围、数据版本和 revision。以 project_id、可选 research_id、expected_revision、agent_model（实际宿主模型声明）、skill_version=`1` 开始分析。记录 run_id / batch_count。未确认范围或未绑定数据时先完成研究规划。
2. 对每个 batch_index 从 `get_semantic_batch` 读取冻结标题、摘要和本批短 ID。已有 completed_batches 时只继续缺少的批次。以回执中的 next_batch_index 选择下一批；batch_status=completed 的批次不再提取或改写提交。固定 run_id 和范围不变时无需逐批重复读取项目上下文。
3. 按 [extraction.md](references/extraction.md) 分析全部记录。`submit_semantic_batch` 的 request 为 `{run_id,batch_index,records}`；每条为 `{id,elements,methods,scenarios}`，标签为 `{label,field,quote,confidence}`。每个短 ID 恰好一次，三维度都提供；无依据用空数组。短 ID 仅在本批有效，不跨批复用结果。
4. 读取提交返回的 review 原因。后端排除无法逐字核验或低置信度的标签并保留待复核状态，不能将待复核说成已确认。已提交检查点不可覆盖；需要重做时保留旧任务，重新开始分析。网络重试沿用原 idempotency_key。
5. 全部原文批次完成后，分别对 elements / methods / scenarios 调用 `get_semantic_taxonomy`。按 [taxonomy.md](references/taxonomy.md) 归并。`submit_semantic_taxonomy` 传 `{run_id,dimension,themes:[{label,members:[短ID]}]}`；本维度全部短 ID 恰好各用一次，最多16主题。没有标签的维度提交空 themes。
6. `finish_semantic_analysis` 提交完成；通过 `get_run` 等待 succeeded。后端生成主题 ID、映射、归属、统计、证据、图表、候选线索与路线图并回写项目/研究。后台阶段失败时读取 error，只有可恢复失败才使用 `retry_run`；上下文或原文改变时重新读取并开始新任务。

不要自己计算统计、拼接资源 UUID、绕过引文校验，或把分批子集当作全量。取消用 `cancel_run`，取消后的任务不再接受标签。最终交付包含实际总量、已分类/待复核数量和数据范围；低覆盖方向仍是待验证线索。
