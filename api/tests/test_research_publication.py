"""Publication and conversation results commit together at the worker boundary."""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app import jobs, report_service
from app.main import create_app
from app.models import (AnalysisResult, Conversation, DatasetVersion, Message, Report, ReportVersion,
                        ResearchRun, ResearchVersion, RunEvent, RunStep, ScopeVersion)
from app.research_service import create_draft, ensure_conversation, set_context
from test_reports import report_analysis


@pytest.fixture(autouse=True)
def no_heartbeat_thread(monkeypatch):
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


@pytest.fixture
def first(db_session, project, context_factory, report_analysis):
    return report_service.generate_report(context_factory(project, "generate_report", {
        "analysis_id": report_analysis.id}))


def _events(db_session, run_id):
    return list(db_session.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)))


def test_outcome_failure_rolls_back_publication_and_retry_publishes_draft_once(
        client, db_session, project, first, report_analysis, monkeypatch):
    report = db_session.get(Report, first["report_id"])
    original_content = deepcopy(db_session.get(ReportVersion, first["report_version_id"]).content)
    draft = create_draft(db_session, report, first["report_version_id"])
    conversation = ensure_conversation(db_session, draft)
    origin = Message(conversation_id=conversation.id, role="user", content="生成本轮研究报告")
    db_session.add(origin)
    db_session.flush()
    run = jobs.create_run(db_session, "generate_report", project, {
        "research_id": draft.id, "analysis_id": report_analysis.id, "semantic_mode": "off",
        "report_mode": "statistical", "source_message_id": origin.id}, conversation.id)
    origin.run_id = run.id
    db_session.flush()
    run_id, draft_id, report_id = run.id, draft.id, report.id
    original_outcome = jobs._outcome_message
    provisional_ids = []

    def fail_after_publication(session, context, result):
        provisional = session.get(ReportVersion, result["report_version_id"])
        research = session.get(ResearchVersion, draft_id)
        current_report = session.get(Report, report_id)
        assert provisional.research_id == draft_id
        assert research.number == 2 and research.status == "published"
        assert research.current_revision_id == current_report.current_version_id == provisional.id
        assert current_report.research_counter == 2 and current_report.version_counter == 2
        provisional_ids.append(provisional.id)
        original_outcome(session, context, result)
        session.flush()
        raise RuntimeError("Controlled outcome failure after provisional publication")

    monkeypatch.setattr(jobs, "_outcome_message", fail_after_publication)
    jobs.execute_run(run_id)
    db_session.expire_all()
    failed = db_session.get(ResearchRun, run_id)
    restored_report = db_session.get(Report, report_id)
    restored_draft = db_session.get(ResearchVersion, draft_id)
    assert failed.status == "failed" and failed.result == {}
    assert len(provisional_ids) == 1 and db_session.get(ReportVersion, provisional_ids[0]) is None
    assert restored_report.current_version_id == first["report_version_id"]
    assert restored_report.current_research_id == first["research_id"]
    assert restored_report.research_counter == 1 and restored_report.version_counter == 1
    assert restored_draft.status == "draft" and restored_draft.number is None
    assert restored_draft.current_revision_id is None and restored_draft.initial_revision_id is None
    assert db_session.scalar(select(func.count()).select_from(ReportVersion).where(
        ReportVersion.report_id == report_id)) == 1
    assert db_session.scalar(select(Message.id).where(Message.run_id == run_id, Message.role == "assistant")) is None
    events = _events(db_session, run_id)
    assert any(event.event_type == "run.failed" for event in events)
    assert not any(event.event_type == "artifact.ready" or
                   event.event_type == "run.completed" and event.payload.get("status") == "succeeded" for event in events)
    assert not any(event.payload.get("report_version_id") in provisional_ids for event in events)
    checkpoints = {step.key: step for step in db_session.scalars(select(RunStep).where(RunStep.run_id == run_id))}
    assert all(checkpoints[key].status == "succeeded" for key in (
        "report_target_snapshot", "report_snapshot", "report_candidate"))
    assert checkpoints["report_candidate"].result["research_id"] == draft_id
    assert checkpoints["report_candidate"].result["content"]["title"]
    monkeypatch.setattr(jobs, "_outcome_message", original_outcome)
    monkeypatch.setattr(report_service, "_load_snapshot", lambda ctx: pytest.fail("Retry recomputed a saved report snapshot"))
    headers = {"Idempotency-Key": "retry-publication-once"}
    retry = client.post(f"/api/runs/{run_id}/retry", headers=headers)
    repeated = client.post(f"/api/runs/{run_id}/retry", headers=headers)
    assert retry.status_code == repeated.status_code == 202, retry.text
    assert repeated.json() == retry.json()
    retry_id = retry.json()["run_id"]
    jobs.execute_run(retry_id)
    jobs.execute_run(retry_id)
    db_session.expire_all()
    completed = db_session.get(ResearchRun, retry_id)
    assert completed.status == "succeeded", completed.error
    published = db_session.get(ReportVersion, completed.result["report_version_id"])
    saved_draft = db_session.get(ResearchVersion, draft_id)
    adopted_report = db_session.get(Report, report_id)
    assert saved_draft.number == 2 and saved_draft.status == "published"
    assert published.research_id == draft_id and published.operation_id == failed.payload["operation_id"]
    assert published.number == 2 and published.parent_id is None
    assert adopted_report.current_version_id == saved_draft.current_revision_id == published.id
    assert adopted_report.current_research_id == draft_id
    assert adopted_report.research_counter == adopted_report.version_counter == 2
    assert db_session.scalar(select(func.count()).select_from(ReportVersion).where(
        ReportVersion.research_id == draft_id)) == 1
    assert db_session.get(ReportVersion, first["report_version_id"]).content == original_content
    success_events = _events(db_session, retry_id)
    assert len([event for event in success_events if event.event_type == "artifact.ready"]) == 1
    assert len([event for event in success_events if event.event_type == "run.completed"
                and event.payload.get("status") == "succeeded"]) == 1
    assert any(event.event_type == "step.updated" and event.payload.get("reused") for event in success_events)


