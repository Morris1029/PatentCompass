# PatentCompass API

独立 FastAPI 后端，提供专利样本导入、项目与研究范围、对话历史、持久化任务、统计分析、图表与证据、报告版本以及 Word 导出。当前不接入前端，也不连接外部专利数据库。

## 开发环境

本机准备 Docker Desktop（Linux containers）、Python 3.11、uv。Node.js 24 只在本地单独验证图表渲染时需要；正常导出由 Docker worker 内置的 Node.js 执行。

| 服务 | 运行位置 | 本机访问地址 |
| --- | --- | --- |
| FastAPI | `api/.venv` | `http://127.0.0.1:8000` |
| PostgreSQL 17＋pgvector | Docker | `127.0.0.1:15432` |
| Redis 7 | Docker | `127.0.0.1:16379` |
| Celery worker | Linux Docker | 通过 Redis 接收任务，无 HTTP 端口 |

Compose 工程名为 `patentcompass-dev`。数据库、Redis 使用独立命名卷；原始文件和导出文件保存在项目 `storage/`。worker 只读挂载 `api/app/` 和 `database/`，与本地 API 共享 `storage/`，不会读取 Windows 虚拟环境。

从项目根目录执行：

```powershell
.\scripts\start-dev.ps1
```

脚本会首次复制 `api/.env.example` 到 `api/.env`、同步锁定依赖、启动数据库和 Redis、执行 Alembic 迁移、构建并启动 worker，最后在当前终端启动本地 API 热重载。按 Ctrl+C 结束 API 后，Docker 服务仍保留。

已有镜像时可跳过构建；只准备依赖服务而不启动 API 时使用 `-ServicesOnly`：

```powershell
.\scripts\start-dev.ps1 -SkipBuild -ServicesOnly
Set-Location api
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Swagger 为 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)，完整请求、响应及参数约束以 `/openapi.json` 为准。`/health/live` 检查 API 存活，`/health/ready` 检查 PostgreSQL、pgvector 和 Redis，并返回模型能力状态。本地调试器选择 `api/.venv/Scripts/python.exe`，工作目录设为 `api/`，模块使用 `uvicorn`，参数同上。

## 配置与重启

`api/.env` 仅用于本地开发，不提交到 Git。本地 API 使用 `127.0.0.1:15432` 和 `127.0.0.1:16379`；worker 由 Compose 覆盖为 `postgres:5432`、`redis:6379`。开发库凭证见环境模板，数据库名为 `patentcompass`。

- 修改 API Python 代码：本地 `--reload` 自动重载。
- 修改任务 Python 代码：从项目根目录执行 `docker compose -f deploy/compose.yml restart worker`。
- 修改 Python、图表依赖或 Dockerfile：更新对应锁文件，再执行 `docker compose -f deploy/compose.yml up -d --build worker`。修改 `chart-renderer/render.mjs` 也需要重建镜像。
- 修改 `.env`：重启本地 API，并执行 `docker compose -f deploy/compose.yml up -d --force-recreate worker`；仅 `restart` 不重新加载容器环境变量。
- 修改数据库结构：编写并检查迁移后，进入 `api/` 执行 `uv run --frozen alembic upgrade head`。应用启动不会自动建表。

停止本项目容器：

```powershell
.\scripts\stop-dev.ps1
```

此脚本保留数据卷和文件。容器重建也保留这些数据；不需要执行删除数据卷的清理命令。备份时应同时保存 PostgreSQL 备份及 `storage/`，确保文件引用可恢复。

外部兼容模型分开配置：

```dotenv
LLM_BASE_URL=https://provider.example/v1
LLM_API_KEY=...
LLM_MODEL=...
EMBEDDING_BASE_URL=https://provider.example/v1
EMBEDDING_API_KEY=...
EMBEDDING_MODEL=...
```

这些地址只是格式示例；基础地址需包含提供商要求的 API 前缀，适配器分别附加 `/chat/completions` 和 `/embeddings`。默认全部留空，模型密钥不写入源码或日志。可选 `MODEL_TIMEOUT` 控制模型请求超时秒数。

## 接口与最小使用流程

当前使用固定内部身份 `local-dev`，资源仍检查所属项目及引用关系。此配置适用于本机验证，尚未提供登录、组织或多租户权限体系。

以下为设计中的 29 个业务操作；健康检查和文档路由另外提供。长任务返回 `202` 和 `run_id`，结果通过运行接口读取。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/projects` | 创建项目与初始范围 |
| `GET /api/projects` | 项目列表 |
| `GET /api/projects/{id}` | 项目详情及最新资源引用 |
| `PATCH /api/projects/{id}/scope` | 保存或确认范围版本 |
| `GET /api/datasets` | 数据集及版本列表 |
| `POST /api/datasets/imports` | 上传 Excel 或复用已上传文件，创建导入任务 |
| `GET /api/datasets/{id}/quality` | 读取指定数据版本的质量及覆盖说明 |
| `PUT /api/projects/{id}/datasets` | 绑定一个就绪数据版本 |
| `GET /api/projects/{id}/patents` | 字段、关键词或语义检索及历史快照下钻 |
| `GET /api/projects/{id}/patents/{patent_id}` | 专利摘要、来源行与公开记录 |
| `POST /api/projects/{id}/conversations` | 原子保存新对话、首条消息及关联任务 |
| `GET /api/projects/{id}/conversations` | 项目对话历史 |
| `GET /api/conversations/{id}/messages` | 消息历史及引用 |
| `POST /api/conversations/{id}/messages` | 发送消息并创建关联任务 |
| `POST /api/projects/{id}/runs` | 创建分析、报告或向量建索引任务 |
| `GET /api/runs/{id}` | 状态、待输入问题、错误、步骤与产物引用 |
| `GET /api/runs/{id}/events` | SSE 事件及按序号补读 |
| `POST /api/runs/{id}/inputs` | 确认范围并继续等待输入的任务 |
| `POST /api/runs/{id}/cancel` | 请求取消任务 |
| `POST /api/runs/{id}/retry` | 创建关联重试任务 |
| `GET /api/projects/{id}/analysis` | 读取已保存的工作台分析结果 |
| `GET /api/charts/{id}` | 图表配置、数据、口径及版本 |
| `GET /api/evidence/{id}` | 固定版本的原文或统计证据 |
| `GET /api/reports/{id}/versions` | 报告版本列表与父版本 |
| `GET /api/reports/{id}/versions/{version}` | 不可变报告正文与证据快照 |
| `POST /api/reports/{id}/revisions` | 基于指定版本修改段落或标题 |
| `PUT /api/reports/{id}/current-version` | 切换当前采用版本 |
| `POST /api/reports/{id}/exports` | 指定版本导出 Word |
| `GET /api/files/{id}` | 下载原始文件或完成产物 |

