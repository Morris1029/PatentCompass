"""Conversation execution consent and persisted, user-facing activity summaries."""
import hashlib
import json
import re

from sqlalchemy import select

from app.db import session_scope
from app.errors import AppError
from app.models import (Dataset, DatasetVersion, Message, Project, Report, ReportVersion, ResearchRun,
                        ResearchVersion, RunEvent, RunStep, ScopeVersion, new_id)


CONTROL_FIELDS = {"execution_authorization", "operation_id"}
SEARCH_ACTIONS = {"search_patents", "search_materials"}
REPORT_ACTIONS = {"generate_report", "revise_report"}
LABELS = {"search_patents": "检索专利", "search_materials": "检索项目资料", "analyze": "分析专利样本",
          "generate_report": "生成导航报告", "revise_report": "修改导航报告", "revise_chart": "修改图表",
          "scope": "更新研究范围"}
RESULT_FIELDS = {"matched_count", "report_version_id", "report_id", "analysis_id", "chart_version_id", "chart_id",
                 "file_id", "number", "parent_id", "download_url", "application_count", "query_counts",
                 "scope_application_count", "research_id", "research_number", "main_conversation_id", "source_message_id",
                 "source_conversation_id", "change_count", "change_summary", "change_kind", "unchanged"}


def prepare_assignment(session, run, plan=None, *, refresh=False):
    """Freeze one candidate without assigning a conversation or changing research context."""
    supplied_plan = plan is not None
    plan = plan or run.payload.get("plan", {})
    if plan.get("action", run.kind) not in REPORT_ACTIONS:
        return None
    proposal = run.payload.get("research_proposal")
    if run.payload.get("research_decision") or (proposal and not refresh):
        return proposal
    report = session.scalar(select(Report).where(Report.project_id == run.project_id))
    if proposal:
        existing_id = proposal.get("existing_research_id")
        research = session.get(ResearchVersion, existing_id) if existing_id else None
    else:
        from app.research_service import for_conversation
        message = session.get(Message, run.payload["message_id"]) if run.payload.get("message_id") else None
        reference_id = (message.reference or {}).get("report_version_id") if message else None
        reference = session.get(ReportVersion, reference_id) if reference_id else None
        research = session.get(ResearchVersion, reference.research_id) if reference and reference.research_id else None
        bound = research is not None
        target_id = plan.get("target_research_id")
        if not research and target_id and plan.get("research_intent") == "target":
            research = session.get(ResearchVersion, str(target_id))
            if not research or not report or research.report_id != report.id:
                raise AppError(422, "RESEARCH_TARGET_INVALID", "计划中的研究目标不属于此项目，请重新核对")
        if not research:
            research = (session.get(ResearchVersion, run.payload["research_id"])
                        if run.payload.get("research_id") else None)
            if not research and (not supplied_plan or run.retry_of_id):
                for key in ("target_version_id", "plan_report_version_id", "reference_version_id", "base_version_id"):
                    frozen = session.get(ReportVersion, run.payload[key]) if run.payload.get(key) else None
                    if frozen and frozen.research_id:
                        research = session.get(ResearchVersion, frozen.research_id)
                        break
            research = research or for_conversation(session, run.conversation_id)
            bound = research is not None
        if not research and report and report.current_research_id:
            research = session.get(ResearchVersion, report.current_research_id)
        intent = plan.get("research_intent") or ("continue" if bound else "choose")
        reason = plan.get("research_reason", "")
        if intent == "continue" and not bound:
            intent = "choose"
            reason = "当前普通会话尚未绑定研究，请选择归属"
        if bound and intent == "choose" and not plan.get("research_reason"):
            intent = "continue"
        if not research:
            intent = "new"
        proposal = {"intent": intent, "reason": reason,
                    "topic": plan.get("research_topic", ""),
                    "existing_research_id": research.id if research else None,
                    "reference_revision_id": reference.id if reference else None,
                    "expected_adopted_version_id": report.current_version_id if report else None}
    if research and (not report or research.report_id != report.id):
        raise AppError(422, "RESEARCH_TARGET_INVALID", "计划中的研究目标不属于此项目，请重新核对")
    if (research and not research.current_revision_id and plan.get("action", run.kind) == "revise_report"
            and proposal["intent"] != "new"):
        raise AppError(409, "RESEARCH_REPORT_REQUIRED", "本轮尚未生成报告，请先生成本轮报告")
    proposal = {**proposal,
        "base_revision_id": proposal.get("reference_revision_id") or (
            (research.current_revision_id or research.base_revision_id) if research else None),
        "existing_current_revision_id": research.current_revision_id if research else None,
        "existing_revision": research.revision if research else None,
        "existing_scope_version_id": research.scope_version_id if research else None,
        "existing_dataset_version_ids": list(research.dataset_version_ids) if research else []}
    run.payload = {**run.payload, "research_proposal": proposal}
    return proposal


