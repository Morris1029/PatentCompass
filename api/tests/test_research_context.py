"""Research analysis and planning must stay independent of project adoption."""
import json

import pytest
from sqlalchemy import select

from app import chart_service, data_service, model_provider, orchestration, report_service, semantic_service
from app.errors import AppError
from app.jobs import WaitingForInput
from app.models import (Chart, Conversation, Message, Report, ReportVersion, ResearchRun,
                        ResearchVersion, ScopeVersion, new_id)
from test_reports import report_analysis
from test_semantic_charts import semantic_data, semantic_model


def _research(db_session, project, analysis_id=None):
    report = db_session.scalar(select(Report).where(Report.project_id == project.id))
    if not report:
        report = Report(project_id=project.id)
        db_session.add(report)
        db_session.flush()
    research = ResearchVersion(report_id=report.id, scope_version_id=project.scope_version_id,
                               dataset_version_ids=[project.dataset_version_id] if project.dataset_version_id else [],
                               analysis_id=analysis_id)
    db_session.add(research)
    db_session.flush()
    return research


def _context(db_session, project, context_factory, research, kind="auto", payload=None):
    conversation = Conversation(project_id=project.id, title="当前研究")
    db_session.add(conversation)
    db_session.flush()
    message = Message(conversation_id=conversation.id, role="user", content="修改本轮研究报告")
    db_session.add(message)
    db_session.flush()
    context = context_factory(project, kind, {"research_id": research.id, "message_id": message.id,
                                             "semantic_mode": "off", **(payload or {})})
    run = db_session.get(ResearchRun, context.run_id)
    run.conversation_id = conversation.id
    run.scope_version_id = research.scope_version_id
    run.dataset_version_ids = list(research.dataset_version_ids)
    message.run_id = run.id
    db_session.flush()
    context.conversation_id = conversation.id
    context.scope_version_id = research.scope_version_id
    context.dataset_version_ids = list(research.dataset_version_ids)
    return context


def _revision(db_session, project, context_factory, research, analysis_id, title, parent=None, chart=None):
    report = db_session.get(Report, research.report_id)
    context = context_factory(project, "generate_report", {"research_id": research.id})
    blocks = [{"id": new_id(), "type": "paragraph", "text": title + "正文"}]
    if chart:
        blocks.append({"id": new_id(), "type": "chart", "chart_id": chart.id, "chart_version_id": chart.version_id})
    version = ReportVersion(report_id=report.id, research_id=research.id, number=report.version_counter + 1,
                            run_id=context.run_id, parent_id=parent.id if parent else None, analysis_id=analysis_id,
                            scope_version_id=research.scope_version_id, dataset_version_ids=research.dataset_version_ids,
                            content={"title": title, "blocks": blocks})
    db_session.add(version)
    db_session.flush()
    report.version_counter = version.number
    research.current_revision_id = version.id
    research.initial_revision_id = research.initial_revision_id or version.id
    research.status = "published"
    db_session.flush()
    return version


def test_statistical_analysis_updates_only_its_research_context(
        db_session, project, context_factory, report_analysis):
    adopted_analysis_id = report_analysis.id
    research = _research(db_session, project, adopted_analysis_id)
    context = context_factory(project, "analyze", {"research_id": research.id})
    result = data_service.analyze(context)
    db_session.expire_all()
    assert db_session.get(ResearchVersion, research.id).analysis_id == result["analysis_id"]
    assert db_session.get(type(project), project.id).current_analysis_id == adopted_analysis_id
    assert result["analysis_id"] != adopted_analysis_id


def test_completed_analysis_does_not_replace_newer_research_scope_or_analysis(
        db_session, project, context_factory, report_analysis):
    research = _research(db_session, project, report_analysis.id)
    context = context_factory(project, "analyze", {"research_id": research.id})
    step = context.step
    newer_scope = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "电池"})
    db_session.add(newer_scope)
    db_session.flush()

    def change_scope_after_calculation(key, callback):
        result = step(key, callback)
        if key.startswith("calculate_statistics_"):
            research.scope_version_id = newer_scope.id
            db_session.flush()
        return result

    context.step = change_scope_after_calculation
    result = data_service.analyze(context)
    db_session.expire_all()
    saved = db_session.get(ResearchVersion, research.id)
    assert saved.scope_version_id == newer_scope.id and saved.analysis_id == report_analysis.id
    assert db_session.get(type(project), project.id).current_analysis_id == report_analysis.id
    assert result["analysis_id"] != report_analysis.id


