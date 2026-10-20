import test from "node:test";
import assert from "node:assert/strict";
import { expandFromRawCode, pickRanges, tokenize } from "react-diff-view";
import { buildReportComparison, normalizeReport, reportChangeSummary } from "../src/lib/report-diff.ts";

const version = (blocks, extra = {}) => ({ id: "report-version", number: 2, content: { title: "无人机研究", layout: { paper_size: "A4", body_font_pt: 10.5, margins_inches: { top: 0.75, left: 0.8 } }, blocks, ...extra } });
const paragraph = (id, text) => ({ id, type: "paragraph", text });
const tokenText = tokens => tokens.map(token => token.value || tokenText(token.children || [])).join("");
const editedText = tokens => tokens.flatMap(token => token.type === "edit" ? [tokenText(token.children || [])] : token.children ? editedText(token.children) : []);

test("normalization exposes all table cells and readable layout while keeping metadata out of report lines", () => {
  const content = version([
    { id: "private-heading-id", type: "heading", level: 2, text: "年度情况" },
    paragraph("private-paragraph-id", "第一行\r\n第二行"),
    { id: "private-table-id", type: "table", columns: ["年度", "申请数"], rows: [["2025", 18]], caption: "全部样本" },
    { id: "private-chart-id", type: "chart", chart_id: "private-source-id", chart: { id: "private-source-id", number: 3, version_id: "private-version-id", data_sha256: "private-hash", kind: "yearly", option: { title: { text: "年度分布" } } }, layout: { width_percent: 50, alignment: "right" } },
  ]).content;
  const result = normalizeReport(content);
  assert.match(result.text, /报告标题：无人机研究/);
  assert.match(result.text, /正文字号：10.5 磅/);
  assert.match(result.text, /上页边距：0.75 英寸/);
  assert.match(result.text, /年度：2025 ｜ 申请数：18/);
  assert.match(result.text, /图表版本：V3；年度分布/);
  assert.match(result.text, /图表排版：宽度 50%；右对齐/);
  assert.doesNotMatch(result.text, /private-|sha256|chart_id|version_id|\r/);
  assert.equal(result.lines.find(line => line.text === "第二行").blockId, "private-paragraph-id");
  assert.equal(result.lines.find(line => line.text.startsWith("年度：")).blockId, "private-table-id");
});

test("Git-compatible headers parse changes and Unicode inline ranges preserve complete surrogate pairs", () => {
  const before = version([paragraph("same-block", "甲😀乙旧")]);
  const after = version([paragraph("same-block", "甲😁乙新")]);
  const result = buildReportComparison(before, after);
  assert.equal(result.hunks.length, 1);
  assert.equal(result.insertedLines, 1);
  assert.equal(result.deletedLines, 1);
  const sourceLine = result.before.lines.findIndex(line => line.text === "甲😀乙旧") + 1;
  assert.deepEqual(result.oldRanges, [{ type: "edit", lineNumber: sourceLine, start: 1, length: 2 }, { type: "edit", lineNumber: sourceLine, start: 4, length: 1 }]);
  const tokens = tokenize(result.hunks, { highlight: false, oldSource: result.before.text, enhancers: [pickRanges(result.oldRanges, result.newRanges)] });
  assert.deepEqual(editedText(tokens.old[sourceLine - 1]), ["😀", "旧"]);
  assert.deepEqual(editedText(tokens.new[sourceLine - 1]), ["😁", "新"]);
  assert.equal(tokenText(tokens.old[sourceLine - 1]), "甲😀乙旧");
});

test("new and removed table rows show their actual cell values without renumbering unrelated rows", () => {
  const table = rows => ({ id: "table", type: "table", columns: ["年度", "数量"], rows });
  const result = buildReportComparison(version([table([["2024", 10], ["2025", 20]])]), version([table([["2023", 5], ["2024", 10], ["2025", 21]])]));
  const changes = result.hunks.flatMap(hunk => hunk.changes);
  assert.ok(changes.some(change => change.type === "insert" && change.content.includes("年度：2023 ｜ 数量：5")));
  assert.ok(changes.some(change => change.type === "normal" && change.content.includes("年度：2024 ｜ 数量：10")));
  assert.ok(changes.some(change => change.type === "delete" && change.content.includes("年度：2025 ｜ 数量：20")));
  assert.ok(changes.some(change => change.type === "insert" && change.content.includes("年度：2025 ｜ 数量：21")));
  assert.equal(result.summary.modified, 1);
  assert.equal(result.summary.order, 0);
});

