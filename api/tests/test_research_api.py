"""Research rounds keep adoption, history, context and conversations distinct."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import jobs, model_provider
from app.config import get_settings
from app.main import create_app
from app.models import (Conversation, DatasetVersion, Message, Project, Report, ReportVersion,
                        ResearchRun, ResearchVersion)
from app.report_service import generate_report, revise_report
from app.research_service import create_draft, ensure_conversation
from test_report_version_reads import capture_version_reads
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
    result = generate_report(context_factory(project, "generate_report", {"analysis_id": report_analysis.id}))
    db_session.flush()
    return result


def _draft(client, first, key="new-round"):
    response = client.post(f"/api/reports/{first['report_id']}/research-versions",
        headers={"Idempotency-Key": key}, json={"base_revision_id": first["report_version_id"]})
    assert response.status_code == 201, response.text
    return response.json()


def _second(db_session, project, context_factory, first, report_analysis):
    draft = create_draft(db_session, db_session.get(Report, first["report_id"]), first["report_version_id"])
    db_session.flush()
    return generate_report(context_factory(project, "generate_report", {
        "research_id": draft.id, "analysis_id": report_analysis.id}))


def _status(client, run_id):
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_create_draft_is_idempotent_new_conversation_without_version_number(client, db_session, first):
    created = _draft(client, first)
    repeated = _draft(client, first)
    assert repeated == created
    assert created["status"] == "draft" and created["number"] is None
    assert created["current_revision_id"] is None and created["current_version_id"] is None
    assert created["base_revision_id"] == first["report_version_id"]
    assert created["main_conversation_id"] == created["conversation_id"]
    conversation = client.get(f"/api/conversations/{created['conversation_id']}")
    assert conversation.status_code == 200
    assert conversation.json()["research_id"] == created["research_id"]
    db_session.expire_all()
    report = db_session.get(Report, first["report_id"])
    assert report.current_version_id == first["report_version_id"] and report.research_counter == 1


def test_research_directory_lists_published_only_detail_exposes_frozen_context(client, first, report_analysis):
    draft = _draft(client, first)
    response = client.get(f"/api/reports/{first['report_id']}/research-versions")
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == [first["research_id"]]
    detail = client.get(f"/api/research-versions/{draft['research_id']}").json()
    assert detail["status"] == "draft" and detail["number"] is None
    assert detail["context"]["scope_version_id"] == report_analysis.scope_version_id
    assert detail["context"]["dataset_version_ids"] == report_analysis.dataset_version_ids
    assert detail["context"]["analysis_id"] == report_analysis.id
    assert detail["context"]["scope"]["content"]["industry"] == "无人机"


def test_main_conversation_is_fixed_and_ensure_is_idempotent(client, db_session, first):
    responses = [client.post(f"/api/research-versions/{first['research_id']}/conversation") for _ in range(2)]
    assert all(response.status_code == 200 for response in responses)
    first_id, second_id = [response.json()["conversation_id"] for response in responses]
    assert first_id == second_id
    detail = client.get(f"/api/conversations/{first_id}").json()
    assert detail["research_id"] == first["research_id"]
    assert detail["research"]["number"] == 1
    assert detail["context"]["current_revision_id"] == first["report_version_id"]


def test_history_filters_research_and_projects_lightweight_metadata(
        client, db_session, project, context_factory, first, report_analysis):
    second = _second(db_session, project, context_factory, first, report_analysis)
    with capture_version_reads(db_session) as statements:
        response = client.get(f"/api/reports/{first['report_id']}/versions", params={
            "summary": "true", "research_id": first["research_id"], "limit": 1})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1 and page["returned_count"] == 1
    item = page["items"][0]
    assert item["id"] == first["report_version_id"] and item["research_number"] == 1
    assert item["research_id"] == first["research_id"] and item["change_kind"] == "generate"
    assert "change_count" in item and "change_summary" in item and "content" not in item
    assert page["current_version_id"] == second["report_version_id"]
    assert statements and all("report_versions.content" not in sql for sql, _ in statements)


def test_restore_run_appends_record_and_adopts_old_research(
        client, db_session, project, context_factory, first, report_analysis):
    frozen = deepcopy(db_session.get(ReportVersion, first["report_version_id"]).content)
    second = _second(db_session, project, context_factory, first, report_analysis)
    response = client.post(f"/api/research-versions/{first['research_id']}/restore", json={
        "revision_id": first["report_version_id"], "expected_current_version_id": first["report_version_id"],
        "expected_adopted_version_id": second["report_version_id"]})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    jobs.execute_run(run_id)
    done = _status(client, run_id)
    assert done["status"] == "succeeded", done
    result = done["result"]
    assert result["change_kind"] == "restore" and result["research_number"] == 1
    assert result["report_version_id"] != first["report_version_id"]
    db_session.expire_all()
    report = db_session.get(Report, first["report_id"])
    restored = db_session.get(ReportVersion, result["report_version_id"])
    assert report.current_research_id == first["research_id"] and report.current_version_id == restored.id
    assert restored.content == frozen and restored.restored_from_id == first["report_version_id"]
    assert restored.parent_id == first["report_version_id"]


def test_inactive_research_revision_api_updates_only_that_head(
        client, db_session, project, context_factory, first, report_analysis):
    second = _second(db_session, project, context_factory, first, report_analysis)
    response = client.post(f"/api/reports/{first['report_id']}/revisions", json={
        "research_id": first["research_id"], "base_version_id": first["report_version_id"],
        "expected_current_version_id": first["report_version_id"],
        "operations": [{"op": "set_title", "text": "Update V1 independently"}]})
    assert response.status_code == 202, response.text
    jobs.execute_run(response.json()["run_id"])
    done = _status(client, response.json()["run_id"])
    assert done["status"] == "succeeded", done
    db_session.expire_all()
    assert db_session.get(Report, first["report_id"]).current_version_id == second["report_version_id"]
    assert db_session.get(ResearchVersion, first["research_id"]).current_revision_id == done["result"]["report_version_id"]


def test_legacy_adoption_requires_restore_for_history_and_old_links_stay_frozen(
        client, db_session, project, context_factory, first):
    original = db_session.get(ReportVersion, first["report_version_id"])
    frozen = deepcopy(original.content)
    second = revise_report(context_factory(project, "revise_report", {
        "research_id": first["research_id"], "report_id": first["report_id"],
        "base_version_id": original.id, "expected_current_version_id": original.id,
        "operations": [{"op": "set_title", "text": "New title"}]}))
    response = client.put(f"/api/reports/{first['report_id']}/current-version", json={
        "version_id": original.id, "expected_current_version_id": second["report_version_id"]})
    assert response.status_code == 409 and response.json()["code"] == "RESTORE_REQUIRED"
    full = client.get(f"/api/reports/{first['report_id']}/versions/{original.id}")
    display = client.get(f"/api/reports/{first['report_id']}/versions/{original.id}", params={"view": "display"})
    assert full.status_code == display.status_code == 200
    assert full.json()["content"] == frozen
    assert display.json()["content"] == {key: frozen[key] for key in ("title", "layout", "blocks")}
    assert full.json()["research_number"] == display.json()["research_number"] == 1


def test_message_context_locates_target_and_checks_conversation_and_owner(client, db_session, project, first):
    research = db_session.get(ResearchVersion, first["research_id"])
    main = ensure_conversation(db_session, research)
    other = Conversation(project_id=project.id, title="Other")
    outsider = Project(owner_id="another-owner", name="Private")
    db_session.add_all([other, outsider])
    db_session.flush()
    private = Conversation(project_id=outsider.id, title="Private")
    db_session.add(private)
    db_session.flush()
    messages = [Message(conversation_id=main.id, role="user", content=f"Message {index}") for index in range(9)]
    hidden = Message(conversation_id=private.id, role="user", content="private")
    db_session.add_all([*messages, hidden])
    db_session.flush()
    target = messages[4]
    response = client.get(f"/api/conversations/{main.id}/messages/{target.id}/context", params={"limit": 5})
    assert response.status_code == 200, response.text
    assert target.id in {item["id"] for item in response.json()["items"]}
    assert response.json()["returned_count"] <= 5
    assert client.get(f"/api/conversations/{other.id}/messages/{target.id}/context").status_code == 404
    assert client.get(f"/api/conversations/{private.id}/messages/{hidden.id}/context").status_code == 404


def test_research_scope_and_dataset_binding_do_not_change_adopted_project(
        client, db_session, project, first, report_analysis):
    draft = _draft(client, first)
    old_scope, old_dataset, old_analysis = project.scope_version_id, project.dataset_version_id, project.current_analysis_id
    response = client.patch(f"/api/projects/{project.id}/scope", json={"research_id": draft["research_id"],
        "expected_scope_version_id": old_scope, "changes": {"industry": "Only next round"}, "confirmed": True})
    assert response.status_code == 200, response.text
    scope_id = response.json()["id"]
    base = db_session.get(DatasetVersion, old_dataset)
    replacement = DatasetVersion(dataset_id=base.dataset_id, source_file_id=base.source_file_id, status="ready")
    db_session.add(replacement)
    db_session.flush()
    context = client.get(f"/api/research-versions/{draft['research_id']}").json()["context"]
    response = client.put(f"/api/projects/{project.id}/datasets", json={"research_id": draft["research_id"],
        "expected_revision": context["revision"], "dataset_version_ids": [replacement.id]})
    assert response.status_code == 200, response.text
    detail = client.get(f"/api/research-versions/{draft['research_id']}").json()
    assert detail["context"]["scope_version_id"] == scope_id
    assert detail["context"]["dataset_version_ids"] == [replacement.id]
    assert detail["context"]["analysis_id"] is None
    db_session.expire_all()
    adopted = db_session.get(Project, project.id)
    assert (adopted.scope_version_id, adopted.dataset_version_id, adopted.current_analysis_id) == (
        old_scope, old_dataset, old_analysis)
    repeated = client.put(f"/api/projects/{project.id}/datasets", json={"research_id": draft["research_id"],
        "expected_revision": context["revision"], "dataset_version_ids": [old_dataset]})
    assert repeated.status_code == 409


def test_research_and_revision_endpoints_hide_foreign_ownership(client, db_session, project, first):
    foreign_project = Project(owner_id="another-owner", name="Private")
    db_session.add(foreign_project)
    db_session.flush()
    report = Report(project_id=foreign_project.id)
    db_session.add(report)
    db_session.flush()
    research = ResearchVersion(report_id=report.id)
    db_session.add(research)
    db_session.flush()
    assert client.get(f"/api/research-versions/{research.id}").status_code == 404
    assert client.get(f"/api/reports/{report.id}/research-versions").status_code == 404
    assert client.get(f"/api/reports/{first['report_id']}/versions", params={
        "summary": "true", "research_id": research.id}).status_code == 404
    assert client.post(f"/api/research-versions/{research.id}/conversation").status_code == 404


def test_ordinary_conversation_transfers_confirmed_edit_once_to_main(
        client, db_session, project, context_factory, first, monkeypatch, buffered_chat_stream):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", "https://controlled.invalid/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test")
    monkeypatch.setattr(settings, "llm_model", "controlled-model")
    planner_calls = []

    def model(messages, tools=None):
        planner_calls.append(messages)
        return {"content": json.dumps({"action": "revise_report", "reason": "明确修改标题", "operations": [
            {"op": "set_title", "text": "Confirmed transfer"}]})}

    monkeypatch.setattr(model_provider, "chat", model)
    main_response = client.post(f"/api/research-versions/{first['research_id']}/conversation")
    main = main_response.json()["conversation_id"]
    sent_response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
        "content": "将报告标题改为Confirmed transfer", "action": "auto", "semantic_mode": "off",
        "reference": {"report_version_id": first["report_version_id"]}}})
    assert sent_response.status_code == 202, sent_response.text
    sent = sent_response.json()
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["status"] == "waiting_input", waiting
    assert waiting["input_request"]["kind"] == "execution_confirmation"
    assert waiting["input_request"]["target_conversation_id"] == main
    confirmed = client.post(f"/api/runs/{sent['run_id']}/inputs", json={
        "input_request_id": waiting["input_request"]["id"], "values": {"confirmed": True}})
    assert confirmed.status_code == 202, confirmed.text
    assert confirmed.json()["run_id"] == sent["run_id"] and confirmed.json()["conversation_id"] == main
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert len(planner_calls) == 1
    db_session.expire_all()
    original = db_session.get(Message, sent["message_id"])
    run = db_session.get(ResearchRun, sent["run_id"])
    assert original.run_id is None and original.conversation_id == sent["conversation_id"]
    assert len([card for card in original.cards if card.get("type") == "forward"]) == 1
    assert run.conversation_id == main and run.payload["source_message_id"] == original.id
    main_messages = list(db_session.scalars(select(Message).where(Message.conversation_id == main,
        Message.run_id == run.id)))
    assert [row.role for row in main_messages].count("user") == 1
    assert [row.role for row in main_messages].count("assistant") == 1
    assert done["result"]["source_conversation_id"] == original.conversation_id
    assert done["result"]["source_message_id"] == original.id