def test_semantic_analysis_and_correction_use_research_analysis_for_conflict_checks(
        db_session, project, context_factory, report_analysis, semantic_data, semantic_model):
    adopted_analysis_id = report_analysis.id
    research = _research(db_session, project)
    first = semantic_service.analyze(context_factory(project, "analyze", {"research_id": research.id, "semantic_mode": "full"}))
    correction = context_factory(project, "revise_analysis", {
        "research_id": research.id, "base_analysis_id": first["analysis_id"],
        "expected_current_analysis_id": first["analysis_id"], "corrections": [{
            "application_id": semantic_data[0].id, "dimension": "methods", "theme_ids": []}]})
    revised = semantic_service.revise_analysis(correction)
    db_session.expire_all()
    assert db_session.get(ResearchVersion, research.id).analysis_id == revised["analysis_id"]
    assert revised["analysis_id"] != first["analysis_id"]
    assert db_session.get(type(project), project.id).current_analysis_id == adopted_analysis_id


def test_planner_uses_research_head_analysis_and_frozen_chart_versions(
        db_session, project, context_factory, report_analysis, monkeypatch):
    research = _research(db_session, project)
    own_analysis = data_service.analyze(context_factory(project, "analyze", {"research_id": research.id}))
    own_chart = db_session.scalar(select(Chart).where(Chart.analysis_id == own_analysis["analysis_id"]))
    head = _revision(db_session, project, context_factory, research, own_analysis["analysis_id"], "本轮报告", chart=own_chart)
    frozen_chart_version = own_chart.version_id
    own_chart.version_id = new_id()
    other = _research(db_session, project, report_analysis.id)
    adopted = _revision(db_session, project, context_factory, other, report_analysis.id, "项目采用的其他报告")
    report = db_session.get(Report, research.report_id)
    report.current_version_id, report.current_research_id = adopted.id, other.id
    db_session.flush()
    context = _context(db_session, project, context_factory, research)
    prompts = []

    def model(messages, tools=None):
        prompts.append(json.loads(messages[-1]["content"]))
        return {"content": json.dumps({"action": "chat", "reason": "解释本轮报告"})}

    monkeypatch.setattr(model_provider, "chat", model)
    orchestration._prepare_plan(context)
    assert prompts[0]["report"]["id"] == head.id
    assert prompts[0]["report"]["title"] == "本轮报告"
    assert prompts[0]["charts"] == [{"id": own_chart.id, "kind": own_chart.kind, "version_id": frozen_chart_version}]
    assert context.payload["analysis_id"] == own_analysis["analysis_id"]
    assert context.payload["plan_report_current_id"] == head.id
    assert db_session.get(Report, report.id).current_version_id == adopted.id


def test_draft_uses_base_report_as_reference_without_treating_it_as_its_head(
        db_session, project, context_factory, report_analysis, monkeypatch):
    previous = _research(db_session, project, report_analysis.id)
    chart = db_session.scalar(select(Chart).where(Chart.analysis_id == report_analysis.id))
    base = _revision(db_session, project, context_factory, previous, report_analysis.id, "上一轮报告", chart=chart)
    draft = _research(db_session, project, report_analysis.id)
    draft.base_revision_id = base.id
    db_session.flush()
    context = _context(db_session, project, context_factory, draft)
    prompts = []

    def model(messages, tools=None):
        prompts.append(json.loads(messages[-1]["content"]))
        return {"content": json.dumps({"action": "chat", "reason": "参考上一轮研究"})}

    monkeypatch.setattr(model_provider, "chat", model)
    orchestration._prepare_plan(context)
    assert prompts[0]["report"]["id"] == base.id and prompts[0]["charts"] == []
    assert context.payload["plan_report_version_id"] == base.id
    assert context.payload["plan_report_current_id"] is None


