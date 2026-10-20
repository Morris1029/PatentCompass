"""Concurrency and interrupted-run regressions for constrained research plans."""
import pytest
from fastapi.testclient import TestClient

from app import jobs, model_provider, orchestration, report_service, semantic_service
from app.config import get_settings
from app.jobs import RunContext
from app.main import create_app
from app.models import AnalysisResult, Conversation, DatasetVersion, Message, Report, ReportVersion, ResearchRun, ScopeVersion, new_id
from test_orchestration import _confirm_execution, _message, _plan, _ready_data
from test_reports import report_analysis


@pytest.fixture
def client(db_session, monkeypatch):
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


def test_dataset_wait_does_not_silently_replace_concurrent_adoption(client, db_session, project, monkeypatch):
    first = _ready_data(db_session, project)
    second = DatasetVersion(dataset_id=first.dataset_id, source_file_id=first.source_file_id,
                            status="ready", quality={"application_count": 1}, source_metadata={})
    db_session.add(second)
    db_session.flush()
    _plan(monkeypatch)
    task = _message(client, project)
    jobs.execute_run(task["run_id"])
    waiting = client.get(f"/api/runs/{task['run_id']}").json()
    assert waiting["input_request"]["kind"] == "dataset_selection"
    assert waiting["input_request"]["expected_project_revision"] == 0
    adopted = client.put(f"/api/projects/{project.id}/datasets", json={
        "expected_revision": 0, "dataset_version_ids": [first.id]})
    assert adopted.status_code == 200
    body = {"input_request_id": waiting["input_request"]["id"], "values": {"dataset_version_id": second.id}}
    rejected = client.post(f"/api/runs/{task['run_id']}/inputs", json=body)
    assert rejected.status_code == 409 and rejected.json()["code"] == "PROJECT_REVISION_CONFLICT"
    assert client.get(f"/api/projects/{project.id}").json()["dataset_version_id"] == first.id
    assert client.get(f"/api/runs/{task['run_id']}").json()["status"] == "waiting_input"
    body["values"].update({"confirm_project_change": True, "expected_revision": 0})
    assert client.post(f"/api/runs/{task['run_id']}/inputs", json=body).status_code == 409
    body["values"]["expected_revision"] = adopted.json()["revision"]
    accepted = client.post(f"/api/runs/{task['run_id']}/inputs", json=body)
    assert accepted.status_code == 202, accepted.text
    latest = client.get(f"/api/projects/{project.id}").json()
    assert latest["dataset_version_id"] == second.id and latest["revision"] == 2
    assert accepted.json()["run_id"] == task["run_id"]


def test_scope_wait_requires_explicit_acknowledgement_of_concurrent_scope(client, db_session, project, monkeypatch):
    version = _ready_data(db_session, project)
    project.dataset_version_id = version.id
    db_session.get(ScopeVersion, project.scope_version_id).confirmed = False
    db_session.flush()
    _plan(monkeypatch)
    task = _message(client, project)
    jobs.execute_run(task["run_id"])
    waiting = client.get(f"/api/runs/{task['run_id']}").json()
    assert waiting["input_request"]["kind"] == "scope_confirmation"
    concurrent = client.patch(f"/api/projects/{project.id}/scope", json={
        "expected_scope_version_id": waiting["scope_version_id"],
        "changes": {"goal": "Concurrent research goal", "q": "drone"}, "confirmed": True})
    assert concurrent.status_code == 200
    # A polling UI may know the latest ID while still displaying an older form.
    body = {"input_request_id": waiting["input_request"]["id"], "expected_scope_version_id": concurrent.json()["id"],
            "values": {"confirmed": True, "scope": {"goal": "Outdated form goal"}}}
    rejected = client.post(f"/api/runs/{task['run_id']}/inputs", json=body)
    assert rejected.status_code == 409 and rejected.json()["code"] == "SCOPE_VERSION_CONFLICT"
    current = client.get(f"/api/projects/{project.id}").json()
    assert current["scope"]["content"]["goal"] == "Concurrent research goal"
    body["values"] = {"confirmed": True, "confirm_scope_change": True, "scope": {"focus": "Explicitly reviewed"}}
    accepted = client.post(f"/api/runs/{task['run_id']}/inputs", json=body)
    assert accepted.status_code == 202, accepted.text
    current = client.get(f"/api/projects/{project.id}").json()
    assert current["scope"]["content"]["goal"] == "Concurrent research goal"
    assert current["scope"]["content"]["focus"] == "Explicitly reviewed"
    assert current["scope"]["content"]["q"] == "drone"


