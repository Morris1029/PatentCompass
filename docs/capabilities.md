# 独立专利能力、HTTP 与 MCP

本轮在原仓库内分离可复用计算与项目业务。`api/patentcompass_core/` 不读取应用配置，也不依赖 `app`、数据库、Redis 或 Celery；`api/app/` 负责身份与资源归属、快照装配、任务、文件及版本发布。原前端继续使用已有项目接口。

组件独立调用、HTTP 开放和 MCP 接入共用实现。这里没有为每个工具新建微服务，也没有接入 OpenAI Agents SDK、AgentScope 或 Skills。底层生成统一 Responses，不新增对外 Responses 聊天接口。

## 能力边界

| 组件 | 直接 Python 能力 | 应用层仍负责 |
| --- | --- | --- |
| `parsing`、`documents` | Excel、补充行、Word 正文和表格解析 | 文件上传、散列校验、导入冲突、数据版本及资料保存 |
| `retrieval` | 字段与关键词过滤、余弦排序、RRF 混合排序 | 已授权数据装载、数据库和 pgvector 查询、向量索引保存 |
| `statistics` | 申请、公开、年份、申请人、IPC 和来源国家统计 | 分析、图表、证据入库及当前工作分析更新 |
| `semantics` | 逐条抽取、原文核验、主题归并、归属及本地候选方向 | 模型配置、可复用缓存、持久化任务与研究上下文 |
| `reports`、`charts` | 报告内容、段落操作、图表配置及样式 | 报告和图表版本归属、并发检查、发布与采用 |
| `rendering` | 图表 PNG、结构化报告 Word 导出 | 运行环境、产物存储、文件引用与下载权限 |
| `generation` | 显式配置的 Responses 与 Embedding 适配器 | 从环境装配配置、密钥及必要任务记录 |

核心接口接受普通字典或 `PatentRecord`。记录标识缺失时按原文及编号生成稳定标识；同一次应用能力请求不允许重复标识。统计以申请记录为单位：共同申请人和多 IPC 分别全计数，类别合计可能超过申请总量。

语义分类使用标题和摘要调用生成模型，不依赖 Embedding。引用必须对应原文，低置信度或无法核验的标签进入待复核；候选方向只根据样本内方法与场景组合整理，外部补检仍未执行。语义检索与混合检索使用另一条 Embedding 通道，返回候选数量不能替代范围总数。

## 直接 Python 调用

从项目根目录运行独立示例，不需要启动 API 或任务服务，也不调用外部模型：

```powershell
uv run --project api --frozen python examples/capabilities.py
uv run --project api --frozen python examples/capabilities.py --workbook database/patent-demo.xlsx --query landing
```

第二条命令使用仓库内工作簿，也可以换成自己的文件。未提供文件时使用明确标记的两条合成记录，只展示组件调用，不代表真实专利结果。原文件只读。

在 `api/` 目录或将其加入 Python 模块路径后，可以直接导入：

```python
from patentcompass_core.parsing import parse_workbook
from patentcompass_core.retrieval import search_patents
from patentcompass_core.statistics import compute_statistics

parsed = parse_workbook("patents.xlsx")
if parsed["quality"]["status"] != "ready":
    raise ValueError(parsed["quality"]["errors"])
analysis = compute_statistics(parsed["applications"])
found = search_patents(parsed["applications"], {"q": "landing", "mode": "keywords"})
```

`compute_statistics(records, publications=None, versions=None)` 返回 `summary`、`patent_ids`、`selections` 和代表记录。没有单独提供公开记录时，从每条记录的 `publications` 读取。日期和来源位置沿用输入。

`classify_patents(records, generate_callable, hooks=None)` 返回抽取、主题体系、归属、候选方向及覆盖数量。`generate_callable` 接受消息列表并返回生成结果；可使用 `ResponsesGateway.chat`。`hooks` 可接入步骤、取消和进度；不提供时直接执行，不承诺进程崩溃恢复。

分类结果还包含原始记录指纹。`semantics.analysis_draft(records, classification)` 将它转换为统一的统计、工作台视图、图表和证据；项目分析保存和独立报告使用同一构建逻辑。记录、归属或引文与原输入不一致时明确拒绝，不静默丢弃分类。

```python
from patentcompass_core.generation import GenerationConfig, ResponsesGateway
from patentcompass_core.semantics import classify_patents

# 配置由调用方提供，不从 app 或本地 .env 隐式读取。
gateway = ResponsesGateway(GenerationConfig(base_url=base_url, api_key=api_key, model=model))
classified = classify_patents(records, gateway.chat)
```

Word `.docx` 解析不要求 LibreOffice；旧 `.doc` 转换需要 LibreOffice。图表与含图 Word 渲染需要 Node、ECharts、Sharp 和明确的渲染器路径。缺少依赖时返回明确错误，不生成占位成功产物。

## HTTP 调用

原项目接口保留。新增入口为：

| 方法与路径 | 内容 |
| --- | --- |
| `GET /api/capabilities` | 目录、描述、输入 JSON Schema、同步或任务执行方式 |
| `POST /api/capabilities/{name}/invoke` | 直接 JSON 参数调用能力 |
| `POST /api/capabilities/files` | 上传文件，取得解析使用的 `file_id` |

