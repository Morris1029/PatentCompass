"""Intent confirmation fixes a complete task to one research and conversation."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app import jobs, model_provider, report_service
from app.config import get_settings
from app.main import create_app
from app.models import (Conversation, DatasetVersion, Message, Project, Report, ReportVersion, ResearchRun,
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


def _confirm(client, sent, waiting, choice="new", scope=None, key=None, block_ids=None):
    values = {"confirmed": True, "research_choice": choice}
    if scope is not None:
        values["scope"] = scope
    if block_ids is not None:
        values["block_ids"] = block_ids
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
    snapshot = db_session.get(ReportVersion, first["report_version_id"])
    working_scope = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "电池"})
    db_session.add(working_scope)
    db_session.flush()
    project.scope_version_id, project.current_analysis_id = working_scope.id, None
    db_session.flush()
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
    run = db_session.get(ResearchRun, sent["run_id"])
    assert not run.payload.get("research_id")
    assert run.payload["reference_version_id"] == snapshot.id
    assert run.payload["analysis_id"] == snapshot.analysis_id
    assert run.scope_version_id == snapshot.scope_version_id
    assert run.dataset_version_ids == snapshot.dataset_version_ids
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
    target_details = [detail for action in request["actions"] for detail in action["details"]
                      if detail.startswith("报告目标：")]
    assert target_details and set(target_details) == {"报告目标：新研究：无人机起飞研究（尚未编号）"}
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


@pytest.mark.parametrize("adopt_before_plan", [False, True])
def test_existing_target_does_not_follow_project_adoption(
        client, db_session, project, context_factory, first, report_analysis, monkeypatch, adopt_before_plan):
    if adopt_before_plan:
        draft = create_draft(db_session, db_session.get(Report, first["report_id"]), first["report_version_id"])
        db_session.flush()
        second = report_service.generate_report(context_factory(project, "generate_report", {
            "research_id": draft.id, "analysis_id": report_analysis.id}))
    calls = _plan(monkeypatch, intent="target", target_research_id=first["research_id"],
                  action="revise_report", operations=[{"op": "set_title", "text": "只更新V1"}])
    sent = _send(client, project, "把V1标题改一下")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["input_request"]["research_assignment"]["existing_research_id"] == first["research_id"]
    if not adopt_before_plan:
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
    calls = _plan(monkeypatch, action="analyze", intent="new")
    sent = _send(client, project, "只分析起飞技术，不需要报告")
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


def test_revision_target_selection_and_execution_share_one_confirmation(
        client, db_session, project, first, monkeypatch):
    calls = []
    rewritten = "本次研究结论已按要求简化，保留原有样本和来源。"

    def model(messages, tools=None):
        calls.append(messages)
        if "block" in json.loads(messages[-1]["content"]):
            return {"content": rewritten}
        return {"content": json.dumps({"action": "revise_report", "reason": "用户要求简化研究结论",
            "research_intent": "target", "target_research_id": first["research_id"],
            "research_reason": "明确修改V1", "block_ids": [], "operations": []})}

    monkeypatch.setattr(model_provider, "chat", model)
    sent = _send(client, project, "把V1的研究结论简化一下")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    request = waiting["input_request"]
    assert waiting["status"] == "waiting_input" and request["kind"] == "execution_confirmation", waiting
    assert request["select_blocks"] is True and request["blocks"]
    snapshot = db_session.get(ReportVersion, first["report_version_id"])
    block = next(block for block in snapshot.content["blocks"] if block["type"] == "paragraph")
    assert block["id"] in {item["id"] for item in request["blocks"]}
    _confirm(client, sent, waiting, choice="existing", block_ids=[block["id"]])
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["result"]["research_id"] == first["research_id"] and done["result"]["research_number"] == 1
    db_session.expire_all()
    saved = db_session.get(ReportVersion, done["result"]["report_version_id"])
    assert next(item["text"] for item in saved.content["blocks"] if item["id"] == block["id"]) == rewritten
    assert len(calls) == 2 and len(_input_events(db_session, sent["run_id"])) == 1
    assert len(_researches(db_session, first["report_id"])) == 1


def test_first_report_without_existing_report_creates_unnumbered_draft_then_v1(
        client, db_session, project, report_analysis, monkeypatch):
    calls = _plan(monkeypatch)
    assert db_session.scalar(select(Report).where(Report.project_id == project.id)) is None
    sent = _send(client, project, "根据现有样本生成首份导航报告")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    request = waiting["input_request"]
    assert waiting["status"] == "waiting_input" and request["kind"] == "execution_confirmation", waiting
    assert request["research_assignment"]["recommended"] == "new"
    assert request["research_assignment"]["allow_existing"] is False
    _confirm(client, sent, waiting)
    db_session.expire_all()
    run = db_session.get(ResearchRun, sent["run_id"])
    research = db_session.get(ResearchVersion, run.payload["research_decision"]["research_id"])
    report = db_session.get(Report, research.report_id)
    assert report.project_id == project.id and report.research_counter == 0
    assert research.number is None and research.status == "draft" and research.base_revision_id is None
    assert research.main_conversation_id == sent["conversation_id"]
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded" and done["result"]["research_number"] == 1, done
    assert done["result"]["research_id"] == research.id and len(calls) == 1
    assert len(_input_events(db_session, sent["run_id"])) == 1


def test_concurrent_new_cards_reconfirm_after_current_conversation_becomes_bound(
        client, db_session, project, first, monkeypatch):
    _plan(monkeypatch)
    first_task = _send(client, project)
    second_task = _send(client, project, "再做一份新的无人机运输研究", conversation_id=first_task["conversation_id"])
    jobs.execute_run(first_task["run_id"])
    jobs.execute_run(second_task["run_id"])
    first_waiting = _status(client, first_task["run_id"])
    second_waiting = _status(client, second_task["run_id"])
    assert second_waiting["input_request"]["research_assignment"]["new_uses_current_conversation"] is True
    _confirm(client, first_task, first_waiting)
    assert len(_researches(db_session, first["report_id"])) == 2
    _confirm(client, second_task, second_waiting)
    refreshed = _status(client, second_task["run_id"])
    assert refreshed["status"] == "waiting_input", refreshed
    assert refreshed["input_request"]["id"] != second_waiting["input_request"]["id"]
    assert refreshed["input_request"]["research_assignment"]["new_uses_current_conversation"] is False
    db_session.expire_all()
    run = db_session.get(ResearchRun, second_task["run_id"])
    assert run.conversation_id == first_task["conversation_id"] and not run.payload.get("research_decision")
    assert not any(card.get("type") == "forward" for card in db_session.get(Message, second_task["message_id"]).cards)
    assert len(_researches(db_session, first["report_id"])) == 2
    accepted = _confirm(client, second_task, refreshed)
    db_session.expire_all()
    run = db_session.get(ResearchRun, second_task["run_id"])
    assert run.conversation_id == accepted["conversation_id"] != first_task["conversation_id"]
    assert run.payload["research_decision"]["conversation_id"] == run.conversation_id
    assert len(_researches(db_session, first["report_id"])) == 3
    assert db_session.get(Report, first["report_id"]).research_counter == 1
    assert len(_input_events(db_session, second_task["run_id"])) == 2


@pytest.mark.parametrize("choice", ["new", "existing"])
def test_followup_report_preserves_ordinary_conversation_working_analysis_basis(
        client, db_session, project, first, monkeypatch, choice):
    snapshot = db_session.get(ReportVersion, first["report_version_id"])
    v1_scope_id = snapshot.scope_version_id
    v1_content = deepcopy(snapshot.content)
    original_data = db_session.get(DatasetVersion, project.dataset_version_id)
    working_data = DatasetVersion(dataset_id=original_data.dataset_id, source_file_id=original_data.source_file_id,
        status="ready", quality={"application_count": 0}, source_metadata={"partial_results": True})
    db_session.add(working_data)
    db_session.flush()
    project.dataset_version_id, project.current_analysis_id = working_data.id, None
    db_session.flush()
    analysis_calls = _plan(monkeypatch, action="analyze", scope_changes={"need": "起飞", "start": 2020, "end": 2024})
    analysis_task = _send(client, project, "按2020—2024年范围只分析无人机起飞")
    jobs.execute_run(analysis_task["run_id"])
    scope_waiting = _status(client, analysis_task["run_id"])
    assert scope_waiting["input_request"]["kind"] == "plan_confirmation", scope_waiting
    _confirm(client, analysis_task, scope_waiting)
    jobs.execute_run(analysis_task["run_id"])
    execution_waiting = _status(client, analysis_task["run_id"])
    assert execution_waiting["input_request"]["kind"] == "execution_confirmation", execution_waiting
    _confirm(client, analysis_task, execution_waiting)
    jobs.execute_run(analysis_task["run_id"])
    analysis_done = _status(client, analysis_task["run_id"])
    assert analysis_done["status"] == "succeeded", analysis_done
    db_session.expire_all()
    analysis_run = db_session.get(ResearchRun, analysis_task["run_id"])
    working_scope_id = analysis_run.scope_version_id
    analysis_id = analysis_done["result"]["analysis_id"]
    assert working_scope_id != v1_scope_id and analysis_run.dataset_version_ids == [working_data.id]
    assert not analysis_run.payload.get("research_id") and len(analysis_calls) == 1
    report_calls = _plan(monkeypatch)
    report_task = _send(client, project, "把刚刚的分析生成完整报告", conversation_id=analysis_task["conversation_id"])
    jobs.execute_run(report_task["run_id"])
    waiting = _status(client, report_task["run_id"])
    assert waiting["input_request"]["kind"] == "execution_confirmation", waiting
    assert waiting["input_request"]["scope"]["start"] == 2020
    assert waiting["input_request"]["scope"]["end"] == 2024
    assert waiting["input_request"]["scope"]["need"] == "起飞"
    db_session.expire_all()
    report_run = db_session.get(ResearchRun, report_task["run_id"])
    assert report_run.scope_version_id == working_scope_id
    assert report_run.dataset_version_ids == [working_data.id] and report_run.payload["analysis_id"] == analysis_id
    _confirm(client, report_task, waiting, choice=choice)
    jobs.execute_run(report_task["run_id"])
    done = _status(client, report_task["run_id"])
    assert done["status"] == "succeeded", done
    db_session.expire_all()
    saved = db_session.get(ReportVersion, done["result"]["report_version_id"])
    assert saved.scope_version_id == working_scope_id and saved.dataset_version_ids == [working_data.id]
    assert saved.analysis_id == analysis_id and len(report_calls) == 1
    v1 = db_session.get(ResearchVersion, first["research_id"])
    if choice == "new":
        assert done["result"]["research_number"] == 2 and saved.research_id != v1.id
        assert v1.current_revision_id == snapshot.id and v1.scope_version_id == v1_scope_id
    else:
        assert done["result"]["research_number"] == 1 and saved.research_id == v1.id
        assert v1.current_revision_id == saved.id and v1.scope_version_id == working_scope_id
    assert db_session.get(ReportVersion, first["report_version_id"]).content == v1_content
    assert len(_input_events(db_session, report_task["run_id"])) == 1


@pytest.mark.parametrize("frozen_source", ["research_id", "plan_report_version_id"])
def test_legacy_retry_keeps_frozen_research_when_ordinary_conversation_later_binds_another(
        client, db_session, project, context_factory, first, report_analysis, monkeypatch, frozen_source):
    original_research = db_session.get(ResearchVersion, first["research_id"])
    original_main = ensure_conversation(db_session, original_research)
    ordinary = Conversation(project_id=project.id, title="先前普通对话")
    db_session.add(ordinary)
    db_session.flush()
    message = Message(conversation_id=ordinary.id, role="user", content="修改原来V1的报告标题")
    db_session.add(message)
    db_session.flush()
    payload = {"message_id": message.id, "semantic_mode": "off", "analysis_id": report_analysis.id,
        "plan": {"action": "revise_report", "reason": "已保存的旧任务计划",
                 "operations": [{"op": "set_title", "text": "Legacy retry still edits V1"}]},
        frozen_source: first["research_id"] if frozen_source == "research_id" else first["report_version_id"]}
    old_ctx = context_factory(project, "auto", payload)
    old = db_session.get(ResearchRun, old_ctx.run_id)
    old.conversation_id, old.status = ordinary.id, "failed"
    message.run_id = old.id
    db_session.flush()
    later = create_draft(db_session, db_session.get(Report, first["report_id"]),
                         first["report_version_id"], conversation_id=ordinary.id)
    db_session.flush()
    adopted = report_service.generate_report(context_factory(project, "generate_report", {
        "research_id": later.id, "analysis_id": report_analysis.id}))
    assert later.main_conversation_id == ordinary.id and adopted["research_number"] == 2
    monkeypatch.setattr(model_provider, "chat", lambda *args, **kwargs: pytest.fail("Retry replanned a frozen task"))
    retry_response = client.post(f"/api/runs/{old.id}/retry")
    assert retry_response.status_code == 202, retry_response.text
    retry_id = retry_response.json()["run_id"]
    jobs.execute_run(retry_id)
    waiting = _status(client, retry_id)
    assert waiting["status"] == "waiting_input", waiting
    assignment = waiting["input_request"]["research_assignment"]
    assert assignment["existing_research_id"] == original_research.id
    assert assignment["existing_conversation_id"] == original_main.id
    assert assignment["recommended"] == "existing"
    db_session.expire_all()
    retry = db_session.get(ResearchRun, retry_id)
    assert retry.payload["research_proposal"]["existing_research_id"] == original_research.id
    assert db_session.get(ResearchVersion, later.id).current_revision_id == adopted["report_version_id"]
    accepted = _confirm(client, {"run_id": retry_id}, waiting, choice="existing")
    assert accepted["conversation_id"] == original_main.id
    jobs.execute_run(retry_id)
    done = _status(client, retry_id)
    assert done["status"] == "succeeded" and done["result"]["research_id"] == original_research.id, done
    db_session.expire_all()
    assert db_session.get(Report, first["report_id"]).current_version_id == adopted["report_version_id"]
    assert db_session.get(ResearchVersion, later.id).current_revision_id == adopted["report_version_id"]
    assert len(_researches(db_session, first["report_id"])) == 2


def test_unbound_ambiguous_regeneration_normalizes_model_continue_to_choose(
        client, db_session, project, first, monkeypatch):
    _plan(monkeypatch, intent="continue", research_reason="延续当前对话的研究")
    sent = _send(client, project, "重新生成一份报告")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["status"] == "waiting_input", waiting
    assignment = waiting["input_request"]["research_assignment"]
    assert assignment["intent"] == "choose" and assignment["recommended"] is None
    assert assignment["reason"] == "当前普通会话尚未绑定研究，请选择归属"
    assert assignment["allow_new"] and assignment["allow_existing"]
    assert assignment["existing_research_id"] == first["research_id"]
    response = client.post(f"/api/runs/{sent['run_id']}/inputs", json={
        "input_request_id": waiting["input_request"]["id"], "values": {"confirmed": True}})
    assert response.status_code == 422 and response.json()["code"] == "RESEARCH_CHOICE_REQUIRED"
    assert client.get(f"/api/conversations/{sent['conversation_id']}").json()["research_id"] is None
    assert len(_researches(db_session, first["report_id"])) == 1
