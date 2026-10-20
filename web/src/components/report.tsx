"use client";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import { api, describe, Json, Page, post, put, refresh, useApi, useReportVersion } from "@/lib/api";
import { useStore } from "@/lib/store";
import { researchLabel, revisionLabel } from "@/lib/research";
import { ChartCard, ChartOption } from "./charts";
import { DataTable, Empty, ErrorNotice, Modal, TaskCard } from "./shared";
import type { Navigate } from "./workspace";

const ReportComparison = dynamic(() => import("./report-comparison"), { loading: () => <p className="notice" role="status">正在准备版本对比…</p>, ssr: false });

function TextBlock({ block, editable, changed }: { block: Json; editable: boolean; changed: (id: string, text: string) => void }) {
  const editor = useEditor({ immediatelyRender: false, editable, extensions: [StarterKit.configure({ heading: false }), Placeholder.configure({ placeholder: "输入正文" })], content: { type: "doc", content: [{ type: "paragraph", content: block.text ? [{ type: "text", text: block.text }] : [] }] }, onUpdate: ({ editor }) => changed(block.id, editor.getText()) });
  useEffect(() => { editor?.setEditable(editable); }, [editable, editor]);
  return <EditorContent editor={editor} className={block.type === "heading" ? "report-heading-editor" : "report-paragraph-editor"} />;
}
export default function Report({ project, navigate }: { project: Json; navigate: Navigate }) {
  const params = useSearchParams(); const reportId = project.report_id;
  const { data: linkedVersion } = useReportVersion(reportId, params.get("version"));
  const researchId = linkedVersion?.research_id || params.get("research") || project.current_research_id;
  const { data: research, error: researchError } = useApi(researchId ? `/api/research-versions/${researchId}` : null);
  const [editingVersionId, setEditingVersionId] = useState<string>();
  const { data: researches, error: researchesError } = useApi<Page>(reportId ? `/api/reports/${reportId}/research-versions` : null);
  const [researchPages, setResearchPages] = useState<Page[]>([]); const [researchBusy, setResearchBusy] = useState(false);
  const researchDirectory = useRef(researches); researchDirectory.current = researches;
  useEffect(() => { setResearchPages([]); }, [researches]);
  const researchItems = Array.from(new Map([...(researches?.items || []), ...researchPages.flatMap(page => page.items), ...(research ? [research] : [])].map(item => [item.id, item])).values());
  const researchCursor = (researchPages.at(-1) || researches)?.next_cursor;
  const selectedVersion = params.get("version") || editingVersionId || research?.current_revision_id || research?.current_version_id || (!researchId ? project.current_report_version_id : undefined);
  const directoryPath = reportId && researchId ? `/api/reports/${reportId}/versions?summary=true&limit=50&research_id=${researchId}` : null;
  const { data: versions, error: versionsError } = useApi<Page>(directoryPath);
  const [versionPages, setVersionPages] = useState<Page[]>([]); const [directoryBusy, setDirectoryBusy] = useState(false);
  const directoryRef = useRef(versions); directoryRef.current = versions;
  useEffect(() => { setVersionPages([]); }, [directoryPath, versions]);
  const { data: version, error: versionError } = useReportVersion(reportId, selectedVersion);
  const compareSelection = params.get("diff"); const compareId = compareSelection === "parent" ? version?.parent_id : compareSelection;
  const { data: comparison, error: comparisonError } = useReportVersion(reportId, compareId);
  const versionItems = Array.from(new Map([...(versions?.items || []), ...versionPages.flatMap(page => page.items), ...[version, comparison].filter((item): item is Json => !!item && item.research_id === researchId)].map(item => [item.id, item])).values()).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const nextVersionCursor = (versionPages.at(-1) || versions)?.next_cursor;
  const { data: analysis } = useApi(`/api/projects/${project.id}/analysis?view=overview${researchId ? `&research_id=${researchId}` : ""}${version?.analysis_id ? `&analysis_id=${version.analysis_id}` : ""}`);
  const [error, setError] = useState<unknown>(); const [run, setRun] = useState<string>(); const [busy, setBusy] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false); const [restoreTarget, setRestoreTarget] = useState<Json>(); const [adopting, setAdopting] = useState(false);
  const [transfer, setTransfer] = useState<{ conversationId: string; reference: Json; content?: string }>();
  const moreVersions = async () => {
    if (!directoryPath || !nextVersionCursor || directoryBusy) return;
    const directory = versions; setDirectoryBusy(true); setError(undefined);
    try { const page = await api<Page>(`${directoryPath}&cursor=${encodeURIComponent(nextVersionCursor)}`); if (directoryRef.current === directory) setVersionPages(pages => [...pages, page]); }
    catch (e) { setError(e); } finally { setDirectoryBusy(false); }
  };
  const moreResearches = async () => { if (!researchCursor || researchBusy) return; const directory = researches; setResearchBusy(true); try { const page = await api<Page>(`/api/reports/${reportId}/research-versions?cursor=${encodeURIComponent(researchCursor)}`); if (researchDirectory.current === directory) setResearchPages(pages => [...pages, page]); } catch (e) { setError(e); } finally { setResearchBusy(false); } };
  const [title, setTitle] = useState(""); const [edits, setEdits] = useState<Record<string, string>>({}); const [mode, setMode] = useState<"modify" | "chart" | "layout" | "blocks" | "insert">();
  const [selected, setSelected] = useState<{ ids: string[]; text: string }>(); const [instruction, setInstruction] = useState(""); const [replacement, setReplacement] = useState(""); const [editMode, setEditMode] = useState("instruction");
  const [insertType, setInsertType] = useState("paragraph"); const [afterId, setAfterId] = useState(""); const [chartChoice, setChartChoice] = useState("");
  const [fontSize, setFontSize] = useState(10.5); const [chartWidth, setChartWidth] = useState(100); const [alignment, setAlignment] = useState("center"); const [selectedChart, setSelectedChart] = useState("");
  const track = useStore(s => s.track); const setDraft = useStore(s => s.draft); const paper = useRef<HTMLElement>(null);
  const currentId = research?.current_revision_id || research?.current_version_id || versions?.current_version_id || (!researchId ? project.current_report_version_id : undefined); const isCurrent = !!version && version.id === currentId;
  const contextProject = { ...project, ...research?.context, research_id: researchId, research_number: research?.number, current_report_version_id: currentId };
  const content = version?.content; const blocks: Json[] = content?.blocks || [];
  const changed = (id: string, text: string) => { setEditingVersionId(version?.id); setEdits(previous => ({ ...previous, [id]: text })); };
  useEffect(() => { setEditingVersionId(undefined); }, [researchId, params.get("version")]);
  useEffect(() => { setTitle(content?.title || ""); setEdits({}); setSelected(undefined); setInstruction(""); }, [version?.id]);
  useEffect(() => { const anchor = params.get("anchor"); if (anchor && version) setTimeout(() => document.getElementById(`report-${anchor}`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 100); }, [params, version]);
  const operation = async (operations: Json[]) => {
    if (!version) return;
    setError(undefined); setBusy(true);
    try {
      const task = await post(`/api/reports/${reportId}/revisions`, { base_version_id: version.id, expected_current_version_id: currentId, research_id: researchId, operations });
      setRun(task.run_id); track(project.id, task.run_id); setMode(undefined); await refresh();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const save = () => {
    const operations: Json[] = [];
    if (title !== content.title) operations.push({ op: "set_title", text: title });
    Object.entries(edits).forEach(([id, text]) => { if (text !== blocks.find(block => block.id === id)?.text) operations.push({ op: "replace_text", block_id: id, text }); });
    if (operations.length) operation(operations);
  };
  const adopt = async () => { if (!researchId) return; setBusy(true); try { await put(`/api/research-versions/${researchId}/adopt`, { expected_current_version_id: project.current_report_version_id }); setAdopting(false); await refresh(); setError(undefined); } catch (e) { setError(e); } finally { setBusy(false); } };
  const restore = async () => { if (!restoreTarget || !researchId) return; setBusy(true); try { const task = await post(`/api/research-versions/${researchId}/restore`, { revision_id: restoreTarget.id, expected_current_version_id: currentId, expected_adopted_version_id: project.current_report_version_id }); setRun(task.run_id); track(project.id, task.run_id); setRestoreTarget(undefined); setHistoryOpen(false); await refresh(); } catch (e) { setError(e); } finally { setBusy(false); } };
  const newResearch = async () => { if (!reportId || busy) return; setBusy(true); try { const result = await post(`/api/reports/${reportId}/research-versions`, { base_revision_id: version?.id || project.current_report_version_id }); await refresh(); navigate({ view: "chat", conversation: result.conversation_id, research: result.research_id, version: null, diff: null, anchor: null, message: null }); } catch (e) { setError(e); } finally { setBusy(false); } };
  const openMain = async () => { if (!researchId) return; try { const conversationId = research?.main_conversation_id || version?.main_conversation_id || (await post(`/api/research-versions/${researchId}/conversation`)).conversation_id; navigate({ view: "chat", research: researchId, conversation: conversationId, version: null, diff: null, anchor: null, message: null }); } catch (e) { setError(e); } };
  const generate = async (ai: boolean) => { setBusy(true); try { const task = await post(`/api/projects/${project.id}/runs`, { action: "generate_report", report_mode: ai ? "ai" : "statistical", semantic_mode: "off", ...(project.current_analysis_id ? { analysis_id: project.current_analysis_id } : {}) }); setRun(task.run_id); track(project.id, task.run_id); await refresh(); } catch (e) { setError(e); } finally { setBusy(false); } };
  const exportWord = async () => { if (!version) return; try { const task = await post(`/api/reports/${reportId}/exports`, { report_version_id: version.id, format: "docx" }); setRun(task.run_id); track(project.id, task.run_id); } catch (e) { setError(e); } };
  const select = () => {
    const selection = window.getSelection(); if (!selection || selection.isCollapsed || !paper.current?.contains(selection.anchorNode) || !paper.current?.contains(selection.focusNode)) return;
    const range = selection.getRangeAt(0); const ids = Array.from(paper.current.querySelectorAll<HTMLElement>("[data-report-block]")).filter(node => range.intersectsNode(node)).map(node => node.dataset.reportBlock!).filter(Boolean);
    if (ids.length) setSelected({ ids, text: selection.toString().slice(0, 20000) });
  };
  const enterMain = async (content?: string) => {
    if (!selected || !version || !researchId) return;
    try {
      const conversationId = version.main_conversation_id || research?.main_conversation_id || (await post(`/api/research-versions/${researchId}/conversation`)).conversation_id;
      const reference = { report_version_id: version.id, ...(selected.ids.length === 1 ? { block_id: selected.ids[0] } : { block_ids: selected.ids }), text: selected.text };
      const pending = { conversationId, reference, content };
      if (useStore.getState().drafts[`${project.id}:${conversationId}`]?.reference) setTransfer(pending); else applyTransfer(pending);
    } catch (e) { setError(e); }
  };
  const applyTransfer = (pending: { conversationId: string; reference: Json; content?: string }) => {
    const key = `${project.id}:${pending.conversationId}`; const existing = useStore.getState().drafts[key];
    setDraft(key, { reference: pending.reference as { report_version_id: string; text: string }, ...(pending.content ? { content: [existing?.content, pending.content].filter(Boolean).join("\n\n") } : {}) });
    setTransfer(undefined); setMode(undefined); navigate({ view: "chat", conversation: pending.conversationId, research: researchId, version: null, diff: null, anchor: null, message: null });
  };
  const modify = async () => {
    if (!selected?.ids.length || !version) return;
    if (editMode === "replacement") { const texts = selected.ids.length === 1 ? [replacement] : replacement.split("\n\n"); if (texts.length !== selected.ids.length) { setError(new Error("请按选择的文字块数量，用空行分隔替换内容。")); return; } await operation(selected.ids.map((id, index) => ({ op: "replace_text", block_id: id, text: texts[index] }))); return; }
    await enterMain(instruction);
  };
  const completed = (result: Json) => { if (result.report_version_id) { setEditingVersionId(undefined); navigate({ research: result.research_id || researchId || null, version: null, diff: null, anchor: null }); refresh(); } };
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
  return <section className="page wide report-workspace" aria-label="导航报告"><div className="section-head report-page-head"><div><span className="label">{research?.number ? `研究 V${research.number}${researchId === project.current_research_id ? " · 当前采用" : ""}` : research?.status === "draft" ? "新研究 · 尚未生成报告" : "项目报告"}</span><h1>导航报告</h1></div><div className="actions">{reportId ? <><button className="btn secondary" disabled={busy || !researchId} onClick={openMain}>来源对话 →</button><button className="btn primary" disabled={busy} onClick={newResearch}>开启新一轮研究</button></> : <><button className="btn secondary" disabled={busy || !project.dataset_version_id} onClick={() => generate(false)}>生成统计简报</button><button className="btn primary" disabled={busy || !project.dataset_version_id} onClick={() => generate(true)}>生成 AI 导航报告</button></>}</div></div><ErrorNotice error={error || versionsError || versionError || researchError || researchesError} />
    {run && <TaskCard key={run} id={run} project={contextProject} onResult={completed} />}
    {reportId && <div className="report-version-toolbar"><label>研究版本 <select value={researchId || ""} onChange={e => navigate({ research: e.target.value, version: null, diff: null, anchor: null, message: null })}>{researchItems.map(item => <option value={item.id} key={item.id}>{item.number ? `V${item.number}` : "新研究 · 未完成"}{item.title ? ` · ${item.title}` : ""}{item.id === project.current_research_id ? " · 当前采用" : ""}</option>)}</select></label>{currentId && <button className="text-button" onClick={() => setHistoryOpen(true)}>修改记录</button>}{research?.status === "published" && researchId !== project.current_research_id && <button className="text-button" onClick={() => setAdopting(true)}>采用 V{research.number}</button>}{version && <><label>跨研究比较 <select value={comparison?.research_id !== researchId ? comparison?.research_id || "" : ""} onChange={e => { const item = researchItems.find(item => item.id === e.target.value); navigate({ diff: item?.current_revision_id || item?.current_version_id || null }); }}><option value="">选择另一研究版本</option>{researchItems.filter(item => item.status === "published" && item.id !== researchId).map(item => <option key={item.id} value={item.id}>V{item.number}{item.title ? ` · ${item.title}` : ""}{item.id === project.current_research_id ? " · 当前采用" : ""}</option>)}</select></label>{compareSelection && <button className="text-button" onClick={() => navigate({ diff: null })}>关闭比较</button>}<button className="text-button" onClick={exportWord}>导出 Word ↓</button></>}{researchCursor && <button className="text-button" disabled={researchBusy} onClick={moreResearches}>{researchBusy ? "正在读取…" : "更早研究版本"}</button>}</div>}
    {!reportId ? <Empty title="项目尚无导航报告"><p>完成数据绑定并确认范围后，可生成导航报告，日常调整将保存在同一研究版本的修改记录中。</p></Empty> : research?.status === "draft" && !selectedVersion ? <Empty title="新研究尚未生成报告"><p>在研究主对话中完善目标与范围，成功生成报告后才会获得新的 V 编号。已有报告继续保留。</p><button className="btn primary" onClick={openMain}>继续新研究 →</button></Empty> : !version ? <p className="notice" role="status">{versionError ? "无法读取此报告内容。" : "正在读取报告…"}</p> : <>
      {!isCurrent && <p className="notice">{editingVersionId && dirty ? "本研究已有新内容，你尚未保存的输入已保留。回到最新内容将放弃这些输入。" : "正在查看一条历史修改记录。"}<button className="text-button" onClick={() => { setEditingVersionId(undefined); navigate({ version: null, diff: null, anchor: null }); }}>回到 {researchLabel(version)} 最新内容</button><button className="text-button" onClick={() => setRestoreTarget(version)}>恢复此内容</button></p>}
      {isCurrent && researchId !== project.current_research_id && <p className="notice">正在查看 {researchLabel(version)}。在此修改仅更新该研究，不会替换当前采用的报告。</p>}
      <p className="small muted report-meta">{revisionLabel(version)} · {version.summary}{version.source_conversation_id && <button className="text-button" onClick={() => navigate({ view: "chat", conversation: version.source_conversation_id, research: researchId || null, message: version.source_message_id || null, version: null, diff: null, anchor: null })}>{version.source_message_id ? "来源消息 →" : "来源对话 →"}</button>}</p>
      {compareSelection && (comparisonError ? <ErrorNotice error={comparisonError} /> : comparison ? <ReportComparison key={`${comparison.id}:${version.id}`} current={version} previous={comparison} projectId={project.id} navigate={navigate} /> : compareId ? <p className="notice" role="status">正在读取比较版本…</p> : <p className="notice">此版本为首版，没有父版本可比较。</p>)}
      <div className="report-edit-toolbar"><button className="btn secondary" disabled={!isCurrent} onClick={() => { setMode("insert"); setReplacement(""); }}>添加段落 / 章节</button><button className="btn secondary" disabled={!isCurrent || blocks.some(block => block.section === "roadmap")} title={blocks.some(block => block.section === "roadmap") ? "此版本已包含布局与路线图章节" : "从冻结分析追加真实章节"} onClick={() => operation([{ op: "append_roadmap" }])}>{blocks.some(block => block.section === "roadmap") ? "已包含布局与路线图" : "完善布局与路线图"}</button><button className="btn secondary" disabled={!isCurrent} onClick={() => setMode("chart")}>插入 / 排版图表</button><button className="btn secondary" disabled={!isCurrent} onClick={() => setMode("blocks")}>章节与顺序</button><button className="btn secondary" disabled={!isCurrent} onClick={() => setMode("layout")}>页面设置</button><button className="btn primary" disabled={!isCurrent || !dirty || busy} onClick={save}>保存修改</button></div>
      {selected && <div className="report-selection-toolbar" role="toolbar" aria-label="所选报告文字操作"><span className="small">已选 {selected.ids.length} 个文字块</span><button className="text-button" disabled={!isCurrent} onClick={() => { setMode("modify"); setReplacement(selected.ids.map(id => edits[id] ?? blocks.find(block => block.id === id)?.text ?? "").join("\n\n")); }}>修改所选文字</button><button className="text-button" onClick={() => enterMain()}>引用到主对话</button><button className="icon-button" onClick={() => setSelected(undefined)} aria-label="取消文字选择">×</button></div>}
      <article ref={paper} className="report-paper" style={{ fontSize: `${content?.layout?.body_font_pt || 10.5}pt`, fontFamily: content?.layout?.font_family ? `${content.layout.font_family}, var(--font)` : undefined }} onMouseUp={select} onKeyUp={select} aria-label={`导航报告 ${researchLabel(version)}`}><input className="report-title-editor" aria-label="报告标题" value={title} readOnly={!isCurrent} onChange={e => { setEditingVersionId(version.id); setTitle(e.target.value); }} /><p className="report-subtitle">依据冻结的数据、研究范围与图表版本生成</p>{blocks.map(block => <section key={`${version.id}-${block.id}`} id={`report-${block.id}`} data-report-block={["heading", "paragraph"].includes(block.type) ? block.id : undefined} className={`report-block report-block-${block.type} ${params.get("anchor") === block.id ? "is-targeted" : ""}`}>
        {block.type === "heading" || block.type === "paragraph" ? <TextBlock block={block} editable={isCurrent} changed={changed} /> : block.type === "table" ? <>{block.title && <h3>{block.title}</h3>}<DataTable headings={block.headers || block.columns || []} rows={(block.rows || []).map((row: unknown) => Array.isArray(row) ? row.map(cell => describe(cell)) : Object.values(row as Json).map(describe))} />{block.caption && <p className="report-chart-caption">{block.caption}</p>}</> : block.type === "chart" ? <div style={{ width: `${block.layout?.width_percent || 100}%`, marginLeft: block.layout?.alignment === "right" ? "auto" : block.layout?.alignment === "center" ? "auto" : 0, marginRight: block.layout?.alignment === "center" ? "auto" : 0 }}><ChartCard id={block.chart_id || block.chart?.id} projectId={project.id} frozen={block.chart} /><p className="report-chart-caption">{block.caption || block.title}</p></div> : <p className="muted">此内容类型暂无法显示：{block.type}</p>}
      </section>)}<footer className="report-footnote">证据与引用保存在此报告版本内。相关趋势、候选方向及布局建议受样本覆盖与字段完整性限制。</footer></article>
    </>}
    {historyOpen && <Modal title={`${research?.number ? `V${research.number}` : "研究"} · 修改记录`} close={() => setHistoryOpen(false)} wide><p className="small muted">日常修改保存在同一研究版本中。恢复会新增一条记录，并将所属研究设为当前采用。</p><div className="report-history-list">{versionItems.map(item => <article className="report-history-item" key={item.id}><div><strong>{new Date(item.created_at).toLocaleString("zh-CN")}{item.id === currentId ? " · 最新内容" : ""}</strong><p>{item.summary || "报告内容已保存"}</p><span className="small muted">{(item.change_kind === "restore" || item.restored_from_id) ? "恢复记录" : !item.parent_id ? "首次生成报告" : item.change_count !== undefined ? `改动 ${item.change_count} 处` : "修改记录"}{!item.source_conversation_id ? " · 报告页面保存" : ""}</span></div><div className="actions"><button className="text-button" onClick={() => { setHistoryOpen(false); navigate({ version: item.id, diff: null, anchor: null, message: null }); }}>查看内容</button>{item.parent_id && <button className="text-button" onClick={() => { setHistoryOpen(false); navigate({ version: item.id, diff: item.parent_id, anchor: null, message: null }); }}>查看改动</button>}{item.id !== currentId && <><button className="text-button" onClick={() => { setHistoryOpen(false); navigate({ version: currentId, diff: item.id, anchor: null, message: null }); }}>与最新比较</button><button className="text-button" disabled={busy} onClick={() => { setHistoryOpen(false); setRestoreTarget(item); }}>恢复此内容</button></>}{item.source_conversation_id && <button className="text-button" onClick={() => { setHistoryOpen(false); navigate({ view: "chat", conversation: item.source_conversation_id, research: researchId || null, message: item.source_message_id || null, version: null, diff: null, anchor: null }); }}>{item.source_message_id ? "来源消息 →" : "来源对话 →"}</button>}</div></article>)}</div>{nextVersionCursor && <button className="text-button" disabled={directoryBusy} onClick={moreVersions}>{directoryBusy ? "正在读取…" : "加载更早记录"}</button>}<ErrorNotice error={versionsError} /></Modal>}
    {restoreTarget && <Modal title="确认恢复报告内容" close={() => setRestoreTarget(undefined)}><p>将 {researchLabel(restoreTarget)} 恢复为 {new Date(restoreTarget.created_at).toLocaleString("zh-CN")} 的内容。</p><p className="small muted">恢复会在所属研究新增一条修改记录，并采用该研究。其它研究版本及现有记录继续保留。</p><div className="actions"><button className="btn primary" disabled={busy} onClick={restore}>确认恢复并采用</button><button className="btn secondary" onClick={() => setRestoreTarget(undefined)}>取消</button></div><ErrorNotice error={error} /></Modal>}
    {adopting && <Modal title={`采用 V${research?.number}`} close={() => setAdopting(false)}><p>将 V{research?.number} 的最新报告设为当前采用，原有研究及修改记录继续保留。</p><div className="actions"><button className="btn primary" disabled={busy} onClick={adopt}>确认采用</button><button className="btn secondary" onClick={() => setAdopting(false)}>取消</button></div><ErrorNotice error={error} /></Modal>}
    {transfer && <Modal title="主对话中已有报告引用" close={() => setTransfer(undefined)}><p>主对话的未发送草稿中已有一处报告引用。替换引用会保留原有文字和附件{transfer.content ? "，并追加这次修改要求" : ""}。</p><div className="actions"><button className="btn primary" onClick={() => applyTransfer(transfer)}>替换引用并继续</button><button className="btn secondary" onClick={() => setTransfer(undefined)}>取消</button></div></Modal>}
    {mode === "modify" && selected && !transfer && <Modal title={`修改所选 ${selected.ids.length} 个文字块`} close={() => setMode(undefined)}><blockquote className="quote"><p className="small">{selected.text.slice(0, 600)}</p></blockquote><div className="segmented"><button className={editMode === "instruction" ? "active" : ""} onClick={() => setEditMode("instruction")}>用自然语言修改</button><button className={editMode === "replacement" ? "active" : ""} onClick={() => setEditMode("replacement")}>直接替换文字</button></div>{editMode === "instruction" ? <label className="form-field">修改要求<textarea autoFocus rows={4} value={instruction} onChange={e => setInstruction(e.target.value)} placeholder="说明要修改的目标、保留的事实及期望表达" /></label> : <label className="form-field">替换内容{selected.ids.length > 1 && <small>按原顺序使用空行分隔每个文字块。</small>}<textarea rows={9} value={replacement} onChange={e => setReplacement(e.target.value)} /></label>}<ErrorNotice error={error} /><button className="btn primary" disabled={busy || (editMode === "instruction" && !instruction.trim())} onClick={modify}>{editMode === "instruction" ? "带入主对话修改" : "保存修改"}</button></Modal>}
    {mode === "insert" && <Modal title="添加正文或章节" close={() => setMode(undefined)}><label className="form-field">类型<select value={insertType} onChange={e => setInsertType(e.target.value)}><option value="paragraph">正文段落</option><option value="heading">章节标题</option></select></label><BlockPosition blocks={blocks} value={afterId} change={setAfterId} /><label className="form-field">文字内容<textarea rows={5} value={replacement} onChange={e => setReplacement(e.target.value)} /></label><button className="btn primary" disabled={!replacement.trim() || busy} onClick={() => operation([{ op: "insert_block", after_id: afterId || null, block: { id: crypto.randomUUID(), type: insertType, text: replacement } }])}>保存修改</button></Modal>}
    {mode === "blocks" && <Modal title="章节、内容与顺序" close={() => setMode(undefined)} wide><p className="small muted">每次调整都会保留修改记录，研究版本编号保持不变。</p>{blocks.map((block, index) => <div className="block-row" key={block.id}><span className="label">{block.type === "heading" ? "章节" : block.type === "chart" ? "图表" : "正文"}</span><span>{block.text?.slice(0, 70) || block.title || block.caption || block.id}</span><button className="text-button" disabled={busy || (block.type === "heading" ? chapterTarget(index, "up") === undefined : index === 0)} onClick={() => move(index, "up")}>{block.type === "heading" ? "上移章节" : "上移"}</button><button className="text-button" disabled={busy || (block.type === "heading" ? chapterTarget(index, "down") === undefined : index === blocks.length - 1)} onClick={() => move(index, "down")}>{block.type === "heading" ? "下移章节" : "下移"}</button><button className="text-button" disabled={busy} onClick={() => remove(index)}>{block.type === "heading" ? "移除章节" : "移除"}</button></div>)}</Modal>}
    {mode === "chart" && <Modal title="图表插入与排版" close={() => setMode(undefined)} wide><section><h3>插入工作台图表</h3><label className="form-field">图表<select value={chartChoice} onChange={e => setChartChoice(e.target.value)}><option value="">选择图表</option>{(analysis?.data?.chart_ids || []).map((id: string) => <ChartOption key={id} id={id} />)}</select></label><BlockPosition blocks={blocks} value={afterId} change={setAfterId} />{chartChoice && <ChartCard id={chartChoice} projectId={project.id} onInsert={chart => operation([{ op: "insert_chart", chart_id: chart.id, chart_version_id: chart.version_id, after_id: afterId || null }])} />}<button className="btn primary" disabled={!chartChoice || busy} onClick={async () => { try { const chart = await api(`/api/charts/${chartChoice}`); await operation([{ op: "insert_chart", chart_id: chartChoice, chart_version_id: chart.version_id, after_id: afterId || null }]); } catch (e) { setError(e); } }}>插入选定图表</button></section><section className="section"><h3>已插入图表排版</h3><label className="form-field">报告图位<select value={selectedChart} onChange={e => setSelectedChart(e.target.value)}><option value="">选择图位</option>{blocks.filter(block => block.type === "chart").map(block => <option key={block.id} value={block.id}>{block.title || block.caption || block.chart?.option?.title?.text || block.id}</option>)}</select></label><div className="form-grid"><label className="form-field">宽度<select value={chartWidth} onChange={e => setChartWidth(Number(e.target.value))}><option value={100}>整行 100%</option><option value={50}>半行 50%</option></select></label><label className="form-field">对齐<select value={alignment} onChange={e => setAlignment(e.target.value)}><option value="left">左对齐</option><option value="center">居中</option><option value="right">右对齐</option></select></label></div><div className="actions"><button className="btn primary" disabled={!selectedChart} onClick={() => operation([{ op: "set_chart_layout", block_id: selectedChart, layout: { width_percent: chartWidth, alignment } }])}>保存图表排版</button><button className="btn secondary" disabled={!selectedChart} onClick={async () => { const block = blocks.find(item => item.id === selectedChart)!; const chart = await api(`/api/charts/${block.chart_id || block.chart?.id}`); operation([{ op: "update_chart", block_id: selectedChart, chart_id: chart.id, chart_version_id: chart.version_id }]); }}>明确更新至工作台采用版本</button><button className="text-button" disabled={!selectedChart} onClick={() => operation([{ op: "remove_block", block_id: selectedChart }])}>移除此图位</button></div></section><ErrorNotice error={error} /></Modal>}
    {mode === "layout" && <Modal title="报告页面设置" close={() => setMode(undefined)}><p className="small muted">页面与 Word 采用同一份排版设置。</p><label className="form-field">正文字号<input type="number" value={fontSize} min="8" max="24" step="0.5" onChange={e => setFontSize(Number(e.target.value))} /></label><button className="btn primary" disabled={busy} onClick={() => operation([{ op: "set_layout", layout: { body_font_pt: fontSize } }])}>保存排版修改</button><ErrorNotice error={error} /></Modal>}
  </section>;
}
function BlockPosition({ blocks, value, change }: { blocks: Json[]; value: string; change: (value: string) => void }) { return <label className="form-field">插入位置<select value={value} onChange={e => change(e.target.value)}><option value="">正文开头</option>{blocks.map(block => <option key={block.id} value={block.id}>在 {block.text?.slice(0, 35) || block.title || block.id} 之后</option>)}</select></label>; }
