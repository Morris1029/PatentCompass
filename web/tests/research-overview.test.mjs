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
function tsxModule(path, imports = {}) {
  const { outputText } = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
  });
  return moduleUrl(outputText.replace(/from ["']([^"']+)["']/g, (_, name) => `from "${imports[name] || pathToFileURL(require.resolve(name)).href}"`));
}
const apiUrl = moduleUrl(`
  let fixture = {};
  export const calls = [];
  export function setFixture(value) { fixture = value; calls.length = 0; }
  export function useReportVersion(...args) { calls.push(args); return { ...fixture, mutate() {} }; }
`);
const { calls, setFixture } = await import(apiUrl);
const { default: ResearchOverview } = await import(tsxModule("../src/components/research-overview.tsx", {
  "@/lib/api": apiUrl,
  "./shared": moduleUrl("export function ErrorNotice({error}) { return error?.message || null; }"),
  "./workspace-icon": tsxModule("../src/components/workspace-icon.tsx"),
}));
const render = (project, analysis) => renderToStaticMarkup(createElement(ResearchOverview, {
  project, analysis, navigate() {}, continueConversation() {}, actions: null,
}, createElement("p", null, "原有证据与风险内容")));

test("overview uses the scoped report revision and preserves zero counts", () => {
  setFixture({ data: { created_at: "2026-10-20T12:49:00Z" } });
  const html = render({ report_id: "report", current_report_version_id: "v1-revision", research_id: "research-v1", research_number: 1, dataset_version_id: "dataset", scope: { confirmed: true } }, {
    analysis_id: "analysis-v1", summary: { application_count: 0, publication_count: 0, abstract_count: 0 },
  });
  assert.deepEqual(calls, [["report", "v1-revision"]]);
  assert.equal((html.match(/<strong>0<\/strong>/g) || []).length, 3);
  assert.match(html, /研究 V1/);
  assert.match(html, /进入导航报告/);
  assert.match(html, /原有证据与风险内容/);
});

test("an unpublished research offers conversation guidance without fabricated results", () => {
  setFixture({});
  const html = render({ report_id: "report", current_report_version_id: null, research_id: "draft", research_number: null }, { analysis_id: null });
  assert.deepEqual(calls, [["report", null]]);
  assert.match(html, /新研究 · 未编号/);
  assert.match(html, /尚未生成/);
  assert.match(html, /在对话中推进研究/);
  assert.doesNotMatch(html, /进入导航报告|已有分析结果|已确认|已选择数据/);
  assert.equal((html.match(/<strong>—<\/strong>/g) || []).length, 3);
});

test("report metadata errors retain the report entry and expose retry without invented dates", () => {
  setFixture({ error: new Error("无法读取报告信息") });
  const html = render({ report_id: "report", current_report_version_id: "revision" });
  assert.match(html, /进入导航报告/);
  assert.match(html, /无法读取报告信息/);
  assert.match(html, /重新读取报告信息/);
  assert.doesNotMatch(html, /\d{4}\/\d{2}\/\d{2}/);
});