建议按下列顺序通过 Swagger 验证：

1. 创建项目；初始范围可保存 `{"industry":"无人机","source_region":"中国","coverage":"部分检索结果"}` 作为说明。
2. 上传 `database/patent-demo.xlsx`，轮询导入运行，检查质量结果为 520 条申请、526 个公开标识、520 份行级摘要。导入不会自动绑定项目。
3. 绑定就绪 `dataset_version_id`。相同原始文件与清洗规则复用已存在版本，不重复计数。
4. 确认范围后提交 `{"action":"analyze"}`；也可先启动未确认范围的任务，等待 `waiting_input` 后按返回问题继续。
5. 读取分析、图表、证据，并使用图表快照下钻到对应专利。
6. 提交 `{"action":"generate_report","report_mode":"statistical"}`，读取结果中的报告和版本标识。
7. 对一个正文块提交 `replacement_text`，读取新版本；浏览旧版只读取，切回旧版使用 `current-version`。
8. 指定 `report_version_id` 和 `format=docx` 创建导出运行，成功后下载 `file_id`。

研究范围的 `industry`、`source_region`、`coverage` 保存来源说明，不会将“中国”转换为公开机构筛选，也不会将“无人机”再次强制筛选成标题关键词。需要缩小当前样本时，在范围中直接设置 `q`、`applicant`、`ipc`、`application_start`、`application_end` 等筛选条件。导入使用 `multipart/form-data`，来源说明放在 `source_metadata` JSON 字符串中，例如 `{"query":"无人机","region":"中国","partial_results":true}`。读取质量需要指定 `dataset_version_id` 查询参数。

向量功能使用相同运行入口的 `action=index`，需先配置 embedding 服务并确认范围。完成索引后可使用 `mode=semantic` 或 `mode=hybrid`；没有模型或完整索引时明确报错，不退回随机结果。切换 embedding 模型后重新建立索引。`mode=field` 为默认模式，`mode=keywords` 提供关键词与常用中英文术语扩展。

运行状态包括 `queued`、`running`、`waiting_input`、`succeeded`、`failed`、`cancelling`、`cancelled`。SSE 断开不取消任务；数据库保存事件和步骤。API 运行时定期补偿派发，并在 worker 租约过期后恢复任务，因此处理新任务和故障恢复时需保持 API 与 worker 均运行。

对话模型通过只读检索工具定位冻结范围内的原文，再生成回答并保存引用；每次消息分别固定发送时的上下文，历史报告追问沿用该报告的数据与范围。SSE 支持 `message.delta` 和 `message.completed`；当前模型适配器先取得完整回答，再将已保存正文分段发送，不代表供应商端逐 token 流式输出。