def _assignment_request(session, run, proposal):
    if not proposal:
        return None
    existing_id = proposal.get("existing_research_id")
    research = session.get(ResearchVersion, existing_id) if existing_id else None
    title = session.scalar(select(ReportVersion.content["title"].astext).where(
        ReportVersion.id == research.current_revision_id)) if research and research.current_revision_id else None
    decision = run.payload.get("research_decision")
    recommended = (decision["choice"] if decision else "new" if not research or proposal["intent"] == "new"
                   else "existing" if proposal["intent"] in {"continue", "target"} else None)
    from app.research_service import for_conversation
    bound = for_conversation(session, run.conversation_id)
    return {"intent": proposal["intent"], "reason": proposal.get("reason", ""),
            "topic": proposal.get("topic", ""), "recommended": recommended,
            "existing_research_id": research.id if research else None,
            "existing_number": research.number if research else None,
            "existing_title": title or ("尚未生成报告的研究" if research else None),
            "existing_conversation_id": research.main_conversation_id if research else None,
            "new_uses_current_conversation": bound is None,
            "allow_new": proposal["intent"] != "target" and (not decision or decision["choice"] == "new"),
            "allow_existing": bool(research) and (not decision or decision["choice"] == "existing"),
            "fixed": bool(decision)}


def _pin_assignment_basis(session, run, proposal):
    decision = run.payload.get("research_decision")
    research_id = decision["research_id"] if decision else proposal.get("existing_research_id")
    research = session.get(ResearchVersion, research_id) if research_id else None
    if not research:
        return
    action = run.payload.get("plan", {}).get("action", run.kind)
    if not decision or decision["choice"] == "existing":
        run.payload = {**run.payload, "plan_report_version_id": proposal.get("base_revision_id"),
                       "plan_report_current_id": proposal.get("existing_current_revision_id")}
    if not decision and proposal["intent"] == "new" and action == "generate_report":
        return
    version = session.get(ReportVersion, research.current_revision_id) if action == "revise_report" and research.current_revision_id else None
    basis = version or research
    run.scope_version_id = basis.scope_version_id
    run.dataset_version_ids = list(basis.dataset_version_ids)
    run.payload = {key: value for key, value in run.payload.items() if key != "analysis_id"}
    if basis.analysis_id:
        run.payload = {**run.payload, "analysis_id": basis.analysis_id}


