# 专利能力组件化与 Responses 迁移验收

日期：2026-10-26。范围为当前原仓库；未引入 Agent 框架、Skills 或对外 Responses 聊天网关，未修改数据库表结构或清空既有数据。

## 已交付

- `patentcompass_core` 分离解析、检索、统计、语义处理、报告、图表、渲染和模型访问；独立进程测试禁止导入应用、SQLAlchemy、FastAPI、Redis 和 Celery，仍能运行核心计算。
- 16 项能力使用共享参数定义；HTTP 保留原 44 项业务操作并增加 3 个能力入口，MCP 挂载 `/mcp/`，提供 16 项能力及 4 项公共任务工具。
- 基础调用无需项目。独立任务复用现有任务、步骤、事件和文件记录，项目研究、事务发布和历史版本仍由应用层管理。
- 新生成请求统一为 `/responses`，使用显式历史、`store=false` 和真实 `call_id` 工具往返；向量通道仍为 `/embeddings`。
- 分类结果可经统一分析草稿直接组合为报告，保留语义章节、候选方向、路线图、图表和证据。来源指纹不一致时拒绝组合。

接口和组件示例见 [组件接入说明](../capabilities.md)。

## 自动化验证

| 验证 | 结果 |
| --- | --- |
| 起始后端基线 | 252 passed |
| 最终后端全量 `python -m pytest -q` | 336 passed；5 项既有 Alembic/openpyxl 提示 |
| 最后接口、编排、语义与研究上下文相关回归 | 65 passed |
| 前端 `pnpm test` | 45 passed |
| 前端 `pnpm typecheck`、`pnpm build` | 通过；前端源码及对接协议保留 |
| Alembic `current`、`check` | 已在原迁移 head；无新增升级操作 |
| Compose 配置、worker 镜像构建、差异检查 | 通过 |

模型测试替身与真实调用分别验证，不将单元测试计为真实模型验收。保留旧模型检查点转换、取消、租约、重试、历史活动和发布防重复相关回归；新增独立任务、跨 HTTP/MCP 幂等、资料重试关联、来源冻结、图表版本选择和私有续接状态过滤测试。

## 真实服务闭环

使用本项目 PostgreSQL、Redis、Linux Celery worker、本地 FastAPI 和 Next.js 同源转发；模型为当前配置的 DashScope `qwen3.7-plus`，Embedding 为 `text-embedding-v4`。未记录密钥。

| 验证 | 本地证据 |
| --- | --- |
| Responses 和 Embedding 探测 | `storage/acceptance/model-connectivity-20261026T150204Z/probe.json` |
| 原项目 520 申请、526 公开、520 摘要；统计、下钻、证据、SSE、报告修改/恢复及三图 Word | `storage/acceptance/20261026T152128Z-6acb02e4/result.json` |
| Responses 实际路径与工具往返；520 条向量任务、语义/混合检索、对话确认、AI 报告、自然语言改写、历史追问及导出 | `storage/acceptance/models-20261026T152006Z-4fe219a2/result.json` |
| Python/HTTP/MCP 事实一致、真实 worker、无项目分类→报告→Word | `storage/acceptance/capabilities-20261026T153456Z-317d2cc7/result.json` |
| 经前端 3000 端口：图表样式/数据/解读，自动分析、生成、标题修改及确认 | `storage/acceptance/frontend-runtime-20261026T153759Z/result.json` |
| 原项目 Word 上传→worker 解析→MCP 资料定位，以及无项目 Word 解析的原文与位置一致 | `storage/acceptance/materials-20261026T155110Z/result.json` |

独立模型验收逐条处理 3 条真实样本并核验引文，组合报告保留 3 条证据与 6 张图表，实际 Word 包内也有 6 张图片；项目列表在独立任务前后保持一致。520 条样本用于真实导入、统计、索引任务及检索，不将小样本语义验收描述为重新完成了 520 条真实模型分类。

worker 中断验收在保存导出检查点后强制停止进程，恢复后同一任务以第二次执行成功完成，复用原检查点，最终仅一个文件。结果位于统计验收记录的 `fault_checks.worker-interrupted`，原文件和数据库卷保留。

最终 worker 镜像另外在无网络、无开发目录挂载的临时容器中完成核心导入与独立统计，确认核心包实际进入镜像。

前端验收前两次失败记录保留：一次指令被识别为查询，另一次被错误升级为报告，确认检查阻止了目标漂移。补充规划约束，明确工作台重新计算与文档交付的区别后，最终固定场景通过，未放宽动作和范围断言。失败测试产生的待确认任务已取消，原有待确认任务未处理。

## 运行与使用

- 前端：`http://127.0.0.1:3000`。
- API 文档：`http://127.0.0.1:8000/docs`；独立能力目录为 `/api/capabilities`。
- MCP：`http://127.0.0.1:8000/mcp/`，使用支持 Streamable HTTP 的客户端连接。
- 后续启动沿用 `scripts/start-dev.ps1` 和 `scripts/start-web.ps1`。worker 镜像及开发挂载均已包含核心包。
- 核心包本轮以同仓可导入模块交付；未发布 PyPI 包或建立额外微服务。