范围、数据绑定和报告当前版本更新分别携带 `expected_scope_version_id`、`expected_revision`、`expected_current_version_id`；过期更新返回 `409`。报告路径中的 `{version}` 使用版本 UUID，不是数字序号。结构化修改指定 `base_version_id`、`expected_current_version_id`、`block_id` 与 `replacement_text`；自然语言修改用 `instruction` 替换 `replacement_text`，两者只能提供一项。需要可靠重试写入时携带 `Idempotency-Key`，相同键与相同请求复用原结果，避免重复提交任务。分页使用接口返回的 `next_cursor`，不自行拼接游标。SSE 使用 `after` 查询参数或 `Last-Event-ID` 请求头续读。

## 模型与结果边界

未配置模型时仍可使用：导入和质量检查、字段及关键词检索、年度/申请人/IPC 统计、原文证据、统计简报、结构化正文修改、版本切换和 Word 导出。

自由对话、AI 摘要、自然语言段落修改及向量索引分别需要对应模型配置；缺失时返回明确错误。普通统计不会因模型缺失而失败。当前技术主题展示 IPC 结构；技术路线、候选机会及地域布局没有形成结论时返回 `unavailable` 和原因，不生成演示数据。

所有统计仅覆盖已导入且入选的申请记录。共同申请人分别全计数，多 IPC 标签分别计数，因此分类或申请人的合计可能大于申请总量。语义检索的 `returned_count` 是候选条数，不能代替总体数量。

图表使用 ECharts 配置保存，导出时由 worker 的 ECharts SVG SSR＋Sharp 转成 PNG 并插入 Word。历史报告冻结正文、图表、数据版本和证据；之后更新范围或数据不会覆盖旧报告。当前只支持 `.docx`，不包含 PDF、Word 资料解析、外部补检、权利要求、完整法律状态或同族分析。

本轮实施和验收记录见 [后端验收记录](../docs/plans/2026-10-19-backend-verification.md)。Word 的正文、版本及内嵌图片已验证；当前文档检查环境缺少 LibreOffice，整篇分页视觉检查仍待完成。

## 验证与故障定位

从 `api/` 执行：

```powershell
uv run --frozen python -m pytest -q
uv run --frozen alembic check
```

测试中的模型调用使用受控测试替身，不代表外部真实模型已经联调。数据库集成测试在同一 PostgreSQL 中自动创建独立的 `patentcompass_test` 数据库，并通过事务回滚隔离用例，不会清空开发业务库；可用 `TEST_DATABASE_URL` 指定测试服务连接。真实接口与 Docker worker 闭环应同时检查导入数、报告版本及下载文件。

本地 API 和 worker 运行时，从项目根目录执行真实样本 HTTP 验收：

```powershell
uv run --project api --frozen python scripts/smoke-test.py
```

脚本创建独立验收项目，读取原始工作簿，不修改样本；验证导入去重、520/526/520 数量、独立统计计算、范围确认续跑、图表下钻、证据、SSE、报告 V1→V2→切回 V1→V3 分支，以及指定 V1 的 Word 下载。结果 JSON 和 Word 保存到 `storage/acceptance/<验收标识>/`；重复执行会新增验收项目并复用同一导入数据版本。

在没有其他本项目任务或测试运行时，可验证真实服务中断恢复；脚本只操作 `patentcompass-dev` 的服务，短时停止 Redis/worker，并重建 PostgreSQL 容器但保留数据卷：

```powershell
uv run --project api --frozen python scripts/failure-smoke-test.py
```

此脚本默认读取最近一次成功验收的 `result.json`，将 Redis 停机期间的 `202/queued`、恢复后的任务成功、worker 重启续处理及 PostgreSQL 重建后的项目/报告/文件留存结果追加到同一文件。可用 `--result <路径>` 指定验收记录，或用 `--checks redis worker` 仅验证选定服务。测试完成后各服务保持运行。

`--checks worker-interrupted` 单独验证执行中崩溃恢复：对指定报告 V1 创建导出，已保存快照检查点后强制停止本项目 worker，重启后等待默认 90 秒租约到期，验证同一运行至少执行第二次、复用原检查点并只生成一个导出文件。此检查最多等待 180 秒，并将恢复结果追加到验收 JSON。

从项目根目录检查服务及 worker：

```powershell
docker compose -f deploy/compose.yml ps
docker compose -f deploy/compose.yml logs --tail 100 worker
docker compose -f deploy/compose.yml exec -T worker celery -A app.worker:celery_app inspect ping --timeout=5
```

任务长期停在 `queued` 时检查 API 是否运行、Redis 和 worker 是否在线；`failed` 时读取运行的结构化错误和 worker 日志。原始文件使用哈希校验及数据库文件标识，不接受任意服务器路径。

只验证图表渲染时，可在 `api/chart-renderer/` 执行 `npm ci`，再从项目根目录调用：

```powershell
node api/chart-renderer/render.mjs --input storage/option.json --output storage/chart.png
```

输入为纯 JSON ECharts option，或 `{"option":{...},"width":1200,"height":640}`；不执行传入的 JavaScript。正常 Word 导出无需本机安装图表依赖。
