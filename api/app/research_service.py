"""Research rounds and atomic publication of immutable report revisions."""

from copy import deepcopy

from sqlalchemy import func, select

from app.errors import AppError
from app.models import Conversation, Message, Project, Report, ReportVersion, ResearchRun, ResearchVersion


_UNSET = object()


def for_conversation(session, conversation_id):
    if not conversation_id:
        return None
    return session.scalar(select(ResearchVersion).where(
        ResearchVersion.main_conversation_id == conversation_id))


def context_for(session, research):
    return {"scope_version_id": research.scope_version_id,
            "dataset_version_ids": list(research.dataset_version_ids or []),
            "analysis_id": research.analysis_id, "current_revision_id": research.current_revision_id,
            "revision": research.revision}


def set_context(session, research, *, scope_id=_UNSET, dataset_ids=_UNSET, analysis_id=_UNSET):
    changes = {"scope_version_id": scope_id, "dataset_version_ids": dataset_ids,
               "analysis_id": analysis_id}
    changed = False
    for key, value in changes.items():
        if value is _UNSET:
            continue
        if key == "dataset_version_ids":
            value = list(value or [])
        if getattr(research, key) != value:
            setattr(research, key, value)
            changed = True
    if changed:
        research.revision = (research.revision or 0) + 1
    return research


def _source_message(session, version):
    run = session.get(ResearchRun, version.run_id)
    payload = (run.payload or {}) if run else {}
    message_id = payload.get("source_message_id") or payload.get("message_id")
    message = session.get(Message, message_id) if message_id else None
    return message.id if message else None


def _changes(content, parent_content):
    from app.conversation_execution import report_change_summary
    if parent_content is None:
        return {}, 0
    summary = report_change_summary(content, parent_content)
    return summary, sum(summary.values())


def ensure_for_report(session, report):
    """Lazily attach old fixtures; deployed legacy rows are handled by migration."""
    if report.current_research_id:
        research = session.get(ResearchVersion, report.current_research_id)
        if research:
            return research
    versions = list(session.scalars(select(ReportVersion).where(
        ReportVersion.report_id == report.id).order_by(ReportVersion.number)))
    head = next((row for row in versions if row.id == report.current_version_id), None)
    if head and head.research_id:
        research = session.get(ResearchVersion, head.research_id)
        report.current_research_id = research.id
        return research
    project = session.get(Project, report.project_id)
    research = ResearchVersion(report_id=report.id, status="published" if versions else "draft",
        number=1 if versions else None, revision=0,
        initial_revision_id=versions[0].id if versions else None,
        current_revision_id=head.id if head else None,
        scope_version_id=head.scope_version_id if head else project.scope_version_id,
        dataset_version_ids=list(head.dataset_version_ids) if head else (
            [project.dataset_version_id] if project.dataset_version_id else []),
        analysis_id=head.analysis_id if head else project.current_analysis_id)
    by_id = {row.id: row for row in versions}
    cursor = head
    seen = set()
    while cursor and cursor.id not in seen:
        seen.add(cursor.id)
        if cursor.conversation_id:
            research.main_conversation_id = cursor.conversation_id
            break
        cursor = by_id.get(cursor.parent_id)
    session.add(research)
    session.flush()
    for version in versions:
        if version.research_id:
            continue
        version.research_id = research.id
        version.change_kind = "generate" if version.parent_id is None else "edit"
        version.operation_id = version.operation_id or version.run_id
        version.source_message_id = version.source_message_id or _source_message(session, version)
        parent = by_id.get(version.parent_id)
        version.change_summary, version.change_count = _changes(
            version.content, parent.content if parent else None)
    report.current_research_id = research.id
    report.research_counter = max(report.research_counter or 0, 1 if versions else 0)
    return research


def ensure_conversation(session, research):
    research = session.scalar(select(ResearchVersion).where(
        ResearchVersion.id == research.id).with_for_update().execution_options(populate_existing=True))
    if research.main_conversation_id:
        return session.get(Conversation, research.main_conversation_id)
    report = session.get(Report, research.report_id)
    conversation = Conversation(project_id=report.project_id,
        title=f"研究 V{research.number}" if research.number else "新一轮研究")
    session.add(conversation)
    session.flush()
    research.main_conversation_id = conversation.id
    return conversation


def create_draft(session, report, base_revision_id, *, conversation_id=None):
    base = session.get(ReportVersion, base_revision_id) if base_revision_id else None
    if (base_revision_id and (not base or base.report_id != report.id)) or (not base and report.current_version_id):
        raise AppError(404, "REPORT_VERSION_NOT_FOUND", "报告记录不存在")
    if base:
        ensure_for_report(session, report)
    project = session.get(Project, report.project_id)
    if conversation_id:
        conversation = session.get(Conversation, conversation_id)
        if not conversation or conversation.project_id != project.id:
            raise AppError(404, "CONVERSATION_NOT_FOUND", "对话不属于此项目")
        if for_conversation(session, conversation_id):
            raise AppError(409, "RESEARCH_CONVERSATION_BOUND", "此对话已有所属研究，请使用新的研究对话")
    research = ResearchVersion(report_id=report.id, status="draft", number=None, revision=0,
        main_conversation_id=conversation_id, base_revision_id=base.id if base else None,
        scope_version_id=base.scope_version_id if base else project.scope_version_id,
        dataset_version_ids=list(base.dataset_version_ids) if base else ([project.dataset_version_id] if project.dataset_version_id else []),
        analysis_id=base.analysis_id if base else project.current_analysis_id)
    session.add(research)
    session.flush()
    ensure_conversation(session, research)
    return research


