"""Immutable, evidence-backed statistical reports and Word exports."""

from patentcompass_agent import read_prompt

from copy import deepcopy
import json
from pathlib import Path
import shutil
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from patentcompass_core import rendering as core_rendering, reports as core_reports

from app.db import session_scope
from app.errors import AppError
from app.files import store_bytes
from app.models import (
    AnalysisResult, Chart, Conversation, DatasetVersion, Evidence, PatentApplication,
    PatentPublication, Project, Report, ReportVersion, ResearchRun, ScopeVersion, StoredFile,
)
from app.research_service import (
    context_for, ensure_for_report, publish_candidate, research_for_context, version_result,
)


DEFAULT_REPORT_LAYOUT = core_reports.DEFAULT_REPORT_LAYOUT


def _version_result(session, report, version):
    return version_result(session, report, version)


def _run_lineage(session, run_id):
    identifiers = []
    while run_id and run_id not in identifiers:
        identifiers.append(run_id)
        run = session.get(ResearchRun, run_id)
        run_id = run.retry_of_id if run else None
    return identifiers


def _saved_version(ctx):
    with session_scope() as session:
        ctx.guard(session)
        lineage = _run_lineage(session, ctx.run_id)
        versions = {version.run_id: version for version in session.scalars(select(ReportVersion).where(
            ReportVersion.run_id.in_(lineage))).all()}
        previous = next((versions[run_id] for run_id in lineage if run_id in versions), None)
        if previous:
            report = session.get(Report, previous.report_id)
            if report.project_id != ctx.project_id:
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "报告版本不存在")
            return _version_result(session, report, previous)
    return None


def _project(session, project_id):
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if not project:
        raise AppError(404, "PROJECT_NOT_FOUND", "项目不存在")
    return project


def _load_snapshot(ctx):
    with session_scope() as session:
        project = session.get(Project, ctx.project_id)
        if not project:
            raise AppError(404, "PROJECT_NOT_FOUND", "项目不存在")
        report = session.scalar(select(Report).where(Report.project_id == project.id))
        research = research_for_context(session, ctx, report) if report else None
        analysis_id = ctx.payload.get("analysis_id") or (
            context_for(session, research)["analysis_id"] if research else project.current_analysis_id)
        analysis = session.get(AnalysisResult, analysis_id) if analysis_id else None
        if not analysis or analysis.project_id != ctx.project_id:
            raise AppError(409, "ANALYSIS_REQUIRED", "请先完成本项目的数据分析")
        if (analysis.scope_version_id != ctx.scope_version_id
                or sorted(analysis.dataset_version_ids) != sorted(ctx.dataset_version_ids)):
            raise AppError(409, "ANALYSIS_SNAPSHOT_MISMATCH", "分析结果与任务的数据或范围版本不一致")
        scope = session.get(ScopeVersion, analysis.scope_version_id)
        sources = []
        for version_id in analysis.dataset_version_ids:
            dataset = session.get(DatasetVersion, version_id)
            source_file = session.get(StoredFile, dataset.source_file_id)
            sources.append({"dataset_version_id": version_id,
                            "source_file_id": dataset.source_file_id,
                            "file_name": source_file.original_name, "sha256": source_file.sha256,
                            "rule_version": dataset.rule_version, "quality": dataset.quality,
                            "source_metadata": dataset.source_metadata})
        from app.chart_service import snapshot as chart_snapshot
        charts = [chart_snapshot(session, chart) for chart in session.scalars(select(Chart).where(
            Chart.analysis_id == analysis.id).order_by(Chart.kind, Chart.id))]
        evidence = [{"id": row.id, "kind": row.kind, "application_id": row.application_id,
                     "content": row.content}
                    for row in session.scalars(select(Evidence).where(
                        Evidence.analysis_id == analysis.id).order_by(Evidence.id))]
        records = list(session.scalars(select(PatentApplication).where(
            PatentApplication.id.in_(analysis.patent_ids)).order_by(
                PatentApplication.application_date.desc(), PatentApplication.application_number).limit(5)))
        representatives = []
        for row in records:
            publications = list(session.scalars(select(PatentPublication.publication_number).where(
                PatentPublication.application_id == row.id).order_by(PatentPublication.publication_number)))
            representatives.append({"application_id": row.id, "application_number": row.application_number,
                                    "publication_numbers": publications, "title": row.title,
                                    "abstract": row.abstract, "applicants": row.applicants,
                                    "source_sheet": row.source_sheet, "source_row": row.source_row,
                                    "dataset_version_id": row.dataset_version_id})
        return deepcopy({"project_name": project.name, "analysis_id": analysis.id,
                         "analysis_run_id": analysis.run_id, "scope_version_id": analysis.scope_version_id,
                         "scope": scope.content, "dataset_version_ids": analysis.dataset_version_ids,
                         "summary": analysis.summary, "views": analysis.views, "sources": sources,
                         "charts": charts, "evidence": evidence, "representatives": representatives})


