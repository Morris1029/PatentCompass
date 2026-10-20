from datetime import timedelta
from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import data_service, jobs
from app.config import get_settings
from app.errors import AppError
from app.main import create_app
from app.models import (AnalysisResult, Conversation, Message, Report, ReportVersion, ResearchRun,
                        RunEvent, RunStep, ScopeVersion, new_id, utcnow)
from app.worker import celery_app


@pytest.fixture(autouse=True)
def no_heartbeat_thread(monkeypatch):
    # The shared savepoint fixture uses one connection. Heartbeats are tested by
    # lease/fencing behavior below instead of sharing that connection across threads.
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


def test_waiting_scope_resumes_same_run_after_confirming_input(db_session, project, monkeypatch, client):
    db_session.get(ScopeVersion, project.scope_version_id).confirmed = False
    run = jobs.create_run(db_session, "analyze", project, {"example": "original"})
    db_session.flush()
    original_id, scope_id = run.id, project.scope_version_id
    jobs.execute_run(run.id)
    db_session.expire_all()
    assert run.status == "waiting_input"
    assert run.lease_token is None and run.input_request["kind"] == "scope_confirmation"
    response = client.post(f"/api/runs/{run.id}/inputs", json={
        "input_request_id": run.input_request["id"], "expected_scope_version_id": scope_id,
        "values": {"confirmed": True, "scope": {"goal": "研究自主降落"}}})
    assert response.status_code == 202, response.text
    db_session.expire_all()
    assert run.status == "queued" and run.input_request is None
    assert run.id == original_id and run.scope_version_id != scope_id
    assert db_session.get(ScopeVersion, run.scope_version_id).confirmed
    monkeypatch.setattr(data_service, "analyze", lambda ctx: {"completed": True})
    jobs.execute_run(run.id)
    db_session.expire_all()
    assert run.status == "succeeded" and run.attempt == 2
    assert run.result == {"completed": True}


def test_cancel_queued_job_never_calls_handler(db_session, project, monkeypatch, client):
    run = jobs.create_run(db_session, "analyze", project)
    db_session.flush()
    calls = []
    monkeypatch.setattr(data_service, "analyze", lambda ctx: calls.append(ctx.run_id))
    response = client.post(f"/api/runs/{run.id}/cancel")
    assert response.status_code == 200, response.text
    jobs.execute_run(run.id)
    db_session.expire_all()
    assert run.status == "cancelled" and run.cancellation_requested
    assert not run.dispatch_pending and calls == []


def test_cancel_running_step_keeps_completed_checkpoint(db_session, project, monkeypatch):
    run = jobs.create_run(db_session, "analyze", project)
    db_session.flush()

    def handler(ctx):
        ctx.step("completed", lambda: {"value": 7})

        def cancelled():
            db_session.expire_all()
            row = db_session.get(ResearchRun, ctx.run_id)
            row.cancellation_requested, row.status = True, "cancelling"
            db_session.flush()
            ctx.check_cancelled()

        ctx.step("pending", cancelled)

    monkeypatch.setattr(data_service, "analyze", handler)
    jobs.execute_run(run.id)
    db_session.expire_all()
    assert run.status == "cancelled" and run.error is None
    steps = {step.key: step for step in db_session.scalars(select(RunStep).where(RunStep.run_id == run.id))}
    assert steps["completed"].status == "succeeded" and steps["completed"].result == {"value": 7}
    assert steps["pending"].status == "cancelled"


def test_duplicate_delivery_cannot_execute_succeeded_job_again(db_session, project, monkeypatch):
    run = jobs.create_run(db_session, "analyze", project)
    db_session.flush()
    calls = []

    def handler(ctx):
        calls.append(ctx.run_id)
        return ctx.step("calculation", lambda: {"value": 3})

    monkeypatch.setattr(data_service, "analyze", handler)
    jobs.execute_run(run.id)
    jobs.execute_run(run.id)
    db_session.expire_all()
    assert calls == [run.id] and run.status == "succeeded" and run.attempt == 1
    assert len(list(db_session.scalars(select(RunStep).where(RunStep.run_id == run.id)))) == 1


