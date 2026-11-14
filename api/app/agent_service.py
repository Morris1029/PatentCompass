"""Portable-agent operations over the existing project and durable task services."""

from dataclasses import dataclass
import math

from pydantic import ValidationError
from sqlalchemy import select

from app import agent_contracts as inputs, research_service
from app.config import get_settings
from app.errors import AppError
from app.jobs import create_run, emit_event
from app.models import (AnalysisResult, Chart, Dataset, DatasetVersion, Project, Report,
                        ReportVersion, ResearchRun, ResearchVersion, RunStep, ScopeVersion)
from app.resource_service import idempotent, owned, record
from patentcompass_core import semantics
from patentcompass_core.retrieval import text_hash


@dataclass(frozen=True)
class Tool:
    input_model: type[inputs.Input]
    description: str
    read_only: bool = False


TOOLS = {
    "list_projects": Tool(inputs.Page, "分页列出当前身份的项目", True),
    "list_datasets": Tool(inputs.Page, "分页列出授权数据集及数据版本、质量和覆盖说明", True),
    "get_agent_context": Tool(inputs.ProjectRef, "读取项目或明确研究的范围、数据、分析、报告和研究目录", True),
    "create_agent_project": Tool(inputs.ProjectCreate, "创建项目及未确认范围"),
    "update_agent_scope": Tool(inputs.ScopeUpdate, "以预期范围版本更新或确认范围，保留旧版本"),
    "bind_agent_dataset": Tool(inputs.DatasetBind, "以预期上下文版本绑定就绪数据，旧成果保留"),
    "create_agent_research": Tool(inputs.ResearchCreate, "明确开启独立研究，保留当前采用报告"),
    "start_semantic_analysis": Tool(inputs.SemanticStart, "冻结范围和记录，创建由外部 Agent 推理的持久分析任务"),
    "get_semantic_batch": Tool(inputs.BatchRead, "读取一个冻结原文批次及已提交进度，不调用模型", True),
    "submit_semantic_batch": Tool(inputs.BatchSubmit, "提交本批全部短ID的语义标签，校验引文并保存检查点"),
    "get_semantic_taxonomy": Tool(inputs.TaxonomyRead, "全部批次完成后读取一个维度的归并短ID", True),
    "submit_semantic_taxonomy": Tool(inputs.TaxonomySubmit, "提交单维度主题，要求所有来源短ID各使用一次"),
    "finish_semantic_analysis": Tool(inputs.RunRef, "全部批次和维度完成后排队校验、统计、生成图表和保存分析"),
    "start_agent_task": Tool(inputs.ProjectTask, "执行无后端生成模型的统计、报告、修改、恢复、图表或导出任务"),
    "get_agent_chart": Tool(inputs.ChartRead, "读取明确图表版本的统计、原文范围和解读", True),
    "adopt_agent_research": Tool(inputs.ResearchAdopt, "以预期采用报告版本切换当前采用研究"),
}


def invoke(session, name, payload, key=None):
    spec = TOOLS[name]
    try:
        request = spec.input_model.model_validate(payload)
    except ValidationError as error:
        raise AppError(422, "VALIDATION_ERROR", "Agent 工具参数无效", error.errors(include_context=False)) from error
    if spec.read_only:
        return _execute(session, name, request)
    return idempotent(session, "agent:" + name, key, request.model_dump(mode="json"),
                      lambda: _execute(session, name, request))[0]


def _context(session, project_id, research_id=None, *, lock=False):
    project = owned(session, Project, project_id, lock=lock)
    research = owned(session, ResearchVersion, research_id, lock=lock) if research_id else None
    if research and session.get(Report, research.report_id).project_id != project.id:
        raise AppError(404, "RESEARCH_NOT_FOUND", "研究不属于当前项目")
    context = research_service.context_for(session, research) if research else {
        "scope_version_id": project.scope_version_id,
        "dataset_version_ids": [project.dataset_version_id] if project.dataset_version_id else [],
        "analysis_id": project.current_analysis_id, "revision": project.revision}
    return project, research, context


def _expected(context, expected):
    if context["revision"] != expected:
        raise AppError(409, "AGENT_CONTEXT_CHANGED", "范围、数据或分析已变化，请读取上下文后重新提交")


