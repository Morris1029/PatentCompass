"use client";
import { useState } from "react";
import { describe, Json } from "@/lib/api";

const fields: Record<string, { label: string; type?: string }> = {
  industry: { label: "研究领域" }, goal: { label: "研究目标" }, need: { label: "研究需求" }, focus: { label: "关注重点" },
  q: { label: "关键词" }, applicant: { label: "申请人" }, ipc: { label: "IPC 分类" }, source_region: { label: "来源地区" },
  coverage: { label: "覆盖范围" }, start: { label: "开始年份", type: "number" }, end: { label: "结束年份", type: "number" },
  application_start: { label: "申请开始日期", type: "date" }, application_end: { label: "申请结束日期", type: "date" },
};
export default function ScopeEditor({ value, change, editable = true }: { value: Json; change: (scope: Json) => void; editable?: boolean }) {
  const [editing, setEditing] = useState<string>();
  const visible = Object.keys(fields).filter(key => value[key] !== undefined || ["industry", "goal"].includes(key));
  const remaining = Object.keys(fields).filter(key => !visible.includes(key));
  const row = (key: string) => <div className="scope-edit-row" key={key}><dt>{fields[key].label}</dt><dd>{editable && editing === key ? <input autoFocus aria-label={fields[key].label} type={fields[key].type || "text"} value={value[key] ?? ""} onChange={event => change({ ...value, [key]: event.target.value })} onBlur={() => setEditing(undefined)} onKeyDown={event => { if (event.key === "Enter" || event.key === "Escape") { event.preventDefault(); setEditing(undefined); } }} /> : editable ? <button type="button" className="scope-edit-value" onClick={() => setEditing(key)} aria-label={`编辑${fields[key].label}`}>{value[key] !== undefined && value[key] !== "" ? describe(value[key]) : "点击填写"}<span aria-hidden="true">✎</span></button> : <span className="scope-readonly-value">{value[key] !== undefined && value[key] !== "" ? describe(value[key]) : "未限定"}</span>}</dd></div>;
  return <div className="scope-editor"><div className="scope-editor-caption"><strong>研究范围</strong><span>{editable ? "点击内容可修改" : "固定版本范围"}</span></div><dl>{visible.map(row)}</dl>{editable && remaining.length > 0 && <details className="scope-more"><summary>更多筛选条件</summary><dl>{remaining.map(row)}</dl></details>}</div>;
}
