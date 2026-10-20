"use client";
import { useEffect, useRef, useState } from "react";
import type { EChartsOption } from "echarts";
import { api, Json, Page, post, put, refresh, useApi } from "@/lib/api";
import { useStore } from "@/lib/store";
import { DataTable, Empty, ErrorNotice, Modal, TaskCard } from "./shared";

function savedFilters(chart: Json) {
  return Object.entries(chart.provenance?.filters || {}).filter(([, value]) => Array.isArray(value) ? value.length > 0 : value !== null && value !== undefined && value !== "");
}
function ChartScope({ chart }: { chart: Json }) {
  const filters = savedFilters(chart);
  const labels: Record<string, string> = { q: "关键词", applicant: "申请人", ipc: "IPC", start: "开始年份", end: "结束年份", application_start: "申请日开始", application_end: "申请日结束", application_number: "申请号", publication_number: "公开号", theme_ids: "主题" };
  const count = Array.isArray(chart.provenance?.application_ids) ? chart.provenance.application_ids.length : undefined;
  return <div className="coverage-strip"><span>此图表版本：<strong>{count === undefined ? "申请数未记录" : `${count} 条申请`}</strong></span><span>{filters.length ? `已保存的局部筛选：${filters.map(([key, value]) => `${labels[key] || key}＝${key === "theme_ids" && Array.isArray(value) ? value.map(id => chart.data?.all_rows?.find((row: Json) => row.value === id)?.label || "已选主题").join("、") : String(value)}`).join("；")}` : "沿用冻结分析的全部入选申请"}</span></div>;
}