def test_explicit_report_revision_with_conversation_records_source_without_asking_execution_again(
        client, db_session, project, first):
    ordinary = Conversation(project_id=project.id, title="页面所在对话")
    db_session.add(ordinary)
    db_session.flush()
    response = client.post(f"/api/reports/{first['report_id']}/revisions", json={
        "research_id": first["research_id"], "base_version_id": first["report_version_id"],
        "expected_current_version_id": first["report_version_id"], "conversation_id": ordinary.id,
        "operations": [{"op": "set_title", "text": "页面直接确认的新标题"}]})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    db_session.expire_all()
    queued = db_session.get(ResearchRun, run_id)
    source_id = queued.payload["source_message_id"]
    assert not queued.payload.get("message_id")
    source = db_session.get(Message, source_id)
    assert source.role == "user" and source.run_id == run_id
    jobs.execute_run(run_id)
    completed = client.get(f"/api/runs/{run_id}").json()
    assert completed["status"] == "succeeded", completed
    assert completed["input_request"] is None
    assert not any(event.event_type == "input.required" for event in _events(db_session, run_id))
    assert completed["result"]["source_message_id"] == source_id
    version = db_session.get(ReportVersion, completed["result"]["report_version_id"])
    assert version.content["title"] == "页面直接确认的新标题"
    assert version.source_message_id == source_id and version.parent_id == first["report_version_id"]
    assert db_session.get(ResearchVersion, first["research_id"]).current_revision_id == version.id
    messages = list(db_session.scalars(select(Message).where(Message.run_id == run_id)))
    assert len([message for message in messages if message.role == "user"]) == 1
    assert len([message for message in messages if message.role == "assistant"]) == 1


def _changed_working_context(db_session, project, context_factory, first, report_analysis):
    head = db_session.get(ReportVersion, first["report_version_id"])
    research = db_session.get(ResearchVersion, first["research_id"])
    scope = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "电池", "goal": "尚未写入报告的新研究"})
    original_data = db_session.get(DatasetVersion, report_analysis.dataset_version_ids[0])
    data = DatasetVersion(dataset_id=original_data.dataset_id, source_file_id=original_data.source_file_id,
                          status="ready", quality={"application_count": 99})
    db_session.add_all([scope, data])
    db_session.flush()
    analysis_context = context_factory(project, "analyze", {})
    analysis = AnalysisResult(project_id=project.id, run_id=analysis_context.run_id,
                              scope_version_id=scope.id, dataset_version_ids=[data.id], patent_ids=[],
                              summary={"application_count": 99, "publication_count": 99}, views={})
    db_session.add(analysis)
    db_session.flush()
    set_context(db_session, research, scope_id=scope.id, dataset_ids=[data.id], analysis_id=analysis.id)
    db_session.flush()
    return research, head, scope, data, analysis


def test_direct_chat_pins_report_head_even_when_research_working_context_has_changed(
        client, db_session, project, context_factory, first, report_analysis):
    research, head, scope, data, analysis = _changed_working_context(
        db_session, project, context_factory, first, report_analysis)
    conversation = ensure_conversation(db_session, research)
    response = client.post(f"/api/conversations/{conversation.id}/messages", json={
        "content": "解释目前报告中的研究依据", "action": "chat", "research_id": research.id})
    assert response.status_code == 202, response.text
    db_session.expire_all()
    run = db_session.get(ResearchRun, response.json()["run_id"])
    assert run.scope_version_id == head.scope_version_id
    assert run.dataset_version_ids == head.dataset_version_ids
    assert run.payload["analysis_id"] == head.analysis_id
    assert run.payload["reference_version_id"] == head.id
    saved = db_session.get(ResearchVersion, research.id)
    assert (saved.scope_version_id, saved.dataset_version_ids, saved.analysis_id) == (scope.id, [data.id], analysis.id)
    assert db_session.get(type(project), project.id).current_analysis_id == report_analysis.id


def test_analysis_without_explicit_research_defaults_to_adopted_research_working_context(
        db_session, project, context_factory, first, report_analysis):
    research, head, scope, data, analysis = _changed_working_context(
        db_session, project, context_factory, first, report_analysis)
    run = jobs.create_run(db_session, "analyze", project, {"semantic_mode": "off"})
    assert run.payload["research_id"] == research.id
    assert run.scope_version_id == scope.id and run.dataset_version_ids == [data.id]
    assert run.payload["analysis_id"] == analysis.id
    assert run.scope_version_id != head.scope_version_id and run.dataset_version_ids != head.dataset_version_ids
    assert db_session.get(type(project), project.id).current_analysis_id == report_analysis.id