def _report_target_request(session, run, proposal):
    if (not proposal or run.payload.get("research_decision")
            or run.payload.get("plan", {}).get("action", run.kind) != "revise_report"):
        return {}
    research_id = proposal.get("existing_research_id")
    research = session.get(ResearchVersion, research_id) if research_id else None
    head = session.get(ReportVersion, research.current_revision_id) if research and research.current_revision_id else None
    if not head:
        return {}
    plan = run.payload.get("plan", {})
    message = session.get(Message, run.payload["message_id"]) if run.payload.get("message_id") else None
    reference = (message.reference or {}) if message else {}
    targets = (run.payload.get("target_block_ids") or reference.get("block_ids")
               or ([reference["block_id"]] if reference.get("block_id") else []) or plan.get("block_ids", []))
    blocks = [{"id": block["id"], "text": block.get("text", "")[:200], "type": block["type"]}
              for block in head.content["blocks"]]
    valid = bool(targets) and set(targets) <= {block["id"] for block in blocks}
    select_blocks = not plan.get("operations") and (
        not valid or proposal.get("base_revision_id") != head.id)
    return {"report_base_version_id": head.id, "target_expected_current_version_id": head.id,
            "select_blocks": select_blocks, "blocks": blocks,
            "block_ids": targets if valid and not select_blocks else []}


def execution_context(session, run):
    from app.research_service import for_conversation
    project = session.get(Project, run.project_id) if run.project_id else None
    payload = run.payload
    research = session.get(ResearchVersion, payload.get("research_id")) if payload.get("research_id") else None
    proposal = payload.get("research_proposal")
    candidate = session.get(ResearchVersion, proposal["existing_research_id"]) if proposal and proposal.get("existing_research_id") else None
    basis = candidate if proposal and not payload.get("research_decision") else research
    conversation_research = for_conversation(session, run.conversation_id)
    frozen = {"scope_version_id": run.scope_version_id, "dataset_version_ids": run.dataset_version_ids,
              "plan": payload.get("plan"), "kind": run.kind, "message_id": payload.get("message_id"),
              "attachments": payload.get("attachments", []),
              "targets": {key: payload.get(key) for key in (
                  "reference_version_id", "plan_report_version_id", "plan_report_current_id", "target_version_id",
                  "target_current_id", "target_block_ids", "chart_plan_version_id", "chart_data_confirmed")},
              "research_id": research.id if research else None,
              "conversation_bound_research_id": conversation_research.id if conversation_research else None,
              "research_proposal": proposal, "research_decision": payload.get("research_decision"),
              "proposal_context": {"head": candidate.current_revision_id, "revision": candidate.revision}
                                  if candidate and not payload.get("research_decision") else None,
              "project_scope": basis.scope_version_id if basis else project.scope_version_id if project else None,
              "project_data": basis.dataset_version_ids if basis else project.dataset_version_id if project else None}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def execution_request(session, run, actions, *, changed=False):
    report_action = bool({action["kind"] for action in actions} & REPORT_ACTIONS)
    proposal = prepare_assignment(session, run, refresh=changed) if report_action else None
    if proposal:
        _pin_assignment_basis(session, run, proposal)
    assignment = _assignment_request(session, run, proposal)
    report_target = _report_target_request(session, run, proposal)
    scope = session.get(ScopeVersion, run.scope_version_id) if run.scope_version_id else None
    changes = run.payload.get("plan", {}).get("scope_changes", {}) if not run.payload.get("plan_confirmed") else {}
    proposed_scope = {**(scope.content if scope else {}), **changes}
    names = {"industry": "研究领域", "goal": "研究目标", "need": "研究需求", "focus": "关注重点", "q": "关键词",
             "applicant": "申请人", "ipc": "技术分类", "start": "开始年份", "end": "结束年份",
             "application_start": "申请开始日期", "application_end": "申请结束日期", "source_region": "来源地区",
             "coverage": "覆盖范围"}
    details = {names[key]: value for key, value in proposed_scope.items()
               if key in names and value not in (None, "")}
    sources = []
    for version_id in run.dataset_version_ids:
        version = session.get(DatasetVersion, version_id)
        dataset = session.get(Dataset, version.dataset_id) if version else None
        if dataset:
            sources.append(dataset.name)
    if sources:
        details["数据来源"] = sources
    version_id = report_target.get("report_base_version_id") or run.payload.get("target_version_id") or (
        run.payload.get("plan_report_version_id") if report_action else None) or run.payload.get("reference_version_id")
    version = session.get(ReportVersion, version_id) if version_id else None
    if version:
        research = session.get(ResearchVersion, version.research_id) if version.research_id else None
        details["报告目标"] = f"{version.content['title']}（V{research.number}）" if research and research.number else version.content['title']
        targets = run.payload.get("target_block_ids") or run.payload.get("plan", {}).get("block_ids", [])
        if targets:
            details["修改段落"] = [block.get("text", block["type"])[:160]
                                   for block in version.content["blocks"] if block["id"] in targets]
    if assignment and not assignment["fixed"] and assignment["recommended"] == "new":
        topic = assignment.get("topic")
        details["报告目标"] = f"新研究：{topic}（尚未编号）" if topic else "新研究（尚未编号）"
    readable = [f"{label}：{'、'.join(str(item) for item in value) if isinstance(value, list) else value}"
                for label, value in details.items()]
    allowed_actions = {action["kind"] for action in actions} | SEARCH_ACTIONS
    if assignment and assignment["allow_new"]:
        allowed_actions |= {"analyze", "generate_report"}
    if "generate_report" in allowed_actions:
        allowed_actions.add("analyze")
        if scope_editable_for_run(run):
            readable.append("调整研究范围后，将先按确认值重新分析样本，再生成报告")
    if changed:
        readable.append("执行说明：本轮继续采用以上已选范围、数据和目标")
    actions = [{**action, "details": readable} for action in actions]
    target = {}
    target_id = (run.payload.get("research_decision", {}).get("research_id") or
                 (proposal.get("existing_research_id") if proposal else run.payload.get("research_id")))
    if target_id and report_action:
        research = session.get(ResearchVersion, target_id)
        target = {"target_research_id": research.id, "target_conversation_id": research.main_conversation_id,
                  "research_number": research.number}
    return {"id": new_id(), "kind": "execution_confirmation", "context": execution_context(session, run), **target,
            "prompt": "本轮研究范围、数据或目标已变化，请核对后确认" if changed else "请确认本轮拟执行的操作",
            "actions": actions, "allowed_actions": sorted(allowed_actions),
            "scope": proposed_scope, "expected_scope_version_id": run.scope_version_id,
            "scope_editable": scope_editable_for_run(run),
            **report_target,
            **({"research_assignment": assignment} if assignment else {})}