def test_failed_retry_reuses_checkpoint_and_original_scope(db_session, project, context_factory, monkeypatch, client):
    original = context_factory(project, "analyze", {"statistic": "yearly"})
    original.step("calculate", lambda: {"value": 520})
    run = db_session.get(ResearchRun, original.run_id)
    run.status, run.lease_token, run.lease_expires_at = "failed", None, None
    db_session.flush()
    frozen_scope = run.scope_version_id
    new_scope = ScopeVersion(project_id=project.id, number=2, content={"industry": "电池"}, confirmed=True)
    db_session.add(new_scope)
    db_session.flush()
    project.scope_version_id = new_scope.id
    db_session.flush()
    headers = {"Idempotency-Key": "retry-once"}
    first = client.post(f"/api/runs/{run.id}/retry", headers=headers)
    repeated = client.post(f"/api/runs/{run.id}/retry", headers=headers)
    assert first.status_code == 202, first.text
    assert repeated.json() == first.json()
    retry_id = first.json()["run_id"]

    def handler(ctx):
        assert ctx.scope_version_id == frozen_scope
        return ctx.step("calculate", lambda: pytest.fail("A completed compatible checkpoint was recalculated"))

    monkeypatch.setattr(data_service, "analyze", handler)
    jobs.execute_run(retry_id)
    db_session.expire_all()
    retried = db_session.get(ResearchRun, retry_id)
    assert retried.status == "succeeded" and retried.result == {"value": 520}
    assert retried.retry_of_id == run.id and retried.scope_version_id == frozen_scope
    events = list(db_session.scalars(select(RunEvent).where(RunEvent.run_id == retry_id)))
    assert any(event.payload.get("reused") for event in events)


def test_dispatch_survives_broker_outage_and_later_sends_pending_job(db_session, project, monkeypatch):
    run = jobs.create_run(db_session, "analyze", project)
    db_session.flush()

    def unavailable(*args, **kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(celery_app, "send_task", unavailable)
    jobs.dispatch_pending()
    db_session.expire_all()
    assert run.status == "queued" and run.dispatch_pending and run.dispatched_at is None
    sent = []
    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: sent.append((args, kwargs)))
    jobs.dispatch_pending()
    jobs.dispatch_pending()
    db_session.expire_all()
    assert len(sent) == 1
    assert sent[0][1]["args"] == [run.id] and sent[0][1]["task_id"] == run.id
    assert not run.dispatch_pending and run.dispatched_at is not None


def test_lease_recovery_fences_old_worker_and_reuses_completed_step(db_session, project, context_factory, monkeypatch):
    stale = context_factory(project, "analyze", {})
    stale.step("saved", lambda: {"value": 11})
    run = db_session.get(ResearchRun, stale.run_id)
    run.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.flush()
    sent = []
    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: sent.append(kwargs["args"][0]))
    jobs.dispatch_pending()
    db_session.expire_all()
    assert sent == [run.id] and run.status == "queued" and run.lease_token is None
    with pytest.raises(jobs.LeaseLost):
        stale.check_cancelled()

    def handler(ctx):
        assert ctx.lease_token != stale.lease_token
        return ctx.step("saved", lambda: pytest.fail("Recovery lost the completed checkpoint"))

    monkeypatch.setattr(data_service, "analyze", handler)
    jobs.execute_run(run.id)
    db_session.expire_all()
    assert run.status == "succeeded" and run.result == {"value": 11}
    with pytest.raises(jobs.LeaseLost):
        stale.emit("artifact.ready", {"late": True})


def test_heartbeat_cannot_revive_expired_or_replace_another_lease(db_session, project, context_factory):
    from app.jobs import _heartbeat
    context = context_factory(project)
    run = db_session.get(ResearchRun, context.run_id)

    class OneHeartbeat:
        def __init__(self):
            self.count = 0

        def wait(self, seconds):
            self.count += 1
            return self.count > 1

    expired = utcnow() - timedelta(seconds=1)
    run.lease_expires_at = expired
    db_session.flush()
    _heartbeat(run.id, context.lease_token, OneHeartbeat())
    db_session.expire_all()
    assert run.lease_expires_at == expired
    run.lease_token = new_id()
    future = utcnow() + timedelta(hours=1)
    run.lease_expires_at = future
    db_session.flush()
    _heartbeat(run.id, context.lease_token, OneHeartbeat())
    db_session.expire_all()
    assert run.lease_expires_at == future


def test_failed_handler_stores_error_and_does_not_claim_success(db_session, project, monkeypatch):
    run = jobs.create_run(db_session, "analyze", project)
    db_session.flush()

    def handler(ctx):
        raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置模型")

    monkeypatch.setattr(data_service, "analyze", handler)
    jobs.execute_run(run.id)
    db_session.expire_all()
    assert run.status == "failed" and run.result == {}
    assert run.error == {"code": "MODEL_NOT_CONFIGURED", "message": "尚未配置模型", "status_code": 503}
    assert run.lease_token is None and run.lease_expires_at is None