def context_assets(session, project_id, context, report_version_id=None):
    """Compact targets shared by conversation planning and external agents."""
    version = session.get(ReportVersion, report_version_id) if report_version_id else None
    if version and session.get(Report, version.report_id).project_id != project_id:
        raise AppError(404, "REPORT_NOT_FOUND", "报告不属于当前项目")
    frozen = {block["chart_id"]: block["chart_version_id"] for block in version.content["blocks"]
              if block.get("chart_id") and block.get("chart_version_id")} if version else {}
    charts = session.scalars(select(Chart).where(Chart.project_id == project_id,
        Chart.id.in_(frozen) if version else Chart.analysis_id == context.get("analysis_id")))
    return {"report": {"id": version.id, "title": version.content["title"],
        "blocks": [{key: (block[key][:250] if key == "text" else block[key])
                    for key in ("id", "type", "text", "chart_id", "chart_version_id") if key in block}
                   for block in version.content["blocks"]]} if version else None,
        "charts": [{"id": chart.id, "kind": chart.kind, "version_id": frozen.get(chart.id, chart.version_id)} for chart in charts]}


def _ready(session, context):
    scope = session.get(ScopeVersion, context["scope_version_id"])
    if not scope or not scope.confirmed:
        raise AppError(409, "SCOPE_CONFIRMATION_REQUIRED", "请先明确确认研究范围")
    if not context["dataset_version_ids"]:
        raise AppError(409, "DATASET_REQUIRED", "请先选择研究数据")
    for identifier in context["dataset_version_ids"]:
        if owned(session, DatasetVersion, identifier).status != "ready":
            raise AppError(409, "DATASET_NOT_READY", "数据版本尚未就绪")
    return scope


def _run(session, identifier, *, writing=False):
    run = owned(session, ResearchRun, identifier, lock=writing)
    if run.kind != "agent_analysis":
        raise AppError(422, "AGENT_RUN_REQUIRED", "请使用外部语义分析任务")
    if writing and (run.status != "waiting_input" or run.cancellation_requested):
        raise AppError(409, "AGENT_RUN_CLOSED", "此任务已关闭或进入执行阶段")
    return run


def _records(session, run):
    from app.models import PatentApplication
    identifiers = [item["id"] for item in run.payload["sources"]]
    rows = {row.id: row for row in session.scalars(select(PatentApplication).where(PatentApplication.id.in_(identifiers)))}
    result = []
    for item in run.payload["sources"]:
        row = rows.get(item["id"])
        if not row or row.dataset_version_id not in run.dataset_version_ids or text_hash(row) != item["sha256"]:
            raise AppError(409, "AGENT_SOURCE_CHANGED", "冻结原文已变化，请重新开始分析")
        result.append(row)
    return result


def _steps(session, run, prefix):
    return {row.key: row.result for row in session.scalars(select(RunStep).where(
        RunStep.run_id == run.id, RunStep.key.startswith(prefix), RunStep.status == "succeeded"))}


def _save_step(session, run, key, result):
    previous = session.scalar(select(RunStep).where(RunStep.run_id == run.id, RunStep.key == key))
    if previous:
        if previous.result != result:
            raise AppError(409, "AGENT_STEP_CONFLICT", "已保存步骤不能覆盖；请保留原任务并重新开始分析")
        return
    from app.models import utcnow
    session.add(RunStep(run_id=run.id, key=key, status="succeeded", fingerprint=semantics._digest(result),
                        result=result, finished_at=utcnow()))
    session.flush()
    emit_event(session, run.id, "step.updated", {"step": key, "status": "succeeded"})


def _extractions(session, run):
    batches = _steps(session, run, "agent_batch_")
    count = math.ceil(len(run.payload["sources"]) / semantics.BATCH_SIZE)
    if set(batches) != {f"agent_batch_{index}" for index in range(count)}:
        raise AppError(409, "SEMANTIC_BATCHES_INCOMPLETE", "请先提交全部原文批次")
    return {key: value for batch in batches.values() for key, value in batch.items()}


def check_context(session, ctx):
    _, _, context = _context(session, ctx.project_id, ctx.payload.get("research_id"), lock=True)
    _expected(context, ctx.payload["expected_revision"])
    if context["scope_version_id"] != ctx.scope_version_id or context["dataset_version_ids"] != ctx.dataset_version_ids:
        raise AppError(409, "AGENT_CONTEXT_CHANGED", "任务上下文已变化")
    if "expected_analysis_id" in ctx.payload and context["analysis_id"] != ctx.payload["expected_analysis_id"]:
        raise AppError(409, "AGENT_CONTEXT_CHANGED", "当前分析已变化，请重新核对")


