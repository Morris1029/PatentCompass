import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const fixtureUrl = moduleUrl(`
  let values = {};
  export function configure(value) { values = value; }
  export function useApi(path) { return { ...values[path], mutate() {} }; }
  export const api = () => {};
  export const statusLabel = { succeeded: "已完成", waiting_input: "等待确认" };
`);
const imports = {
  "@/lib/api": fixtureUrl,
  swr: moduleUrl(`export { useApi as default } from "${fixtureUrl}";`),
  "@/lib/management": pathToFileURL(require.resolve("../src/lib/management.ts")).href,
  "./shared": moduleUrl("export function Empty({title}) { return title; } export function ErrorNotice({error}) { return error?.message || null; }"),
};
const { outputText } = ts.transpileModule(readFileSync(new URL("../src/components/management.tsx", import.meta.url), "utf8"), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
});
const { default: Management, UserMenu } = await import(moduleUrl(outputText.replace(/from ["']([^"']+)["']/g, (_, name) => `from "${imports[name] || pathToFileURL(require.resolve(name)).href}"`)));
const { configure } = await import(fixtureUrl);
const configuration = {
  identity: { display_name: "本地用户", owner_id: "local-dev" }, model: { name: "configured-model", configured: true },
  agent: { name: "PatentCompass", runtime_version: 1, system_prompt: { content: "PROMPT <unsafe>", source: "shared/SYSTEM_PROMPT.md", sha256: "hash" }, adapter_prompt: { content: "ADAPTER", source: "adapter", sha256: "hash2" },
    tools: [{ function: { name: "search" } }, { function: { name: "prepare", description: "host", parameters: {} } }] },
  mcp: { name: "PatentCompass", endpoint: "/mcp/", transport: "Streamable HTTP", tools: [{ name: "search", description: "检索", native: true, parameters: {} }, { name: "external", description: "外部", parameters: {} }, { name: "extra", description: "其他", parameters: {} }] },
  skills: [{ name: "qa", description: "按证据问答" }],
};
const render = (tab, extra = {}) => renderToStaticMarkup(createElement(Management, { search: new URLSearchParams({ management: tab, ...extra }), go() {} }));
const ready = (extra = {}) => configure({ "/api/management/configuration": { data: configuration }, "/api/management/runs?limit=5": { data: { items: [] } }, ...extra });

test("configuration overview works without a project and distinguishes configured from connected", () => {
  ready();
  const html = render("overview");
  for (const label of ["总览", "Agent", "MCP", "Skills", "运行记录", "configured-model", "暂无符合条件的运行记录"]) assert.match(html, new RegExp(label));
  assert.match(html, /不代表模型或 MCP 客户端连通性已经验证/);
  assert.match(html, /MCP 工具<\/span><strong>3/);
  assert.match(html, /内置 Agent 工具<\/span><strong>2/);
});

test("Prompt content is rendered as escaped original text with its source and copy control", () => {
  ready();
  const html = render("agent");
  assert.match(html, /PROMPT &lt;unsafe&gt;/);
  assert.doesNotMatch(html, /<unsafe>/);
  assert.match(html, /shared\/SYSTEM_PROMPT.md/);
  assert.match(html, /复制原文/);
});

test("tool directory identifies host-only and MCP-only tools", () => {
  ready();
  const html = render("mcp");
  assert.match(html, /MCP 注册 3 项/);
  assert.match(html, /内置 Agent 可调用 2 项/);
  assert.match(html, /宿主专用/);
  assert.match(html, /仅 MCP/);
});

test("skill and reference bodies use the selected backend resource", () => {
  ready({ "/api/management/skills/qa?reference=references%2Fqa.md": { data: { content: "REFERENCE BODY", resource: "references/qa.md", references: ["references/qa.md"], source: "qa.md", sha256: "reference-hash" } } });
  assert.match(render("skills", { management_reference: "references/qa.md" }), /REFERENCE BODY/);
});

test("loading and errors remain visible without fabricated configuration", () => {
  configure({});
  assert.match(render("overview"), /正在读取运行配置/);
  configure({ "/api/management/configuration": { error: new Error("后端暂时无法连接") } });
  const html = render("overview");
  assert.match(html, /后端暂时无法连接/);
  assert.doesNotMatch(html, /configured-model/);
  assert.match(html, /刷新配置/);
});

test("legacy task detail clearly identifies missing historical configuration", () => {
  ready({
    "/api/management/runs?limit=20": { data: { items: [] } },
    "/api/management/runs/legacy": { data: { id: "legacy", project_name: "历史项目", kind: "chat", status: "succeeded", plan: {}, snapshot: { model_name: null, tools: null, skills: [] }, children: [], artifacts: {} } },
  });
  const html = render("runs", { management_run: "legacy" });
  assert.match(html, /历史项目/);
  assert.match(html, /启动时模型：未记录/);
  assert.match(html, /任务当时使用的配置快照/);
  assert.doesNotMatch(html, /PROMPT &lt;unsafe&gt;/);
});

test("the account entry supplies an accessible menu trigger", () => {
  configure({});
  const html = renderToStaticMarkup(createElement(UserMenu, { openManagement() {} }));
  assert.match(html, /aria-label="本地用户菜单"/);
  assert.match(html, /aria-haspopup="menu"/);
  assert.match(html, /aria-expanded="false"/);
});
