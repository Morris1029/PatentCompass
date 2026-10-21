"use client";
import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, Json, Page, post, refresh, statusLabel, useApi } from "@/lib/api";
import { researchLabel } from "@/lib/research";
import { useChatViewport } from "@/lib/chat-viewport";
import { DraftAttachment, useStore } from "@/lib/store";
import { AssistantMark, ErrorNotice, Modal, TaskCard } from "./shared";
import MessageMarkdown from "./message-markdown";
import type { Navigate } from "./workspace";

type Attachment = DraftAttachment;
const EMPTY_ATTACHMENTS: Attachment[] = [];
export default function Chat({ project, conversationId, conversation, navigate, createProject }: { project?: Json; conversationId: string | null; conversation?: Json; navigate: Navigate; createProject: () => void }) {
  const params = useSearchParams(); const targetMessage = params.get("message"); const located = useRef<string | undefined>(undefined);
  const key = `${project?.id || "welcome"}:${conversationId || "draft"}`;
  const draft = useStore(s => s.drafts[key]); const setDraft = useStore(s => s.draft); const track = useStore(s => s.track);
  const attachments = draft?.attachments || EMPTY_ATTACHMENTS;
  const setAttachments = (update: Attachment[] | ((current: Attachment[]) => Attachment[])) => { const current = useStore.getState().drafts[key]?.attachments || EMPTY_ATTACHMENTS; const next = typeof update === "function" ? update(current) : update; const changed = current.map(item => item.materialId || item.localId).join() !== next.map(item => item.materialId || item.localId).join(); setDraft(key, { attachments: next, ...(changed ? { submissionKey: undefined } : {}) }); };
  const fileCache = useRef<Map<string, File>>(new Map());
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>();
  const [material, setMaterial] = useState<Json>(); const [older, setOlder] = useState<Json[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  const chat = useRef<HTMLElement>(null); const scroll = useRef<HTMLDivElement>(null); const scrollContent = useRef<HTMLDivElement>(null); const textarea = useRef<HTMLTextAreaElement>(null); const followBottom = useRef(!targetMessage);
  useChatViewport(chat);
  const { data: messages, error: messagesError } = useApi<Page>(conversationId ? `/api/conversations/${conversationId}/messages?limit=200` : null, 3000);
  const { data: messageContext, error: contextError } = useApi<Page>(conversationId && targetMessage ? `/api/conversations/${conversationId}/messages/${targetMessage}/context` : null);
  const { data: materials } = useApi<Page>(project ? `/api/projects/${project.id}/materials?limit=200` : null, attachments.length ? 2000 : 0);
  useEffect(() => { setOlder([]); setError(undefined); fileCache.current.clear(); }, [key]);
  useEffect(() => {
    if (attachments.some(item => item.status === "uploading" && !item.materialId && !fileCache.current.has(item.localId))) setAttachments(current => current.map(item => item.status === "uploading" && !item.materialId && !fileCache.current.has(item.localId) ? { ...item, status: "failed", error: "上传未完成，请移除此附件后重新选择文件。" } : item));
  }, [key, draft?.attachments]);
  useEffect(() => {
    if (!materials) return;
    setAttachments(current => current.map(item => {
      const stored = materials.items.find(entry => entry.id === item.materialId);
      return stored && stored.status !== item.status ? { ...item, status: stored.status, error: stored.error?.message } : item;
    }));
  }, [materials]);
  const upload = async (attachment: Attachment) => {
    if (!project) return;
    try {
      let result: Json;
      if (attachment.runId && attachment.materialId) result = await post(`/api/runs/${attachment.runId}/retry`);
      else {
        const file = fileCache.current.get(attachment.localId);
        if (!file) throw new Error("原文件尚未上传成功，请移除此附件后重新选择文件。");
        const body = new FormData(); body.append("file", file);
        result = await post(`/api/projects/${project.id}/materials`, body);
      }
      setAttachments(current => current.map(item => item.localId === attachment.localId ? { ...item, materialId: result.material_id || result.id || item.materialId, runId: result.run_id, status: result.status === "ready" ? "ready" : "parsing", error: undefined } : item));
      if (result.run_id) track(project.id, result.run_id);
      await refresh();
    } catch (e) { setAttachments(current => current.map(item => item.localId === attachment.localId ? { ...item, status: "failed", error: e instanceof Error ? e.message : "上传失败" } : item)); }
  };
  const selectFiles = (files: FileList | null) => {
    if (!files) return;
    const accepted = Array.from(files).slice(0, 10 - attachments.length).map(file => { const localId = crypto.randomUUID(); return { localId, name: file.name, status: "uploading", file }; });
    accepted.forEach(item => { fileCache.current.set(item.localId, item.file); });
    setAttachments(current => [...current, ...accepted.map(({ file: _file, ...metadata }) => metadata)].slice(0, 10));
    accepted.forEach(upload);
    if (fileInput.current) fileInput.current.value = "";
  };
  const send = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!project) { createProject(); return; }
    if (busy || attachments.some(item => item.status !== "ready") || (!draft?.content?.trim() && !attachments.length)) return;
    setBusy(true); setError(undefined);
    try {
      const submissionKey = useStore.getState().drafts[key]?.submissionKey || crypto.randomUUID();
      setDraft(key, { submissionKey });
      const body = { content: draft?.content?.trim() || "", action: "auto", attachments: attachments.map(item => item.materialId), ...(draft?.reference ? { reference: draft.reference } : {}), report_mode: "ai", semantic_mode: "full" };
      const result = conversationId ? await post(`/api/conversations/${conversationId}/messages`, body, submissionKey) : await post(`/api/projects/${project.id}/conversations`, { initial_message: body }, submissionKey);
      followBottom.current = true;
      track(project.id, result.run_id); setDraft(key, { content: "", reference: undefined, attachments: [], submissionKey: undefined }); await refresh();
      if (!conversationId) navigate({ conversation: result.conversation_id, research: result.research_id || null, message: null, view: "chat" });
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const allMessages = [...older, ...(messageContext?.items || []), ...(messages?.items || [])].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)).filter((message, index, list) => list.findIndex(item => item.id === message.id) === index);
  const hasConversation = !!conversationId || allMessages.length > 0;
  const resizeComposer = useCallback(() => {
    const field = textarea.current; if (!field) return;
    const scrollTop = field.scrollTop;
    field.style.height = "0px";
    const minimum = hasConversation ? Math.ceil(parseFloat(getComputedStyle(field).lineHeight) || 24) : 105;
    const height = Math.min(160, Math.max(minimum, field.value ? field.scrollHeight : 0));
    field.style.height = `${height}px`; field.style.overflowY = field.scrollHeight > height ? "auto" : "hidden"; field.scrollTop = scrollTop;
  }, [hasConversation]);
  useLayoutEffect(resizeComposer, [draft?.content, resizeComposer]);
  useEffect(() => {
    const field = textarea.current; if (!field) return;
    let width = field.clientWidth;
    const observer = new ResizeObserver(() => { if (field.clientWidth !== width) { width = field.clientWidth; resizeComposer(); } });
    observer.observe(field); return () => observer.disconnect();
  }, [resizeComposer]);
  useLayoutEffect(() => { followBottom.current = !targetMessage; }, [key, targetMessage]);
  useEffect(() => {
    const content = scrollContent.current; if (!content) return;
    const observer = new ResizeObserver(() => { if (followBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; });
    observer.observe(content); if (scroll.current) observer.observe(scroll.current); return () => observer.disconnect();
  }, [key]);
  useEffect(() => {
    const location = `${conversationId}:${targetMessage}`;
    if (!targetMessage || located.current === location || !allMessages.some(message => message.id === targetMessage)) return;
    const timer = setTimeout(() => { const element = document.getElementById(`message-${targetMessage}`); if (element) { element.scrollIntoView({ behavior: "smooth", block: "center" }); element.focus({ preventScroll: true }); located.current = location; } }, 100);
    return () => clearTimeout(timer);
  }, [conversationId, targetMessage, messages, messageContext]);
  const taskOwners = new Map<string, string>();
  allMessages.forEach(message => { if (message.run_id && (message.role === "assistant" || !taskOwners.has(message.run_id))) taskOwners.set(message.run_id, message.id); });
  return <section ref={chat} className={`home-workbench ${hasConversation ? "has-conversation" : ""}`} aria-label="研究对话">
    <div ref={scroll} className="chat-scroll" role="region" aria-label="对话内容" tabIndex={0} onScroll={event => { const area = event.currentTarget; followBottom.current = area.scrollHeight - area.clientHeight - area.scrollTop < 80; }}><div ref={scrollContent} className="chat-scroll-content">
    {conversation?.research && <div className="research-conversation-banner"><div><strong>{conversation.research.number ? `V${conversation.research.number} · ${conversation.research.main_conversation_id === conversationId ? "研究主对话" : "关联研究"}` : "新研究 · 准备中"}</strong><p>{conversation.research.status === "draft" ? "完善研究目标后，再确认生成报告。成功保存后才分配新的研究版本编号。" : "日常报告修改保存在本研究的修改记录中。"}</p></div>{(conversation.research.current_revision_id || conversation.research.current_version_id) && <button className="text-button" onClick={() => navigate({ view: "report", research: conversation.research_id, version: null, diff: null, anchor: null, message: null })}>查看报告 →</button>}</div>}
    {!allMessages.length && <section className="agent-welcome"><span className="welcome-mark" aria-hidden="true">✦</span><h1>从一个产业问题开始</h1><p>描述研究目标，逐步完善项目底稿与导航报告。</p>{!project && <button className="btn secondary welcome-create" onClick={createProject}>创建研究项目</button>}</section>}
    <ErrorNotice error={messagesError || contextError || error} />
    {allMessages.length > 0 && <div className="conversation-thread" aria-live="polite">{messages?.next_cursor && <button className="text-button" onClick={async () => { const result = await api<Page>(`/api/conversations/${conversationId}/messages?limit=200&cursor=${messages.next_cursor}`); setOlder(current => [...current, ...result.items]); }}>加载更多消息</button>}
      {allMessages.map(message => <Fragment key={message.id}><article id={`message-${message.id}`} tabIndex={targetMessage === message.id ? -1 : undefined} className={`conversation-message ${message.role}${targetMessage === message.id ? " is-targeted" : ""}`} data-message-id={message.id}>{message.role !== "user" && <AssistantMark />}<div className={message.role === "user" ? "message-content" : "assistant-content"}>
        {message.reference?.text && <blockquote className="message-reference"><span className="label">引用报告历史版本</span><p>{message.reference.text}</p></blockquote>}
        {message.role === "user" ? <p className="message-text">{message.content || (message.attachments?.length ? "已提交项目资料" : "")}</p> : <MessageMarkdown content={message.content || ""} />}
        {message.attachments?.length > 0 && <div className="message-attachments">{message.attachments.map((id: string) => { const item = materials?.items.find(entry => entry.id === id); return <button key={id} className="attachment-chip" onClick={async () => setMaterial(await api(`/api/materials/${id}`))}>▤ {item?.file_name || item?.original_name || item?.name || "Word 资料"}</button>; })}</div>}
        {message.reference?.citations?.map((citation: Json, i: number) => <details key={citation.id || i} className="compact-details"><summary>原文依据 {i + 1} · {citation.application_number || citation.title || "资料"}</summary><p className="small whitespace">{citation.text || citation.abstract || citation.quote}</p><button className="text-button" onClick={async () => { if (citation.material_id) setMaterial(await api(`/api/materials/${citation.material_id}`)); else navigate({ view: "workbench", tab: "data", patent: citation.application_id || citation.patent_id || citation.id, analysis_id: message.reference?.analysis_id || null }); }}>定位记录</button></details>)}
        {!message.run_id && (message.cards || []).filter((card: Json) => card.type !== "task").map((card: Json, i: number) => <div className="artifact-card" key={card.id || i}><strong>{card.report_version_id ? `导航报告${card.research_number ? ` · ${researchLabel(card)}` : ""}` : card.title || card.summary || (card.analysis_id ? "研究分析已完成" : card.chart_version_id ? "图表新版本" : "研究成果")}</strong><p className="small muted">{card.description}</p><div className="actions">{card.type === "forward" && card.conversation_id && <button className="text-button" onClick={() => navigate({ view: "chat", conversation: card.conversation_id, research: card.research_id || null, message: card.message_id || null, version: null, diff: null, anchor: null })}>前往报告主对话 →</button>}{(card.report_version_id || card.version_id) && <button className="text-button" onClick={() => navigate({ view: "report", research: card.research_id || null, version: card.report_version_id || card.version_id, diff: card.parent_id || "parent", anchor: card.block_id || null, message: null })}>查看改动 →</button>}{card.analysis_id && <button className="text-button" onClick={() => navigate({ view: "workbench", tab: "overview", analysis_id: card.analysis_id })}>打开分析 →</button>}</div></div>)}
        {message.role !== "user" && taskOwners.get(message.run_id) === message.id && <TaskCard id={message.run_id} project={project} presentation="chat" hasReply navigate={navigate} artifacts={message.cards || []} />}
      </div></article>{message.role === "user" && taskOwners.get(message.run_id) === message.id && <TaskCard id={message.run_id} project={project} presentation="chat" navigate={navigate} />}</Fragment>)}
    </div>}
    </div></div>
    <div className="chat-composer-dock"><form className="agent-composer" onSubmit={send} aria-busy={busy}>
      <div className="composer-context">
      {draft?.reference && <div className="composer-quote"><div><span className="label">引用到对话 · 固定报告版本</span><p>{draft.reference.text.slice(0, 240)}</p></div><button type="button" className="icon-button" aria-label="移除报告引用" onClick={() => setDraft(key, { reference: undefined, submissionKey: undefined })}>×</button></div>}
      {attachments.length > 0 && <div className="composer-attachments">{attachments.map(item => <div className={`attachment-chip attachment-${item.status}`} key={item.localId}><span>▤ {item.name}<small>{statusLabel[item.status] || (item.status === "uploading" ? "上传中" : item.status)}</small>{item.error && <span className="attachment-error">{item.error}</span>}</span>{item.status === "failed" && <button type="button" className="text-button" onClick={() => upload(item)}>重试</button>}<button type="button" className="icon-button" aria-label={`移除 ${item.name}`} onClick={() => setAttachments(current => current.filter(entry => entry.localId !== item.localId))}>×</button></div>)}</div>}
      {attachments.some(item => item.status !== "ready") && <p className="attachment-note" role="status">资料解析完成后可发送，草稿会保留。解析失败时可移除或重试。</p>}
      </div>
      <label htmlFor="chat-message" className="visually-hidden">描述你的研究问题</label><textarea ref={textarea} id="chat-message" rows={hasConversation ? 1 : 4} placeholder={hasConversation ? "继续提问，或修改报告" : "描述研究问题，或提出对报告的修改要求"} value={draft?.content || ""} onChange={e => setDraft(key, { content: e.target.value, submissionKey: undefined })} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
      <div className="agent-composer-actions"><div className="composer-tools"><button type="button" className="composer-attach" aria-label="添加 Word 文档" title="添加 Word 文档" disabled={!project || attachments.length >= 10} onClick={() => fileInput.current?.click()}><svg aria-hidden="true" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" strokeWidth="1.7"><path d="M12 5v14M5 12h14" strokeLinecap="round" /></svg></button></div><button type="submit" className="composer-send" aria-label={busy ? "正在发送消息" : "发送消息"} title={busy ? "正在发送消息" : "发送消息"} disabled={busy || attachments.some(item => item.status !== "ready") || (!!project && !draft?.content?.trim() && !attachments.length)}>{busy ? <svg className="composer-spinner" aria-hidden="true" viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="2"><path d="M20 12a8 8 0 1 1-8-8" strokeLinecap="round" /></svg> : <svg aria-hidden="true" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 19V5m-6 6 6-6 6 6" strokeLinecap="round" strokeLinejoin="round" /></svg>}</button></div>
      <input ref={fileInput} type="file" accept=".doc,.docx" multiple hidden onChange={e => selectFiles(e.target.files)} />
      {busy && <span className="visually-hidden" role="status">正在发送消息</span>}
    </form></div>
    {material && <Modal title={material.file_name || material.original_name || material.name || "项目资料"} close={() => setMaterial(undefined)} wide><p className="small muted">定位基于段落、表格和单元格；不作为 Word 页码。</p>{material.file_id && <a href={`/api/files/${material.file_id}`} className="text-button">下载原文件</a>}{(material.blocks || []).map((block: Json) => <article key={block.id} className="material-block"><span className="label">{block.location ? Object.entries(block.location).map(([key, value]) => `${({ paragraph: "段落", table: "表格", row: "行", column: "列", cell: "单元格" } as Json)[key] || key} ${value}`).join(" · ") : block.kind}</span><p className="whitespace">{block.text}</p></article>)}</Modal>}
  </section>;
}