def build_report_content(snapshot):
    return core_reports.build_report_content(snapshot, default_layout=DEFAULT_REPORT_LAYOUT)


def _report_target(ctx):
    with session_scope() as session:
        _project(session, ctx.project_id)
        report = session.scalar(select(Report).where(Report.project_id == ctx.project_id).with_for_update())
        if not report:
            report = Report(project_id=ctx.project_id)
            session.add(report)
            session.flush()
        research = research_for_context(session, ctx, report)
        ctx.guard(session)
        if ctx.payload.get("external_agent"):
            from app.agent_service import check_context
            check_context(session, ctx)
            if (research.current_revision_id != ctx.payload.get("expected_current_version_id")
                    or report.current_version_id != ctx.payload.get("expected_adopted_version_id")):
                raise AppError(409, "REPORT_VERSION_CONFLICT", "报告已变化，请重新核对")
        return {"report_id": report.id, "research_id": research.id,
                "expected_current_version_id": research.current_revision_id,
                "expected_research_revision": research.revision,
                "expected_adopted_version_id": report.current_version_id,
                "conversation_id": getattr(ctx, "conversation_id", None) or ctx.payload.get("conversation_id"),
                "source_message_id": ctx.payload.get("source_message_id") or ctx.payload.get("message_id")}


def _complete_candidate(ctx, key, candidate):
    result = {"_report_candidate": ctx.step(key, lambda: deepcopy(candidate))}
    if getattr(ctx, "defer_report_publish", False):
        return result
    with session_scope() as session:
        return publish_candidate(session, ctx, result)


def generate_report(ctx):
    previous = _saved_version(ctx)
    if previous:
        return previous
    target = ctx.step("report_target_snapshot", lambda: _report_target(ctx))
    snapshot = ctx.step("report_snapshot", lambda: _load_snapshot(ctx))
    content = build_report_content(snapshot)
    if ctx.payload.get("report_mode") == "ai":
        from app.model_provider import chat

        def synthesize():
            response = chat([
                {"role": "system", "content": read_prompt('report-summary')},
                {"role": "user", "content": json.dumps({"summary": snapshot["summary"],
                    "records": snapshot["representatives"]}, ensure_ascii=False)},
            ])
            return {"text": _response_text(response)}

        synthesis = ctx.step("report_synthesis", synthesize)
        content["blocks"].insert(1, {"id": str(uuid5(NAMESPACE_URL, f"{snapshot['analysis_id']}:ai-summary")),
                                    "type": "paragraph", "text": synthesis["text"], "origin": "model"})
    if ctx.payload.get("external_agent") and ctx.payload.get("summary_text"):
        content["blocks"].insert(1, {"id": str(uuid5(NAMESPACE_URL, f"{ctx.run_id}:agent-summary")),
            "type": "paragraph", "text": ctx.payload["summary_text"], "origin": "agent"})
    ctx.check_cancelled()
    candidate = {**target, "content": content, "analysis_id": snapshot["analysis_id"],
        "scope_version_id": snapshot["scope_version_id"], "dataset_version_ids": snapshot["dataset_version_ids"],
        "instruction": "生成统计简报", "summary": "根据固定数据与范围生成统计简报", "change_kind": "generate"}
    return _complete_candidate(ctx, "report_candidate", candidate)


def _response_text(response):
    return core_reports._response_text(response)


def replace_block(content, block_id, replacement):
    return core_reports.replace_block(content, block_id, replacement)


