"""Intent confirmation fixes a complete task to one research and conversation."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app import jobs, model_provider, report_service
from app.config import get_settings
from app.main import create_app
from app.models import (Conversation, Message, Project, Report, ReportVersion, ResearchRun,
                        ResearchVersion, RunEvent, ScopeVersion)
from app.research_service import create_draft, ensure_conversation
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
    result = report_service.generate_report(context_factory(project, "generate_report", {
        "analysis_id": report_analysis.id}))
    db_session.flush()
    return result


def _plan(monkeypatch, *, action="generate_report", intent="new", **values):
    calls = []

    def chat(messages, tools=None):
        calls.append(messages)
        if tools:
            return {"content": "这份报告解释了现有无人机专利样本。"}
        return {"content": json.dumps({"action": action, "reason": "完整研究任务",
            "research_intent": intent, "research_reason": "用户提出独立研究目标",
            "research_topic": "无人机起飞研究", **values})}

    monkeypatch.setattr(model_provider, "chat", chat)
    return calls


def _send(client, project, content="生成无人机起飞报告，范围为2020—2024年", conversation_id=None, **values):
    body = {"content": content, "action": "auto", "semantic_mode": "off", **values}
    response = (client.post(f"/api/conversations/{conversation_id}/messages", json=body) if conversation_id else
                client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": body}))
    assert response.status_code == 202, response.text
    return response.json()


def _status(client, run_id):
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(client, sent, waiting, choice="new", scope=None, key=None):
    values = {"confirmed": True, "research_choice": choice}
    if scope is not None:
        values["scope"] = scope
    response = client.post(f"/api/runs/{sent['run_id']}/inputs",
        headers={"Idempotency-Key": key} if key else {},
        json={"input_request_id": waiting["input_request"]["id"],
              "expected_scope_version_id": waiting["input_request"].get("expected_scope_version_id") or waiting.get("scope_version_id"),
              "values": values})
    assert response.status_code == 202, response.text
    return response.json()


def _researches(db_session, report_id):
    db_session.expire_all()
    return list(db_session.scalars(select(ResearchVersion).where(ResearchVersion.report_id == report_id)))


def _input_events(db_session, run_id):
    return list(db_session.scalars(select(RunEvent).where(
        RunEvent.run_id == run_id, RunEvent.event_type == "input.required")))


def test_ordinary_chat_is_not_bound_before_intent_or_after_answer(
        client, db_session, project, first, monkeypatch, buffered_chat_stream):
    settings = get_settings()
    for suffix, value in (("base_url", "https://controlled.invalid/v1"), ("api_key", "test"), ("model", "test")):
        monkeypatch.setattr(settings, f"llm_{suffix}", value)
    calls = _plan(monkeypatch, action="chat", intent="continue")
    sent = _send(client, project, "解释这份报告的结论")
    run = db_session.get(ResearchRun, sent["run_id"])
    assert not run.payload.get("research_id")
    assert client.get(f"/api/conversations/{sent['conversation_id']}").json()["research_id"] is None
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["input_request"] is None
    db_session.expire_all()
    assert not db_session.get(ResearchRun, sent["run_id"]).payload.get("research_id")
    assert client.get(f"/api/conversations/{sent['conversation_id']}").json()["research_id"] is None
    assert len(_researches(db_session, first["report_id"])) == 1
    assert len(calls) == 2  # One planner call and one answer call.


def test_new_report_confirmation_reuses_ordinary_chat_and_completes_edited_scope_once(
        client, db_session, project, first, monkeypatch):
    before = deepcopy(db_session.get(ReportVersion, first["report_version_id"]).content)
    old_scope = db_session.get(ResearchVersion, first["research_id"]).scope_version_id
    old_scope_content = deepcopy(db_session.get(ScopeVersion, old_scope).content)
    calls = _plan(monkeypatch, scope_changes={"need": "起飞", "start": 2020, "end": 2024})
    sent = _send(client, project)
    assert not db_session.get(ResearchRun, sent["run_id"]).payload.get("research_id")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    request = waiting["input_request"]
    assert waiting["status"] == "waiting_input" and request["kind"] == "execution_confirmation", waiting
    assert request["scope"]["start"] == 2020 and request["scope"]["end"] == 2024
    assignment = request["research_assignment"]
    assert assignment["recommended"] == "new" and assignment["allow_new"] and assignment["allow_existing"]
    assert assignment["existing_research_id"] == first["research_id"]
    assert assignment["new_uses_current_conversation"] is True
    assert {action["kind"] for action in request["actions"]} >= {"analyze", "generate_report"}
    assert len(_researches(db_session, first["report_id"])) == 1
    accepted = _confirm(client, sent, waiting, scope={"end": 2023}, key="new-research-once")
    repeated = _confirm(client, sent, waiting, scope={"end": 2023}, key="new-research-once")
    assert repeated == accepted
    db_session.expire_all()
    run = db_session.get(ResearchRun, sent["run_id"])
    decision = run.payload["research_decision"]
    assert decision["choice"] == "new" and decision["conversation_id"] == sent["conversation_id"]
    draft = db_session.get(ResearchVersion, decision["research_id"])
    assert draft.status == "draft" and draft.number is None
    assert draft.main_conversation_id == sent["conversation_id"]
    assert db_session.get(Conversation, draft.main_conversation_id).project_id == project.id
    assert db_session.get(ScopeVersion, draft.scope_version_id).content["end"] == 2023
    assert db_session.get(Report, first["report_id"]).research_counter == 1
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["result"]["research_id"] == draft.id and done["result"]["research_number"] == 2
    assert len(calls) == 1 and len(_input_events(db_session, sent["run_id"])) == 1
    db_session.expire_all()
    v1 = db_session.get(ResearchVersion, first["research_id"])
    assert v1.current_revision_id == first["report_version_id"] and v1.scope_version_id == old_scope
    assert db_session.get(ScopeVersion, old_scope).content == old_scope_content
    assert db_session.get(ReportVersion, first["report_version_id"]).content == before
    v2 = db_session.get(ReportVersion, done["result"]["report_version_id"])
    assert db_session.get(ScopeVersion, v2.scope_version_id).content["end"] == 2023
    assert db_session.get(Report, first["report_id"]).current_research_id == draft.id


def test_user_can_keep_suggested_new_report_in_existing_research(
        client, db_session, project, first, monkeypatch):
    calls = _plan(monkeypatch, scope_changes={"need": "起飞", "start": 2020, "end": 2024})
    main = ensure_conversation(db_session, db_session.get(ResearchVersion, first["research_id"]))
    db_session.flush()
    sent = _send(client, project)
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    accepted = _confirm(client, sent, waiting, choice="existing")
    assert accepted["conversation_id"] == main.id
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["result"]["research_id"] == first["research_id"]
    assert done["result"]["research_number"] == 1 and len(calls) == 1
    assert len(_researches(db_session, first["report_id"])) == 1
    assert db_session.get(Report, first["report_id"]).research_counter == 1
    original = db_session.get(Message, sent["message_id"])
    assert len([card for card in original.cards if card.get("type") == "forward"]) == 1


def test_new_research_from_bound_main_chat_creates_one_same_project_destination(
        client, db_session, project, first, monkeypatch):
    calls = _plan(monkeypatch)
    main = ensure_conversation(db_session, db_session.get(ResearchVersion, first["research_id"]))
    db_session.flush()
    sent = _send(client, project, conversation_id=main.id)
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["input_request"]["research_assignment"]["new_uses_current_conversation"] is False
    accepted = _confirm(client, sent, waiting, key="main-new-round")
    assert _confirm(client, sent, waiting, key="main-new-round") == accepted
    assert accepted["conversation_id"] != main.id
    db_session.expire_all()
    run = db_session.get(ResearchRun, sent["run_id"])
    research = db_session.get(ResearchVersion, run.payload["research_decision"]["research_id"])
    assert research.main_conversation_id == accepted["conversation_id"]
    assert db_session.get(Conversation, research.main_conversation_id).project_id == project.id
    assert research.number is None and len(_researches(db_session, first["report_id"])) == 2
    assert run.conversation_id == research.main_conversation_id
    original = db_session.get(Message, sent["message_id"])
    assert len([card for card in original.cards if card.get("type") == "forward"]) == 1
    linked = list(db_session.scalars(select(Message).where(Message.run_id == run.id, Message.role == "user")))
    assert len(linked) == 1 and linked[0].conversation_id == research.main_conversation_id
    jobs.execute_run(sent["run_id"])
    assert _status(client, sent["run_id"])["status"] == "succeeded"
    assert len(calls) == 1


def test_ambiguous_regeneration_requires_choice_on_the_same_execution_card(
        client, db_session, project, first, monkeypatch):
    _plan(monkeypatch, intent="choose", research_reason="重新生成的归属不明确")
    sent = _send(client, project, "重新生成一份报告")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    request = waiting["input_request"]
    assert request["kind"] == "execution_confirmation" and request["research_assignment"]["recommended"] is None
    response = client.post(f"/api/runs/{sent['run_id']}/inputs", json={
        "input_request_id": request["id"], "values": {"confirmed": True}})
    assert response.status_code == 422, response.text
    assert len(_researches(db_session, first["report_id"])) == 1
    _confirm(client, sent, waiting, choice="existing")
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded" and done["result"]["research_number"] == 1, done


@pytest.mark.parametrize("confirm_first", [False, True])
def test_cancelled_new_research_never_publishes_or_consumes_number(
        client, db_session, project, first, monkeypatch, confirm_first):
    _plan(monkeypatch)
    sent = _send(client, project)
    jobs.execute_run(sent["run_id"])
    if confirm_first:
        _confirm(client, sent, _status(client, sent["run_id"]))
    cancelled = client.post(f"/api/runs/{sent['run_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    jobs.execute_run(sent["run_id"])
    assert _status(client, sent["run_id"])["status"] == "cancelled"
    groups = _researches(db_session, first["report_id"])
    assert len(groups) == (2 if confirm_first else 1)
    assert all(group.number is None for group in groups if group.id != first["research_id"])
    report = db_session.get(Report, first["report_id"])
    assert report.research_counter == 1 and report.current_version_id == first["report_version_id"]


def test_failed_new_research_retry_reuses_assignment_conversation_and_operation(
        client, db_session, project, first, monkeypatch):
    calls = _plan(monkeypatch)
    original_generate = report_service.generate_report
    attempts = []

    def generate(ctx):
        attempts.append(ctx.payload["operation_id"])
        if len(attempts) == 1:
            raise RuntimeError("Controlled failure before publication")
        return original_generate(ctx)

    monkeypatch.setattr(report_service, "generate_report", generate)
    sent = _send(client, project)
    jobs.execute_run(sent["run_id"])
    _confirm(client, sent, _status(client, sent["run_id"]))
    jobs.execute_run(sent["run_id"])
    assert _status(client, sent["run_id"])["status"] == "failed"
    db_session.expire_all()
    run = db_session.get(ResearchRun, sent["run_id"])
    decision = deepcopy(run.payload["research_decision"])
    assert db_session.get(ResearchVersion, decision["research_id"]).number is None
    assert db_session.get(Report, first["report_id"]).research_counter == 1
    response = client.post(f"/api/runs/{run.id}/retry")
    assert response.status_code == 202, response.text
    retry_id = response.json()["run_id"]
    jobs.execute_run(retry_id)
    done = _status(client, retry_id)
    assert done["status"] == "succeeded" and done["result"]["research_number"] == 2, done
    db_session.expire_all()
    retry = db_session.get(ResearchRun, retry_id)
    assert retry.payload["research_decision"] == decision and retry.conversation_id == run.conversation_id
    assert len(set(attempts)) == 1 and len(calls) == 1
    assert len(_researches(db_session, first["report_id"])) == 2
    assert db_session.scalar(select(func.count()).select_from(ReportVersion).where(
        ReportVersion.research_id == decision["research_id"])) == 1
    assert _input_events(db_session, retry_id) == []


def test_pending_existing_target_does_not_follow_later_project_adoption(
        client, db_session, project, context_factory, first, report_analysis, monkeypatch):
    calls = _plan(monkeypatch, intent="target", target_research_id=first["research_id"],
                  action="revise_report", operations=[{"op": "set_title", "text": "只更新V1"}])
    sent = _send(client, project, "把V1标题改一下")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["input_request"]["research_assignment"]["existing_research_id"] == first["research_id"]
    draft = create_draft(db_session, db_session.get(Report, first["report_id"]), first["report_version_id"])
    db_session.flush()
    second = report_service.generate_report(context_factory(project, "generate_report", {
        "research_id": draft.id, "analysis_id": report_analysis.id}))
    _confirm(client, sent, waiting, choice="existing")
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["result"]["research_id"] == first["research_id"] and len(calls) == 1
    db_session.expire_all()
    assert db_session.get(Report, first["report_id"]).current_version_id == second["report_version_id"]


def test_analysis_only_request_does_not_bind_or_create_research(
        client, db_session, project, first, monkeypatch):
    calls = _plan(monkeypatch, action="analyze", intent="new", scope_changes={"start": 2020, "end": 2024})
    sent = _send(client, project, "只分析2020—2024年起飞技术，不需要报告")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["input_request"]["kind"] == "execution_confirmation"
    response = client.post(f"/api/runs/{sent['run_id']}/inputs", json={
        "input_request_id": waiting["input_request"]["id"], "values": {"confirmed": True}})
    assert response.status_code == 202, response.text
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded" and "report_version_id" not in done["result"], done
    db_session.expire_all()
    assert not db_session.get(ResearchRun, sent["run_id"]).payload.get("research_id")
    assert client.get(f"/api/conversations/{sent['conversation_id']}").json()["research_id"] is None
    assert len(_researches(db_session, first["report_id"])) == 1 and len(calls) == 1


def test_model_cannot_assign_report_task_to_another_project_research(
        client, db_session, project, first, monkeypatch):
    foreign_project = Project(owner_id=project.owner_id, name="同用户的另一个项目")
    db_session.add(foreign_project)
    db_session.flush()
    foreign_report = Report(project_id=foreign_project.id)
    db_session.add(foreign_report)
    db_session.flush()
    foreign_research = ResearchVersion(report_id=foreign_report.id)
    db_session.add(foreign_research)
    db_session.flush()
    _plan(monkeypatch, action="revise_report", intent="target", target_research_id=foreign_research.id,
          operations=[{"op": "set_title", "text": "不得写入其他项目"}])
    sent = _send(client, project, "修改指定报告标题")
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "failed", done
    db_session.expire_all()
    assert db_session.get(ResearchVersion, foreign_research.id).current_revision_id is None
    assert db_session.get(Report, first["report_id"]).current_version_id == first["report_version_id"]
    assert len(_researches(db_session, first["report_id"])) == 1


def test_explicit_report_reference_wins_over_model_assignment_to_other_research(
        client, db_session, project, context_factory, first, report_analysis, monkeypatch):
    draft = create_draft(db_session, db_session.get(Report, first["report_id"]), first["report_version_id"])
    db_session.flush()
    second = report_service.generate_report(context_factory(project, "generate_report", {
        "research_id": draft.id, "analysis_id": report_analysis.id}))
    calls = _plan(monkeypatch, action="revise_report", intent="target", target_research_id=draft.id,
                  operations=[{"op": "set_title", "text": "引用V1后修改"}])
    sent = _send(client, project, "修改我引用的报告标题", reference={
        "report_version_id": first["report_version_id"]})
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["status"] == "waiting_input", waiting
    assert waiting["input_request"]["research_assignment"]["existing_research_id"] == first["research_id"]
    _confirm(client, sent, waiting, choice="existing")
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded" and done["result"]["research_id"] == first["research_id"], done
    db_session.expire_all()
    assert db_session.get(Report, first["report_id"]).current_version_id == second["report_version_id"]
    assert db_session.get(ResearchVersion, draft.id).current_revision_id == second["report_version_id"]
    assert len(calls) == 1