test("business counts stay separate from text-line counts and insertion does not count as reordering", () => {
  const before = version([paragraph("a", "原文"), paragraph("b", "第二段")]);
  const after = version([paragraph("added", "新增第一行\n新增第二行"), paragraph("a", "原文"), paragraph("b", "第二段")]);
  const result = buildReportComparison(before, after);
  assert.equal(result.changeCount, 1);
  assert.equal(result.summary.added, 1);
  assert.equal(result.summary.order, 0);
  assert.ok(result.insertedLines > result.changeCount);
  assert.equal(result.deletedLines, 0);
  assert.equal(reportChangeSummary(before.content, { ...before.content, blocks: [...before.content.blocks].reverse() }).order, 1);
});

test("three context lines limit the initial diff and expanded lines keep correct original tokens", () => {
  const before = version(Array.from({ length: 50 }, (_, index) => paragraph(`block-${index}`, `第 ${index + 1} 段内容`)));
  const after = structuredClone(before);
  after.content.blocks[24].text = "第 25 段修订内容";
  const result = buildReportComparison(before, after);
  assert.ok(result.hunks[0].oldStart > 1);
  assert.ok(result.hunks.flatMap(hunk => hunk.changes).length < 12);
  const expanded = expandFromRawCode(result.hunks, result.before.text, 1, result.hunks[0].oldStart);
  assert.equal(expanded[0].oldStart, 1);
  const tokens = tokenize(result.hunks, { highlight: false, oldSource: result.before.text, enhancers: [pickRanges(result.oldRanges, result.newRanges)] });
  assert.equal(tokenText(tokens.old[0]), "报告标题：无人机研究");
});

test("chart snapshots remain untouched and chart-only style changes are available for frozen previews", () => {
  const chart = { id: "chart-block", type: "chart", chart: { id: "chart", number: 1, option: { color: ["red"], title: { text: "年度分布" } } } };
  const before = version([chart]);
  const after = structuredClone(before);
  after.content.blocks[0].chart.option.color = ["green"];
  const savedBefore = structuredClone(before), savedAfter = structuredClone(after);
  const result = buildReportComparison(before, after);
  assert.equal(result.charts.length, 1);
  assert.equal(result.changeCount, 1);
  assert.equal(result.insertedLines, 0);
  assert.equal(result.deletedLines, 0);
  assert.deepEqual(result.charts[0].previous.chart.option.color, ["red"]);
  assert.deepEqual(result.charts[0].current.chart.option.color, ["green"]);
  assert.deepEqual(before, savedBefore);
  assert.deepEqual(after, savedAfter);
});

test("identical reports and JSON property order produce an explicit empty diff", () => {
  const before = version([paragraph("block", "未变内容")]);
  const after = { ...before, content: { blocks: [{ text: "未变内容", type: "paragraph", id: "block" }], layout: { margins_inches: { left: 0.8, top: 0.75 }, body_font_pt: 10.5, paper_size: "A4" }, title: "无人机研究" } };
  const result = buildReportComparison(before, after);
  assert.equal(result.changeCount, 0);
  assert.equal(result.hunks.length, 0);
  assert.equal(result.insertedLines, 0);
  assert.equal(result.deletedLines, 0);
});

test("regenerated block IDs retain Chinese inline highlighting and independent old/new locations", () => {
  const result = buildReportComparison(version([paragraph("old", "原有研究方向")]), version([paragraph("new", "新的研究方向")]));
  assert.equal(result.summary.added, 1);
  assert.equal(result.summary.removed, 1);
  assert.ok(result.oldRanges.length > 0);
  assert.ok(result.newRanges.length > 0);
  assert.equal(result.before.lines[result.oldRanges[0].lineNumber - 1].blockId, "old");
  assert.equal(result.after.lines[result.newRanges[0].lineNumber - 1].blockId, "new");
});

test("object table rows use matching column keys and do not gain false changes from property order", () => {
  const table = row => ({ id: "table", type: "table", columns: ["年度", "申请数"], rows: [row] });
  const before = version([table({ "申请数": 18, "年度": "2025" })]);
  const after = version([table({ "年度": "2025", "申请数": 18 })]);
  const result = buildReportComparison(before, after);
  assert.match(result.before.text, /年度：2025 ｜ 申请数：18/);
  assert.equal(result.changeCount, 0);
  assert.equal(result.hunks.length, 0);
  const fallback = normalizeReport(version([{ ...table({ year: "2025", count: 18 }), columns: ["年份", "数量"] }]).content);
  assert.match(fallback.text, /年份：2025 ｜ 数量：18/);
});