def _chat_context(db_session, project, context_factory, content):
    from app.conversation_execution import execution_context
    conversation = Conversation(project_id=project.id, title=content)
    db_session.add(conversation)
    db_session.flush()
    message = Message(conversation_id=conversation.id, role="user", content=content)
    db_session.add(message)
    db_session.flush()
    context = context_factory(project, "chat", {"message_id": message.id})
    run = db_session.get(ResearchRun, context.run_id)
    run.conversation_id = conversation.id
    message.run_id = run.id
    run.payload = {**run.payload, "execution_authorization": {
        "context": execution_context(db_session, run), "allowed_actions": ["search_patents", "search_materials"]}}
    db_session.flush()
    context.payload = dict(run.payload)
    context.conversation_id = conversation.id
    return context, message


@pytest.fixture
def configured_chat(monkeypatch, buffered_chat_stream):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", "https://controlled.invalid/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test")
    monkeypatch.setattr(settings, "llm_model", "controlled-model")


def test_chat_tool_roundtrip_saves_original_evidence_and_message(db_session, project, context_factory,
                                                              monkeypatch, configured_chat):
    from app import model_provider
    context, anchor = _chat_context(db_session, project, context_factory, "请梳理无人机降落技术")
    captured_queries, captured_prompts = [], []
    record = SimpleNamespace(id=new_id(), application_number="PCT/CONTROLLED/1", title="UAV landing",
        abstract="A visual sensor estimates the landing position.", source_sheet="Patents", source_row=2,
        dataset_version_id=new_id())

    def search(session, supplied_project, filters):
        captured_queries.append(deepcopy(filters))
        return {"items": [record], "scope_application_count": 520}

    def model(prompt, tools=None):
        captured_prompts.append(deepcopy(prompt))
        assert tools[0]["function"]["name"] == "search_patents"
        if len(captured_prompts) == 1:
            return {"content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "search_patents", "arguments": json.dumps({"q": "landing"})}}]}
        tool_result = json.loads(prompt[-1]["content"])
        assert prompt[-1]["role"] == "tool" and prompt[-1]["tool_call_id"] == "call-1"
        assert tool_result["matched_count"] == 1
        assert tool_result["records"][0]["abstract"] == record.abstract
        return {"content": "PCT/CONTROLLED/1 使用视觉传感器估计降落位置，依据为该条摘要。"}

    monkeypatch.setattr(data_service, "search_patents", search)
    monkeypatch.setattr(model_provider, "chat", model)
    result = jobs._chat(context)
    assert jobs._chat(context) == result
    assert len(captured_prompts) == 2 and len(captured_queries) == 1
    assert captured_queries[0]["scope_version_id"] == context.scope_version_id
    assert captured_queries[0]["dataset_version_ids"] == context.dataset_version_ids
    db_session.expire_all()
    assistant = db_session.get(Message, result["message_id"])
    assert assistant.role == "assistant" and assistant.run_id == context.run_id
    assert assistant.reference["citations"][0]["abstract"] == record.abstract
    assert assistant.reference["citations"][0]["source_row"] == 2
    assert assistant.reference["scope_version_id"] == context.scope_version_id
    events = list(db_session.scalars(select(RunEvent).where(RunEvent.run_id == context.run_id)))
    assert any(event.event_type == "evidence.ready" for event in events)
    deltas = sorted((event for event in events if event.event_type == "message.delta"), key=lambda event: event.seq)
    assert "".join(event.payload["delta"] for event in deltas) == assistant.content
    assert any(event.event_type == "message.completed" for event in events)


def test_chat_excludes_messages_sent_after_its_own_anchor(db_session, project, context_factory,
                                                        monkeypatch, configured_chat):
    from app import model_provider
    context, anchor = _chat_context(db_session, project, context_factory, "Question A about landing")
    future = Message(conversation_id=anchor.conversation_id, role="user", content="Question B about batteries",
                     created_at=anchor.created_at + timedelta(seconds=1))
    db_session.add(future)
    db_session.flush()
    prompts = []

    def model(prompt, tools=None):
        prompts.append(deepcopy(prompt))
        return {"content": "Answer to question A"}

    monkeypatch.setattr(model_provider, "chat", model)
    jobs._chat(context)
    assert prompts[0][-1]["content"] == anchor.content
    assert not any("Question B" in str(message.get("content")) for message in prompts[0])


def test_historical_report_chat_freezes_scope_data_and_analysis(db_session, project, context_factory,
                                                              monkeypatch, client, configured_chat):
    from app import model_provider
    old_scope = project.scope_version_id
    old_dataset = new_id()
    analysis_context = context_factory(project, "analyze", {})
    analysis = AnalysisResult(project_id=project.id, run_id=analysis_context.run_id,
        scope_version_id=old_scope, dataset_version_ids=[old_dataset], patent_ids=[new_id()],
        summary={"application_count": 520}, views={})
    db_session.add(analysis)
    report = Report(project_id=project.id, version_counter=1)
    db_session.add(report)
    db_session.flush()
    version_context = context_factory(project, "generate_report", {})
    version = ReportVersion(report_id=report.id, number=1, run_id=version_context.run_id,
        analysis_id=analysis.id, scope_version_id=old_scope, dataset_version_ids=[old_dataset],
        content={"title": "Historical report", "blocks": [{"id": "old-block", "type": "paragraph", "text": "Old record"}]})
    db_session.add(version)
    db_session.flush()
    report.current_version_id = version.id
    newer = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "电池"})
    db_session.add(newer)
    db_session.flush()
    project.scope_version_id = newer.id
    db_session.flush()
    response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
        "content": "请解释旧报告依据", "reference": {"report_version_id": version.id,
                                                 "block_id": "old-block", "text": "Old record"}}})
    assert response.status_code == 202, response.text
    db_session.expire_all()
    run = db_session.get(ResearchRun, response.json()["run_id"])
    assert run.scope_version_id == old_scope and run.dataset_version_ids == [old_dataset]
    assert run.payload["analysis_id"] == analysis.id and run.payload["reference_version_id"] == version.id
    run.status, run.lease_token, run.lease_expires_at = "running", new_id(), utcnow() + timedelta(hours=1)
    from app.conversation_execution import execution_context
    run.payload = {**run.payload, "execution_authorization": {
        "context": execution_context(db_session, run), "allowed_actions": ["search_patents", "search_materials"]}}
    db_session.flush()
    context = jobs.RunContext(run.id, run.lease_token)
    calls = []

    def model(prompt, tools=None):
        assert any("Historical report" in str(message.get("content")) for message in prompt)
        if len(calls) == 0:
            return {"content": None, "tool_calls": [{"id": "call-old", "type": "function", "function": {
                "name": "search_patents", "arguments": "{}"}}]}
        return {"content": "旧报告分析了520条申请。"}

    def search(session, supplied_project, filters):
        calls.append(deepcopy(filters))
        return {"items": [], "scope_application_count": 520}

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(data_service, "search_patents", search)
    result = jobs._chat(context)
    assert calls == [{"q": "", "scope_version_id": old_scope, "dataset_version_ids": [old_dataset],
                      "mode": "keywords", "application_ids": analysis.patent_ids}]
    db_session.expire_all()
    assert db_session.get(Message, result["message_id"]).reference["report_version_id"] == version.id


