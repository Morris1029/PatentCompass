# PatentCompass Agent / Skills / MCP 接入

专业推理由宿主 Agent 完成，后端保留项目、范围、数据、确定性统计、原文校验、任务、图表、报告和导出。原前端及原模型业务入口保留，专业 Prompt 共用 `api/patentcompass_agent/skills/*/references/`；修改指令后原 API / worker 重新读取。部署代码中同样需要保留 `patentcompass_agent` 包。

原系统的新对话同样使用内置 `agent_runtime`：读取完整 SYSTEM_PROMPT.md、发现技能目录，模型通过 load_agent_skill / read_agent_reference 按需加载后自行选择业务工具。`tool_service` 为内置运行层与 MCP 提供同一套 Pydantic 参数契约、校验和业务分发。模型协议适配会展开 JSON Schema 引用，兼容不识别嵌套引用的供应商，不改变字段或后端验证规则。

BUILTIN_RUNTIME.md 仅说明原系统宿主的确认、固定上下文和子任务调度方式；外部平台继续使用自己的宿主机制。内置对话沿用原范围确认、研究归属和执行确认，专业判断由同一宿主模型完成，确定性业务由原脚本执行。模型响应与待执行工具保存在原运行记录中，确认后不重新推理；重试保留兼容进度，取消父任务会请求取消尚未完成的子任务。

新模型对话默认启用统一运行层；已有历史运行继续使用原执行路径恢复。没有生成模型时，显式 action=analyze / generate_report 且为统计模式的请求仍能使用原确定性功能；自然语言 Agent 对话需要生成模型配置。

原语义缓存继续复用完全相同的旧指令；修改抽取/归并 Prompt 后自动使用新的正文指纹，不需要编辑 Python 版本常量，也不会清空历史缓存。独立模型任务同样记录指令指纹，配置或指令改变时需重新提交。外部 Agent 任务记录宿主声明的模型和 skill_version，重试不能更换已冻结的结果。

## 文件和运行依赖

- `api/patentcompass_agent/SYSTEM_PROMPT.md`：可迁移主 Agent 指令。
- `api/patentcompass_agent/skills/`：六项业务技能及专业 Prompt。
- `scripts/package-agent.py`：生成独立迁移 ZIP，默认 `storage/patentcompass-agent.zip`。
- 后端沿用 `scripts/start-dev.ps1`。API 的 MCP 地址是 `http://127.0.0.1:8000/mcp/`，Streamable HTTP。

从项目根目录打包：

```powershell
.\api\.venv\Scripts\python.exe scripts/package-agent.py
```

外部 Agent 路径的生成模型来自宿主。后端可以不配置 LLM；使用关键词检索、外部语义分析、统计报告和结构化修改均不需要后端生成模型。语义/混合检索仍需要后端 Embedding 配置。原 `classify_patents`、独立 `report_mode=ai` 和自然语言 instruction 改写保留为兼容入口，仍会调用后端生成模型，迁移流程不使用这些入口代替宿主推理。

## Codex

1. 将迁移包解压到一个独立业务工作区。把 `skills/` 的五个目录复制到工作区 `.agents/skills/`，保留每个技能的 references。
2. 在该业务工作区的 `AGENTS.md` 中引用实际位置的 `SYSTEM_PROMPT.md`，或合并其中的业务指令。包内 `codex/AGENTS.md` 是路径示例，不能原样放到不同层级后忽略相对路径。不要覆盖已有工程约束。
3. 将包内 `codex/config.toml` 的 MCP 配置合并到受信任工作区 `.codex/config.toml` 或用户 MCP 配置；也可使用宿主 MCP 设置入口。配置地址必须对实际执行环境可访问。
4. 在新会话确认技能发现和工具发现，先读 `list_projects` / `list_datasets` 再开展业务。

Codex 的 AGENTS.md 是工作区指令，不是替换平台自身的最高级系统指令。主 Agent 文件提供相同的业务角色、路由和约束。