def scope_editable_for_run(run):
    action = run.payload.get("plan", {}).get("action", run.kind)
    return action in {"chat", "analyze", "generate_report"} and (not run.payload.get("reference_version_id") or bool(run.payload.get("research_id")) or bool(run.payload.get("research_proposal")))


def require_execution(ctx, kinds):
    if not ctx.conversation_id or not ctx.payload.get("message_id"):
        return
    from app.jobs import WaitingForInput, emit_event
    with session_scope() as session:
        from app.models import ResearchVersion
        session.scalar(select(Project).where(Project.id == ctx.project_id).with_for_update())
        if ctx.payload.get("research_id"):
            session.scalar(select(ResearchVersion).where(ResearchVersion.id == ctx.payload["research_id"]).with_for_update())
        run = ctx.guard(session)
        if set(kinds) & REPORT_ACTIONS:
            prepare_assignment(session, run)
        authorization = run.payload.get("execution_authorization", {})
        assignment_pending = bool(run.payload.get("research_proposal")) and not run.payload.get("research_decision")
        if (not assignment_pending and authorization.get("context") == execution_context(session, run)
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
    if result.get("_report_candidate"):
        return result
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
            from app.models import Report
            from app.research_service import version_result
            unchanged = activity.get("unchanged")
            activity.update(version_result(session, session.get(Report, version.report_id), version))
            parent = session.get(ReportVersion, version.parent_id) if version.parent_id else None
            summary = report_change_summary(version.content, parent.content) if parent else {}
            activity.update(change_summary={} if unchanged else summary, change_count=0 if unchanged else sum(summary.values()), is_initial=parent is None)
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
