"use client";
import { useEffect, useMemo, useState } from "react";
import { computeNewLineNumber, computeOldLineNumber, Decoration, Diff, Hunk, pickRanges, tokenize, useSourceExpansion, type RenderGutter, type ViewType } from "react-diff-view";
import type { Json } from "@/lib/api";
import { buildReportComparison, type ReportChangeSummary } from "@/lib/report-diff";
import { orderReportComparison, reportComparisonLabel, revisionLabel } from "@/lib/research";
import { ChartCard } from "./charts";
import type { Navigate } from "./workspace";
import "./report-diff.css";

const summaryLabels: Record<keyof ReportChangeSummary, string> = { title: "标题", added: "新增内容", removed: "移除内容", modified: "内容修改", order: "顺序调整", layout: "页面排版" };
function ChartSnapshot({ block, version, projectId, navigate }: { block?: Json; version: Json; projectId: string; navigate: Navigate }) {
  if (!block || block.type !== "chart") return <p className="comparison-chart-empty">此版本未包含该图表。</p>;
  const chart = block.chart;
  return <div><div className="comparison-chart-version"><strong>{revisionLabel(version)}</strong><button type="button" className="text-button" onClick={() => navigate({ view: "report", research: version.research_id || null, version: version.id, diff: null, anchor: block.id, message: null })}>定位图表</button></div>{chart?.option ? <ChartCard id={chart.id || block.chart_id} projectId={projectId} frozen={{ ...chart, number: chart.number || chart.version_number || "未标明" }} /> : <p className="comparison-chart-empty">此版本未保存可预览的图表快照。</p>}</div>;
}

export default function ReportComparison({ current: selectedCurrent, previous: selectedPrevious, projectId, navigate }: { current: Json; previous: Json; projectId: string; navigate: Navigate }) {
  const { current, previous } = orderReportComparison(selectedCurrent, selectedPrevious);
  const comparison = useMemo(() => buildReportComparison(previous, current), [previous, current]);
  const labels = reportComparisonLabel(current, previous);
  const [renderingHunks, expand] = useSourceExpansion(comparison.hunks, comparison.before.text);
  const tokens = useMemo(() => tokenize(comparison.hunks, { highlight: false, oldSource: comparison.before.text, enhancers: [pickRanges(comparison.oldRanges, comparison.newRanges)] }), [comparison]);
  const [narrow, setNarrow] = useState(false);
  const [chosenView, setChosenView] = useState<ViewType>();
  const [showCharts, setShowCharts] = useState(false);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 720px)");
    const update = () => setNarrow(query.matches);
    update(); query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => { setShowCharts(false); }, [current.id, previous.id]);
  const viewType = chosenView || (narrow ? "unified" : "split");
  const renderGutter: RenderGutter = ({ change, side, renderDefault }) => {
    const old = side === "old";
    const number = old ? computeOldLineNumber(change) : computeNewLineNumber(change);
    const source = (old ? comparison.before : comparison.after).lines[number - 1];
    if (!source?.blockId) return renderDefault();
    const version = old ? previous : current;
    return <button type="button" className="comparison-line-link" aria-label={`定位${old ? "修改前" : "修改后"} ${revisionLabel(version)} 第 ${number} 行对应内容`} title={`定位 ${revisionLabel(version)} 的报告内容`} onClick={event => { event.stopPropagation(); navigate({ view: "report", research: version.research_id || null, version: version.id, diff: old ? null : previous.id, anchor: source.blockId!, message: null }); }}>{renderDefault()}</button>;
  };
  const hiddenLines = (start: number, end: number, key: string) => end > start ? <Decoration key={key} className="comparison-omitted"><button type="button" onClick={() => expand(start, Math.min(start + 25, end))}>展开 {Math.min(25, end - start)} 行未改动内容{end - start > 25 ? ` · 共 ${end - start} 行` : ""}</button></Decoration> : null;
  const rows = renderingHunks.flatMap((hunk, index) => {
    const previousHunk = renderingHunks[index - 1];
    const start = previousHunk ? previousHunk.oldStart + previousHunk.oldLines : 1;
    const omitted = hiddenLines(start, hunk.oldStart, `before-${hunk.oldStart}`);
    return [...(omitted ? [omitted] : []), <Hunk key={`${hunk.oldStart}-${hunk.newStart}`} hunk={hunk} />];
  });
  const lastHunk = renderingHunks.at(-1);
  const tail = lastHunk ? hiddenLines(lastHunk.oldStart + lastHunk.oldLines, comparison.before.lines.length + 1, "after-last") : null;
  if (tail) rows.push(tail);
  return <section className="report-comparison" aria-label={`${labels.title}：${labels.before} 与 ${labels.after}`}>
    <header className="comparison-heading"><div><span className="comparison-eyebrow">{labels.title}</span><h2>{labels.before} <span aria-hidden="true">→</span> {labels.after}</h2></div><div className="comparison-view-switch" role="group" aria-label="对比展示方式"><button type="button" aria-pressed={viewType === "split"} onClick={() => setChosenView("split")}>左右并排</button><button type="button" aria-pressed={viewType === "unified"} onClick={() => setChosenView("unified")}>单列对照</button></div></header>
    <div className="comparison-record-summaries" aria-label="对比记录摘要"><div><span>修改前 · {revisionLabel(previous)}</span><p>{previous.summary || (!previous.parent_id ? "首次生成报告" : "报告内容已保存")}</p></div><div><span>修改后 · {revisionLabel(current)}</span><p>{current.summary || (!current.parent_id ? "首次生成报告" : "报告内容已保存")}</p></div></div>
    <div className="comparison-summary"><strong>报告改动 {comparison.changeCount} 处</strong><span className="comparison-line-count"><span>新增 {comparison.insertedLines} 行</span><span>删除 {comparison.deletedLines} 行</span></span></div>
    {comparison.changeCount > 0 && <p className="comparison-categories">{Object.entries(comparison.summary).filter(([, count]) => count > 0).map(([kind, count]) => `${summaryLabels[kind as keyof ReportChangeSummary]} ${count} 处`).join(" · ")}</p>}
    {!comparison.hunks.length ? <p className="comparison-no-changes">{comparison.changeCount === 0 ? "两份报告内容没有变化。" : comparison.charts.length ? "正文、表格与排版文字未变化；图表变化可在下方展开查看。" : "正文与排版的可读内容未变化，保存信息存在调整。"}</p> : <><div className={`comparison-column-head comparison-column-head-${viewType}`}><span>修改前 · {labels.before}</span><span>修改后 · {labels.after}</span></div><p className="comparison-line-hint">点击行号可定位对应记录的报告内容。</p><div className="comparison-diff-scroll" tabIndex={0} aria-label="逐行对照内容"><Diff viewType={viewType} diffType="modify" hunks={renderingHunks} tokens={tokens} renderGutter={renderGutter}>{() => rows}</Diff></div></>}
    {comparison.charts.length > 0 && <section className="comparison-charts"><button type="button" className="comparison-charts-toggle" aria-expanded={showCharts} onClick={() => setShowCharts(value => !value)}><span>图表变化</span><span>{comparison.charts.length} 个图表 · {showCharts ? "收起预览 ↑" : "展开两个版本的预览 ↓"}</span></button>{showCharts && comparison.charts.map(pair => <div className="comparison-chart-pair" key={pair.id}><ChartSnapshot block={pair.previous} version={previous} projectId={projectId} navigate={navigate} /><ChartSnapshot block={pair.current} version={current} projectId={projectId} navigate={navigate} /></div>)}</section>}
  </section>;
}