def test_draft_cannot_revise_its_base_report_or_shared_base_charts(
        db_session, project, context_factory, report_analysis, monkeypatch):
    previous = _research(db_session, project, report_analysis.id)
    chart = db_session.scalar(select(Chart).where(Chart.analysis_id == report_analysis.id))
    base = _revision(db_session, project, context_factory, previous, report_analysis.id, "上一轮报告", chart=chart)
    draft = _research(db_session, project, report_analysis.id)
    draft.base_revision_id = base.id
    db_session.flush()
    report_context = _context(db_session, project, context_factory, draft, payload={
        "plan": orchestration.Plan(action="revise_report", reason="改标题", operations=[{"op": "set_title", "text": "新标题"}]).model_dump(mode="json"),
        "plan_report_version_id": base.id})
    with pytest.raises(AppError) as failure:
        orchestration.execute(report_context)
    assert failure.value.code == "RESEARCH_REPORT_REQUIRED"
    chart_context = _context(db_session, project, context_factory, draft, payload={
        "plan": orchestration.Plan(action="revise_chart", reason="修改样式", chart_id=chart.id,
                                   chart_mode="style").model_dump(mode="json")})
    monkeypatch.setattr(chart_service, "revise_chart", lambda ctx: pytest.fail("Draft modified a shared base chart"))
    with pytest.raises(AppError) as failure:
        orchestration.execute(chart_context)
    assert failure.value.code == "RESEARCH_ANALYSIS_REQUIRED"
    assert db_session.get(ResearchVersion, previous.id).current_revision_id == base.id


def test_old_research_reference_requests_latest_own_head_without_adopting_it(
        db_session, project, context_factory, report_analysis):
    research = _research(db_session, project, report_analysis.id)
    old = _revision(db_session, project, context_factory, research, report_analysis.id, "本轮旧稿")
    head = _revision(db_session, project, context_factory, research, report_analysis.id, "本轮最新稿", parent=old)
    other = _research(db_session, project, report_analysis.id)
    adopted = _revision(db_session, project, context_factory, other, report_analysis.id, "项目采用稿")
    report = db_session.get(Report, research.report_id)
    report.current_version_id, report.current_research_id = adopted.id, other.id
    db_session.flush()
    context = _context(db_session, project, context_factory, research, payload={
        "plan": orchestration.Plan(action="revise_report", reason="改标题", operations=[{"op": "set_title", "text": "确认后标题"}]).model_dump(mode="json"),
        "plan_report_version_id": old.id, "plan_report_current_id": old.id})
    with pytest.raises(WaitingForInput):
        orchestration.execute(context)
    db_session.expire_all()
    request = db_session.get(ResearchRun, context.run_id).input_request
    assert request["kind"] == "report_target" and request["base_version_id"] == head.id
    assert request["title"] == "本轮最新稿"
    assert [block["id"] for block in request["blocks"]] == [block["id"] for block in head.content["blocks"]]
    assert db_session.get(Report, report.id).current_version_id == adopted.id
    assert db_session.get(ResearchVersion, research.id).current_revision_id == head.id


def test_current_research_head_can_be_revised_while_another_research_is_adopted(
        db_session, project, context_factory, report_analysis, monkeypatch):
    research = _research(db_session, project, report_analysis.id)
    head = _revision(db_session, project, context_factory, research, report_analysis.id, "本轮报告")
    other = _research(db_session, project, report_analysis.id)
    adopted = _revision(db_session, project, context_factory, other, report_analysis.id, "项目采用稿")
    report = db_session.get(Report, research.report_id)
    report.current_version_id, report.current_research_id = adopted.id, other.id
    db_session.flush()
    context = _context(db_session, project, context_factory, research, payload={
        "plan": orchestration.Plan(action="revise_report", reason="改标题", operations=[{"op": "set_title", "text": "本轮新标题"}]).model_dump(mode="json"),
        "plan_report_version_id": head.id, "plan_report_current_id": head.id})
    observed = []
    monkeypatch.setattr(orchestration, "require_execution", lambda *args: None)

    def revise(ctx):
        observed.append(dict(ctx.payload))
        return {"report_id": report.id, "report_version_id": new_id()}

    monkeypatch.setattr(report_service, "revise_report", revise)
    orchestration.execute(context)
    assert observed[0]["base_version_id"] == head.id
    assert observed[0]["expected_current_version_id"] == head.id
    assert db_session.get(Report, report.id).current_version_id == adopted.id


