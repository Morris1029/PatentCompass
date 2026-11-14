import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const { outputText } = ts.transpileModule(readFileSync(new URL("../src/components/research-strategy.tsx", import.meta.url), "utf8"), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
});
const source = outputText.replace(/from ["']([^"']+)["']/g, (_, name) => `from "${pathToFileURL(require.resolve(name)).href}"`);
const { default: ResearchStrategy } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const item = { id: "direction", title: "视觉着陆", priority: "P1", description: "比较视觉控制方案", conditions: ["补充权利要求"], support_count: 2,
  risks: [{ id: "risk", title: "法律状态尚未验证" }], phases: [{ id: "phase", title: "证据核验", actions: [{ id: "action", title: "核对原文" }], deliverable: "原文对照表", acceptance: "每条引文能定位到原始行" }] };

test("roadmap displays stage actions, deliverables, gates and unresolved risk", () => {
  const html = renderToStaticMarkup(createElement(ResearchStrategy, { item }));
  for (const text of ["视觉着陆", "核对原文", "原文对照表", "每条引文能定位", "法律状态尚未验证", "研究建议 · 待验证"]) assert.ok(html.includes(text));
});

test("mock proposals remain explicitly marked with zero real evidence", () => {
  const html = renderToStaticMarkup(createElement(ResearchStrategy, { item: { ...item, basis: "mock", support_count: 0, provenance: { label: "模拟方案（不计入真实专利证据）" } } }));
  assert.match(html, /模拟方案（不计入真实专利证据）/);
  assert.match(html, /原文支持 0 条/);
});
