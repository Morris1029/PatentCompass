"use client";
import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { api, Json, Page, statusLabel, useApi } from "@/lib/api";
import { activeRunInterval, managementTabs } from "@/lib/management";
import { Empty, ErrorNotice } from "./shared";

type Go = (changes: Record<string, string | null>) => void;
const configurationPath = "/api/management/configuration";
const configOptions = { revalidateOnFocus: false, errorRetryCount: 1 };
const stamp = (value?: string) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "未记录";
const show = (value: unknown) => JSON.stringify(value, null, 2);

export function UserMenu({ openManagement }: { openManagement: () => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const item = useRef<HTMLButtonElement>(null);
  const { data } = useSWR<Json>(open ? configurationPath : null, api, configOptions);
  useEffect(() => {
    if (!open) return;
    item.current?.focus();
    const outside = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return <div className="sidebar-account" ref={ref} onKeyDown={event => {
    if (event.key === "Escape") { event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
    if (open && ["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) { event.preventDefault(); item.current?.focus(); }
    if (event.key === "Tab") setOpen(false);
  }}>
    {open && <div className="account-menu" role="menu" aria-label="用户菜单"><div className="account-identity"><strong>{data?.identity.display_name || "本地用户"}</strong><small>{data?.identity.owner_id || "正在读取用户信息…"}</small></div><button ref={item} role="menuitem" onClick={() => { setOpen(false); openManagement(); }}>运行管理<span aria-hidden="true">↗</span></button></div>}
    <button ref={trigger} className="account-trigger" aria-label="本地用户菜单" aria-haspopup="menu" aria-expanded={open} title="用户与运行管理" onClick={() => setOpen(value => !value)} onKeyDown={event => { if (["ArrowUp", "ArrowDown"].includes(event.key)) { event.preventDefault(); setOpen(true); } }}><span className="account-avatar" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><circle cx="12" cy="8" r="4" /><path d="M4 22v-3a8 8 0 0 1 16 0v3" /></svg></span><span className="account-label"><strong>{data?.identity.display_name || "本地用户"}</strong><small>用户与运行管理</small></span><span className="account-chevron" aria-hidden="true">⌃</span></button>
  </div>;
}

function DocumentView({ title, value }: { title: string; value?: Json | null }) {
  const [copied, setCopied] = useState("");
  return <section className="management-card document-card"><div className="management-card-heading"><h3>{title}</h3>{value && <button className="text-button" onClick={async () => { try { await navigator.clipboard.writeText(value.content); setCopied("已复制"); } catch { setCopied("复制失败，请选择原文复制"); } }}>{copied || "复制原文"}</button>}</div>{value ? <><p className="management-source">{value.source}</p><p className="management-hash">SHA-256 {value.sha256}</p><pre className="management-code" tabIndex={0}>{value.content}</pre></> : <p className="muted">未记录</p>}</section>;
}

function Tools({ configuration }: { configuration: Json }) {
  const [search, setSearch] = useState("");
  const native = configuration.agent.tools.map((tool: Json) => tool.function);
  const shared = configuration.mcp.tools;
  const hostOnly = native.filter((tool: Json) => !shared.some((item: Json) => item.name === tool.name));
  const tools = [...shared, ...hostOnly.map((tool: Json) => ({ ...tool, group: "runtime", native: true, host_only: true }))];
  const visible = tools.filter(tool => `${tool.name} ${tool.description}`.toLowerCase().includes(search.toLowerCase()));
  return <><div className="management-card"><h3>{configuration.mcp.name}</h3><p>{configuration.mcp.transport} · <code>{configuration.mcp.endpoint}</code>（后端端点）</p><p className="muted small">MCP 注册 {shared.length} 项；内置 Agent 可调用 {native.length} 项，其中宿主专用 {hostOnly.length} 项。注册信息不代表客户端连通性已经验证。</p></div><label className="form-field management-search">搜索工具<input value={search} onChange={event => setSearch(event.target.value)} placeholder="工具名称或用途" /></label><div className="management-tool-list">{visible.map(tool => <details className="management-card tool-card" key={tool.name}><summary><span><strong>{tool.name}</strong><small>{tool.description}</small></span><span className="management-badge">{tool.host_only ? "宿主专用" : tool.native ? "MCP · 内置 Agent" : "仅 MCP"}</span></summary><p className="muted small">分组：{tool.group} · {tool.host_only ? "宿主确认适配器" : tool.read_only ? "只读工具" : "业务操作工具"}</p><pre className="management-code" tabIndex={0}>{show(tool.parameters)}</pre></details>)}</div>{!visible.length && <Empty title="没有匹配的工具" />}</>;
}

function Skills({ configuration, search, go }: { configuration: Json; search: URLSearchParams; go: Go }) {
  const name = search.get("management_skill") || configuration.skills[0]?.name;
  const reference = search.get("management_reference");
  const { data, error } = useApi(name ? `/api/management/skills/${encodeURIComponent(name)}${reference ? `?reference=${encodeURIComponent(reference)}` : ""}` : null);
  return <div className="management-split"><nav className="management-selection" aria-label="技能目录">{configuration.skills.map((skill: Json) => <button key={skill.name} aria-current={name === skill.name ? "true" : undefined} onClick={() => go({ management_skill: skill.name, management_reference: null })}><strong>{skill.name}</strong><small>{skill.description}</small></button>)}</nav><div className="management-detail"><ErrorNotice error={error} />{data ? <><div className="management-reference-nav"><button className={`btn ${!reference ? "primary" : "secondary"}`} onClick={() => go({ management_reference: null })}>SKILL.md</button>{data.references.map((path: string) => <button className={`btn ${reference === path ? "primary" : "secondary"}`} key={path} onClick={() => go({ management_reference: path })}>{path.replace("references/", "")}</button>)}</div><DocumentView key={`${name}:${reference}`} title={`${name} / ${data.resource}`} value={data} /></> : !error && <Empty title="正在读取技能" />}</div></div>;
}

function ArtifactLinks({ run, go }: { run: Json; go: Go }) {
  const value = run.artifacts || {};
  return <div className="management-artifacts">{run.project_id && run.conversation_id && <button className="text-button" onClick={() => go({ management: null, project: run.project_id, conversation: run.conversation_id, research: null, view: "chat", version: null, diff: null, anchor: null, message: null, analysis_id: null, patent: null, chart_id: null })}>打开原对话</button>}{value.analysis_id && <button className="text-button" onClick={() => go({ management: null, project: run.project_id, conversation: null, view: "workbench", tab: "analysis", analysis_id: value.analysis_id, research: value.research_id || null, version: null, diff: null, anchor: null, patent: null, chart_id: null })}>查看分析</button>}{value.report_version_id && <button className="text-button" onClick={() => go({ management: null, project: run.project_id, conversation: null, view: "report", version: value.report_version_id, diff: null, anchor: null, research: null })}>查看报告版本</button>}{value.file_id && <a className="text-button" href={value.download_url}>下载导出文件</a>}</div>;
}

function Timeline({ id, active }: { id: string; active: boolean }) {
  const [after, setAfter] = useState(0);
  const [items, setItems] = useState<Json[]>([]);
  const path = `/api/management/runs/${id}/events?after=${after}`;
  const { data, error, mutate } = useApi(path, active ? 5000 : 0);
  useEffect(() => { if (!active) void mutate(); }, [active, mutate]);
  useEffect(() => {
    if (!data) return;
    setItems(previous => [...new Map([...previous, ...data.items].map(item => [item.seq, item])).values()]);
    if (data.has_more) setAfter(data.after);
  }, [data]);
  const labels: Record<string, string> = { "agent.started": "Agent 启动", "agent.skill.loaded": "加载技能", "agent.tool.called": "调用工具", "agent.tool.completed": "工具回执", "input.required": "等待用户确认", "input.received": "收到确认", "plan.ready": "执行计划", "activity.updated": "业务任务状态", "artifact.ready": "生成产物", "run.completed": "任务完成", "run.failed": "任务失败", "run.cancelled": "任务取消", "step.updated": "执行步骤" };
  return <section className="management-card"><h3>调用时间线</h3><p className="small muted">工具返回表示调用已返回；业务是否完成以任务状态和实际产物为准。历史事件未记录的回执不推断为成功。</p><ErrorNotice error={error} /><ol className="management-timeline">{items.map(event => <li key={event.seq}><time>{stamp(event.created_at)}</time><div><strong>{labels[event.event_type] || event.event_type}</strong><span className="management-event-name">{event.payload.name || event.payload.kind || event.payload.action || ""}</span><small>{event.payload.outcome === "returned" ? "已返回" : event.payload.outcome === "error" ? `工具错误：${event.payload.error_code}` : statusLabel[event.payload.status] || event.payload.status || ""}</small><details><summary>事件详情 · #{event.seq}</summary><pre className="management-code" tabIndex={0}>{show(event.payload)}</pre></details></div></li>)}</ol>{!items.length && <p className="muted">{data ? "没有可展示的运行事件" : "正在读取事件…"}</p>}</section>;
}

function RunDetail({ id, go }: { id: string; go: Go }) {
  const { data: run, error } = useApi<Json>(`/api/management/runs/${id}`, activeRunInterval);
  if (!run) return <><ErrorNotice error={error} />{!error && <Empty title="正在读取运行详情" />}</>;
  const snapshot = run.snapshot;
  return <div className="management-run-detail"><ErrorNotice error={error} /><div className="management-card"><div className="management-card-heading"><h3>{run.project_name || "后台任务"}</h3><span className="management-badge">{statusLabel[run.status] || run.status}</span></div><p className="management-source">{run.id}</p><p>执行意图：{run.action || run.kind} · 启动时模型：{snapshot.model_name || "未记录"}</p><p className="small muted">创建于 {stamp(run.created_at)} · 更新于 {stamp(run.updated_at)}</p>{run.waiting_input && <p className="notice">等待用户确认：{run.waiting_input.title || run.waiting_input.kind}。请前往原对话处理。</p>}{run.error && <p className="notice error-notice">{run.error.code}：{run.error.message}</p>}<ArtifactLinks run={run} go={go} /><details><summary>执行计划</summary><pre className="management-code">{show(run.plan)}</pre></details></div><details className="management-card"><summary>任务当时使用的配置快照 · 已加载 {snapshot.skills.length} 项技能</summary><p className="small muted">以下内容来自任务保存的快照；缺失字段显示“未记录”。当前配置请查看 Agent、MCP 和 Skills 标签。</p><DocumentView title="主 Prompt 快照" value={snapshot.system_prompt} /><DocumentView title="宿主适配 Prompt 快照" value={snapshot.adapter_prompt} /><h3>工具清单快照</h3><p className="management-hash">{snapshot.tools_sha256 || "未记录"}</p>{snapshot.tools ? <pre className="management-code" tabIndex={0}>{show(snapshot.tools)}</pre> : <p className="muted">未记录</p>}{snapshot.skills.map((skill: Json) => <div key={skill.name}><DocumentView title={skill.name} value={skill} />{skill.references.map((reference: Json) => <DocumentView key={reference.name} title={reference.name} value={reference} />)}</div>)}</details>{run.children.length > 0 && <section className="management-card"><h3>业务子任务</h3>{run.children.map((child: Json) => <div className="management-child" key={child.id}><button className="text-button" onClick={() => go({ management_run: child.id })}>{child.kind} · {statusLabel[child.status] || child.status}</button><small className="management-source">{child.id}</small>{child.error && <p className="error-notice">{child.error.message}</p>}<ArtifactLinks run={child} go={go} /></div>)}</section>}<Timeline key={id} id={id} active={!!activeRunInterval(run)} /></div>;
}

function Runs({ projects, search, go, compact = false }: { projects: Page | undefined; search: URLSearchParams; go: Go; compact?: boolean }) {
  const project = compact ? "" : search.get("management_project") || "";
  const status = compact ? "" : search.get("management_status") || "";
  const cursor = compact ? "" : search.get("management_cursor") || "";
  const id = !compact && search.get("management_run");
  const params = new URLSearchParams({ limit: compact ? "5" : "20" });
  if (project) params.set("project_id", project); if (status) params.set("status", status); if (cursor) params.set("cursor", cursor);
  const { data, error, mutate } = useSWR<Page>(`/api/management/runs?${params}`, api, { revalidateOnFocus: false });
  return <><div className="management-card-heading"><h3>{compact ? "最近运行" : "运行记录"}</h3><button className="text-button" onClick={() => mutate()}>刷新记录</button></div>{!compact && <div className="management-filters"><label className="form-field">项目<select value={project} onChange={event => go({ management_project: event.target.value || null, management_cursor: null, management_run: null })}><option value="">全部项目</option>{projects?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label className="form-field">状态<select value={status} onChange={event => go({ management_status: event.target.value || null, management_cursor: null, management_run: null })}><option value="">全部状态</option>{["queued", "running", "waiting_input", "cancelling", "succeeded", "failed", "cancelled"].map(value => <option value={value} key={value}>{statusLabel[value]}</option>)}</select></label></div>}<ErrorNotice error={error} /><div className="management-runs">{data?.items.map(run => <button key={run.id} className={id === run.id ? "selected" : ""} onClick={() => go({ management: "runs", management_run: run.id })}><span><strong>{run.project_name || "后台任务"}</strong><small>{run.action || run.kind} · {run.runtime === "agent" ? "内置 Agent" : "业务 / 兼容执行"}</small><small>{run.skills.length ? run.skills.join(" · ") : "未记录已加载技能"}</small></span><span><span className="management-badge">{statusLabel[run.status] || run.status}</span><small>{stamp(run.created_at)}</small></span></button>)}</div>{!data && !error && <Empty title="正在读取运行记录" />}{data && !data.items.length && <Empty title="暂无符合条件的运行记录" />}{!compact && <div className="actions">{cursor && <button className="btn secondary" onClick={() => go({ management_cursor: null })}>返回最新</button>}{data?.next_cursor && <button className="btn secondary" onClick={() => go({ management_cursor: data.next_cursor || null })}>更早记录</button>}</div>}{id && <RunDetail key={id} id={id} go={go} />}</>;
}

export default function Management({ search, go, projects }: { search: URLSearchParams; go: Go; projects?: Page }) {
  const tab = managementTabs.some(item => item.id === search.get("management")) ? search.get("management") : "overview";
  const { data, error, mutate } = useSWR<Json>(configurationPath, api, configOptions);
  return <section className="management-page" aria-label="运行管理"><header className="management-heading"><div><span className="management-eyebrow">PATENTCOMPASS / RUNTIME</span><h1>运行管理</h1><p>查看当前配置与实际运行记录</p></div><div className="actions"><button className="btn secondary" onClick={() => mutate()}>刷新配置</button><button className="btn secondary" onClick={() => go({ management: null })}>返回工作台</button></div></header><nav className="management-tabs" aria-label="管理分类">{managementTabs.map(item => <button key={item.id} aria-current={tab === item.id ? "page" : undefined} onClick={() => go({ management: item.id })}>{item.label}</button>)}</nav><ErrorNotice error={error} />{!data && !error && <Empty title="正在读取运行配置" />}{data && <div className="management-content">{tab === "overview" && <><div className="management-metrics">{[["主 Agent", 1], ["Skills", data.skills.length], ["MCP 工具", data.mcp.tools.length], ["内置 Agent 工具", data.agent.tools.length]].map(([title, value]) => <div className="management-card" key={title}><span>{title}</span><strong>{value}</strong></div>)}</div><div className="management-card"><h3>当前运行配置</h3><p>生成模型：<strong>{data.model.name || "未配置"}</strong> <span className="management-badge">{data.model.configured ? "已配置" : "未配置"}</span></p><p className="small muted">“已配置”表示必要参数齐全，不代表模型或 MCP 客户端连通性已经验证。</p><p className="small muted">主 Agent 按需加载 Skills，内置运行层与 MCP 共用工具契约和后端业务服务。</p><p className="management-hash">主 Prompt · {data.agent.system_prompt.sha256}</p></div><Runs compact projects={projects} search={search} go={go} /></>}{tab === "agent" && <><div className="management-card"><h3>{data.agent.name} · 内置运行层 V{data.agent.runtime_version}</h3><p className="muted">以下为当前配置；任务使用过的内容请查看运行记录中的快照。</p></div><DocumentView title="主 Agent Prompt" value={data.agent.system_prompt} /><DocumentView title="原系统宿主适配 Prompt" value={data.agent.adapter_prompt} /></>}{tab === "mcp" && <Tools configuration={data} />}{tab === "skills" && <Skills configuration={data} search={search} go={go} />}{tab === "runs" && <Runs projects={projects} search={search} go={go} />}</div>}</section>;
}
