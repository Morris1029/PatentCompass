import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
function tsxModule(path, localImports = {}) {
  const { outputText } = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
  });
  const source = outputText.replace(/from ["']([^"']+)["']/g, (_, name) =>
    `from "${localImports[name] || pathToFileURL(require.resolve(name)).href}"`);
  return `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
}
const markdownUrl = tsxModule("../src/components/message-markdown.tsx");
const { MarkdownContent } = await import(markdownUrl);
const { default: TextBlock } = await import(tsxModule("../src/components/report-text-block.tsx", {
  "./message-markdown": markdownUrl,
}));
const render = content => renderToStaticMarkup(createElement(MarkdownContent, { content }));

test("report Markdown renders Chinese nested lists, emphasis, quotations and emoji", () => {
  const html = render("**研究结论** 👩🏽‍🔬\n\n1. 起飞分析\n   - *视觉感知*\n   - ~~旧结论~~\n\n> 样本范围已冻结。");
  assert.match(html, /<strong>研究结论<\/strong> 👩🏽‍🔬/);
  assert.match(html, /<ol>[\s\S]*<ul>[\s\S]*<em>视觉感知<\/em>/);
  assert.match(html, /<del>旧结论<\/del>/);
  assert.match(html, /<blockquote>[\s\S]*样本范围已冻结/);
});

test("code and GFM tables use their own containers while preserving code source", () => {
  const html = render("```ts\nconst value = '<tag>';\n```\n\n| 技术 | 结论 |\n| --- | --- |\n| 感知 | **有效** |");
  assert.match(html, /<pre><code class="language-ts">const value = &#x27;&lt;tag&gt;&#x27;;/);
  assert.match(html, /class="message-table-wrap"><table>/);
  assert.match(html, /<td><strong>有效<\/strong><\/td>/);
});

test("shared renderer disables raw HTML and dangerous link protocols", () => {
  const html = render('<script>alert(1)</script>\n\n<img src="x" onerror="alert(2)">\n\n[危险](javascript:alert%281%29) [安全](https://example.com/report)');
  assert.doesNotMatch(html, /<script|onerror=|href="javascript:/);
  assert.match(html, /<span>危险<\/span>/);
  assert.match(html, /href="https:\/\/example.com\/report" target="_blank" rel="noopener noreferrer"/);
});

test("inline report headings and table cells have formatting without block markup", () => {
  const html = renderToStaticMarkup(createElement(MarkdownContent, {
    content: "### **技术结论** 与 `IPC`\n\n[来源](https://example.com)", inline: true,
  }));
  assert.match(html, /<strong>技术结论<\/strong>/);
  assert.match(html, /<code>IPC<\/code>/);
  assert.doesNotMatch(html, /<p[ >]|<h[1-6]|<div|<table/);
});

test("inline images do not introduce alternative text absent from report text references", () => {
  const html = renderToStaticMarkup(createElement(MarkdownContent, {
    content: "研究![不可引用的图片说明](https://example.com/image.png)结论", inline: true,
  }));
  assert.equal(html, "研究结论");
});

test("current report text defaults to preview with an explicit source edit action", () => {
  const html = renderToStaticMarkup(createElement(TextBlock, {
    id: "body-id", type: "paragraph", text: "保留 **Markdown** 原文。", editable: true,
    changed: () => assert.fail("Rendering must not change the local draft"), editingChanged: () => {},
  }));
  assert.match(html, /data-report-text=/);
  assert.match(html, /<strong>Markdown<\/strong>/);
  assert.match(html, /aria-label="编辑正文原文"/);
  assert.doesNotMatch(html, /<textarea|contenteditable/);
});

test("historical report title remains a formatted preview without editing controls", () => {
  const html = renderToStaticMarkup(createElement(TextBlock, {
    id: "report-title", type: "title", text: "**无人机**起飞研究", editable: false,
    changed: () => assert.fail("Historical content cannot be changed"), editingChanged: () => {},
  }));
  assert.match(html, /<h1><strong>无人机<\/strong>起飞研究<\/h1>/);
  assert.doesNotMatch(html, /<textarea|<button|contenteditable/);
});
