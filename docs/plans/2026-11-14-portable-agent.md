# Portable PatentCompass Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将专业推理迁入可移植的主 Agent / Skills，后端继续执行、校验和保存业务成果，保留现有业务入口。

**Architecture:** `api/patentcompass_agent` 保存独立指令和五项 Skills；现有模型入口读取同一份专业 Prompt。新增 MCP 应用服务负责项目上下文、外部语义批次和确定性业务任务，复用现有 ResearchRun / RunStep、统计、证据、图表和报告版本发布，不引入新 Agent 框架或数据库表。

**Tech Stack:** Python 3.11, FastAPI, MCP, SQLAlchemy/PostgreSQL, Celery, Markdown Skills.

---

### Task 1: 专业指令与迁移包

- 新增 `api/patentcompass_agent/SYSTEM_PROMPT.md`、五项 `skills/*/SKILL.md` 及专业 Prompt 引用。
- 将编排、问答、抽取、归并、报告总结/改写、图表解读的静态专业指令迁出 Python，保留动态上下文和 Schema 装配。
- 新增打包脚本和 Codex / WorkBuddy 接入说明，不自动修改用户全局配置。
- 验证：Skill 验证器、指令加载、独立包导入、打包内容检查；旧提示约束相关测试通过。

### Task 2: 无后端生成模型的语义分析

- 新增 `api/app/agent_contracts.py`、`agent_service.py`。
- MCP 开始分析时冻结项目/研究、范围、数据记录、模型声明及 Skill 版本。
- 按批次取原文、提交标签；复用原文校验并记录每条复核原因。缺项、重复、额外 ID 明确拒绝。
- 归并时提供服务端生成的短 ID；提交各维度主题后由代码生成映射、归属、统计、证据和工作台。
- 外部分析以 `waiting_input` 保留进度；完整后排队交给原任务执行器。取消、幂等、重试和上下文冲突检查继续有效。
- 验证：模型调用禁止条件下，完整覆盖、无效引文、漏项、跨项目、断点恢复、重试和重复提交。

### Task 3: 业务闭环 MCP

- 增补项目/数据目录、范围与研究配置、图表快照、报告与分析任务接口。
- 结构化任务仅允许确定性执行；外部 Agent 提供报告摘要/段落操作/图表解读，不隐式调用后端模型。
- 生成/修改/恢复/导出继续复用原业务脚本和事务发布；保留 scope / dataset / analysis / research / report version 关系及预期版本检查。
- 验证：MCP 协议发现与调用、项目报告发布/改写/采用/导出、旧 HTTP 接口读取成果、旧业务回归。

### Task 4: 交付验收

- 后端全量测试及原前端检查；新改动不触及已有前端未提交内容。
- 使用真实 PostgreSQL 和 MCP 客户端完成外部推理结果驱动的完整闭环，并明确区分受控结果测试和真实平台联调。
- 检查 Docker 镜像/挂载包含指令包；生成可搬迁 ZIP。
- 迁移文档说明：客户端的身份/网络配置、文件上传、工具错误、任务等待、产物与版本读取、两平台安装差异。
- 本地模拟跨平台协议闭环不等于已在 WorkBuddy/Codex 新会话中实测；如实际平台入口不可用，准确报告未验证范围。

## 完成记录

- 已交付主 Agent Prompt、五项 Skills、10 份共享专业指令、Codex / WorkBuddy 接入配置示例和可重复打包脚本。
- 已增加 16 项 Agent MCP 工具，保留原 20 项工具；真实服务发现 36 项工具。
- 已通过后端全量 349 项测试、前端 51 项测试和 TypeScript 类型检查。新增外部业务测试禁止调用后端生成模型，覆盖语义提交、证据、报告发布与修改、图表解读、恢复、幂等、取消、上下文和权限、分批恢复与检查点重试。
- 五项 Skills 通过官方本地 quick_validate；ZIP 包结构、UTF-8 文本和内容检查通过。
- worker 镜像已包含指令包，独立无网络容器导入检查通过；本地 worker 已使用新增挂载。
- 真实 HTTP / MCP / PostgreSQL / Celery / Word 闭环证据：`storage/acceptance/portable-agent-20261114T105045Z-87318c16/result.json`。明确合成的 3 条记录，由受控宿主判断结果驱动：语义分析 → 证据/图表 → 发布报告 → 修改标题 → Word 导出 → 原项目 HTTP 读取。报告冻结的 6 张图表均进入实际 Word。
- 首次真实验收错误地将工作台全部图表数当作报告图表数，检查失败记录保留；核对原模板后，修正验收为报告实际冻结图表与 Word 图形一致，未改变原报告选图业务。
- 原模型入口继续共用专业 Prompt；相同指令保留旧缓存兼容性，语义指令修改自动改变缓存指纹，独立任务也检查指令版本。
- 迁移包：`storage/patentcompass-agent.zip`。尚未在 WorkBuddy 或 Codex 新业务会话中完成客户端安装与模型质量验收；接入方式与该边界已写入 `docs/portable-agent.md`。