def test_auto_report_reuses_full_analysis_without_statistical_downgrade(client, db_session, project, context_factory, monkeypatch):
    version = _ready_data(db_session, project)
    project.dataset_version_id = version.id
    db_session.flush()
    result = jobs.run_analysis(context_factory(project, "analyze", {}))
    existing = db_session.get(AnalysisResult, result["analysis_id"])
    existing.summary = {**existing.summary, "semantic": {"processed_count": 1, "classified_count": 1, "review_count": 0}}
    db_session.flush()
    _plan(monkeypatch, action="generate_report")
    monkeypatch.setattr(jobs, "run_analysis", lambda ctx: pytest.fail("Full frozen analysis must not be replaced with statistics"))
    monkeypatch.setattr(semantic_service, "analyze", lambda ctx: pytest.fail("Matching full analysis must not be recomputed"))
    observed = []

    def report(ctx):
        observed.append(ctx.payload["analysis_id"])
        return {"analysis_id": ctx.payload["analysis_id"], "report_version_id": new_id()}

    monkeypatch.setattr(report_service, "generate_report", report)
    task = _message(client, project, "根据现有分析生成报告")
    jobs.execute_run(task["run_id"])
    _confirm_execution(client, task)
    completed = client.get(f"/api/runs/{task['run_id']}").json()
    assert completed["status"] == "succeeded", completed
    assert observed == [existing.id]


def test_global_title_operation_can_confirm_new_version_without_body_target(client, db_session, project, context_factory, report_analysis, monkeypatch):
    first = report_service.generate_report(context_factory(project, "generate_report", {"analysis_id": report_analysis.id}))
    v1 = db_session.get(ReportVersion, first["report_version_id"])
    second = report_service.revise_report(context_factory(project, "revise_report", {
        "report_id": first["report_id"], "base_version_id": v1.id, "expected_current_version_id": v1.id,
        "operations": [{"op": "set_title", "text": "Current title"}]}))
    _plan(monkeypatch, action="revise_report", operations=[{"op": "set_title", "text": "Confirmed title"}])
    task = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
        "content": "修改报告标题", "action": "auto", "reference": {"report_version_id": v1.id}}}).json()
    jobs.execute_run(task["run_id"])
    waiting = client.get(f"/api/runs/{task['run_id']}").json()
    assert waiting["input_request"]["kind"] == "report_target"
    accepted = client.post(f"/api/runs/{task['run_id']}/inputs", json={"input_request_id": waiting["input_request"]["id"],
        "values": {"confirmed": True, "base_version_id": second["report_version_id"],
                   "expected_current_version_id": second["report_version_id"], "block_ids": []}})
    assert accepted.status_code == 202, accepted.text
    jobs.execute_run(task["run_id"])
    _confirm_execution(client, task)
    completed = client.get(f"/api/runs/{task['run_id']}").json()
    assert completed["status"] == "succeeded", completed
    revised = db_session.get(ReportVersion, completed["result"]["report_version_id"])
    assert revised.content["title"] == "Confirmed title" and revised.parent_id == second["report_version_id"]


def test_chat_commit_completion_gap_reuses_original_message(client, db_session, project, monkeypatch, buffered_chat_stream):
    for field in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(get_settings(), field, "controlled")
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        return {"content": "Controlled committed answer"}

    monkeypatch.setattr(model_provider, "chat", chat)
    task = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {"content": "解释现有范围"}}).json()
    jobs.execute_run(task["run_id"])
    original_result = client.get(f"/api/runs/{task['run_id']}").json()["result"]
    db_session.expire_all()
    db_session.get(ResearchRun, task["run_id"]).status = "failed"
    db_session.flush()
    retry = client.post(f"/api/runs/{task['run_id']}/retry").json()
    jobs.execute_run(retry["run_id"])
    messages = client.get(f"/api/conversations/{task['conversation_id']}/messages").json()["items"]
    assert len([message for message in messages if message["role"] == "assistant"]) == 1
    assert client.get(f"/api/runs/{retry['run_id']}").json()["result"] == original_result
    assert len(calls) == 1