def test_chat_synonym_queries_union_records_and_keep_per_query_counts(db_session, project, context_factory,
                                                                    monkeypatch, configured_chat):
    from app import model_provider
    context, _ = _chat_context(db_session, project, context_factory, "检索自主降落")
    record = SimpleNamespace(id=new_id(), application_number="PCT/CONTROLLED/2", title="Autonomous landing",
        abstract="An autonomous landing method.", source_sheet="Patents", source_row=2, dataset_version_id=new_id())
    queries, outputs = [], []

    def search(session, supplied_project, filters):
        queries.append(filters["q"])
        return {"items": [record], "scope_application_count": 520}

    def model(prompt, tools=None):
        if not outputs:
            outputs.append("searched")
            return {"content": None, "tool_calls": [{"id": "union", "type": "function", "function": {
                "name": "search_patents", "arguments": '{"queries":["autonomous landing","landing"]}'}}]}
        result = json.loads(prompt[-1]["content"])
        assert result["matched_count"] == 1
        assert len(result["query_counts"]) == 2 and len(result["records"]) == 1
        return {"content": "两式并集共1条申请，依据为PCT/CONTROLLED/2原文。"}

    monkeypatch.setattr(data_service, "search_patents", search)
    monkeypatch.setattr(model_provider, "chat", model)
    jobs._chat(context)
    assert queries == ["autonomous landing", "landing"]


def test_chat_final_turn_closes_tools_and_answers_from_observed_evidence(db_session, project, context_factory,
                                                                      monkeypatch, configured_chat):
    from app import model_provider
    context, _ = _chat_context(db_session, project, context_factory, "检索并说明证据边界")
    turns = []

    def model(prompt, tools=None):
        turns.append(tools)
        if len(turns) < 4:
            return {"content": None, "tool_calls": [{"id": f"call-{len(turns)}", "type": "function", "function": {
                "name": "search_patents", "arguments": '{"q":"landing"}'}}]}
        assert tools is None and "预算已用完" in prompt[-1]["content"]
        return {"content": "目前检索未取得证据，不能据此证明技术不存在。"}

    monkeypatch.setattr(data_service, "search_patents", lambda *args: {"items": [], "scope_application_count": 520})
    monkeypatch.setattr(model_provider, "chat", model)
    result = jobs._chat(context)
    assert len(turns) == 4 and result["message_id"]