def confirm_assignment(session, run, choice):
    proposal = run.payload.get("research_proposal")
    if not proposal:
        return None, None
    decision = run.payload.get("research_decision")
    if decision:
        if choice and choice != decision["choice"]:
            raise AppError(409, "RESEARCH_ASSIGNMENT_FIXED", "本任务的研究归属已确认，不能在重试时更换")
        return session.get(ResearchVersion, decision["research_id"]), session.get(Conversation, decision["conversation_id"])
    offered = run.input_request.get("research_assignment", {})
    choice = choice or offered.get("recommended")
    if choice not in {"new", "existing"} or not offered.get(f"allow_{choice}"):
        raise AppError(422, "RESEARCH_CHOICE_REQUIRED", "请选择更新原研究或开启新一轮研究")
    project = session.get(Project, run.project_id)
    existing = session.get(ResearchVersion, proposal.get("existing_research_id")) if proposal.get("existing_research_id") else None
    if existing and session.get(Report, existing.report_id).project_id != project.id:
        raise AppError(404, "RESEARCH_NOT_FOUND", "研究版本不属于此项目")
    if choice == "existing":
        if not existing:
            raise AppError(404, "RESEARCH_NOT_FOUND", "目标研究不存在")
        research = existing
        conversation = ensure_conversation(session, research)
    else:
        report = session.get(Report, existing.report_id) if existing else session.scalar(select(Report).where(Report.project_id == project.id))
        if not report:
            report = Report(project_id=project.id)
            session.add(report)
            session.flush()
        reuse_conversation = run.conversation_id if not for_conversation(session, run.conversation_id) else None
        research = create_draft(session, report, proposal.get("base_revision_id"), conversation_id=reuse_conversation)
        set_context(session, research, scope_id=run.scope_version_id,
                    dataset_ids=run.dataset_version_ids, analysis_id=run.payload.get("analysis_id"))
        conversation = session.get(Conversation, research.main_conversation_id)
    payload = {**run.payload, "research_id": research.id, "plan_confirmed": True,
               "research_decision": {"choice": choice, "research_id": research.id, "conversation_id": conversation.id}}
    if choice == "new":
        payload = {key: value for key, value in payload.items() if key not in {
            "reference_version_id", "plan_report_version_id", "plan_report_current_id", "target_version_id",
            "target_current_id", "target_block_ids", "base_version_id", "expected_current_version_id", "chart_plan_version_id"}}
        if payload.get("plan", {}).get("action") == "revise_report":
            payload["plan"] = {**payload["plan"], "action": "generate_report", "operations": [], "block_ids": []}
    else:
        payload.update(plan_report_version_id=proposal.get("base_revision_id"),
                       plan_report_current_id=proposal.get("existing_current_revision_id"))
    run.payload = payload
    return research, conversation


def adopt(session, research, expected_current_version_id):
    report = session.get(Report, research.report_id)
    if report.current_version_id != expected_current_version_id:
        raise AppError(409, "REPORT_VERSION_CONFLICT", "当前采用的报告已变化，请重新核对")
    if research.status != "published" or not research.current_revision_id:
        raise AppError(409, "RESEARCH_REPORT_REQUIRED", "本轮研究尚未生成报告")
    report.current_research_id = research.id
    report.current_version_id = research.current_revision_id
    project = session.get(Project, report.project_id)
    values = (research.scope_version_id,
              research.dataset_version_ids[0] if research.dataset_version_ids else None,
              research.analysis_id)
    if (project.scope_version_id, project.dataset_version_id, project.current_analysis_id) != values:
        project.scope_version_id, project.dataset_version_id, project.current_analysis_id = values
        project.revision += 1
    return research


def version_result(session, report, version):
    research = session.get(ResearchVersion, version.research_id) if version.research_id else None
    source_message = session.get(Message, version.source_message_id) if version.source_message_id else None
    return {"report_id": report.id, "report_version_id": version.id, "number": version.number,
            "current_version_id": report.current_version_id, "parent_id": version.parent_id,
            "research_id": version.research_id, "research_number": research.number if research else None,
            "research_status": research.status if research else None,
            "main_conversation_id": research.main_conversation_id if research else None,
            "source_message_id": version.source_message_id,
            "source_conversation_id": source_message.conversation_id if source_message else version.conversation_id,
            "conversation_id": version.conversation_id,
            "change_count": version.change_count, "change_summary": version.change_summary,
            "change_kind": version.change_kind, "restored_from_id": version.restored_from_id}


