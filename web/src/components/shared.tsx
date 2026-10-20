"use client";
import { useEffect, useRef, useState } from "react";
import { api, describe, Json, post, refresh, statusLabel, terminal, useApi } from "@/lib/api";
import { useStore } from "@/lib/store";
import { EMPTY_MESSAGE_STREAM, MessageStream, updateMessageStream } from "@/lib/message-stream";
import { researchAssignmentChoice, researchLabel, requiresReportBlocks, withResearchContext, type ResearchAssignment } from "@/lib/research";
import MessageMarkdown from "./message-markdown";
import ScopeEditor from "./scope-editor";
import type { Navigate } from "./workspace";

export function Empty({ title, children }: { title: string; children?: React.ReactNode }) {
  return <div className="empty-state compact"><span className="empty-mark" aria-hidden="true" /><h3>{title}</h3>{children && <div className="empty-copy">{children}</div>}</div>;
}
export function ErrorNotice({ error }: { error: unknown }) {
  return error ? <p className="notice error-notice" role="alert">{error instanceof Error ? error.message : String(error)}</p> : null;
}
export function Modal({ title, children, close, wide = false }: { title: string; children: React.ReactNode; close: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement; ref.current?.showModal(); return () => { previous?.focus(); }; }, []);
  return <dialog ref={ref} className={`app-modal ${wide ? "wide-modal" : ""}`} onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <header className="modal-head"><h2>{title}</h2><button type="button" className="icon-button" onClick={close} aria-label="关闭">×</button></header><div className="modal-body">{children}</div>
  </dialog>;
}
export function DataTable({ rows, headings }: { rows: React.ReactNode[][]; headings: string[] }) {
  return <div className="table-wrap"><table className="data-table"><thead><tr>{headings.map(heading => <th key={heading}>{heading}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, i) => <td key={i}>{cell}</td>)}</tr>)}</tbody></table></div>;
}
export function AssistantMark() {
  return <span className="assistant-avatar" aria-hidden="true"><svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="12" cy="12" r="9" /><path d="m15.8 8.2-2.2 5.4-5.4 2.2 2.2-5.4 5.4-2.2Z" fill="currentColor" strokeLinejoin="round" /><path d="M12 3v2m0 14v2M3 12h2m14 0h2" strokeLinecap="round" /></svg></span>;
}
function ReportResultCard({ report, navigate }: { report: Json; navigate?: Navigate }) {
  const initial = report.is_initial === true || (report.is_initial === undefined && !report.parent_id);
  const summaryLabels: Record<string, string> = { title: "标题", added: "新增内容", removed: "移除内容", modified: "正文修改", order: "顺序调整", layout: "页面排版" };
  const summary = Object.entries(report.change_summary || {}).filter(([, count]) => Number(count) > 0).map(([kind, count]) => `${summaryLabels[kind] || "内容"} ${count} 处`).join(" · ");
  return <button type="button" className="report-result-card" onClick={() => navigate?.({ view: "report", research: report.research_id || null, version: report.report_version_id, diff: report.parent_id || null, anchor: null, message: null })}><span className="report-result-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="25" height="25" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M7 3h7l4 4v14H6V3h1Zm7 0v5h4M9 12h6m-6 4h6" strokeLinecap="round" strokeLinejoin="round" /></svg></span><span className="report-result-copy"><strong>报告{report.research_number ? ` ${researchLabel(report)}` : ""} · {initial ? "新增报告" : report.change_count !== undefined ? `改动 ${report.change_count} 处` : "修改已保存"}</strong><span>{initial ? "报告已保存，可打开阅读全文。" : summary || "打开报告，查看本次改动与原文对照。"}</span><span className="report-result-link">{initial ? "打开报告" : "查看改动"} →</span></span></button>;
}
export function TaskCard({ id: initialId, project, onResult, kinds, presentation, hasReply, navigate, artifacts = [] }: { id: string; project?: Json; onResult?: (result: Json) => void; kinds?: string[]; presentation?: "chat"; hasReply?: boolean; navigate?: Navigate; artifacts?: Json[] }) {
  const [id, setId] = useState(initialId);
  useEffect(() => { setId(initialId); }, [initialId]);
  const { data: run, error: runError, mutate } = useApi(`/api/runs/${id}`, state => terminal(state?.status) ? 0 : 2000);
  const researchId = run?.input_request?.target_research_id || run?.research_id;
  const { data: research } = useApi(researchId ? `/api/research-versions/${researchId}` : null, state => terminal(run?.status) ? 0 : 6000);
  const context = withResearchContext(project || {}, research || (researchId ? { id: researchId } : undefined), research?.context || run?.context);
  const [events, setEvents] = useState<Json[]>([]);
  const [stream, setStream] = useState<MessageStream>(EMPTY_MESSAGE_STREAM);
  const [error, setError] = useState<unknown>();
  const [input, setInput] = useState<Json>({});
  const [actionBusy, setActionBusy] = useState(false);
  const track = useStore(s => s.track);
  const completed = useRef(false);
  useEffect(() => { completed.current = false; setEvents([]); setStream(EMPTY_MESSAGE_STREAM); setError(undefined); setInput({}); }, [id]);
  useEffect(() => { setInput({}); }, [run?.input_request?.id]);
  useEffect(() => { if (run?.stream?.stream_id) setStream(current => updateMessageStream(current, { type: "snapshot", seq: Number(run.stream.seq || 0), payload: run.stream })); }, [id, run?.stream]);
  useEffect(() => {
    if (!run || run.id !== id || terminal(run.status) || run.status === "waiting_input" || (kinds && !kinds.includes(run.kind))) return;
    let source: EventSource | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const connect = () => {
      const after = useStore.getState().cursors[id] || 0;
      source = new EventSource(`/api/runs/${id}/events?after=${after}`);
      const handle = (message: MessageEvent) => {
        const event = JSON.parse(message.data);
        const seq = Number(event.seq || message.lastEventId);
        if (seq <= (useStore.getState().cursors[id] || 0)) return;
        useStore.getState().cursor(id, seq);
        if (event.event_type.startsWith("message.")) setStream(current => updateMessageStream(current, { type: event.event_type, seq, payload: event.payload || {} }));
        setEvents(old => [...old.filter(item => item.seq !== seq), event].slice(-30));
        if (["run.completed", "run.failed", "run.waiting_input", "input.required", "artifact.ready", "plan.ready", "evidence.ready", "activity.updated", "message.started", "message.reset", "message.completed", "analysis.progress", "semantic.progress"].includes(event.event_type)) { mutate(); if (!["analysis.progress", "semantic.progress", "activity.updated", "message.started", "message.reset"].includes(event.event_type)) refresh(); }
      };
      for (const type of ["step.updated", "run.started", "run.completed", "run.failed", "run.waiting_input", "input.required", "message.started", "message.delta", "message.reset", "message.completed", "analysis.ready", "analysis.progress", "report.ready", "chart.ready", "evidence.ready", "plan.ready", "activity.updated", "artifact.ready", "progress", "semantic.progress"]) source.addEventListener(type, handle as EventListener);
      source.onerror = () => { source?.close(); if (!stopped) timer = setTimeout(connect, 3000); };
    };
    connect();
    return () => { stopped = true; source?.close(); if (timer) clearTimeout(timer); };
  }, [id, run?.status, mutate]);
  useEffect(() => {
    if (run?.id === id && run.status === "succeeded" && !completed.current) { completed.current = true; if (presentation !== "chat" || !hasReply) refresh(); onResult?.(run.result); }
  }, [id, run, onResult, presentation, hasReply]);
  const action = async (name: string, body = {}) => {
    if (actionBusy) return;
    try { setActionBusy(true); setError(undefined); const result = await post(`/api/runs/${id}/${name}`, body); if (name === "retry") { if (project) track(project.id, result.run_id); if (presentation === "chat") setId(result.run_id); } await mutate(); await refresh(); if (name === "inputs" && result.conversation_id && result.conversation_id !== run?.conversation_id && navigate) navigate({ view: "chat", conversation: result.conversation_id, research: result.research_id || run?.input_request?.target_research_id || null, message: result.message_id || null, version: null, diff: null, anchor: null }); }
    catch (e) { setError(e); }
    finally { setActionBusy(false); }
  };
  const chat = presentation === "chat";
  const wrap = (content: React.ReactNode) => chat && !hasReply ? <article className="conversation-message assistant turn-pending"><AssistantMark /><div className="assistant-content"><section className="turn-activity" aria-live="polite" data-run-id={id}>{content}</section></div></article> : <section className={chat ? "turn-activity" : "task-card"} aria-live="polite" data-run-id={id}>{content}</section>;
  if (!run) return chat ? runError ? wrap(<ErrorNotice error={runError} />) : hasReply ? null : wrap(<p className="turn-waiting" role="status">正在回复…</p>) : runError ? <ErrorNotice error={runError} /> : <div className="task-card muted">正在读取任务…</div>;
  if (kinds && !kinds.includes(run.kind)) return null;
  if (!chat && project?.research_id && run.research_id && project.research_id !== run.research_id) return null;
  const runTitle = ({ import: "导入专利数据", parse_material: "解析项目资料", material_parse: "解析项目资料", material: "解析项目资料", analyze: "研究分析", auto: "研究任务", generate_report: "生成导航报告", revise_report: "修改报告", revise_chart: "修改图表", revise_analysis: "纠正技术分类", export: "导出 Word", chat: "研究问答", index: "建立语义索引" } as Json)[run.kind] || "后台任务";
  const activities: Json[] = run.activities || [];
  const fallbackArtifacts = artifacts.filter(card => card.type !== "task" && (card.report_version_id || card.analysis_id || card.chart_version_id || card.file_id) && !activities.some(activity => ["report_version_id", "analysis_id", "chart_version_id", "file_id"].some(key => card[key] && card[key] === activity[key])));
  const latestProgress = events.filter(event => ["analysis.progress", "semantic.progress"].includes(event.event_type)).at(-1)?.payload;
  const draftReply = chat && !hasReply && stream.content && stream.status !== "reset" ? <div className="streaming-reply" data-stream-id={stream.stream_id}><MessageMarkdown content={stream.content} />{stream.status === "streaming" && !terminal(run.status) && <span className="streaming-indicator" role="status">正在生成回答…</span>}</div> : null;
  const results = chat && <>{activities.length > 0 && <div className="turn-process"><p className="turn-process-label">本轮执行过程</p>{activities.map(activity => {
    const labels: Record<string, string> = { search_patents: "专利检索", search_materials: "项目资料检索", analyze: "样本分析", generate_report: "导航报告生成", revise_report: "报告修改", revise_chart: "图表修改" };
    const running: Record<string, string> = { search_patents: "正在检索本地专利样本", search_materials: "正在检索项目资料", analyze: "正在分析样本", generate_report: "正在生成导航报告", revise_report: "正在修改报告", revise_chart: "正在修改图表" };
    const label = labels[activity.kind] || "操作";
    const text = activity.status === "succeeded" ? (activity.kind.startsWith("search_") ? `${label}已完成${activity.matched_count !== undefined ? ` · 命中 ${activity.matched_count} 条` : ""}` : `${label}已完成`) : activity.status === "failed" ? `${label}失败` : activity.status === "cancelled" ? `${label}已停止` : `${running[activity.kind] || "正在执行操作"}…`;
    const queries: string[] = Array.isArray(activity.queries) ? activity.queries : activity.query ? [activity.query] : [];
    const searchDetails: string[] = Array.isArray(activity.details) && activity.details.length ? activity.details : queries.length ? [`检索词：${queries.join(" / ")}`] : [];
    const progress = activity.kind === "analyze" ? { ...activity, ...latestProgress } : activity;
    const stage = progress.phase || ({ extract: "提取技术要素", taxonomy: "归并技术主题", statistics: "计算样本统计", persist: "保存分析结果" } as Record<string, string>)[progress.stage];
    return <div className={`turn-action ${activity.status}`} key={activity.id}><span className="turn-action-mark" aria-hidden="true">{activity.status === "succeeded" ? "✓" : activity.status === "failed" ? "!" : "·"}</span><div className="turn-action-copy"><span>{text}</span>{activity.kind.startsWith("search_") && <p className="turn-action-detail">{activity.kind === "search_patents" ? "本地专利样本" : "项目资料"}{searchDetails.length > 0 && ` · ${searchDetails.join(" · ")}`}</p>}{activity.kind === "analyze" && (stage || progress.processed_count !== undefined || progress.completed_dimensions !== undefined) && <div className="turn-action-detail">{stage && <span>{stage} · </span>}{progress.completed_dimensions !== undefined ? `技术维度 ${progress.completed_dimensions} / ${progress.total_dimensions || "—"}` : progress.processed_count !== undefined ? `已处理 ${progress.processed_count} / ${progress.total_count || "—"} 条` : null}{progress.total_count > 0 && progress.processed_count !== undefined && <progress aria-label="样本分析进度" value={progress.processed_count} max={progress.total_count} />}</div>}</div>{activity.status === "succeeded" && <>
      {!activity.report_version_id && activity.analysis_id && navigate && <button className="text-button" onClick={() => navigate({ view: "workbench", tab: "overview", analysis_id: activity.analysis_id })}>打开分析 →</button>}
      {activity.file_id && <a className="text-button" href={`/api/files/${activity.file_id}`}>下载 Word ↓</a>}
    </>}</div>;
  })}</div>}{[...activities.filter(activity => activity.status === "succeeded"), ...fallbackArtifacts].filter(item => item.report_version_id).map(report => <ReportResultCard key={report.report_version_id} report={report} navigate={navigate} />)}{fallbackArtifacts.filter(card => !card.report_version_id).map((card, index) => <div className="turn-action succeeded" key={card.id || index}><span className="turn-action-mark" aria-hidden="true">✓</span><span>{card.title || (card.analysis_id ? "分析已完成" : card.chart_version_id ? "图表新版本已保存" : "文档已生成")}</span>{card.analysis_id && navigate && <button className="text-button" onClick={() => navigate({ view: "workbench", tab: "overview", analysis_id: card.analysis_id })}>打开分析 →</button>}{card.file_id && <a className="text-button" href={`/api/files/${card.file_id}`}>下载 Word ↓</a>}</div>)}</>;
  const forwarding = run.result?.forwarding && (typeof run.result.forwarding === "object" ? run.result.forwarding : run.result);
  const forwarded = forwarding?.conversation_id && <div className="notice"><p>本次请求已转入所属研究的主对话。</p><button className="text-button" onClick={() => navigate?.({ view: "chat", conversation: forwarding.conversation_id, research: forwarding.research_id || researchId || null, message: forwarding.message_id || null, version: null, diff: null, anchor: null })}>继续查看 →</button></div>;
  if (run.status === "succeeded") return chat ? forwarded || activities.length || fallbackArtifacts.length || draftReply ? wrap(<>{draftReply}{forwarded}{results}</>) : null : <details className="task-card completed-task"><summary><span>{runTitle}</span><span className="status-chip succeeded">已完成</span>{run.result?.file_id && <a className="text-button" href={`/api/files/${run.result.file_id}`} onClick={event => event.stopPropagation()}>下载 Word ↓</a>}</summary><p className="small muted">结果已保存，可从报告或对应对话中打开。</p></details>;
  const request = run.input_request;
  const assignment: ResearchAssignment | undefined = request?.research_assignment;
  const researchChoice = researchAssignmentChoice(assignment, input.research_choice);
  const existingLabel = assignment?.existing_number ? `V${assignment.existing_number}` : "原研究";
  const existingDraft = !!assignment?.existing_research_id && !assignment.existing_number;
  const assignmentExpectedScope = request?.expected_scope_version_id || run.scope_version_id;
  const scopeKind = ["scope_confirmation", "plan_confirmation", "execution_confirmation"].includes(request?.kind);
  const scopeValue = input.scope || request?.scope || { ...(context.scope?.content || {}), ...(request?.changes || {}) };
  const scopeEditable = scopeKind && ((assignment?.allow_new && researchChoice === "new") || (request.kind === "execution_confirmation" ? request.scope_editable === true : request.scope_editable !== false));
  const confirmationDetails: string[] = [...new Set<string>((request?.actions || []).flatMap((item: Json) => Array.isArray(item.details) ? item.details : []))].filter(detail => (!request?.scope || /^(数据来源|报告目标|修改段落|执行说明|调整范围说明)：|^调整研究范围后/.test(detail)) && !(assignment && /^报告目标：/.test(detail)));
  const conflicts: Json[] = request?.conflicts || request?.data?.conflicts || [];
  const scopeChanged = !assignment && ["scope_confirmation", "plan_confirmation"].includes(request?.kind) && context.scope_version_id && context.scope_version_id !== run.scope_version_id;
  const expectedRevision = request?.expected_research_revision ?? request?.expected_project_revision;
  const datasetChanged = request?.kind === "dataset_selection" && expectedRevision !== undefined && context.revision !== expectedRevision;
  const targetHead = request?.expected_current_version_id || request?.current_revision_id || context.current_report_version_id;
  const reportChanged = request?.kind === "report_target" && (request.version_changed || request.base_version_id !== targetHead);
  const plannedOperations = (run.plan?.operations || []).length > 0;
  const selectBlocks = requiresReportBlocks(request || {}, researchChoice, plannedOperations);
  const stepLabel = (key: string) => {
    const labels: Record<string, string> = { load_data: "读取数据", calculate_statistics: "计算样本统计", persist_analysis: "保存分析成果", scope_confirmation: "确认研究范围", plan: "识别研究要求", plan_execution: "执行研究计划", taxonomy: "归并分类体系", semantic: "提取技术要素", generate_report: "编写导航报告", persist_report: "保存报告版本", parse_material: "解析资料正文", render_charts: "渲染统计图表", build_docx: "生成 Word 文档", import: "清洗并导入数据", merge: "合并补充数据", export: "导出指定版本", full_semantic: "全量语义分析", deterministic_analysis: "计算统计分布" };
    return labels[key] || (key.includes("extract") ? "提取原文技术要素" : key.includes("classif") ? "归类并保存检查点" : key.includes("batch") ? "批次处理原文" : key.includes("embed") ? "生成语义索引" : key.includes("persist") ? "保存研究成果" : key.includes("model") || key.includes("chat") ? "调用对话模型" : key.includes("search") ? "检索原文证据" : key.includes("report") ? "处理报告内容" : key.includes("rewrite") ? "修订目标正文" : "执行研究步骤");
  };
  return wrap(<>
    {draftReply}
    {!chat && <div className="section-head"><strong>{runTitle}</strong><span className={`status-chip ${run.status}`}>{statusLabel[run.status] || run.status}</span></div>}
    {!chat && (run.steps || []).length > 0 && <details className="task-step-details"><summary className="small muted">{run.steps.filter((step: Json) => step.status === "succeeded").length} / {run.steps.length} 个步骤已完成 · 查看检查点</summary><ol className="task-steps">{run.steps.map((step: Json) => <li key={step.id || step.step_key}><span>{statusLabel[step.status] || step.status}</span>{stepLabel(step.step_key || step.key || "")}</li>)}</ol></details>}
    {chat && results}
    {chat && !draftReply && !terminal(run.status) && run.status !== "waiting_input" && !activities.some(activity => activity.status === "running") && <p className="turn-waiting" role="status">{run.status === "cancelling" ? "正在停止…" : "正在回复…"}</p>}
    {chat && run.status === "cancelled" && <p className="small muted">本次操作已取消</p>}
    {!chat && !terminal(run.status) && events.filter(event => event.payload?.message || event.payload?.processed_count !== undefined).slice(-2).map(event => <p key={event.seq} className="small muted">{event.payload?.message || "全量分类进度"}{event.payload?.processed_count !== undefined ? ` · 已处理 ${event.payload.processed_count} / ${event.payload.total_count || "—"}` : ""}</p>)}
    {run.error && <p className="notice">{run.error.message || describe(run.error)}</p>}
    {!chat && run.status === "running" && events.some(event => event.event_type === "message.delta") && <p className="message-text streaming-text">{events.filter(event => event.event_type === "message.delta").map(event => event.payload?.text || event.payload?.delta || "").join("")}</p>}
    {run.status === "waiting_input" && request && <div className="task-input">
      <h3>{request.prompt || request.message || request.question || "请确认后继续"}</h3>
      {request.kind === "execution_confirmation" && <ul className="turn-confirm-actions">{(request.actions || []).map((item: Json, index: number) => <li key={item.kind || index}>{item.label}</li>)}</ul>}
      {!assignment && request.target_conversation_id && <p className="notice">确认后将在目标研究的主对话中继续，原对话保留来源记录。</p>}
      {assignment && <section className="research-assignment" aria-label="本轮研究归属"><div className="research-assignment-heading"><span className="label">本轮执行目标</span><strong>{researchChoice === "new" ? "开启新研究 · 尚未编号" : researchChoice === "existing" ? existingDraft ? "继续新研究 · 尚未编号" : `更新 ${existingLabel}` : "请选择研究归属"}</strong></div>{assignment.reason && <p className="small muted">{assignment.reason}</p>}{assignment.topic && <p className="research-assignment-topic"><span>研究主题</span><strong>{assignment.topic}</strong></p>}{assignment.intent === "choose" && !assignment.fixed && <fieldset className="research-assignment-options"><legend>这次研究要保存在哪里？</legend>{assignment.allow_new && <label className="radio-choice"><input type="radio" name={`research-${id}`} value="new" checked={researchChoice === "new"} disabled={actionBusy} onChange={() => setInput(current => ({ ...current, research_choice: "new" }))} />开启一轮新研究</label>}{assignment.allow_existing && <label className="radio-choice"><input type="radio" name={`research-${id}`} value="existing" checked={researchChoice === "existing"} disabled={actionBusy} onChange={() => setInput(current => ({ ...current, research_choice: "existing" }))} />更新 {existingLabel}{assignment.existing_title ? ` · ${assignment.existing_title}` : ""}</label>}</fieldset>}{researchChoice === "new" ? <div className="research-assignment-target"><p>{assignment.new_uses_current_conversation ? "确认后在当前对话开始新研究。" : "确认后在独立主对话中开始新研究。"}首份报告成功保存后才分配新的 V 编号。</p>{assignment.existing_research_id && <p>{existingLabel}{assignment.existing_title ? ` · ${assignment.existing_title}` : ""} 继续保留，已有报告不会被本轮新研究覆盖。</p>}</div> : researchChoice === "existing" ? <div className="research-assignment-target"><p>目标报告：{existingLabel}{assignment.existing_title ? ` · ${assignment.existing_title}` : ""}</p><p>{existingDraft ? "继续使用这轮研究的目标和范围，首份报告成功保存后才分配 V 编号。" : "本轮修改保存在该研究的修改记录中，研究编号保持不变。"}</p>{assignment.existing_conversation_id ? <p>{assignment.existing_conversation_id === run.conversation_id ? "执行位置：当前研究主对话。" : "确认后转入该研究的主对话，当前对话保留来源记录。"}{navigate && <button type="button" className="text-button" onClick={() => navigate({ view: "chat", conversation: assignment.existing_conversation_id!, research: assignment.existing_research_id || null, message: null, version: null, diff: null, anchor: null })}>查看主对话 →</button>}</p> : <p>执行位置：该研究的主对话，确认后建立并继续。</p>}</div> : <p className="small muted">选择后仍可核对和编辑下方研究范围，确认后才执行。</p>}{!assignment.fixed && assignment.intent !== "choose" && assignment.intent !== "target" && assignment.allow_existing && assignment.allow_new && <button type="button" className="btn secondary research-assignment-alternate" disabled={actionBusy} onClick={() => setInput(current => ({ ...current, research_choice: researchChoice === "new" ? "existing" : "new" }))}>{researchChoice === "new" ? `改为更新 ${existingLabel}` : "改为开启新研究"}</button>}</section>}
      {request.kind === "execution_confirmation" && confirmationDetails.length > 0 && <div className="turn-confirm-context">{confirmationDetails.map(detail => <p key={detail}>{detail}</p>)}</div>}
      {conflicts.length > 0 ? conflicts.map(conflict => <label key={conflict.id || conflict.conflict_id} className="form-field"><span>{conflict.application_number} · {conflict.field}</span><small>原值：{describe(conflict.old_value)}<br />新值：{describe(conflict.new_value)}</small><select aria-label={`处理冲突 ${conflict.field}`} value={input[conflict.id || conflict.conflict_id] || ""} onChange={e => setInput({ ...input, [conflict.id || conflict.conflict_id]: e.target.value })}><option value="">请选择</option><option value="old">保留原值</option><option value="new">采用新值</option>{(Array.isArray(conflict.old_value) || conflict.allow_merge) && <option value="merge">合并多值</option>}</select></label>) : <>
        {scopeKind && (request.scope || request.changes) ? <ScopeEditor key={request.id} value={scopeValue} editable={scopeEditable} change={scope => setInput(current => ({ ...current, scope }))} /> : request.scope && <Readable value={request.scope} />}
        {!request.scope && !scopeKind && request.changes && <Readable value={request.changes} />}
        {request.filters && <Readable value={request.filters} />}
        {(request.options || request.datasets || request.versions || []).map((option: Json) => <label key={option.id || option.value} className="radio-choice"><input type="radio" name={`input-${id}`} onChange={() => setInput({ ...input, dataset_version_id: option.dataset_version_id || option.id, choice: option.value || option.id })} />{option.name || option.label || option.dataset_name || "就绪数据版本"}{option.quality?.application_count ? ` · ${option.quality.application_count} 条申请` : ""}</label>)}
        {(request.kind === "report_target" || selectBlocks) && <><p className="small muted">{request.title && `${request.title}。`}{selectBlocks ? "选择至少一处需要修改的内容，保持此次任务的原始修改要求。" : "确认采用此版本，继续执行本轮已明确的修改。"}</p>{selectBlocks && (request.blocks || []).map((block: Json) => <label key={block.id} className="radio-choice"><input type="checkbox" checked={(input.block_ids || []).includes(block.id)} onChange={e => setInput({ ...input, block_ids: e.target.checked ? [...(input.block_ids || []), block.id] : (input.block_ids || []).filter((item: string) => item !== block.id) })} />{block.text || (block.type === "chart" ? "图表" : block.type === "table" ? "表格" : "报告内容")}</label>)}</>}
        {scopeChanged && <div className="notice"><p>等待期间本研究的范围已经变化，请核对最新范围。</p><Readable value={context.scope?.content} /><label className="radio-choice"><input type="checkbox" checked={input.scope_ack_id === context.scope_version_id} onChange={event => setInput({ ...input, scope_ack_id: event.target.checked ? context.scope_version_id : undefined })} />已核对最新范围，仍按这次要求更新</label></div>}
        {datasetChanged && <div className="notice"><p>等待期间本研究的数据已更新。当前数据状态：{context.dataset_version_id ? "已绑定就绪版本" : "尚未绑定"}。</p><label className="radio-choice"><input type="checkbox" checked={input.project_ack_revision === context.revision} onChange={event => setInput({ ...input, project_ack_revision: event.target.checked ? context.revision : undefined })} />确认使用所选版本替换本研究的数据</label></div>}
        {reportChanged && <div className="notice"><p>引用内容与目标研究的最新报告不同，请核对本次修改目标。</p><label className="radio-choice"><input type="checkbox" checked={input.report_ack_id === targetHead} onChange={event => setInput({ ...input, report_ack_id: event.target.checked ? targetHead : undefined })} />已核对目标研究的最新内容，继续本次修改</label></div>}
      </>}
      <button className="btn primary" onClick={() => action("inputs", { input_request_id: request.id, expected_scope_version_id: assignment ? assignmentExpectedScope : context.scope_version_id || run.scope_version_id, values: conflicts.length ? { choices: conflicts.map(c => ({ conflict_id: c.id || c.conflict_id, choice: input[c.id || c.conflict_id] })) } : { confirmed: true, ...input, ...(assignment ? { research_choice: researchChoice, ...(researchChoice === "new" ? { block_ids: undefined } : {}) } : {}), ...(scopeKind && (scopeEditable || request.kind !== "execution_confirmation") ? { scope: scopeValue } : {}), ...(["scope_confirmation", "plan_confirmation"].includes(request.kind) ? { confirm_scope_change: input.scope_ack_id === context.scope_version_id } : {}), ...(request.kind === "dataset_selection" ? { expected_revision: context.revision, confirm_project_change: input.project_ack_revision === context.revision } : {}), ...(request.kind === "report_target" ? { base_version_id: request.base_version_id, expected_current_version_id: targetHead } : {}) } })} disabled={actionBusy || !!(assignment && !researchChoice) || conflicts.some(c => !input[c.id || c.conflict_id]) || (selectBlocks && !(input.block_ids || []).length) || (request.kind === "dataset_selection" && !input.dataset_version_id) || !!(scopeChanged && input.scope_ack_id !== context.scope_version_id) || !!(datasetChanged && input.project_ack_revision !== context.revision) || !!(reportChanged && input.report_ack_id !== targetHead)}>{assignment ? researchChoice === "new" ? "确认并开始" : researchChoice === "existing" ? existingDraft ? "确认并开始" : `确认并更新 ${existingLabel}` : "请先选择研究归属" : request.kind === "execution_confirmation" ? "确认并执行" : "确认并继续"}</button>
      {(chat || assignment) && <button className="text-button turn-cancel" disabled={actionBusy} onClick={() => action("cancel")}>取消</button>}
    </div>}
    {((!chat && !assignment) || run.status !== "waiting_input") && <div className="actions">{!terminal(run.status) && <button className="text-button" disabled={actionBusy || run.status === "cancelling"} onClick={() => action("cancel")}>{chat ? "停止" : "停止后续步骤"}</button>}{run.status === "failed" && <button className="btn secondary" disabled={actionBusy} onClick={() => action("retry")}>重试</button>}{!chat && run.result?.file_id && <a className="btn primary" href={`/api/files/${run.result.file_id}`}>下载 Word</a>}</div>}
    <ErrorNotice error={error} />
  </>);
}
const EMPTY_TASKS: string[] = [];
export function ProjectTasks({ project, kinds, local, onResult }: { project: Json; kinds: string[]; local?: string; onResult?: (result: Json) => void }) {
  const stored = useStore(state => state.runs[project.id] || EMPTY_TASKS);
  const ids = [...new Set([...(local ? [local] : []), ...(project.active_runs || []).map((item: Json) => item.id), ...stored])].slice(0, 12);
  return <>{ids.map(id => <TaskCard key={id} id={id} project={project} kinds={kinds} onResult={local === id ? onResult : undefined} />)}</>;
}
export function Readable({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return null;
  const terms: Record<string, string> = { ...statusLabel, claims: "权利要求", full_text: "完整说明书", legal_status: "法律状态", family: "完整同族", detailed_address: "省市与详细地址", document_type: "文献类型", candidate_requires_review: "相近记录，待人工复核", pending_validation: "待验证", pending_external: "待执行外部补检", not_performed: "尚未执行", completed: "已完成", local_original_text_term_overlap: "本地原文词汇匹配", classified: "已归类", review: "待复核", semantic: "全量语义分类" };
  const humanValue = (item: unknown) => typeof item === "string" ? terms[item] || item : describe(item);
  if (typeof value !== "object") return <p className="whitespace">{humanValue(value)}</p>;
  if (Array.isArray(value)) return <div className="result-list">{value.map((item, i) => <Readable key={item?.id || i} value={item} />)}</div>;
  const object = value as Json;
  const title = object.title || object.name || object.label || object.direction;
  const keys = Object.keys(object).filter(key => object[key] !== undefined && !["id", "stable_id", "title", "name", "label", "direction", "patent_ids", "application_ids", "supporting_application_ids", "option", "chart_ids", "dataset_version_ids", "dataset_version_id", "scope_version_id", "application_id", "source_file_id", "sha256", "text_sha256", "fingerprint", "model", "prompt_version", "taxonomy_version", "method_theme_id", "scenario_theme_id", "theme_id", "row_sources"].includes(key));
  const labels: Json = { description: "说明", reason: "依据", rationale: "依据", evidence: "原文依据", evidence_gap: "证据缺口", status: "验证状态", priority: "优先级", conditions: "推进条件", actions: "行动", risks: "风险关注", opportunities: "候选方向", phases: "阶段", findings: "主要发现", limitations: "数据边界", count: "申请数", support: "支持依据", counterexamples: "反例核查", external_search: "外部补检", external_validation: "外部验证", counterexample_search: "本地反例检索", owner: "责任人", delivery: "交付物", entry_conditions: "进入条件", text: "内容", elements: "基础要素", methods: "技术方法", scenarios: "应用场景", processed_count: "处理数", classified_count: "已归类", review_count: "待复核", total_count: "入选申请", cached_count: "缓存复用", application_count: "申请数", publication_count: "公开标识", abstract_count: "行级摘要", industry: "目标行业", goal: "决策目标", need: "应用需求", focus: "关注对象", source_region: "来源地区", coverage: "覆盖说明", start: "开始年", end: "结束年", countries: "来源国家", counting_method: "统计口径", source: "来源", source_description: "来源说明", completeness: "完整性", warnings: "注意事项", rule_version: "清洗规则", file_name: "来源文件", required_fields: "必需字段", missing_fields: "缺失字段", duplicates: "重复记录", errors: "异常记录", query: "检索条件", keywords: "关键词", ipc: "IPC", applicant: "申请人", q: "关键词", searched_count: "检索数", matches: "本地匹配", validation_status: "验证状态", next_actions: "下一步", requirement: "目标要求", deliverables: "交付物", responsible: "责任人", evidence_quotes: "原文摘录", application_number: "申请号", quote: "原文摘录", source_row: "来源行", source_sheet: "来源工作表", annual_theme: "年度主题分布" };
  Object.assign(labels, { descriptions: "待复核原因", counting_note: "计数说明", sheets: "工作表", headers: "原始字段", missing_columns: "缺失列", source_row_count: "来源行数", unavailable_fields: "未提供字段", document_type: "文献类型", publication_prefixes: "公开编号前缀", supplement_row_count: "补充行数", unresolved_conflict_count: "待确认差异", base_dataset_version_id: "基础版本", search_query: "原始检索式", region: "原始地区筛选", partial_results: "部分检索结果", imported_at: "导入日期", support_count: "支持记录数", priority_reason: "排序理由", method: "检索方法", examined_count: "已核查样本", query_terms: "对照检索词", results: "相近记录", interpretation: "解读边界", matched_terms: "匹配词", abstract: "原始摘要", publication_numbers: "公开标识", semantic_evidence: "分类原文依据", metric: "统计项", rows: "统计记录", unit: "统计单位", country: "来源国家", year: "申请年", name: "名称", inventor: "发明人", ordinal: "资料顺序", type: "类型", location: "原文位置", paragraph: "段落", table: "表格", row: "行", column: "列", source_scope: "来源范围", normalization_rule: "清洗规则", counts: "数量", ready_count: "就绪数" });
  return <article className="readable-card" id={object.id}>{title && <h3>{title}</h3>}{keys.map(key => <div key={key} className="readable-row"><span className="small muted">{labels[key] || key}</span><div>{typeof object[key] === "object" ? <Readable value={object[key]} /> : <span className="whitespace">{humanValue(object[key])}</span>}</div></div>)}</article>;
}