export function EChart({ option, onSelect, large = false }: { option: Json; onSelect?: (name: string) => void; large?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false; let cleanup: (() => void) | undefined;
    import("echarts").then(echarts => {
      if (!ref.current || disposed) return;
      const chart = echarts.init(ref.current, undefined, { renderer: "svg" });
      chart.setOption(option as EChartsOption);
      chart.on("click", event => onSelect?.(event.name));
      const observer = new ResizeObserver(() => chart.resize()); observer.observe(ref.current);
      cleanup = () => { observer.disconnect(); chart.dispose(); };
    });
    return () => { disposed = true; cleanup?.(); };
  }, [option, onSelect]);
  return <div ref={ref} className={`echart ${large ? "echart-large" : ""}`} role="img" aria-label={typeof option.title?.text === "string" ? option.title.text : "专利统计图，可点击数据查看原文记录"} />;
}
export function ChartCard({ id, projectId, frozen, onInsert }: { id: string; projectId: string; frozen?: Json; onInsert?: (chart: Json) => void }) {
  const { data } = useApi(frozen ? null : `/api/charts/${id}`); const chart = frozen || data;
  const [open, setOpen] = useState(false);
  if (!chart) return <div className="chart-loading">正在读取图表…</div>;
  return <section className="chart-card"><div className="section-head"><h3>{chart.option?.title?.text || chart.kind}</h3><button className="text-button" onClick={() => setOpen(true)}>{frozen ? "放大 / 查看 →" : "放大 / 编辑 →"}</button></div><EChart option={chart.option || {}} onSelect={() => setOpen(true)} /><p className="small muted">图表版本 {chart.version_number || chart.number || chart.version_id?.slice(0, 8)} · {frozen ? "报告快照" : "项目工作台"}{savedFilters(chart).length > 0 ? " · 局部筛选" : ""}</p><ChartScope chart={chart} />{open && <ChartDialog id={id} initial={chart} projectId={projectId} frozen={!!frozen} close={() => setOpen(false)} onInsert={onInsert} />}</section>;
}
export function ChartOption({ id }: { id: string }) {
  const { data } = useApi(`/api/charts/${id}`);
  return <option value={id}>{data?.option?.title?.text || data?.data?.title || "正在读取图表标题…"}</option>;
}
export function ChartDialog({ id, initial, projectId, close, frozen, onInsert }: { id: string; initial: Json; projectId: string; close: () => void; frozen?: boolean; onInsert?: (chart: Json) => void }) {
  const [version, setVersion] = useState(initial.version_id); const [mode, setMode] = useState("style"); const [name, setName] = useState<string>();
  const [instruction, setInstruction] = useState(""); const [title, setTitle] = useState(initial.option?.title?.text || ""); const [color, setColor] = useState("#0d7376"); const [query, setQuery] = useState(""); const [applicant, setApplicant] = useState(""); const [ipc, setIpc] = useState("");
  const [error, setError] = useState<unknown>(); const [run, setRun] = useState<string>();
  const { data: history } = useApi<Page>(`/api/charts/${id}/versions?limit=200`);
  const { data: current } = useApi(`/api/charts/${id}?version_id=${version}`);
  const chart = frozen && version === initial.version_id ? initial : current || initial;
  const { data: drill, error: drillError } = useApi<Page>(name ? `/api/projects/${projectId}/patents?chart_id=${id}&chart_version_id=${version}&dimension_value=${encodeURIComponent(name)}&limit=100` : null);
  const track = useStore(s => s.track);
  const modify = async () => {
    try {
      setError(undefined);
      const result = await post(`/api/charts/${id}/revisions`, { base_version_id: version, expected_current_version_id: history?.current_version_id || initial.version_id, mode, option: mode === "style" ? { title: { text: title }, color: [color], series: [{ itemStyle: { color } }] } : {}, filters: mode === "data" ? { ...(query ? { q: query } : {}), ...(applicant ? { applicant } : {}), ...(ipc ? { ipc } : {}) } : {}, instruction });
      if (result.run_id) { setRun(result.run_id); track(projectId, result.run_id); }
      else { setVersion(result.version_id || result.id); await refresh(); }
    } catch (e) { setError(e); }
  };
  return <Modal title="图表与原文证据" wide close={close}>
    <div className="chart-version-bar"><label>浏览版本 <select value={version} onChange={e => { setVersion(e.target.value); setName(undefined); }}>{(history?.items || [initial]).map(row => <option key={row.version_id || row.id} value={row.version_id || row.id}>V{row.number || row.version_number || 1}{(row.version_id || row.id) === history?.current_version_id ? " · 当前采用" : ""}</option>)}</select></label>{!frozen && history?.current_version_id && history.current_version_id !== version && <button className="text-button" onClick={async () => { try { await put(`/api/charts/${id}/current-version`, { version_id: version, expected_current_version_id: history.current_version_id }); await refresh(); } catch (e) { setError(e); } }}>采用此图表版本</button>}{onInsert && <button className="btn secondary" onClick={() => onInsert(chart)}>插入报告</button>}</div>
    <ChartScope chart={chart} />
    <EChart option={chart.option || {}} large onSelect={label => setName(chart.data?.all_rows?.find((row: Json) => row.label === label)?.value || label)} />
    <p className="small muted">点击数据查看对应快照中的申请记录。共同申请人采用全计数；多 IPC 标签分别计数。</p>
    {name && <section className="section"><div className="section-head"><h3>原文下钻 · {name}</h3><button className="text-button" onClick={() => setName(undefined)}>收起</button></div><ErrorNotice error={drillError} />{drill?.items && <DataTable headings={["申请号", "名称", "申请人"]} rows={drill.items.map(item => [item.application_number, <details key={item.id}><summary>{item.title}</summary><p className="small whitespace">{item.abstract}</p></details>, item.applicants?.join("；")])} />}</section>}
    {!frozen ? <section className="section"><div className="segmented" role="tablist" aria-label="图表编辑方式">{[["style", "呈现样式"], ["data", "数据范围"], ["insight", "图表解读"]].map(([value, label]) => <button key={value} role="tab" aria-selected={mode === value} className={mode === value ? "active" : ""} onClick={() => setMode(value)}>{label}</button>)}</div>
      {mode === "style" && <div className="form-grid"><label className="form-field">图表标题<input value={title} onChange={e => setTitle(e.target.value)} /></label><label className="form-field">主色<input type="color" value={color} onChange={e => setColor(e.target.value)} /></label><p className="small muted">只修改展示配置，统计数据保持原有快照。</p></div>}
      {mode === "data" && <><div className="form-grid"><label className="form-field">关键词<input value={query} onChange={e => setQuery(e.target.value)} /></label><label className="form-field">申请人<input value={applicant} onChange={e => setApplicant(e.target.value)} /></label><label className="form-field">IPC<input value={ipc} onChange={e => setIpc(e.target.value)} /></label></div><p className="small muted">确认后基于此图表的数据版本重新统计；项目共享范围保持原有设置。</p></>}
      {mode === "insight" && <label className="form-field">需要解读的问题<textarea rows={3} value={instruction} onChange={e => setInstruction(e.target.value)} placeholder="例如：解释这张图的统计变化，并给出原文依据" /></label>}
      <button className="btn primary" onClick={modify}>{mode === "data" ? "确认条件并重新统计" : "生成图表新版本"}</button><ErrorNotice error={error} />{run && <TaskCard id={run} project={{ id: projectId }} onResult={result => { if (result.version_id || result.chart_version_id) setVersion(result.version_id || result.chart_version_id); }} />}
    </section> : <p className="notice">当前展示报告生成时的图表快照。工作台采用新版本后，需明确插入或更新报告才会改变报告内容。</p>}
    {(chart.insight || chart.interpretation?.text) && <section className="section"><h3>此版本图表解读</h3><p className="whitespace">{chart.interpretation?.text || (typeof chart.insight === "string" ? chart.insight : chart.insight?.text || chart.insight?.content)}</p></section>}
  </Modal>;
}