def test_report_edit_pins_run_to_head_basis_without_resetting_working_context(
        db_session, project, context_factory, report_analysis, monkeypatch):
    research = _research(db_session, project, report_analysis.id)
    head = _revision(db_session, project, context_factory, research, report_analysis.id, "已保存报告")
    working_scope = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "无人机", "goal": "新的工作目标"})
    db_session.add(working_scope)
    db_session.flush()
    research.scope_version_id = working_scope.id
    research.analysis_id = None
    db_session.flush()
    working = data_service.analyze(_context(db_session, project, context_factory, research, kind="analyze"))
    context = _context(db_session, project, context_factory, research, payload={
        "analysis_id": working["analysis_id"],
        "plan": orchestration.Plan(action="revise_report", reason="修改已保存报告", operations=[{"op": "set_title", "text": "修订标题"}]).model_dump(mode="json"),
        "plan_report_version_id": head.id, "plan_report_current_id": head.id})
    observed = []

    def confirm(ctx, kinds):
        db_session.expire_all()
        run = db_session.get(ResearchRun, ctx.run_id)
        observed.append((ctx.scope_version_id, run.scope_version_id, ctx.payload["analysis_id"], run.payload["analysis_id"]))

    monkeypatch.setattr(orchestration, "require_execution", confirm)
    monkeypatch.setattr(report_service, "revise_report", lambda ctx: {"report_version_id": new_id()})
    orchestration.execute(context)
    assert observed == [(head.scope_version_id, head.scope_version_id, head.analysis_id, head.analysis_id)]
    db_session.expire_all()
    saved = db_session.get(ResearchVersion, research.id)
    assert saved.scope_version_id == working_scope.id and saved.analysis_id == working["analysis_id"]


@pytest.mark.parametrize("answer_context,explicit_reference,report_basis", [
    ("report", False, True), ("working", False, False), ("working", True, True),
])
def test_report_answers_and_working_analysis_answers_use_distinct_frozen_basis(
        db_session, project, context_factory, report_analysis, monkeypatch,
        answer_context, explicit_reference, report_basis):
    research = _research(db_session, project, report_analysis.id)
    head = _revision(db_session, project, context_factory, research, report_analysis.id, "已保存报告")
    working_scope = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "无人机", "goal": "新的工作分析"})
    db_session.add(working_scope)
    db_session.flush()
    research.scope_version_id, research.analysis_id = working_scope.id, None
    db_session.flush()
    working = data_service.analyze(_context(db_session, project, context_factory, research, kind="analyze"))
    context = _context(db_session, project, context_factory, research, payload={
        "analysis_id": working["analysis_id"], "reference_version_id": head.id})
    if explicit_reference:
        db_session.get(Message, context.payload["message_id"]).reference = {"report_version_id": head.id}
        db_session.flush()
    monkeypatch.setattr(model_provider, "chat", lambda *args, **kwargs: {"content": json.dumps({
        "action": "chat", "reason": "选择回答依据", "answer_context": answer_context})})
    orchestration._prepare_plan(context)
    db_session.expire_all()
    run = db_session.get(ResearchRun, context.run_id)
    assert context.scope_version_id == run.scope_version_id == (head.scope_version_id if report_basis else working_scope.id)
    assert context.payload["analysis_id"] == run.payload["analysis_id"] == (head.analysis_id if report_basis else working["analysis_id"])
    assert run.payload.get("reference_version_id") == (head.id if report_basis else None)
    saved = db_session.get(ResearchVersion, research.id)
    assert saved.scope_version_id == working_scope.id and saved.analysis_id == working["analysis_id"]
