"""Conversation execution consent and persisted, user-facing activity summaries."""
import hashlib
import json
import re

from sqlalchemy import select

from app.db import session_scope
from app.models import Dataset, DatasetVersion, Project, ReportVersion, ResearchRun, RunEvent, RunStep, ScopeVersion, new_id


CONTROL_FIELDS = {"execution_authorization"}
SEARCH_ACTIONS = {"search_patents", "search_materials"}
LABELS = {"search_patents": "检索专利", "search_materials": "检索项目资料", "analyze": "分析专利样本",
          "generate_report": "生成导航报告", "revise_report": "修改导航报告", "revise_chart": "修改图表",
          "scope": "更新研究范围"}
RESULT_FIELDS = {"matched_count", "report_version_id", "report_id", "analysis_id", "chart_version_id", "chart_id",
                 "file_id", "number", "parent_id", "download_url", "application_count", "query_counts",
                 "scope_application_count"}


def execution_context(session, run):
    project = session.get(Project, run.project_id) if run.project_id else None
    payload = run.payload
    frozen = {"scope_version_id": run.scope_version_id, "dataset_version_ids": run.dataset_version_ids,
              "plan": payload.get("plan"), "kind": run.kind, "message_id": payload.get("message_id"),
              "attachments": payload.get("attachments", []),
              "targets": {key: payload.get(key) for key in (
                  "reference_version_id", "plan_report_version_id", "plan_report_current_id", "target_version_id",
                  "target_current_id", "target_block_ids", "chart_plan_version_id", "chart_data_confirmed")},
              "project_scope": project.scope_version_id if project else None,
              "project_data": project.dataset_version_id if project else None}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def execution_request(session, run, actions, *, changed=False):
    scope = session.get(ScopeVersion, run.scope_version_id) if run.scope_version_id else None
    names = {"industry": "研究领域", "goal": "研究目标", "need": "研究需求", "focus": "关注重点", "q": "关键词",
             "applicant": "申请人", "ipc": "技术分类", "start": "开始年份", "end": "结束年份",
             "application_start": "申请开始日期", "application_end": "申请结束日期", "source_region": "来源地区",
             "coverage": "覆盖范围"}
    details = {names[key]: value for key, value in (scope.content if scope else {}).items()
               if key in names and value not in (None, "")}
    sources = []
    for version_id in run.dataset_version_ids:
        version = session.get(DatasetVersion, version_id)
        dataset = session.get(Dataset, version.dataset_id) if version else None
        if dataset:
            sources.append(dataset.name)
    if sources:
        details["数据来源"] = sources
    version_id = run.payload.get("target_version_id") or run.payload.get("reference_version_id") or run.payload.get("plan_report_version_id")
    version = session.get(ReportVersion, version_id) if version_id else None
    if version:
        details["报告目标"] = f"{version.content['title']}（第 {version.number} 版）"
        targets = run.payload.get("target_block_ids") or run.payload.get("plan", {}).get("block_ids", [])
        if targets:
            details["修改段落"] = [block.get("text", block["type"])[:160]
                                   for block in version.content["blocks"] if block["id"] in targets]
    readable = [f"{label}：{'、'.join(str(item) for item in value) if isinstance(value, list) else value}"
                for label, value in details.items()]
    allowed_actions = {action["kind"] for action in actions} | SEARCH_ACTIONS
    if "generate_report" in allowed_actions:
        allowed_actions.add("analyze")
        if scope_editable_for_run(run):
            readable.append("调整研究范围后，将先按确认值重新分析样本，再生成报告")
    if changed:
        readable.append("执行说明：本轮继续采用以上已选范围、数据和目标")
    actions = [{**action, "details": readable} for action in actions]
    return {"id": new_id(), "kind": "execution_confirmation", "context": execution_context(session, run),
            "prompt": "本轮研究范围、数据或目标已变化，请核对后确认" if changed else "请确认本轮拟执行的操作",
            "actions": actions, "allowed_actions": sorted(allowed_actions),
            "scope": dict(scope.content) if scope else {}, "scope_editable": scope_editable_for_run(run)}


def scope_editable_for_run(run):
    action = run.payload.get("plan", {}).get("action", run.kind)
    return action in {"chat", "analyze", "generate_report"} and not run.payload.get("reference_version_id")


def require_execution(ctx, kinds):
    if not ctx.conversation_id or not ctx.payload.get("message_id"):
        return
    from app.jobs import WaitingForInput, emit_event
    with session_scope() as session:
        run = ctx.guard(session)
        authorization = run.payload.get("execution_authorization", {})
        if (authorization.get("context") == execution_context(session, run)
                and set(kinds) <= set(authorization.get("allowed_actions", []))):
            return
        actions = [{"kind": kind, "label": LABELS[kind]} for kind in dict.fromkeys(kinds)]
        request = execution_request(session, run, actions, changed=bool(authorization))
        run.status, run.input_request = "waiting_input", request
        run.lease_token, run.lease_expires_at = None, None
        emit_event(session, run.id, "input.required", request)
    raise WaitingForInput()


def retrieval_details(kind, filters):
    labels = {"q": "关键词", "applicant": "申请人", "ipc": "技术分类", "application_number": "申请号",
              "publication_number": "公开号"}
    details = [f"{label}：{filters[key]}" for key, label in labels.items() if filters.get(key)]
    if filters.get("queries"):
        details.append("检索词：" + "、".join(filters["queries"]))
    return {"source": "local_patents" if kind == "search_patents" else "project_materials", "details": details,
            **({"query": filters["q"]} if filters.get("q") else {}),
            **({"queries": filters["queries"]} if filters.get("queries") else {})}


