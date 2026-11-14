"use client";
import type { ReactNode } from "react";
import { Json, useReportVersion } from "@/lib/api";
import { ErrorNotice } from "./shared";
import WorkspaceIcon, { type WorkspaceIconName } from "./workspace-icon";
import type { Navigate } from "./workspace";

export default function ResearchOverview({ project, analysis, navigate, actions, continueConversation, children }: {
  project: Json; analysis?: Json; navigate: Navigate; actions: ReactNode; continueConversation: () => void; children: ReactNode;
}) {
  const reportVersionId = project.current_report_version_id;
  const { data: report, error: reportError, mutate: reloadReport } = useReportVersion(project.report_id, reportVersionId);
  const data = analysis?.data?.data;
  const hasReport = !!reportVersionId;
  const reportDate = report?.created_at ? new Date(report.created_at) : undefined;
  const updated = reportDate && !Number.isNaN(reportDate.getTime()) ? reportDate.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "—";
  const steps = [
    { title: "数据准备", done: !!project.dataset_version_id, status: project.dataset_version_id ? "已选择数据" : "待准备" },
    { title: "范围与目标", done: !!project.scope?.confirmed, status: project.scope?.confirmed ? "已确认" : "待确认" },
    { title: "分析与图表", done: !!analysis?.analysis_id, status: analysis?.analysis_id ? "已有分析结果" : "待生成" },
    { title: "导航报告", done: hasReport, status: hasReport ? "已生成" : "待生成" },
  ];
  const shortcuts: { tab: string; icon: WorkspaceIconName; title: string; description: string }[] = [
    { tab: "analysis", icon: "analysis", title: "态势分析", description: "查看技术发展、竞争主体与地域布局" },
    { tab: "layout", icon: "layout", title: "布局与路线图", description: "梳理候选方向、推进条件与阶段行动" },
    { tab: "data", icon: "data", title: "数据与证据", description: "管理专题数据，回查专利与原文依据" },
  ];
  return <div className="research-overview">
    <div className="overview-grid">
      <div className="overview-body">
        <section className="overview-hero"><span className="label">RESEARCH OVERVIEW</span><h1>研究概览</h1><p>{project.scope?.content?.goal || "汇集研究基础、分析发现与原文证据，逐步形成专利导航报告。"}</p><div className="actions overview-analysis-actions">{actions}</div></section>
        <div className="overview-metrics">{[
          ["入选申请", analysis?.summary?.application_count ?? data?.application_count, "report"],
          ["公开标识", analysis?.summary?.publication_count ?? data?.publication_count, "scope"],
          ["行级摘要", analysis?.summary?.abstract_count ?? data?.abstract_count, "analysis"],
          ["统计单位", "件申请", "data"],
        ].map(([label, value, icon]) => <div className="overview-metric" key={label}><span className="overview-metric-icon"><WorkspaceIcon name={icon as WorkspaceIconName} size={26} /></span><div><span>{label}</span><strong>{value ?? "—"}</strong></div></div>)}</div>
        <section className="overview-progress overview-surface"><h2><WorkspaceIcon name="layout" />研究进展</h2><ol>{steps.map((step, index) => <li key={step.title} className={step.done ? "is-complete" : ""}><span className="progress-node">{step.done ? <WorkspaceIcon name="check" size={15} /> : index + 1}</span><strong>{step.title}</strong><span>{step.status}</span></li>)}</ol><p className="overview-progress-note">按当前研究的已有数据与成果展示；你可以随时回到对话继续梳理。</p></section>
        {children}
      </div>
      <aside className="overview-report overview-surface" aria-label="导航报告入口"><h2><WorkspaceIcon name="report" size={26} />导航报告</h2><div className="overview-report-feature"><div className="report-cover" aria-hidden="true"><WorkspaceIcon name="report" size={25} /><strong>导航报告</strong><span>PATENT NAVIGATION</span><i /></div><p>汇集研究结论与证据，<br />形成可持续完善的导航成果。</p><button className="btn primary" onClick={hasReport ? () => navigate({ view: "report", research: project.research_id || null, version: null, diff: null, anchor: null, message: null }) : continueConversation}>{hasReport ? "进入导航报告" : "在对话中推进研究"}<span aria-hidden="true">→</span></button></div><dl className="overview-report-meta"><div><dt><WorkspaceIcon name="report" size={17} />研究版本</dt><dd>{project.research_number ? `研究 V${project.research_number}` : "新研究 · 未编号"}</dd></div><div><dt><WorkspaceIcon name="clock" size={17} />报告更新</dt><dd>{hasReport && !report && !reportError ? "读取中…" : updated}</dd></div><div><dt><span className={`report-state-dot ${hasReport ? "is-ready" : ""}`} />报告状态</dt><dd>{hasReport ? "已生成，可继续修改" : "尚未生成"}</dd></div></dl>{!hasReport && <p className="small muted">先在对话中明确目标、完成分析，再生成报告。</p>}<ErrorNotice error={reportError} />{reportError && <button className="text-button" onClick={() => reloadReport()}>重新读取报告信息</button>}</aside>
    </div>
    <section className="overview-shortcuts" aria-label="分析工作入口"><div className="overview-shortcuts-heading"><h2><WorkspaceIcon name="data" />分析工作入口</h2><p>按研究需要，继续查看与梳理成果</p></div><div className="overview-shortcut-grid">{shortcuts.map(item => <button key={item.tab} className="overview-shortcut" onClick={() => navigate({ tab: item.tab, analysis: null })}><span className="overview-shortcut-icon"><WorkspaceIcon name={item.icon} size={30} /></span><span><strong>{item.title}</strong><small>{item.description}</small></span><span className="shortcut-arrow" aria-hidden="true">›</span></button>)}</div></section>
  </div>;
}
