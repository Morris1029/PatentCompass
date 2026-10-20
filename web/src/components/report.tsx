"use client";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import { api, describe, Json, Page, post, put, refresh, useApi, useReportVersion } from "@/lib/api";
import { useStore } from "@/lib/store";
import { ChartCard, ChartOption } from "./charts";
import { DataTable, Empty, ErrorNotice, Modal, TaskCard, ProjectTasks } from "./shared";
import type { Navigate } from "./workspace";

const ReportComparison = dynamic(() => import("./report-comparison"), { loading: () => <p className="notice" role="status">正在准备版本对比…</p>, ssr: false });

function TextBlock({ block, editable, changed }: { block: Json; editable: boolean; changed: (id: string, text: string) => void }) {
  const editor = useEditor({ immediatelyRender: false, editable, extensions: [StarterKit.configure({ heading: false }), Placeholder.configure({ placeholder: "输入正文" })], content: { type: "doc", content: [{ type: "paragraph", content: block.text ? [{ type: "text", text: block.text }] : [] }] }, onUpdate: ({ editor }) => changed(block.id, editor.getText()) });
  useEffect(() => { editor?.setEditable(editable); }, [editable, editor]);
  return <EditorContent editor={editor} className={block.type === "heading" ? "report-heading-editor" : "report-paragraph-editor"} />;
}
export default function Report({ project, navigate }: { project: Json; navigate: Navigate }) {
  const params = useSearchParams(); const reportId = project.report_id;
  const [browseVersion, setBrowseVersion] = useState<string | undefined>(project.current_report_version_id);
  const selectedVersion = params.get("version") || browseVersion || project.current_report_version_id;
  useEffect(() => { if (!browseVersion && project.current_report_version_id) setBrowseVersion(project.current_report_version_id); }, [browseVersion, project.current_report_version_id]);
  const directoryPath = reportId ? `/api/reports/${reportId}/versions?summary=true&limit=50` : null;
  const { data: versions, error: versionsError } = useApi<Page>(directoryPath);
  const [versionPages, setVersionPages] = useState<Page[]>([]); const [directoryBusy, setDirectoryBusy] = useState(false);
  const directoryRef = useRef(versions); directoryRef.current = versions;
  useEffect(() => { setVersionPages([]); }, [reportId, versions]);
  const { data: version, error: versionError } = useReportVersion(reportId, selectedVersion);
  const compareSelection = params.get("diff"); const compareId = compareSelection === "parent" ? version?.parent_id : compareSelection;
  const { data: comparison, error: comparisonError } = useReportVersion(reportId, compareId);
  const versionItems = Array.from(new Map([...(versions?.items || []), ...versionPages.flatMap(page => page.items), ...[version, comparison].filter((item): item is Json => !!item)].map(item => [item.id, item])).values()).sort((a, b) => b.number - a.number);
  const nextVersionCursor = (versionPages.at(-1) || versions)?.next_cursor;
  const { data: analysis } = useApi(`/api/projects/${project.id}/analysis?view=overview`);
  const [error, setError] = useState<unknown>(); const [run, setRun] = useState<string>(); const [busy, setBusy] = useState(false);
  const moreVersions = async () => {
    if (!directoryPath || !nextVersionCursor || directoryBusy) return;
    const directory = versions; setDirectoryBusy(true); setError(undefined);
    try { const page = await api<Page>(`${directoryPath}&cursor=${encodeURIComponent(nextVersionCursor)}`); if (directoryRef.current === directory) setVersionPages(pages => [...pages, page]); }
    catch (e) { setError(e); } finally { setDirectoryBusy(false); }
  };
  const [title, setTitle] = useState(""); const [edits, setEdits] = useState<Record<string, string>>({}); const [mode, setMode] = useState<"modify" | "chart" | "layout" | "blocks" | "insert">();
  const [selected, setSelected] = useState<{ ids: string[]; text: string }>(); const [instruction, setInstruction] = useState(""); const [replacement, setReplacement] = useState(""); const [editMode, setEditMode] = useState("instruction");
  const [insertType, setInsertType] = useState("paragraph"); const [afterId, setAfterId] = useState(""); const [chartChoice, setChartChoice] = useState("");
  const [fontSize, setFontSize] = useState(10.5); const [chartWidth, setChartWidth] = useState(100); const [alignment, setAlignment] = useState("center"); const [selectedChart, setSelectedChart] = useState("");
  const track = useStore(s => s.track); const setDraft = useStore(s => s.draft); const paper = useRef<HTMLElement>(null);
  const currentId = versions?.current_version_id || project.current_report_version_id; const isCurrent = !!version && version.id === currentId;
  const content = version?.content; const blocks: Json[] = content?.blocks || [];
  const changed = (id: string, text: string) => setEdits(previous => ({ ...previous, [id]: text }));
  useEffect(() => { setTitle(content?.title || ""); setEdits({}); setSelected(undefined); setInstruction(""); }, [version?.id]);
  useEffect(() => { const anchor = params.get("anchor"); if (anchor && version) setTimeout(() => document.getElementById(`report-${anchor}`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 100); }, [params, version]);
  const operation = async (operations: Json[]) => {
    if (!version) return;
    setError(undefined); setBusy(true);
    try {
      const task = await post(`/api/reports/${reportId}/revisions`, { base_version_id: version.id, expected_current_version_id: currentId, operations, ...(params.get("conversation") ? { conversation_id: params.get("conversation") } : {}) });
      setRun(task.run_id); track(project.id, task.run_id); setMode(undefined); await refresh();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const save = () => {
    const operations: Json[] = [];
    if (title !== content.title) operations.push({ op: "set_title", text: title });
    Object.entries(edits).forEach(([id, text]) => { if (text !== blocks.find(block => block.id === id)?.text) operations.push({ op: "replace_text", block_id: id, text }); });
    if (operations.length) operation(operations);
  };
  const adopt = async () => { if (!version) return; try { await put(`/api/reports/${reportId}/current-version`, { version_id: version.id, expected_current_version_id: currentId }); await refresh(); setError(undefined); } catch (e) { setError(e); } };
  const generate = async (ai: boolean) => { setBusy(true); try { const task = await post(`/api/projects/${project.id}/runs`, { action: "generate_report", report_mode: ai ? "ai" : "statistical", semantic_mode: "off", ...(project.current_analysis_id ? { analysis_id: project.current_analysis_id } : {}) }); setRun(task.run_id); track(project.id, task.run_id); await refresh(); } catch (e) { setError(e); } finally { setBusy(false); } };
  const exportWord = async () => { if (!version) return; try { const task = await post(`/api/reports/${reportId}/exports`, { report_version_id: version.id, format: "docx" }); setRun(task.run_id); track(project.id, task.run_id); } catch (e) { setError(e); } };
  const select = () => {
    const selection = window.getSelection(); if (!selection || selection.isCollapsed || !paper.current?.contains(selection.anchorNode) || !paper.current?.contains(selection.focusNode)) return;
    const range = selection.getRangeAt(0); const ids = Array.from(paper.current.querySelectorAll<HTMLElement>("[data-report-block]")).filter(node => range.intersectsNode(node)).map(node => node.dataset.reportBlock!).filter(Boolean);
    if (ids.length) setSelected({ ids, text: selection.toString().slice(0, 20000) });
  };
  const quote = () => { if (!selected || !version) return; const conversationId = params.get("conversation"); setDraft(`${project.id}:${conversationId || "draft"}`, { reference: { report_version_id: version.id, ...(selected.ids.length === 1 ? { block_id: selected.ids[0] } : { block_ids: selected.ids }), text: selected.text } }); navigate({ view: "chat" }); };
  const modify = async () => {
    if (!selected?.ids.length || !version) return;
    if (editMode === "replacement") { const texts = selected.ids.length === 1 ? [replacement] : replacement.split("\n\n"); if (texts.length !== selected.ids.length) { setError(new Error("请按选择的文字块数量，用空行分隔替换内容。")); return; } await operation(selected.ids.map((id, index) => ({ op: "replace_text", block_id: id, text: texts[index] }))); return; }
    if (selected.ids.length === 1) {
      try { const task = await post(`/api/reports/${reportId}/revisions`, { base_version_id: version.id, expected_current_version_id: currentId, block_id: selected.ids[0], instruction, ...(params.get("conversation") ? { conversation_id: params.get("conversation") } : {}) }); setRun(task.run_id); track(project.id, task.run_id); setMode(undefined); } catch (e) { setError(e); }
    } else {
      const conversation = params.get("conversation"); const body = { content: instruction, action: "auto", reference: { report_version_id: version.id, block_ids: selected.ids, text: selected.text }, attachments: [], report_mode: "ai", semantic_mode: "full" };
      try { const task = conversation ? await post(`/api/conversations/${conversation}/messages`, body) : await post(`/api/projects/${project.id}/conversations`, { initial_message: body }); setRun(task.run_id); track(project.id, task.run_id); setMode(undefined); await refresh(); } catch (e) { setError(e); }
    }
  };
  const completed = (result: Json) => { if (result.report_version_id) { navigate({ version: result.report_version_id, diff: result.parent_id || (result.report_version_id !== version?.id ? version?.id || null : null) }); refresh(); } };
  const dirty = title !== content?.title || Object.entries(edits).some(([id, text]) => text !== blocks.find(block => block.id === id)?.text);
  const chapterRange = (index: number) => {
    const level = blocks[index].level || 1;
    let end = index + 1;
    while (end < blocks.length && !(blocks[end].type === "heading" && (blocks[end].level || 1) <= level)) end++;
    return { level, end, items: blocks.slice(index, end) };
  };
  const chapterTarget = (index: number, direction: "up" | "down"): string | null | undefined => {
    const range = chapterRange(index);
    if (direction === "up") {
      for (let previous = index - 1; previous >= 0; previous--) {
        if (blocks[previous].type !== "heading") continue;
        const level = blocks[previous].level || 1;
        if (level < range.level) return undefined;
        if (level === range.level) return previous > 0 ? blocks[previous - 1].id : null;
      }
      return undefined;
    }
    if (range.end >= blocks.length || (blocks[range.end].level || 1) !== range.level) return undefined;
    return blocks[chapterRange(range.end).end - 1].id;
  };
  const move = (index: number, direction: "up" | "down") => {
    const block = blocks[index];
    if (block.type !== "heading") { operation([{ op: "move_block", block_id: block.id, after_id: direction === "up" ? (index > 1 ? blocks[index - 2].id : null) : blocks[index + 1].id }]); return; }
    const target = chapterTarget(index, direction);
    if (target === undefined) return;
    const group = chapterRange(index).items;
    operation(group.map((item, offset) => ({ op: "move_block", block_id: item.id, after_id: offset ? group[offset - 1].id : target })));
  };
  const remove = (index: number) => operation((blocks[index].type === "heading" ? chapterRange(index).items : [blocks[index]]).map(block => ({ op: "remove_block", block_id: block.id })));
  return <section className="page wide report-workspace" aria-label="导航报告"><div className="section-head report-page-head"><div><span className="label">项目报告 · 版本独立保存</span><h1>导航报告</h1></div><div className="actions"><button className="btn secondary" disabled={busy || !project.dataset_version_id} onClick={() => generate(false)}>生成统计简报</button><button className="btn primary" disabled={busy || !project.dataset_version_id} onClick={() => generate(true)}>生成 AI 导航报告</button></div></div><ErrorNotice error={error || versionsError || versionError} />
    <ProjectTasks project={project} kinds={["generate_report", "revise_report", "export"]} local={run} onResult={completed} />
    {!reportId ? <Empty title="项目尚无导航报告"><p>完成数据绑定并确认范围后，可先生成包含真实年度、申请人、IPC 与原文依据的统计简报。</p></Empty> : !version ? <p className="notice" role="status">{versionError ? "无法读取此报告版本。" : "正在读取报告…"}</p> : <>
      <div className="report-version-toolbar"><label>浏览版本 <select value={version.id} onChange={e => navigate({ version: e.target.value, diff: null, anchor: null })}>{versionItems.map(item => <option value={item.id} key={item.id}>V{item.number}{item.id === currentId ? " · 当前采用" : ""} · {item.summary?.slice(0, 24)}</option>)}</select></label>{!isCurrent && <button className="btn secondary" onClick={adopt}>切回此版本</button>}<label>比较版本 <select value={compareId || ""} onChange={e => navigate({ diff: e.target.value || null })}><option value="">不比较</option>{versionItems.filter(item => item.id !== version.id).map(item => <option key={item.id} value={item.id}>V{item.number}</option>)}</select></label>{nextVersionCursor && <button className="text-button" disabled={directoryBusy} onClick={moreVersions}>{directoryBusy ? "正在读取版本目录…" : "加载更早版本"}</button>}<button className="text-button" onClick={exportWord}>导出此版本 Word ↓</button></div>
      {!isCurrent && <p className="notice">正在浏览历史 V{version.number}，当前采用版本保持不变。需要以此继续修改时，请先切回此版本。</p>}
      <p className="small muted report-meta">V{version.number} · {new Date(version.created_at).toLocaleString("zh-CN")} · {version.summary}{version.parent_id && " · 保留父版本与分支"}{version.conversation_id && <button className="text-button" onClick={() => navigate({ view: "chat", conversation: version.conversation_id })}>来源对话 →</button>}</p>
      {compareSelection && (comparisonError ? <ErrorNotice error={comparisonError} /> : comparison ? <ReportComparison key={`${comparison.id}:${version.id}`} current={version} previous={comparison} projectId={project.id} navigate={navigate} /> : compareId ? <p className="notice" role="status">正在读取比较版本…</p> : <p className="notice">此版本为首版，没有父版本可比较。</p>)}
      <div className="report-edit-toolbar"><button className="btn secondary" disabled={!isCurrent} onClick={() => { setMode("insert"); setReplacement(""); }}>添加段落 / 章节</button><button className="btn secondary" disabled={!isCurrent || blocks.some(block => block.section === "roadmap")} title={blocks.some(block => block.section === "roadmap") ? "此版本已包含布局与路线图章节" : "从冻结分析追加真实章节"} onClick={() => operation([{ op: "append_roadmap" }])}>{blocks.some(block => block.section === "roadmap") ? "已包含布局与路线图" : "完善布局与路线图"}</button><button className="btn secondary" disabled={!isCurrent} onClick={() => setMode("chart")}>插入 / 排版图表</button><button className="btn secondary" disabled={!isCurrent} onClick={() => setMode("blocks")}>章节与顺序</button><button className="btn secondary" disabled={!isCurrent} onClick={() => setMode("layout")}>页面设置</button><button className="btn primary" disabled={!isCurrent || !dirty || busy} onClick={save}>保存为新版本</button></div>
      {selected && <div className="report-selection-toolbar" role="toolbar" aria-label="所选报告文字操作"><span className="small">已选 {selected.ids.length} 个文字块</span><button className="text-button" disabled={!isCurrent} onClick={() => { setMode("modify"); setReplacement(selected.ids.map(id => edits[id] ?? blocks.find(block => block.id === id)?.text ?? "").join("\n\n")); }}>修改所选文字</button><button className="text-button" onClick={quote}>引用到对话</button><button className="icon-button" onClick={() => setSelected(undefined)} aria-label="取消文字选择">×</button></div>}
      <article ref={paper} className="report-paper" style={{ fontSize: `${content?.layout?.body_font_pt || 10.5}pt`, fontFamily: content?.layout?.font_family ? `${content.layout.font_family}, var(--font)` : undefined }} onMouseUp={select} onKeyUp={select} aria-label={`导航报告版本 ${version.number}`}><input className="report-title-editor" aria-label="报告标题" value={title} readOnly={!isCurrent} onChange={e => setTitle(e.target.value)} /><p className="report-subtitle">依据冻结的数据、研究范围与图表版本生成</p>{blocks.map(block => <section key={`${version.id}-${block.id}`} id={`report-${block.id}`} data-report-block={["heading", "paragraph"].includes(block.type) ? block.id : undefined} className={`report-block report-block-${block.type} ${params.get("anchor") === block.id ? "is-targeted" : ""}`}>
        {block.type === "heading" || block.type === "paragraph" ? <TextBlock block={block} editable={isCurrent} changed={changed} /> : block.type === "table" ? <>{block.title && <h3>{block.title}</h3>}<DataTable headings={block.headers || block.columns || []} rows={(block.rows || []).map((row: unknown) => Array.isArray(row) ? row.map(cell => describe(cell)) : Object.values(row as Json).map(describe))} />{block.caption && <p className="report-chart-caption">{block.caption}</p>}</> : block.type === "chart" ? <div style={{ width: `${block.layout?.width_percent || 100}%`, marginLeft: block.layout?.alignment === "right" ? "auto" : block.layout?.alignment === "center" ? "auto" : 0, marginRight: block.layout?.alignment === "center" ? "auto" : 0 }}><ChartCard id={block.chart_id || block.chart?.id} projectId={project.id} frozen={block.chart} /><p className="report-chart-caption">{block.caption || block.title}</p></div> : <p className="muted">此内容类型暂无法显示：{block.type}</p>}
      </section>)}<footer className="report-footnote">证据与引用保存在此报告版本内。相关趋势、候选方向及布局建议受样本覆盖与字段完整性限制。</footer></article>
    </>}
    {mode === "modify" && selected && <Modal title={`修改所选 ${selected.ids.length} 个文字块`} close={() => setMode(undefined)}><blockquote className="quote"><p className="small">{selected.text.slice(0, 600)}</p></blockquote><div className="segmented"><button className={editMode === "instruction" ? "active" : ""} onClick={() => setEditMode("instruction")}>用自然语言修改</button><button className={editMode === "replacement" ? "active" : ""} onClick={() => setEditMode("replacement")}>直接替换文字</button></div>{editMode === "instruction" ? <label className="form-field">修改要求<textarea autoFocus rows={4} value={instruction} onChange={e => setInstruction(e.target.value)} placeholder="说明要修改的目标、保留的事实及期望表达" /></label> : <label className="form-field">替换内容{selected.ids.length > 1 && <small>按原顺序使用空行分隔每个文字块。</small>}<textarea rows={9} value={replacement} onChange={e => setReplacement(e.target.value)} /></label>}<ErrorNotice error={error} /><button className="btn primary" disabled={busy || (editMode === "instruction" && !instruction.trim())} onClick={modify}>提交并采用新版本</button></Modal>}
    {mode === "insert" && <Modal title="添加正文或章节" close={() => setMode(undefined)}><label className="form-field">类型<select value={insertType} onChange={e => setInsertType(e.target.value)}><option value="paragraph">正文段落</option><option value="heading">章节标题</option></select></label><BlockPosition blocks={blocks} value={afterId} change={setAfterId} /><label className="form-field">文字内容<textarea rows={5} value={replacement} onChange={e => setReplacement(e.target.value)} /></label><button className="btn primary" disabled={!replacement.trim() || busy} onClick={() => operation([{ op: "insert_block", after_id: afterId || null, block: { id: crypto.randomUUID(), type: insertType, text: replacement } }])}>保存新版本</button></Modal>}
    {mode === "blocks" && <Modal title="章节、内容与顺序" close={() => setMode(undefined)} wide><p className="small muted">每次明确的移除或移动都产生新版本；原有版本继续保留。</p>{blocks.map((block, index) => <div className="block-row" key={block.id}><span className="label">{block.type === "heading" ? "章节" : block.type === "chart" ? "图表" : "正文"}</span><span>{block.text?.slice(0, 70) || block.title || block.caption || block.id}</span><button className="text-button" disabled={block.type === "heading" ? chapterTarget(index, "up") === undefined : index === 0} onClick={() => move(index, "up")}>{block.type === "heading" ? "上移章节" : "上移"}</button><button className="text-button" disabled={block.type === "heading" ? chapterTarget(index, "down") === undefined : index === blocks.length - 1} onClick={() => move(index, "down")}>{block.type === "heading" ? "下移章节" : "下移"}</button><button className="text-button" onClick={() => remove(index)}>{block.type === "heading" ? "移除章节" : "移除"}</button></div>)}</Modal>}
    {mode === "chart" && <Modal title="图表插入与排版" close={() => setMode(undefined)} wide><section><h3>插入工作台图表</h3><label className="form-field">图表<select value={chartChoice} onChange={e => setChartChoice(e.target.value)}><option value="">选择图表</option>{(analysis?.data?.chart_ids || []).map((id: string) => <ChartOption key={id} id={id} />)}</select></label><BlockPosition blocks={blocks} value={afterId} change={setAfterId} />{chartChoice && <ChartCard id={chartChoice} projectId={project.id} onInsert={chart => operation([{ op: "insert_chart", chart_id: chart.id, chart_version_id: chart.version_id, after_id: afterId || null }])} />}<button className="btn primary" disabled={!chartChoice || busy} onClick={async () => { try { const chart = await api(`/api/charts/${chartChoice}`); await operation([{ op: "insert_chart", chart_id: chartChoice, chart_version_id: chart.version_id, after_id: afterId || null }]); } catch (e) { setError(e); } }}>插入选定图表</button></section><section className="section"><h3>已插入图表排版</h3><label className="form-field">报告图位<select value={selectedChart} onChange={e => setSelectedChart(e.target.value)}><option value="">选择图位</option>{blocks.filter(block => block.type === "chart").map(block => <option key={block.id} value={block.id}>{block.title || block.caption || block.chart?.option?.title?.text || block.id}</option>)}</select></label><div className="form-grid"><label className="form-field">宽度<select value={chartWidth} onChange={e => setChartWidth(Number(e.target.value))}><option value={100}>整行 100%</option><option value={50}>半行 50%</option></select></label><label className="form-field">对齐<select value={alignment} onChange={e => setAlignment(e.target.value)}><option value="left">左对齐</option><option value="center">居中</option><option value="right">右对齐</option></select></label></div><div className="actions"><button className="btn primary" disabled={!selectedChart} onClick={() => operation([{ op: "set_chart_layout", block_id: selectedChart, layout: { width_percent: chartWidth, alignment } }])}>保存图表排版</button><button className="btn secondary" disabled={!selectedChart} onClick={async () => { const block = blocks.find(item => item.id === selectedChart)!; const chart = await api(`/api/charts/${block.chart_id || block.chart?.id}`); operation([{ op: "update_chart", block_id: selectedChart, chart_id: chart.id, chart_version_id: chart.version_id }]); }}>明确更新至工作台采用版本</button><button className="text-button" disabled={!selectedChart} onClick={() => operation([{ op: "remove_block", block_id: selectedChart }])}>移除此图位</button></div></section><ErrorNotice error={error} /></Modal>}
    {mode === "layout" && <Modal title="报告页面设置" close={() => setMode(undefined)}><p className="small muted">页面与 Word 采用同一版本的排版设置。</p><label className="form-field">正文字号<input type="number" value={fontSize} min="8" max="24" step="0.5" onChange={e => setFontSize(Number(e.target.value))} /></label><button className="btn primary" onClick={() => operation([{ op: "set_layout", layout: { body_font_pt: fontSize } }])}>保存排版新版本</button><ErrorNotice error={error} /></Modal>}
  </section>;
}
function BlockPosition({ blocks, value, change }: { blocks: Json[]; value: string; change: (value: string) => void }) { return <label className="form-field">插入位置<select value={value} onChange={e => change(e.target.value)}><option value="">正文开头</option>{blocks.map(block => <option key={block.id} value={block.id}>在 {block.text?.slice(0, 35) || block.title || block.id} 之后</option>)}</select></label>; }