def run_activity(ctx, activity_id, kind, callback, *, details=None):
    from app.jobs import Cancelled, LeaseLost, WaitingForInput
    activity = {"id": activity_id, "kind": kind, **(details or {})}
    ctx.emit("activity.updated", {**activity, "status": "running"})
    try:
        result = callback()
    except (Cancelled, LeaseLost, WaitingForInput):
        raise
    except Exception:
        ctx.emit("activity.updated", {**activity, "status": "failed"})
        raise
    ctx.emit("activity.updated", {**activity, "status": "succeeded",
                                  **{key: value for key, value in result.items() if key in RESULT_FIELDS}})
    return result


def report_change_summary(content, parent_content):
    before = {block["id"]: block for block in parent_content.get("blocks", [])}
    after = {block["id"]: block for block in content.get("blocks", [])}
    common = before.keys() & after.keys()
    return {"title": int(content.get("title") != parent_content.get("title")),
            "added": len(after.keys() - before.keys()), "removed": len(before.keys() - after.keys()),
            "modified": sum(before[key] != after[key] for key in common),
            "order": int([key for key in before if key in common] != [key for key in after if key in common]),
            "layout": int(content.get("layout") != parent_content.get("layout"))}


def _artifact_details(session, activity):
    if activity.get("report_version_id"):
        version = session.get(ReportVersion, activity["report_version_id"])
        if version:
            activity.update(report_id=version.report_id, number=version.number, parent_id=version.parent_id,
                            analysis_id=version.analysis_id)
            parent = session.get(ReportVersion, version.parent_id) if version.parent_id else None
            summary = report_change_summary(version.content, parent.content) if parent else {}
            activity.update(change_summary=summary, change_count=sum(summary.values()), is_initial=parent is None)
    return activity


def activities_for_run(session, run):
    # Retry attempts remain one conversation turn, including work already completed by an earlier attempt.
    chain, seen, current = [], set(), run
    while current and current.id not in seen:
        chain.append(current)
        seen.add(current.id)
        current = session.get(ResearchRun, current.retry_of_id) if current.retry_of_id else None
    activities = {}
    for attempt in reversed(chain):
        steps = session.scalars(select(RunStep).where(RunStep.run_id == attempt.id).order_by(RunStep.created_at)).all()
        by_key = {step.key: step for step in steps}
        events = session.scalars(select(RunEvent).where(RunEvent.run_id == attempt.id,
                                    RunEvent.event_type.in_(["activity.updated", "analysis.progress"]))
                                 .order_by(RunEvent.seq)).all()
        actual = {event.payload["id"] for event in events if event.event_type == "activity.updated"}
        # Older runs predate activity events. Infer only from actual saved tool calls and computation steps.
        for step in steps:
            match = re.fullmatch(r"chat_search_(\d+)_(\d+)", step.key)
            if not match or step.key in actual:
                continue
            model = by_key.get(f"chat_model_{match[1]}")
            calls = model.result.get("tool_calls", []) if model else []
            index = int(match[2])
            kind = calls[index].get("function", {}).get("name") if index < len(calls) else None
            if kind in SEARCH_ACTIONS:
                activities[step.key] = {"id": step.key, "kind": kind, "status": step.status,
                                        **retrieval_details(kind, json.loads(calls[index]["function"]["arguments"])),
                                        **({"matched_count": step.result["matched_count"]}
                                           if "matched_count" in step.result else {})}
        analysis_steps = [step for step in steps if step.key.startswith(("calculate_statistics_", "semantic_extract_",
                            "semantic_taxonomy_", "persist_analysis", "persist_semantic_analysis"))]
        if analysis_steps and "analyze" not in actual:
            persisted = next((step for step in reversed(analysis_steps) if step.key.startswith("persist_")
                              and step.status == "succeeded"), None)
            activities["analyze"] = {"id": "analyze", "kind": "analyze",
                                      "status": "succeeded" if persisted else attempt.status,
                                      **{key: value for key, value in (persisted.result if persisted else {}).items()
                                         if key in RESULT_FIELDS}}
        action = attempt.payload.get("plan", {}).get("action", attempt.kind)
        report_steps = any(step.key.startswith(("report_snapshot", "report_synthesis", "revision_snapshot", "rewrite_"))
                           for step in steps)
        if action in {"generate_report", "revise_report"} and action not in actual and (
                report_steps or attempt.result.get("report_version_id")):
            activities[action] = {"id": action, "kind": action, "status": attempt.status,
                                  **{key: value for key, value in attempt.result.items() if key in RESULT_FIELDS}}
        if action == "revise_chart" and "revise_chart" not in actual and (
                any(step.key.startswith("chart_") for step in steps) or attempt.result.get("chart_version_id")):
            activities[action] = {"id": action, "kind": action, "status": attempt.status,
                                  **{key: value for key, value in attempt.result.items() if key in RESULT_FIELDS}}
        for event in events:
            if event.event_type == "activity.updated":
                key = event.payload["id"]
                activities[key] = {**activities.get(key, {}), **event.payload}
            elif "analyze" in activities:
                progress = {key: value for key, value in event.payload.items() if key in {
                    "processed_count", "total_count", "cached_count", "classified_count", "review_count",
                    "completed_dimensions", "total_dimensions"}}
                phase = {"extract": "识别专利技术", "taxonomy": "整理技术主题"}.get(event.payload.get("stage"))
                activities["analyze"].update(progress, **({"phase": phase} if phase else {}))
    for activity in activities.values():
        if activity["status"] == "running" and run.status in {"failed", "cancelled"}:
            activity["status"] = run.status
        _artifact_details(session, activity)
    return list(activities.values())