def _execute(session, name, request):
    from app.main import update_scope, validate_scope
    if name in {"list_projects", "list_datasets"}:
        model = Project if name == "list_projects" else Dataset
        rows = session.scalars(select(model).where(model.owner_id == get_settings().dev_owner_id)
            .order_by(model.created_at, model.id).offset(request.offset).limit(request.limit + 1)).all()
        items = [record(row) for row in rows[:request.limit]]
        if model is Dataset:
            for item in items:
                item["versions"] = [record(v) for v in session.scalars(select(DatasetVersion)
                    .where(DatasetVersion.dataset_id == item["id"]).order_by(DatasetVersion.created_at, DatasetVersion.id))]
        return {"items": items, "next_offset": request.offset + request.limit if len(rows) > request.limit else None}
    if name == "create_agent_project":
        project = Project(owner_id=get_settings().dev_owner_id, name=request.name)
        session.add(project)
        session.flush()
        scope = ScopeVersion(project_id=project.id, number=1, confirmed=False, content=validate_scope(dict(request.scope)))
        session.add(scope)
        session.flush()
        project.scope_version_id = scope.id
        session.flush()
        return record(project)
    if name == "adopt_agent_research":
        research = owned(session, ResearchVersion, request.research_id)
        report = owned(session, Report, research.report_id)
        owned(session, Project, report.project_id, lock=True)
        research = owned(session, ResearchVersion, research.id, lock=True)
        return research_service.adopt(session, research, str(request.expected_current_version_id) if request.expected_current_version_id else None)
    if name == "get_agent_chart":
        from app.chart_service import snapshot
        chart = owned(session, Chart, request.chart_id)
        return snapshot(session, chart, str(request.chart_version_id) if request.chart_version_id else None)
    if name in {"get_semantic_batch", "submit_semantic_batch", "get_semantic_taxonomy",
                "submit_semantic_taxonomy", "finish_semantic_analysis"}:
        return _semantic_step(session, name, request)
    project, research, context = _context(session, request.project_id, getattr(request, "research_id", None),
                                          lock=name != "get_agent_context")
    report = session.scalar(select(Report).where(Report.project_id == project.id))
    if name == "get_agent_context":
        version_id = context.get("current_revision_id") if research else report.current_version_id if report else None
        return {**context_assets(session, project.id, context, version_id),
            "project": record(project), "research_id": research.id if research else None,
            "context": context, "scope": record(session.get(ScopeVersion, context["scope_version_id"])),
            "report_id": report.id if report else None,
            "adopted_report_version_id": report.current_version_id if report else None,
            "researches": [record(row) for row in session.scalars(select(ResearchVersion).where(
                ResearchVersion.report_id == report.id).order_by(ResearchVersion.created_at, ResearchVersion.id))] if report else []}
    if name == "update_agent_scope":
        return record(update_scope(session, project, request.expected_scope_version_id,
                                   request.changes, request.confirmed, research))
    if name == "create_agent_research":
        expected = str(request.expected_adopted_version_id) if request.expected_adopted_version_id else None
        if (report.current_version_id if report else None) != expected:
            raise AppError(409, "REPORT_VERSION_CONFLICT", "当前采用的报告已变化")
        if not report:
            report = Report(project_id=project.id)
            session.add(report)
            session.flush()
        draft = research_service.create_draft(session, report, expected)
        research_service.set_context(session, draft, scope_id=project.scope_version_id,
            dataset_ids=[project.dataset_version_id] if project.dataset_version_id else [], analysis_id=project.current_analysis_id)
        session.flush()
        return {"research_id": draft.id, "conversation_id": draft.main_conversation_id,
                "context": research_service.context_for(session, draft)}
    _expected(context, request.expected_revision)
    if name == "bind_agent_dataset":
        version = owned(session, DatasetVersion, request.dataset_version_id)
        if version.status != "ready":
            raise AppError(409, "DATASET_NOT_READY", "数据尚未就绪")
        if research:
            research_service.set_context(session, research, dataset_ids=[version.id], analysis_id=None)
        else:
            project.dataset_version_id, project.current_analysis_id = version.id, None
            project.revision += 1
        return {"project_id": project.id, "dataset_version_id": version.id,
                "revision": research.revision if research else project.revision}
    if name == "start_semantic_analysis" or request.action in {"analyze", "generate_report"}:
        _ready(session, context)
    if name == "start_semantic_analysis":
        from app.data_service import search_patents
        rows = search_patents(session, project, {"scope_version_id": context["scope_version_id"],
            "dataset_version_ids": context["dataset_version_ids"]})["items"]
        if not rows:
            raise AppError(422, "NO_SELECTED_PATENTS", "当前范围没有申请记录")
        run = create_run(session, "agent_analysis", project, {"research_id": research.id if research else None,
            "external_agent": True, "agent_model": request.agent_model, "skill_version": request.skill_version,
            "expected_revision": context["revision"], "expected_analysis_id": context["analysis_id"],
            "sources": [{"id": row.id, "sha256": text_hash(row)} for row in rows]},
            conversation_id=research.main_conversation_id if research else None)
        run.status, run.dispatch_pending = "waiting_input", False
        run.scope_version_id, run.dataset_version_ids = context["scope_version_id"], list(context["dataset_version_ids"])
        run.input_request = {"id": run.id, "kind": "agent_semantics", "prompt": "由外部 Agent 分批提交语义标签和主题"}
        emit_event(session, run.id, "input.required", run.input_request)
        return {"run_id": run.id, "status": run.status, "total_count": len(rows),
                "batch_size": semantics.BATCH_SIZE, "batch_count": math.ceil(len(rows) / semantics.BATCH_SIZE)}
    return _project_task(session, project, research, context, request)