能力参数以目录返回的 Schema 为准。专利输入在 `records` 与 `dataset_version_ids` 中必须且只能选一种；前者无需项目，后者检查数据版本归属和就绪状态。`scope` 继续限定输入范围，检索的 `filters` 在范围内进一步筛选。

例如在 PowerShell 调用同步统计：

```powershell
$capabilityInput = @{
    records = @(@{
        id = "example-1"
        application_number = "EXAMPLE-1"
        application_date = "2024-01-01"
        title = "Example landing"
        abstract = "Example camera landing method."
        applicants = @("Example A")
    })
}
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/capabilities/compute_statistics/invoke" -ContentType "application/json" -Body ($capabilityInput | ConvertTo-Json -Depth 20)
```

同步返回结构化结果。解析、全量分类、建索引、报告构建/改写和导出等耗时操作返回 `202` 以及 `run_id/status`，提交时必须提供 `Idempotency-Key`。同一操作、标识与参数重复提交复用结果；相同标识对应不同参数会报冲突。不要在重试同一次提交时换标识。

上传使用 multipart 字段 `file`。解析能力仅接受已上传的 `file_id`，不会接受任意服务器路径。下载使用结果提供的 `file_id` 或 `download_url`。上传文件和导出产物均保留在既有文件服务中。

## MCP 调用

MCP 使用 Streamable HTTP，连接地址为 `http://127.0.0.1:8000/mcp/`。由支持该协议的客户端初始化连接并发现工具。工具目录来自同一能力目录，参数结构统一为 `request`；耗时工具另需 `idempotency_key`。

以下分别是 `compute_statistics` 和 `classify_patents` 工具的调用参数：

```json
{"request":{"records":[{"id":"example-1","application_date":"2024-01-01","title":"Example landing","abstract":"Example camera landing method."}]}}
```

```json
{"request":{"records":[{"id":"example-1","title":"Example landing","abstract":"Example camera landing method."}]},"idempotency_key":"classification-example-1"}
```

MCP 不传文件内容或服务器路径来替代文件上传。先通过 HTTP 上传文件，再将 `file_id` 交给解析工具。

MCP 另提供 `get_run`、`get_run_events`、`cancel_run`、`retry_run`。参数中的 `run_id` 与 HTTP 返回值相同。工具结果包含可读取文本与结构化结果；失败有明确错误码和 `isError`，不能仅凭协议请求成功判断业务成功。

## 任务与版本约定

- HTTP 与 MCP 共用 `ResearchRun`、`RunStep`、`RunEvent`，复用现有 worker、取消、租约与恢复机制。
- 任务状态通过 `GET /api/runs/{id}` 或 MCP `get_run` 读取。事件通过原 SSE 或 `get_run_events` 按序号补读。
- 取消是协作式请求；请求已接受不等于正在进行的模型 HTTP 请求立即结束。以最终任务状态为准。
- 重试创建关联运行并复用兼容检查点，原任务和已完成结果保留。
- 若原项目任务返回 `waiting_input`，使用其 `input_request_id` 和期望版本通过原 `POST /api/runs/{id}/inputs` 确认继续。
- 独立 `build_report`、`revise_report_content` 返回报告内容，`export_docx` 返回文件；它们不会自动创建研究轮次、发布或采用项目报告。项目发布继续使用原业务接口及版本校验。
- 旧步骤键、可兼容结果及统计口径保留，不重写历史报告或清空原有语义缓存。
- 将 `classify_patents` 任务的完整 `result` 作为 `build_report` 的 `analysis`，并同时传入相同的 `records` 或同一数据版本及范围，可以生成含语义章节、候选方向、路线图、图表和证据的报告。缺省 `analysis` 时生成包含三张统计图的统计简报。
- 独立报告图表插入和更新通过 `charts` 提供完整冻结快照。存在同一图表的多个版本时，操作必须指定 `chart_version_id`；不能根据列表先后猜测目标。
- 模型任务保存提交时的配置指纹，模型或参数改变后明确返回 `MODEL_CONFIGURATION_CHANGED`，应重新提交。已完成历史结果和原有业务检查点不被批量清空。

## 模型与验证

所有新发起的规划、问答、语义处理、摘要及改写生成请求走 `/responses`。向量生成独立走 `/embeddings`。原 `app.model_provider.chat/stream_chat` 名称作为内部兼容入口保留，其实现同样使用 Responses；不会隐藏回退到 Chat Completions。

配置仍使用 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL/LLM_EXTRA_BODY` 与原 Embedding 变量。供应商必须支持所用 Responses 功能；流式文本、完整输出、工具调用和返回结果续接需要实际联调。后端自行保存必要续接项，固定 `store=false`，不依赖服务端会话；不完整、拒绝和失败响应不会当作成功回答。

本轮源码、受控测试和真实服务验收分别记录。单元测试通过不代表真实供应商、MCP 客户端或 Docker worker 已经验证；真实模型请求可能产生费用。不得将本轮协议迁移前的历史验收记录当作 Responses 已通过的证据。

本轮已完成的验证和本地证据见 [组件化与 Responses 验收记录](plans/2026-10-26-capability-components-verification.md)。