官方文档：[Skills](https://learn.chatgpt.com/docs/build-skills)、[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

## WorkBuddy

1. 创建专利导航专家，将 SYSTEM_PROMPT.md 的业务角色、流程和约束放入专家提示词。
2. 导入五个技能目录（各自含 SKILL.md 和 references），或根据当前客户端的技能导入方式分别打包安装。
3. 添加自定义 MCP 连接器，使用服务地址。包内 `workbuddy/mcp.json` 为 Streamable HTTP 配置示例，字段和安装位置以当前客户端配置入口为准。
4. 将专家、技能与连接器用于同一业务任务，先发现工具并读取项目上下文。无需把数据库凭证或后端模型密钥写入专家和技能。

官方文档：[专家](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Expert-Center)、[技能](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)、[连接器](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector)。

这两个平台共用业务 Prompt、Skills、MCP 工具和后端数据；平台安装界面、模型能力与输出形式不同，不能承诺逐字相同的报告。远端/云端宿主的 127.0.0.1 指向其自身，需要配置实际可达地址。当前后端仍使用原 local-dev 身份；本改造不新增公网登录和多租户认证，不直接将本地开发服务当作公网服务发布。

## 外部推理业务工具

通过 MCP list_tools 读取实时 JSON Schema。新写工具参数为 `request` 与 `idempotency_key`，只读工具只有 `request`。同次请求重试沿用相同 key；修改参数必须使用新 key。不能仅凭 MCP 协议成功判断业务成功，还需检查 isError 与返回错误码。

| 工具 | 用途 |
| --- | --- |
| list_projects / list_datasets | 分页发现授权项目和数据版本 |
| get_agent_context | 读取项目或明确研究的 scope、数据、analysis、revision 和研究目录 |
| create_agent_project / update_agent_scope / bind_agent_dataset | 创建项目、确认范围、绑定就绪数据 |
| create_agent_research / adopt_agent_research | 明确新研究或切换采用项，保留历史 |
| start_semantic_analysis | 冻结原文、范围、数据和宿主模型/技能声明 |
| get_semantic_batch / submit_semantic_batch | 按批次读取和提交语义结果，保留检查点 |
| get_semantic_taxonomy / submit_semantic_taxonomy | 按维度归并全部已核验标签 |
| finish_semantic_analysis | 排队由原脚本汇总分析、统计、图表、证据及候选路线 |
| get_agent_chart | 读取冻结图表版本 |
| start_agent_task | 原业务脚本执行统计、报告生成/修改/恢复、图表修改、Word 导出 |

原 16 个能力工具及 get_run / get_run_events / cancel_run / retry_run 保留。search_patents、read_analysis、get_evidence、get_report_version 等仍用于数据读取。

另有 list_agent_skills / load_agent_skill / read_agent_reference 三项只读技能工具，合计 40 项 MCP 工具。内置宿主另提供 prepare_agent_action 适配原确认界面；这不是另一套业务实现，实际分析、检索、报告、图表和导出仍调用共用工具服务。

### 最小请求示例

```json
{"request":{"project_id":"实际项目UUID","expected_revision":0,"agent_model":"实际宿主模型名","skill_version":"1"},"idempotency_key":"本轮固定开始标识"}
```

上述示例调用 start_semantic_analysis；已有研究时同时传 research_id。返回 run_id 后从 batch_index=0 读取 get_semantic_batch；逐批提交全部短ID，再对三个维度提交主题，最后 finish。每批短ID局部有效。

```json
{"request":{"run_id":"实际任务UUID","batch_index":0,"records":[{"id":"p0","elements":[],"methods":[],"scenarios":[]}]},"idempotency_key":"本轮批次0固定标识"}
```

只有本批恰好一条记录且证据不足时才适用此空标签示例，不能用它替代实际分析。标签结构为 label / field / quote / confidence；具体业务与边界见语义 Skill。

报告生成用 start_agent_task：action=generate_report，显式传 research_id、analysis_id、expected_revision、expected_current_version_id 和 expected_adopted_version_id（get_agent_context 顶层的 adopted_report_version_id）；Agent 可提供 summary_text。恢复报告同样要求预期采用版本。后续深度章节用 revise_report 的 operations 添加，保留程序统计和冻结依据。新研究先 create_agent_research；已保存研究不会因普通新对话被自动覆盖。

### 文件与任务

- Excel 导入、Word 资料上传继续使用原 HTTP 文件/导入接口；二进制内容不塞进工具上下文。可用宿主 HTTP/文件能力调用原 API，解析工具接收已上传的 file_id，不接受任意服务器路径。已有数据直接通过 MCP 数据目录选择。
- start_agent_task 和 finish 返回 run_id 后，使用 get_run 等待 succeeded；queued/running 均未完成。批次阶段为 waiting_input + agent_semantics，用对应批次工具恢复，不通过旧 POST /runs/{id}/inputs 提交。
- 待复核标签保留原因，不算作已确认归属。缺项、额外/重复ID、跨维度短ID明确拒绝；批次保存后不能覆盖。原文或研究上下文变化时保留旧任务，重新开始。
- cancel_run 取消批次阶段或请求后台取消；后台失败可 retry_run 并复用兼容检查点。错误来自范围/数据变化时重新读取上下文，而非反复重试旧快照。
- 文件结果 download_url 是相对 API 地址；根据连接的服务基础地址构造可打开链接。原前端也会显示同一份保存成果。

## 验收边界

### 原系统实际对话入口

`scripts/builtin-agent-smoke-test.py` 使用原前端同源地址（默认 3000）的项目、消息、确认、事件和文件接口，实际调用配置的 Responses 模型及 worker，不通过 MCP 绕过消息入口，也不使用模型替身。

```powershell
.\api\.venv\Scripts\python.exe scripts/builtin-agent-smoke-test.py --dataset-version "<dataset-version-uuid>"
```

将占位符替换为明确选择的就绪小样本版本。默认最多5条，创建独立验收项目，并固定范围、数据和各阶段允许的操作。脚本要求分析、问答、报告、修改、导出分别完成；检查真实主 Prompt / 技能 / 工具轨迹、标题修改不改变正文和证据、实际 Word 的标题与图表。结果和失败记录保存在 storage/acceptance/builtin-agent-*，不会因为某次模型回答“已完成”就判为成功。

需要恢复中断时使用 --resume 指向原 result.json；只有已失败的任务才调用原 retry 接口。修复后确需更换失败阶段的新请求时，使用 --restart-phase，原尝试完整保留。不得为了通过验收放宽范围、目标或跳过实际产物校验。

2026-11-14 已从 3000 原消息入口完成真实模型五阶段闭环，证据在 `storage/acceptance/builtin-agent-20261114T144136Z-1e60be3f/result.json`：3条明确标记的合成样本，实际 Word 含6张冻结图表。该记录保留开发期间发现和修复的失败尝试；它是运行与业务闭环验收，不代表真实行业报告质量或外部客户端安装验收。

### 外部平台

自动测试使用独立 PostgreSQL 测试库，禁止后端生成模型调用，验证外部结果提交到原业务版本闭环；MCP 协议测试覆盖工具发现、Schema、错误和实际调用。它们证明协议和业务可运行，不等于两个客户端的安装界面已经实测。实际接入时各执行一次同数据/同范围的“语义分析 → 报告 → 修改 → 指定版导出”，比较计数、覆盖、证据、版本关系与文件，不比较模型措辞。

## 候选方向与阶段方案

`patent-research-strategy` 使用固定分析和核验原文形成研究建议，通过共享 `save_research_strategy` 保存新分析版本。原统计、图表、报告历史保留，工作台与报告共用方案；这不代表已完成全量语义分类。支持明确授权的 mock 方案，模拟项不带真实证据，支持数为零，并在页面与报告标注。

需要细化已有语义主题时，`start_semantic_analysis` 可传 `reuse_run_id`，受控复用同一用户、项目、范围及未变化原文的已完成抽取任务。新任务保留原引文和待复核原因，只重新提交三个维度主题；分析来源记录原抽取任务与模型。通过 `get_run(view="progress")` 轮询可省略检查点原文，保留真实状态、产物和批次进度，默认完整视图仍可读取。