def _semantic_step(session, name, request):
    run = _run(session, request.run_id, writing=not TOOLS[name].read_only)
    rows = _records(session, run)
    if name in {"get_semantic_batch", "submit_semantic_batch"}:
        start = request.batch_index * semantics.BATCH_SIZE
        batch = rows[start:start + semantics.BATCH_SIZE]
        if not batch:
            raise AppError(422, "BATCH_NOT_FOUND", "批次不存在")
        mapping = {f"p{index}": row for index, row in enumerate(batch)}
        if name == "get_semantic_batch":
            return {"run_id": run.id, "status": run.status, "batch_index": request.batch_index,
                "batch_count": math.ceil(len(rows) / semantics.BATCH_SIZE), "total_count": len(rows),
                "completed_batches": sorted(int(key.rsplit("_", 1)[1]) for key in _steps(session, run, "agent_batch_")),
                "records": [{"id": key, "application_number": row.application_number,
                    "title": row.title, "abstract": row.abstract} for key, row in mapping.items()]}
        submitted = [item.model_dump() for item in request.records]
        if len(submitted) != len(mapping) or {item["id"] for item in submitted} != set(mapping):
            raise AppError(422, "SEMANTIC_COVERAGE_MISMATCH", "必须恰好提交本批全部短ID，不可重复、遗漏或新增")
        result = {mapping[item["id"]].id: semantics.validate_extraction(mapping[item["id"]], item) for item in submitted}
        _save_step(session, run, f"agent_batch_{request.batch_index}", result)
        return {"run_id": run.id, "batch_index": request.batch_index,
                "review": {key: value["review_reasons"] for key, value in result.items() if value["review_reasons"]}}
    extractions = _extractions(session, run)
    labels = semantics.taxonomy_labels(extractions)
    if name in {"get_semantic_taxonomy", "submit_semantic_taxonomy"}:
        dimension = request.dimension
        ids = {f"{dimension[0]}{index}": label for index, label in enumerate(labels[dimension])}
        if name == "get_semantic_taxonomy":
            return {"run_id": run.id, "dimension": dimension, "labels": [{"id": k, "label": v} for k, v in ids.items()]}
        members = [member for theme in request.themes for member in theme.members]
        if len(members) != len(ids) or set(members) != set(ids):
            raise AppError(422, "TAXONOMY_COVERAGE_MISMATCH", "主题必须恰好覆盖本维度全部短ID")
        value = semantics.validate_taxonomy_dimension(dimension, labels,
            {dimension: [theme.model_dump() for theme in request.themes]})
        _save_step(session, run, "agent_taxonomy_" + dimension, value)
        return {"run_id": run.id, "dimension": dimension, "theme_count": len(value["themes"])}
    dimensions = _steps(session, run, "agent_taxonomy_")
    if set(dimensions) != {"agent_taxonomy_" + dimension for dimension in semantics.DIMENSIONS}:
        raise AppError(409, "TAXONOMY_INCOMPLETE", "请先提交全部三个维度的归并结果，空维度提交空主题列表")
    run.payload = {**run.payload, "taxonomy": semantics.assemble_taxonomy(labels,
        {d: dimensions["agent_taxonomy_" + d] for d in semantics.DIMENSIONS}), "extractions": extractions}
    run.status, run.dispatch_pending, run.input_request = "queued", True, None
    emit_event(session, run.id, "step.updated", {"step": "agent_submissions_complete", "status": "queued"})
    return {"run_id": run.id, "status": run.status}