def apply_operations(session, project_id, content, operations, namespace):
    """Resolve authorized frozen assets before applying detached operations."""
    from app.chart_service import snapshot as chart_snapshot

    charts = []
    resolved_operations = deepcopy(operations)
    for operation in resolved_operations:
        if operation["op"] not in {"insert_chart", "update_chart"}:
            continue
        chart = session.get(Chart, str(operation.get("chart_id")))
        if not chart or chart.project_id != project_id:
            raise AppError(404, "CHART_NOT_FOUND", "图表不存在或不属于当前项目")
        frozen = chart_snapshot(session, chart, str(operation["chart_version_id"]) if operation.get("chart_version_id") else None)
        operation["chart_version_id"] = frozen["version_id"]
        frozen["evidence_snapshot"] = [{"id": row.id, "kind": row.kind, "application_id": row.application_id,
            "content": deepcopy(row.content)} for row in session.scalars(select(Evidence).where(Evidence.analysis_id == chart.analysis_id))]
        charts.append(frozen)
    return core_reports.revise_report_content(content, resolved_operations, namespace=namespace, charts=charts,
                                             default_layout=DEFAULT_REPORT_LAYOUT)


def revise_report(ctx):
    previous = _saved_version(ctx)
    if previous:
        return previous
    payload = ctx.payload

    def prepare():
        with session_scope() as session:
            report = session.get(Report, payload["report_id"])
            base = session.get(ReportVersion, payload["base_version_id"])
            if not report or report.project_id != ctx.project_id or not base or base.report_id != report.id:
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "报告版本不存在")
            research = research_for_context(session, ctx, report)
            legacy = not getattr(ctx, "defer_report_publish", False) and not payload.get("research_id")
            if legacy and report.current_research_id == research.id:
                # Direct service callers historically adopt by changing this pointer.
                research.current_revision_id = report.current_version_id
            if not research.current_revision_id:
                raise AppError(409, "RESEARCH_REPORT_REQUIRED", "本轮研究请先生成首份报告")
            if (base.research_id != research.id or base.id != research.current_revision_id
                    or research.current_revision_id != payload["expected_current_version_id"]):
                raise AppError(409, "REPORT_VERSION_CONFLICT", "本研究的最新报告已变化，请重新核对修改目标")
            conversation_id = getattr(ctx, "conversation_id", None) or payload.get("conversation_id")
            if conversation_id:
                conversation = session.get(Conversation, conversation_id)
                if not conversation or conversation.project_id != ctx.project_id:
                    raise AppError(404, "CONVERSATION_NOT_FOUND", "来源对话不存在或不属于当前项目")
            block = None
            content = deepcopy(base.content)
            if payload.get("operations"):
                content = apply_operations(session, ctx.project_id, content, payload["operations"], ctx.run_id)
            else:
                block = next((row for row in base.content["blocks"] if row["id"] == payload["block_id"]), None)
                if not block:
                    raise AppError(404, "BLOCK_NOT_FOUND", "指定正文块不存在")
                if block["type"] not in {"paragraph", "heading"}:
                    raise AppError(422, "BLOCK_TYPE_UNSUPPORTED", "文字修改仅支持段落或标题")
            return {"content": content, "analysis_id": base.analysis_id,
                    "scope_version_id": base.scope_version_id, "dataset_version_ids": base.dataset_version_ids,
                    "chart_ids": base.chart_ids, "block": deepcopy(block), "conversation_id": conversation_id,
                    "report_id": report.id, "research_id": research.id,
                    "expected_current_version_id": research.current_revision_id,
                    "expected_adopted_version_id": report.current_version_id,
                    "legacy_adopted_check": legacy,
                    "source_message_id": payload.get("source_message_id") or payload.get("message_id")}

    base = ctx.step("revision_snapshot", prepare)
    if payload.get("operations"):
        content = base["content"]
    elif payload.get("replacement_text") is not None:
        replacement = payload["replacement_text"]
    else:
        from app.model_provider import chat

        def rewrite():
            revised = core_reports.revise_report_content(base["content"], instruction=payload["instruction"],
                block_id=payload["block_id"], generate_callable=chat)
            return {"text": next(block["text"] for block in revised["blocks"] if block["id"] == payload["block_id"])}

        replacement = ctx.step("revision_text", rewrite)["text"]
    if not payload.get("operations"):
        content = replace_block(base["content"], payload["block_id"], replacement)
    ctx.check_cancelled()
    candidate = {key: value for key, value in base.items() if key not in {"block", "content", "chart_ids"}}
    candidate.update({"content": content, "change_kind": "edit",
        "instruction": payload.get("instruction") or ("按结构化操作修改报告" if payload.get("operations") else "替换指定文字块"),
        "summary": f"完成 {len(payload['operations'])} 项报告修改" if payload.get("operations") else "修改所选报告文字"})
    return _complete_candidate(ctx, "revision_candidate", candidate)


