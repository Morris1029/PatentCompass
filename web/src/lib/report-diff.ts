import { createTwoFilesPatch, diffChars, FILE_HEADERS_ONLY } from "diff";
import { parseDiff, type HunkData, type RangeTokenNode } from "react-diff-view";

type ReportValue = Record<string, any>;
export type ReportLine = { text: string; kind: string; blockId?: string };
export type NormalizedReport = { text: string; lines: ReportLine[] };
export type ReportChangeSummary = { title: number; added: number; removed: number; modified: number; order: number; layout: number };

export function sameReportValue(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const a = left as Record<string, unknown>, b = right as Record<string, unknown>;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(key => Object.hasOwn(b, key) && sameReportValue(a[key], b[key]));
}
export function reportOrderChanged(before: Record<string, unknown>[], after: Record<string, unknown>[]): boolean {
  const afterIds = new Set(after.map(block => block.id));
  const shared = new Set(before.filter(block => afterIds.has(block.id)).map(block => block.id));
  return before.filter(block => shared.has(block.id)).map(block => block.id).join() !== after.filter(block => shared.has(block.id)).map(block => block.id).join();
}

function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map(text).join("、");
  if (typeof value === "object") return Object.entries(value).filter(([key]) => !/(^id$|_ids?$|sha256|fingerprint)/.test(key)).sort(([a], [b]) => a.localeCompare(b)).map(([, item]) => text(item)).join("、");
  return String(value).replace(/\r\n?/g, "\n");
}
const alignment: Record<string, string> = { left: "左对齐", center: "居中", right: "右对齐" };
const chartKinds: Record<string, string> = { yearly: "年度分布", applicants: "申请人分布", ipc: "IPC 分类", country: "国家分布", geography: "地域分布", technology: "技术主题", landscape: "主体分布" };

export function normalizeReport(content: ReportValue): NormalizedReport {
  const lines: ReportLine[] = [];
  const append = (value: unknown, kind: string, blockId?: string) => text(value).split("\n").forEach(line => lines.push({ text: line, kind, ...(blockId ? { blockId } : {}) }));
  append(`报告标题：${text(content.title)}`, "title");
  const layout = content.layout || {};
  const layoutFields: [string, string, string][] = [["paper_size", "纸张", ""], ["page_width_inches", "页面宽度", " 英寸"], ["page_height_inches", "页面高度", " 英寸"], ["font_family", "正文字体", ""], ["body_font_pt", "正文字号", " 磅"], ["chart_width_inches", "图表最大宽度", " 英寸"]];
  for (const [key, label, unit] of layoutFields) if (layout[key] !== undefined) append(`${label}：${text(layout[key])}${unit}`, "layout");
  for (const [key, label] of Object.entries({ top: "上", bottom: "下", left: "左", right: "右" })) if (layout.margins_inches?.[key] !== undefined) append(`${label}页边距：${text(layout.margins_inches[key])} 英寸`, "layout");
  for (const block of content.blocks || []) {
    append("", "separator", block.id);
    if (block.type === "heading") append(`${block.level || 1} 级标题：${text(block.text)}`, "heading", block.id);
    else if (block.type === "paragraph") append(block.text || "（空段落）", "paragraph", block.id);
    else if (block.type === "table") {
      append(`表格：${text(block.title || block.caption) || "数据表"}`, "table", block.id);
      const columns: unknown[] = block.headers || block.columns || [];
      if (columns.length) append(`表头：${columns.map(text).join(" ｜ ")}`, "table", block.id);
      for (const row of block.rows || []) {
        const columnKeys = columns.map(column => text(column));
        const cells = Array.isArray(row) ? row : columnKeys.length && columnKeys.every(key => Object.hasOwn(row, key)) ? columnKeys.map(key => row[key]) : Object.values(row);
        append(cells.map((cell: unknown, index: number) => `${text(columns[index]) || `列 ${index + 1}`}：${text(cell).replace(/\n/g, " ↵ ")}`).join(" ｜ "), "table", block.id);
      }
      if (block.caption && block.title) append(`表格说明：${text(block.caption)}`, "table", block.id);
    } else if (block.type === "chart") {
      const chart = block.chart || {};
      append(`图表：${text(chart.option?.title?.text || block.title || block.caption) || "统计图表"}`, "chart", block.id);
      const number = chart.number || chart.version_number;
      append(`图表版本：${number ? `V${number}` : "未标明"}${chart.kind ? `；${chartKinds[chart.kind] || "统计图表"}` : ""}`, "chart", block.id);
      if (block.caption) append(`图表说明：${text(block.caption)}`, "chart", block.id);
      append(`图表排版：宽度 ${block.layout?.width_percent || 100}%；${alignment[block.layout?.alignment || "center"] || "居中"}`, "chart", block.id);
      if (chart.interpretation?.text) append(`图表解读：${text(chart.interpretation.text)}`, "chart", block.id);
    } else append(block.text || block.title || "其它报告内容", "content", block.id);
  }
  return { text: lines.map(line => line.text).join("\n") + "\n", lines };
}

