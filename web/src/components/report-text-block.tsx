"use client";
import { useState } from "react";
import { MarkdownContent } from "./message-markdown";

export default function ReportTextBlock({ id, type, level, text, editable, changed, editingChanged }: {
  id: string; type: string; level?: number; text: string; editable: boolean;
  changed: (id: string, text: string) => void; editingChanged: (id: string, active: boolean) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [source, setSource] = useState(text);
  const finish = (apply: boolean) => {
    if (apply) changed(id, source);
    setEditing(false); editingChanged(id, false);
  };
  const label = type === "title" ? "报告标题" : type === "heading" ? "章节标题" : "正文";
  return <div className={`report-text-block ${editing ? "is-editing" : ""}`}>
    {editing ? <div className="report-source-editor">
      <label className="form-field">{label}原文<textarea autoFocus aria-label={`${label}原文`} rows={type === "paragraph" ? 8 : 3} value={source} readOnly={!editable} spellCheck={false} onChange={event => setSource(event.target.value)} /></label>
      <p className="small muted">支持 Markdown。完成编辑后，点击“保存修改”统一保存。</p>
      <div className="actions"><button type="button" className="btn secondary" onClick={() => finish(true)}>完成编辑</button><button type="button" className="text-button" onClick={() => finish(false)}>取消</button></div>
    </div> : <>
      <div data-report-text className={`report-markdown report-${type}-preview`}>
        {type === "title" ? <h1><MarkdownContent content={text} inline /></h1> : type === "heading" ? <div role="heading" aria-level={Math.min(6, Math.max(2, (level || 1) + 1))}><MarkdownContent content={text} inline /></div> : <MarkdownContent content={text} />}
      </div>
      {editable && <div className="report-text-actions"><button type="button" className="text-button" aria-label={`编辑${label}原文`} onClick={() => { setSource(text); setEditing(true); editingChanged(id, true); }}>编辑原文</button></div>}
    </>}
  </div>;
}