def restore_report(ctx):
    previous = _saved_version(ctx)
    if previous:
        return previous

    def prepare():
        with session_scope() as session:
            from app.models import ResearchVersion
            research = session.get(ResearchVersion, ctx.payload["research_id"])
            report = session.get(Report, research.report_id) if research else None
            source = session.get(ReportVersion, ctx.payload["restore_revision_id"])
            if (not report or report.project_id != ctx.project_id or not source
                    or source.research_id != research.id):
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "恢复记录不存在")
            if research.current_revision_id != ctx.payload["expected_current_version_id"]:
                raise AppError(409, "REPORT_VERSION_CONFLICT", "本研究的报告已变化，请重新核对")
            if report.current_version_id != ctx.payload["expected_adopted_version_id"]:
                raise AppError(409, "REPORT_VERSION_CONFLICT", "当前采用的报告已变化，请重新核对")
            return {"report_id": report.id, "research_id": research.id,
                "expected_current_version_id": research.current_revision_id,
                "expected_research_revision": research.revision,
                "expected_adopted_version_id": report.current_version_id,
                "content": deepcopy(source.content), "analysis_id": source.analysis_id,
                "scope_version_id": source.scope_version_id, "dataset_version_ids": list(source.dataset_version_ids),
                "restored_from_id": source.id, "change_kind": "restore",
                "instruction": "恢复所选报告记录", "summary": "恢复历史记录并采用本研究版本",
                "conversation_id": getattr(ctx, "conversation_id", None) or ctx.payload.get("conversation_id"),
                "source_message_id": ctx.payload.get("source_message_id") or ctx.payload.get("message_id")}

    candidate = ctx.step("restore_snapshot", prepare)
    ctx.check_cancelled()
    return _complete_candidate(ctx, "restore_candidate", candidate)


def render_chart(option, output_path):
    renderer = core_rendering.RendererConfig(
        node_binary=shutil.which("node") or "",
        script_path=Path(__file__).resolve().parents[1] / "chart-renderer" / "render.mjs")
    return core_rendering.render_chart(option, output_path, renderer=renderer)


def build_docx(content, version_number, *, check_cancelled=lambda: None):
    return core_rendering.build_docx(content, version_number, render_chart_callable=render_chart,
                                    default_layout=DEFAULT_REPORT_LAYOUT, check_cancelled=check_cancelled)


def export_report(ctx):
    payload = ctx.payload
    if payload.get("format", "docx") != "docx":
        raise AppError(422, "EXPORT_FORMAT_UNSUPPORTED", "首轮仅支持 Word 导出")
    with session_scope() as session:
        ctx.guard(session)
        lineage = _run_lineage(session, ctx.run_id)
        files = {file.run_id: file for file in session.scalars(select(StoredFile).where(
            StoredFile.run_id.in_(lineage), StoredFile.kind == "export")).all()}
        existing = next((files[run_id] for run_id in lineage if run_id in files), None)
        if existing:
            if existing.project_id != ctx.project_id:
                raise AppError(404, "FILE_NOT_FOUND", "导出文件不存在")
            return {"file_id": existing.id, "download_url": f"/api/files/{existing.id}",
                    "report_version_id": payload["report_version_id"]}

    def prepare():
        with session_scope() as session:
            report = session.get(Report, payload["report_id"])
            version = session.get(ReportVersion, payload["report_version_id"])
            if not report or report.project_id != ctx.project_id or not version or version.report_id != report.id:
                raise AppError(404, "REPORT_VERSION_NOT_FOUND", "报告版本不存在")
            return {"content": deepcopy(version.content), "number": version.number,
                    "report_id": report.id, "report_version_id": version.id}

    snapshot = ctx.step("export_snapshot", prepare)
    with session_scope() as session:
        existing = session.scalar(select(StoredFile).where(StoredFile.run_id == ctx.run_id))
        if existing:
            return {"file_id": existing.id, "download_url": f"/api/files/{existing.id}",
                    "report_version_id": snapshot["report_version_id"]}
    document = build_docx(snapshot["content"], snapshot["number"], check_cancelled=ctx.check_cancelled)
    ctx.check_cancelled()
    with session_scope() as session:
        ctx.guard(session)
        file = store_bytes(session, document, f"patentcompass-report-v{snapshot['number']}.docx",
                           kind="export", project_id=ctx.project_id, run_id=ctx.run_id)
        ctx.guard(session)
        return {"file_id": file.id, "download_url": f"/api/files/{file.id}",
                "report_version_id": snapshot["report_version_id"]}