export function reportChangeSummary(before: ReportValue, after: ReportValue): ReportChangeSummary {
  const oldBlocks: ReportValue[] = before.blocks || [], newBlocks: ReportValue[] = after.blocks || [];
  const oldById = new Map(oldBlocks.map(block => [block.id, block])), newById = new Map(newBlocks.map(block => [block.id, block]));
  return {
    title: Number(before.title !== after.title),
    added: newBlocks.filter(block => !oldById.has(block.id)).length,
    removed: oldBlocks.filter(block => !newById.has(block.id)).length,
    modified: newBlocks.filter(block => oldById.has(block.id) && !sameReportValue(oldById.get(block.id), block)).length,
    order: Number(reportOrderChanged(oldBlocks, newBlocks)),
    layout: Number(!sameReportValue(before.layout, after.layout)),
  };
}

export function inlineReportRanges(hunks: HunkData[], before: NormalizedReport, after: NormalizedReport) {
  const oldRanges: RangeTokenNode[] = [], newRanges: RangeTokenNode[] = [];
  for (const hunk of hunks) for (let index = 0; index < hunk.changes.length - 1; index++) {
    const oldLine = hunk.changes[index], newLine = hunk.changes[index + 1];
    if (oldLine.type !== "delete" || newLine.type !== "insert") continue;
    const oldSource = before.lines[oldLine.lineNumber - 1], newSource = after.lines[newLine.lineNumber - 1];
    if (!oldSource || !newSource || oldSource.kind !== newSource.kind) continue;
    let oldOffset = 0, newOffset = 0;
    for (const change of diffChars(oldLine.content, newLine.content)) {
      // The diff uses Unicode code points; the renderer's range offsets use UTF-16 string indexes.
      const length = change.value.length;
      if (change.removed) { oldRanges.push({ type: "edit", lineNumber: oldLine.lineNumber, start: oldOffset, length }); oldOffset += length; }
      else if (change.added) { newRanges.push({ type: "edit", lineNumber: newLine.lineNumber, start: newOffset, length }); newOffset += length; }
      else { oldOffset += length; newOffset += length; }
    }
  }
  return { oldRanges, newRanges };
}

export function buildReportComparison(previous: ReportValue, current: ReportValue) {
  const before = normalizeReport(previous.content || {}), after = normalizeReport(current.content || {});
  const patch = createTwoFilesPatch("a/report.txt", "b/report.txt", before.text, after.text, "", "", { context: 3, headerOptions: FILE_HEADERS_ONLY });
  const file = parseDiff(`diff --git a/report.txt b/report.txt\n${patch}`, { nearbySequences: "zip" })[0];
  const hunks = file?.hunks || [];
  const summary = reportChangeSummary(previous.content || {}, current.content || {});
  const changes = hunks.flatMap(hunk => hunk.changes);
  const oldBlocks: ReportValue[] = previous.content?.blocks || [], newBlocks: ReportValue[] = current.content?.blocks || [];
  const chartIds = new Set([...oldBlocks, ...newBlocks].filter(block => block.type === "chart").map(block => block.id));
  const charts = [...chartIds].map(id => ({ id, previous: oldBlocks.find(block => block.id === id), current: newBlocks.find(block => block.id === id) })).filter(pair => !sameReportValue(pair.previous, pair.current));
  return { before, after, hunks, summary, changeCount: Object.values(summary).reduce((sum, count) => sum + count, 0), insertedLines: changes.filter(change => change.type === "insert").length, deletedLines: changes.filter(change => change.type === "delete").length, charts, ...inlineReportRanges(hunks, before, after) };
}