def test_explicit_report_edit_button_with_conversation_does_not_ask_again(
        client, db_session, project, context_factory, report_analysis):
    first = report_service.generate_report(context_factory(project, "generate_report", {"analysis_id": report_analysis.id}))
    conversation = Conversation(project_id=project.id, title="报告编辑")
    db_session.add(conversation)
    db_session.flush()
    edited = client.post(f"/api/reports/{first['report_id']}/revisions", json={
        "base_version_id": first["report_version_id"], "expected_current_version_id": first["report_version_id"],
        "conversation_id": conversation.id, "operations": [{"op": "set_title", "text": "明确点击修改"}]})
    assert edited.status_code == 202, edited.text
    task = edited.json()
    jobs.execute_run(task["run_id"])
    completed = client.get(f"/api/runs/{task['run_id']}").json()
    assert completed["status"] == "succeeded", completed
    assert completed["input_request"] is None and completed["activities"][0]["kind"] == "revise_report"
    origin_id = completed["origin_message_id"]
    assert db_session.get(Message, origin_id).role == "user"
    db_session.get(ResearchRun, task["run_id"]).status = "failed"
    db_session.flush()
    retried = client.post(f"/api/runs/{task['run_id']}/retry").json()
    jobs.execute_run(retried["run_id"])
    repeated = client.get(f"/api/runs/{retried['run_id']}").json()
    assert repeated["status"] == "succeeded" and repeated["origin_message_id"] == origin_id
    main_conversation_id = repeated["result"]["main_conversation_id"]
    messages = client.get(f"/api/conversations/{main_conversation_id}/messages").json()["items"]
    assert len(messages) == 2 and all(message["run_id"] == retried["run_id"] for message in messages)


def test_plan_commit_before_checkpoint_gap_keeps_original_report_pin(client, db_session, project, context_factory, report_analysis, monkeypatch):
    first = report_service.generate_report(context_factory(project, "generate_report", {"analysis_id": report_analysis.id}))
    v1 = db_session.get(ReportVersion, first["report_version_id"])
    second = report_service.revise_report(context_factory(project, "revise_report", {
        "report_id": first["report_id"], "base_version_id": v1.id, "expected_current_version_id": v1.id,
        "operations": [{"op": "set_title", "text": "Later adopted title"}]}))
    report = db_session.get(Report, first["report_id"])
    report.current_version_id = v1.id
    from app.models import ResearchVersion
    research = db_session.get(ResearchVersion, first["research_id"])
    research.current_revision_id = v1.id
    db_session.flush()
    task = _message(client, project, "修改报告标题")
    payload = dict(db_session.get(ResearchRun, task["run_id"]).payload)
    original = context_factory(project, "auto", payload)
    original_step = original.step
    calls = _plan(monkeypatch, action="revise_report", operations=[{"op": "set_title", "text": "Wanted title"}])

    def interrupted_step(key, callback):
        def after_callback_commit():
            callback()  # Real callback atomically commits the model plan and original pins.
            raise RuntimeError("Simulated crash before the checkpoint is marked complete")
        return original_step(key, after_callback_commit)

    original.step = interrupted_step
    with pytest.raises(RuntimeError):
        orchestration._prepare_plan(original)
    db_session.expire_all()
    old = db_session.get(ResearchRun, original.run_id)
    old.status = "failed"
    assert old.payload["plan_report_version_id"] == v1.id
    report.current_version_id = second["report_version_id"]
    research.current_revision_id = second["report_version_id"]
    db_session.flush()
    retry = client.post(f"/api/runs/{old.id}/retry").json()
    retry_context = RunContext(retry["run_id"], "unused-before-claim")
    plan = orchestration._prepare_plan(retry_context)
    assert plan["action"] == "revise_report"
    assert retry_context.payload["plan_report_version_id"] == v1.id
    assert retry_context.payload["plan_report_current_id"] == v1.id
    assert len(calls) == 1


def test_cancelled_material_can_be_selected_again_and_parsed(client, project):
    from io import BytesIO
    from docx import Document

    document = Document()
    document.add_paragraph("无人机研究需求")
    stream = BytesIO()
    document.save(stream)
    content = stream.getvalue()
    first_response = client.post(f"/api/projects/{project.id}/materials", files={"file": ("needs.docx", content)})
    assert first_response.status_code == 202
    first = first_response.json()
    cancelled = client.post(f"/api/runs/{first['run_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    old = client.get(f"/api/materials/{first['material_id']}").json()
    assert old["status"] == "cancelled"
    resumed_response = client.post(f"/api/projects/{project.id}/materials", files={"file": ("needs.docx", content)})
    assert resumed_response.status_code == 202
    resumed = resumed_response.json()
    assert resumed["material_id"] == first["material_id"]
    assert resumed["run_id"] != first["run_id"]
    assert client.get(f"/api/runs/{resumed['run_id']}").json()["status"] == "queued"
    jobs.execute_run(resumed["run_id"])
    ready = client.get(f"/api/materials/{first['material_id']}").json()
    assert ready["status"] == "ready" and ready["run_id"] == resumed["run_id"]
    assert ready["file_id"] == old["file_id"] and ready["sha256"] == old["sha256"]
    assert [block["text"] for block in ready["blocks"]] == ["无人机研究需求"]
    repeated = client.post(f"/api/projects/{project.id}/materials", files={"file": ("needs.docx", content)}).json()
    assert repeated["run_id"] == resumed["run_id"] and repeated["material_id"] == first["material_id"]
    assert client.get(f"/api/runs/{first['run_id']}").json()["status"] == "cancelled"