def execute_analysis(ctx):
    from app.db import session_scope
    from app.semantic_service import _persist
    with session_scope() as session:
        run = session.get(ResearchRun, ctx.run_id)
        rows = _records(session, run)
        for row in rows:
            session.expunge(row)
    taxonomy = ctx.payload["taxonomy"]
    assignments = semantics.assign_themes(ctx.payload["extractions"], taxonomy)
    return ctx.step("persist_agent_analysis", lambda: _persist(ctx, rows, assignments, taxonomy, 0))


def _project_task(session, project, research, context, request):
    payload = request.model_dump(mode="json", exclude_none=True)
    payload.update(external_agent=True, expected_revision=context["revision"])
    payload.pop("project_id")
    payload.pop("action")
    action = request.action
    if action in {"generate_report", "revise_report", "restore_report"} and not research:
        raise AppError(422, "RESEARCH_TARGET_REQUIRED", "请明确选择研究；新主题先创建独立研究")
    report = session.get(Report, research.report_id) if research else None
    if action in {"generate_report", "restore_report"}:
        expected_adopted = str(request.expected_adopted_version_id) if request.expected_adopted_version_id else None
        if report.current_version_id != expected_adopted:
            raise AppError(409, "REPORT_VERSION_CONFLICT", "当前采用的报告已变化，请重新核对")
        payload["expected_adopted_version_id"] = expected_adopted
    if action == "generate_report":
        analysis = owned(session, AnalysisResult, request.analysis_id or context.get("analysis_id"))
        if analysis.project_id != project.id or analysis.scope_version_id != context["scope_version_id"] or analysis.dataset_version_ids != context["dataset_version_ids"]:
            raise AppError(409, "ANALYSIS_SNAPSHOT_MISMATCH", "分析与研究上下文不一致")
        if research.current_revision_id != payload.get("expected_current_version_id"):
            raise AppError(409, "REPORT_VERSION_CONFLICT", "报告版本已变化")
        payload.update(analysis_id=analysis.id, report_mode="statistical")
    if action in {"revise_report", "restore_report", "export"}:
        identifier = request.report_version_id if action != "revise_report" else request.base_version_id
        version = owned(session, ReportVersion, identifier)
        if session.get(Report, version.report_id).project_id != project.id or (research and version.research_id != research.id):
            raise AppError(404, "REPORT_VERSION_NOT_FOUND", "报告版本不属于目标研究")
        payload.update(report_id=version.report_id)
        if action == "revise_report":
            if not request.operations:
                raise AppError(422, "REPORT_OPERATIONS_REQUIRED", "请提交 Agent 已完成的结构化修改")
        elif action == "restore_report":
            payload.update(restore_revision_id=version.id)
    if action == "revise_chart":
        chart = owned(session, Chart, request.chart_id)
        if chart.project_id != project.id or (research and chart.analysis_id != research.analysis_id):
            raise AppError(404, "CHART_NOT_FOUND", "图表不属于当前研究")
        if not request.base_version_id or not request.expected_current_version_id:
            raise AppError(422, "CHART_VERSION_REQUIRED", "请提供图表基础版本和预期采用版本")
        if request.chart_mode == "insight" and not request.interpretation:
            raise AppError(422, "CHART_INTERPRETATION_REQUIRED", "请由 Agent 提供图表解读")
        payload.update(mode=request.chart_mode, option=request.chart_option, filters=request.chart_filters)
    run = create_run(session, action, project, payload, conversation_id=research.main_conversation_id if research else None)
    run.scope_version_id, run.dataset_version_ids = context["scope_version_id"], list(context["dataset_version_ids"])
    if action == "export":
        run.scope_version_id, run.dataset_version_ids = version.scope_version_id, list(version.dataset_version_ids)
    return {"run_id": run.id, "status": run.status}
