# PatentCompass 前端

基于已确认的静态原型接入真实 API。运行状态、消息、资料、分析、图表和报告均来自后端；浏览器仅保存草稿、视图偏好与任务事件游标。

## 启动

先按 `api/README.md` 启动本地 API 与 Docker 服务，然后在本目录运行：

```powershell
pnpm install --frozen-lockfile
pnpm dev
```

打开 `http://127.0.0.1:3000`。如 API 使用其他地址，把 `.env.example` 复制为 `.env.local` 后调整 `PATENTCOMPASS_API_URL`。模型密钥只配置在后端 `api/.env`。

## 主要流程

创建项目 → 数据与证据导入 Excel → 明确采用数据版本 → 确认范围 → 运行分析 → 生成报告 → 编辑并保存新版本 → 导出指定版本 Word。

研究对话默认使用自然语言编排。Word 附件先上传解析，成功后才能提交消息；仅附件发送也会创建持久对话。报告文字选择后可修改或引用到对话，引用固定历史版本。浏览报告和图表旧版不会改变当前采用版本。

完整语义分析会处理全部入选申请，其费用和进度由后端实际模型与缓存决定。未配置能力、缺少字段或执行失败会展示真实原因，不补充原型示例数据。

## 校验

```powershell
pnpm typecheck
pnpm build
```

`src/app/api/[...path]/route.ts` 只流式转发 HTTP、上传、下载与 SSE；不实现分析业务。`front-point/prototype/` 保持为原型对照，`prototype.css` 为其视觉样式副本，不执行原型脚本或读取其本地业务存储。