def operation_id(session, ctx):
    if ctx.payload.get("operation_id"):
        return str(ctx.payload["operation_id"])
    identifier = ctx.run_id
    seen = set()
    while identifier not in seen:
        seen.add(identifier)
        run = session.get(ResearchRun, identifier)
        if not run or not run.retry_of_id:
            break
        identifier = run.retry_of_id
    return identifier


def research_for_context(session, ctx, report):
    research_id = ctx.payload.get("research_id")
    research = session.get(ResearchVersion, research_id) if research_id else for_conversation(
        session, getattr(ctx, "conversation_id", None))
    research = research or ensure_for_report(session, report)
    if research.report_id != report.id:
        raise AppError(404, "RESEARCH_NOT_FOUND", "研究版本不存在")
    return research


def publish_candidate(session, ctx, result):
    candidate = result.get("_report_candidate") if isinstance(result, dict) else None
    if not candidate:
        return result
    project = session.scalar(select(Project).where(Project.id == ctx.project_id).with_for_update())
    report = session.scalar(select(Report).where(Report.id == candidate["report_id"]).with_for_update())
    if not project or not report or report.project_id != project.id:
        raise AppError(404, "REPORT_NOT_FOUND", "报告不存在")
    research = session.scalar(select(ResearchVersion).where(
        ResearchVersion.id == candidate["research_id"]).with_for_update())
    if not research or research.report_id != report.id:
        raise AppError(404, "RESEARCH_NOT_FOUND", "研究版本不存在")
    ctx.guard(session)
    identifier = operation_id(session, ctx)
    previous = session.scalar(select(ReportVersion).where(ReportVersion.operation_id == identifier))
    if previous:
        if previous.report_id != report.id:
            raise AppError(409, "REPORT_OPERATION_CONFLICT", "该操作已用于其他报告")
        return version_result(session, report, previous)
    expected = candidate["expected_current_version_id"]
    if research.current_revision_id != expected:
        raise AppError(409, "REPORT_VERSION_CONFLICT", "修改期间本研究的报告发生变化，请重新核对")
    if (candidate["change_kind"] != "edit"
            and research.revision != candidate.get("expected_research_revision")):
        raise AppError(409, "RESEARCH_CONTEXT_CONFLICT", "生成或恢复期间研究范围、数据或分析已变化，请重新核对")
    if candidate.get("legacy_adopted_check") and report.current_version_id != expected:
        raise AppError(409, "REPORT_VERSION_CONFLICT", "修改期间当前采用的报告发生变化")
    will_adopt = research.status == "draft" or candidate["change_kind"] == "restore"
    if will_adopt and report.current_version_id != candidate.get("expected_adopted_version_id"):
        raise AppError(409, "REPORT_VERSION_CONFLICT", "当前采用的研究报告已变化，请重新核对")
    parent = session.get(ReportVersion, expected) if expected else None
    if (parent and parent.content == candidate["content"] and not (
            candidate["change_kind"] == "restore" and report.current_research_id != research.id)):
        return {**version_result(session, report, parent), "unchanged": True,
                "change_summary": {}, "change_count": 0, "change_kind": candidate["change_kind"]}
    report.version_counter = (report.version_counter or 0) + 1
    change_summary, change_count = _changes(candidate["content"], parent.content if parent else None)
    version = ReportVersion(report_id=report.id, research_id=research.id, number=report.version_counter,
        parent_id=expected, run_id=ctx.run_id, operation_id=identifier,
        analysis_id=candidate["analysis_id"], scope_version_id=candidate["scope_version_id"],
        dataset_version_ids=list(candidate["dataset_version_ids"]), content=deepcopy(candidate["content"]),
        chart_ids=list(dict.fromkeys(block["chart_id"] for block in candidate["content"]["blocks"]
                                    if block["type"] == "chart")),
        instruction=candidate["instruction"], summary=candidate["summary"],
        conversation_id=candidate.get("conversation_id"), source_message_id=candidate.get("source_message_id"),
        change_kind=candidate["change_kind"], restored_from_id=candidate.get("restored_from_id"),
        change_summary=change_summary, change_count=change_count)
    session.add(version)
    session.flush()
    if research.status == "draft":
        highest = session.scalar(select(func.max(ResearchVersion.number)).where(
            ResearchVersion.report_id == report.id)) or 0
        report.research_counter = max(report.research_counter or 0, highest) + 1
        research.number = report.research_counter
        research.status = "published"
    research.initial_revision_id = research.initial_revision_id or version.id
    research.current_revision_id = version.id
    if candidate["change_kind"] != "edit":
        set_context(session, research, scope_id=version.scope_version_id,
                    dataset_ids=version.dataset_version_ids, analysis_id=version.analysis_id)
    if not research.main_conversation_id and candidate.get("conversation_id"):
        research.main_conversation_id = candidate["conversation_id"]
    if candidate["change_kind"] == "edit" and report.current_research_id == research.id:
        report.current_version_id = version.id
    elif will_adopt or report.current_research_id == research.id:
        adopt(session, research, report.current_version_id)
    ctx.guard(session)
    return version_result(session, report, version)
