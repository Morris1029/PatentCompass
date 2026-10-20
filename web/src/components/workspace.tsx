"use client";
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Json, Page, post, refresh, useApi, useReportVersion } from "@/lib/api";
import { useStore } from "@/lib/store";
import { withResearchContext } from "@/lib/research";
import { Empty, ErrorNotice, Modal } from "./shared";
import Chat from "./chat";
import Workbench from "./workbench";
import Report from "./report";

export type Navigate = (changes: Record<string, string | null>) => void;
export default function Workspace() {
  const query = useSearchParams(); const router = useRouter(); const pathname = usePathname();
  const projectId = query.get("project"); const conversationId = query.get("conversation"); const view = query.get("view") || "chat";
  const { data: projects, error: projectsError } = useApi<Page>("/api/projects?limit=200");
  const { data: project, error: projectError } = useApi(projectId ? `/api/projects/${projectId}` : null, 6000);
  const { data: conversations } = useApi<Page>(projectId ? `/api/projects/${projectId}/conversations?limit=200` : null, 6000);
  const { data: conversation, error: conversationError } = useApi(conversationId ? `/api/conversations/${conversationId}` : null, 6000);
  const { data: selectedRevision } = useReportVersion(project?.report_id, view === "report" ? query.get("version") : null);
  const researchId = view === "chat" ? (conversationId ? conversation?.research_id : undefined) : (view === "report" ? selectedRevision?.research_id : null) || query.get("research") || project?.current_research_id;
  const { data: research, error: researchError } = useApi(researchId ? `/api/research-versions/${researchId}` : null, view === "report" ? 0 : 6000);
  const context = research?.context || (conversation?.research_id === researchId ? conversation?.context : undefined);
  const scopedProject = project && withResearchContext(project, research || (conversation?.research_id === researchId ? conversation?.research : undefined), context);
  const contextLoading = (!!conversationId && !conversation) || (!!researchId && !context);
  const collapsed = useStore(s => s.collapsed); const collapse = useStore(s => s.collapse);
  const [mobile, setMobile] = useState(false); const [creating, setCreating] = useState(false); const [name, setName] = useState(""); const [error, setError] = useState<unknown>(); const [busy, setBusy] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  useEffect(() => { useStore.persist.rehydrate(); }, []);
  useEffect(() => {
    if (!mobile) return;
    const previous = document.activeElement as HTMLElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    sidebar.current?.querySelector<HTMLButtonElement>(".mobile-close")?.focus();
    return () => { document.body.style.overflow = previousOverflow; previous?.focus(); };
  }, [mobile]);
  const navigate: Navigate = changes => {
    const next = new URLSearchParams(query.toString());
    if (changes.project !== undefined && changes.project !== projectId) {
      for (const key of ["conversation", "research", "message", "version", "diff", "anchor", "analysis_id", "patent", "chart_id", "tab", "analysis"]) next.delete(key);
    }
    if (changes.conversation !== undefined && changes.conversation !== conversationId) { next.delete("message"); if (changes.research === undefined) next.delete("research"); }
    if (changes.research !== undefined && changes.research !== query.get("research")) for (const key of ["version", "diff", "anchor", "message", "analysis_id", "patent", "chart_id", "analysis"]) next.delete(key);
    if (changes.view !== undefined && changes.view !== view) { next.delete("message"); next.delete("anchor"); }
    Object.entries(changes).forEach(([key, val]) => val === null ? next.delete(key) : next.set(key, val));
    router.push(`${pathname}?${next}`); setMobile(false);
  };
  const create = async (event: React.FormEvent) => {
    event.preventDefault(); if (!name.trim()) return; setBusy(true); setError(undefined);
    try { const result = await post("/api/projects", { name: name.trim(), scope: {} }); await refresh(); setCreating(false); setName(""); navigate({ project: result.id, conversation: null, view: "chat", version: null, anchor: null }); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  const draft = () => navigate({ conversation: null, research: null, message: null, view: "chat", version: null, diff: null, anchor: null });
  return <div className={`${collapsed ? "sidebar-is-collapsed" : ""} ${mobile ? "mobile-sidebar-open" : ""} workspace-view-${view}`}>
    <button className="mobile-nav-toggle icon-button" onClick={() => setMobile(true)} aria-label="打开项目导航">☰</button>
    {mobile && <button className="sidebar-backdrop" onClick={() => setMobile(false)} aria-label="关闭项目导航" />}
    <aside ref={sidebar} className={`workspace-sidebar ${collapsed ? "is-collapsed" : ""}`} aria-label="工作台导航" onKeyDown={e => { if (e.key === "Escape") setMobile(false); if (mobile && e.key === "Tab") { const items = Array.from(sidebar.current?.querySelectorAll<HTMLElement>("button,a") || []).filter(item => item.offsetParent !== null); const first = items[0]; const last = items.at(-1); if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); } } }}>
      <div className="sidebar-head"><a className="sidebar-brand" href="/"><span className="sidebar-brand-mark" aria-hidden="true">导</span><strong>专利导航</strong></a><button className="sidebar-collapse" onClick={collapse} aria-label={collapsed ? "展开侧栏" : "收起侧栏"} aria-expanded={!collapsed}>▥</button><button className="mobile-close icon-button" onClick={() => setMobile(false)} aria-label="关闭导航">×</button></div>
      <nav className="sidebar-nav" aria-label="项目导航"><button className="sidebar-link sidebar-new" onClick={() => setCreating(true)} title="新建项目"><span className="sidebar-icon">＋</span><span className="sidebar-text">新建项目</span></button><span className="sidebar-caption">项目</span>
        <div className="research-list">{projects?.items.map(item => <div className={`project-group ${projectId === item.id ? "is-active" : ""}`} key={item.id}>
          <div className="project-heading"><button className="project-name" title={item.name} onClick={() => navigate({ project: item.id, conversation: null, view: "chat", version: null, anchor: null })}><span aria-hidden="true">◇</span><span className="sidebar-text">{item.name}</span></button><button className="project-add" aria-label={`在 ${item.name} 新建对话`} onClick={() => { if (item.id === projectId) draft(); else navigate({ project: item.id, conversation: null, view: "chat" }); }}>＋</button></div>
          {projectId === item.id && <div className="project-children"><button className={`sidebar-link ${view === "workbench" ? "active" : ""}`} onClick={() => navigate({ view: "workbench", research: researchId || null, tab: "overview", analysis: null, version: null, diff: null })} title="分析工作台"><span className="sidebar-icon">▦</span><span className="sidebar-text">分析工作台</span></button><button className={`sidebar-link ${view === "report" ? "active" : ""}`} onClick={() => navigate({ view: "report", research: null, version: null, diff: null, anchor: null, message: null })} title="导航报告"><span className="sidebar-icon">▤</span><span className="sidebar-text">导航报告</span></button><span className="sidebar-caption">对话历史</span>{conversations?.items.map(conversation => <button key={conversation.id} className={`sidebar-link conversation-link ${conversationId === conversation.id && view === "chat" ? "active" : ""}`} onClick={() => navigate({ conversation: conversation.id, research: conversation.research_id || null, view: "chat", version: null, diff: null, anchor: null, message: null })} title={conversation.title}><span className="sidebar-icon">{conversation.is_main ? "◈" : "◌"}</span><span className="sidebar-text">{conversation.title}</span>{conversation.is_main && <small className="conversation-research-tag">{conversation.research_number ? `V${conversation.research_number}` : "新研究"}</small>}</button>)}{!conversations?.items.length && <p className="small muted sidebar-text">首次发送后保存</p>}</div>}
        </div>)}{!projects?.items.length && <p className="research-list-empty">还没有项目</p>}</div>
      </nav><div className="sidebar-foot">行业专利导航助手</div>
    </aside>
    <main className="workspace-main"><header className="workspace-topline conversation-context"><span>{project?.name || "研究工作台"}</span>{project && <><span className="context-separator">/</span><span>{view === "workbench" ? "分析工作台" : view === "report" ? "导航报告" : conversation?.title || conversations?.items.find(c => c.id === conversationId)?.title || "新对话"}</span><span className="context-status">{research?.number ? `研究 V${research.number}` : research?.status === "draft" ? "新研究 · 未编号" : project.dataset_version_id ? "已绑定专利数据" : "尚未选择数据"}</span></>}</header>
      <ErrorNotice error={projectsError || projectError || conversationError || researchError} />
      {!project && projectId ? <Empty title="正在读取项目" /> : view === "report" ? project ? <Report key={project.id} project={project} navigate={navigate} /> : <Empty title="先创建或选择一个项目" /> : contextLoading ? <Empty title={conversationError || researchError ? "无法读取研究上下文" : "正在读取研究上下文"}>{(conversationError || researchError) && <button className="text-button" onClick={() => refresh()}>重试</button>}</Empty> : view === "workbench" ? scopedProject ? <Workbench key={`${project.id}:${researchId || "project"}`} project={scopedProject} navigate={navigate} /> : <Empty title="先创建或选择一个项目" /> : <Chat project={scopedProject} conversationId={conversationId} conversation={conversation} navigate={navigate} createProject={() => setCreating(true)} />}
    </main>
    {creating && <Modal title="新建研究项目" close={() => setCreating(false)}><form onSubmit={create}><label className="form-field">项目名称<input autoFocus value={name} maxLength={300} onChange={e => setName(e.target.value)} placeholder="例如：无人机技术导航研究" required /></label><p className="small muted">项目共享数据和资料，各轮研究分别保留范围、分析与报告。</p><ErrorNotice error={error} /><div className="actions"><button className="btn primary" disabled={busy}>创建项目</button><button type="button" className="btn secondary" onClick={() => setCreating(false)}>取消</button></div></form></Modal>}
  </div>;
}
