"""Conversation execution requires one approval and stays attached to its user turn."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import data_service, jobs, model_provider, report_service
from app.config import get_settings
from app.main import create_app
from app.models import (Dataset, DatasetVersion, Material, MaterialBlock, Message, ResearchRun,
                        RunEvent, RunStep, ScopeVersion, StoredFile, new_id)


@pytest.fixture(autouse=True)
def no_heartbeat_thread(monkeypatch):
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


@pytest.fixture
def configured_chat(monkeypatch, buffered_chat_stream):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", "https://controlled.invalid/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test")
    monkeypatch.setattr(settings, "llm_model", "controlled-model")


def _message(client, project, content="检索自主降落", action="chat"):
    response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
        "content": content, "action": action, "semantic_mode": "off"}})
    assert response.status_code == 202, response.text
    return response.json()


def _status(client, run_id):
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(client, run_id, waiting):
    response = client.post(f"/api/runs/{run_id}/inputs", json={
        "input_request_id": waiting["input_request"]["id"], "values": {"confirmed": True}})
    assert response.status_code == 202, response.text


def _tool(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def _input_events(db_session, run_id):
    return list(db_session.scalars(select(RunEvent).where(
        RunEvent.run_id == run_id, RunEvent.event_type == "input.required")))


def _source(db_session, project, name):
    source = StoredFile(owner_id=project.owner_id, project_id=project.id, kind="raw", key=f"raw/{name}",
                        original_name=name, sha256="a" * 64, size=1, media_type="application/octet-stream")
    db_session.add(source)
    db_session.flush()
    return source


def _ready_dataset(db_session, project):
    source = _source(db_session, project, "execution.xlsx")
    dataset = Dataset(owner_id=project.owner_id, name="Execution sample", source_sha256="a" * 64)
    db_session.add(dataset)
    db_session.flush()
    version = DatasetVersion(dataset_id=dataset.id, source_file_id=source.id, status="ready")
    db_session.add(version)
    db_session.flush()
    project.dataset_version_id = version.id
    db_session.flush()
    return version


def test_auto_greeting_has_no_execution_confirmation_activity_or_task_card(
        db_session, project, monkeypatch, client, configured_chat):
    calls = []

    def model(messages, tools=None):
        calls.append(tools)
        return ({"content": "我可以帮助检索专利和解释已有分析。"} if tools else
                {"content": json.dumps({"action": "chat", "reason": "用户询问能力"})})

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(data_service, "search_patents", lambda *args: pytest.fail("Greeting searched patents"))
    sent = _message(client, project, "你能做什么", action="auto")
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["origin_message_id"] == sent["message_id"]
    assert done["activities"] == [] and done["input_request"] is None
    assert len(calls) == 2 and _input_events(db_session, sent["run_id"]) == []
    messages = client.get(f"/api/conversations/{sent['conversation_id']}/messages").json()["items"]
    assert len(messages) == 2
    assert not any(card["type"] == "task" for message in messages for card in message["cards"])


def test_retrieval_approval_covers_material_search_and_followup_queries_without_replaying_model(
        db_session, project, monkeypatch, client, configured_chat):
    material = Material(project_id=project.id, file_id=_source(db_session, project, "landing.docx").id,
                        status="ready")
    db_session.add(material)
    db_session.flush()
    db_session.add(MaterialBlock(material_id=material.id, ordinal=1, kind="paragraph",
                                location={"paragraph": 1}, text="landing reference", text_sha256="b" * 64))
    db_session.flush()
    record = SimpleNamespace(id=new_id(), application_number="PCT/EXECUTION/1", title="UAV landing",
                             abstract="Autonomous landing", source_sheet="Patents", source_row=2,
                             dataset_version_id=new_id())
    model_calls, queries = [], []

    def model(messages, tools=None):
        model_calls.append(messages)
        if len(model_calls) == 1:
            return {"content": None, "tool_calls": [
                _tool("search_patents", {"q": "landing"}, "patents"),
                _tool("search_materials", {"q": "landing"}, "materials")]}
        if len(model_calls) == 2:
            return {"content": None, "tool_calls": [
                _tool("search_patents", {"q": "autonomous"}, "followup")]}
        return {"content": "PCT/EXECUTION/1 的摘要包含自主降落，项目资料也记录了降落要求。"}

    def search(session, supplied_project, filters):
        queries.append(filters["q"])
        return {"items": [record] if filters["q"] == "landing" else [], "scope_application_count": 1}

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(data_service, "search_patents", search)
    sent = _message(client, project)
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["status"] == "waiting_input", waiting
    assert waiting["input_request"]["kind"] == "execution_confirmation"
    actions = waiting["input_request"]["actions"]
    assert {action["kind"] for action in actions} == {"search_patents", "search_materials"}
    assert all(action["label"] for action in actions)
    assert waiting["activities"] == [] and queries == [] and len(model_calls) == 1
    assert _status(client, sent["run_id"])["input_request"] == waiting["input_request"]
    _confirm(client, sent["run_id"], waiting)
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["origin_message_id"] == sent["message_id"]
    assert queries == ["landing", "autonomous"] and len(model_calls) == 3
    assert len(_input_events(db_session, sent["run_id"])) == 1
    activities = done["activities"]
    assert [activity["kind"] for activity in activities] == ["search_patents", "search_materials", "search_patents"]
    assert [activity["matched_count"] for activity in activities] == [1, 1, 0]
    assert [activity["source"] for activity in activities] == ["local_patents", "project_materials", "local_patents"]
    assert [activity["query"] for activity in activities] == ["landing", "landing", "autonomous"]
    assert all(activity["status"] == "succeeded" for activity in activities)
    assert _status(client, sent["run_id"])["activities"] == activities
    jobs.execute_run(sent["run_id"])
    assert queries == ["landing", "autonomous"] and len(model_calls) == 3


def test_reading_existing_analysis_does_not_request_execution_confirmation(
        db_session, project, monkeypatch, client, configured_chat):
    calls = []

    def model(messages, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": None, "tool_calls": [_tool("read_analysis", {"view": "overview"}, "overview")]}
        assert json.loads(messages[-1]["content"])["status"] == "unavailable"
        return {"content": "当前对话尚未绑定已有分析。"}

    monkeypatch.setattr(model_provider, "chat", model)
    sent = _message(client, project, "解释已有分析")
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert done["input_request"] is None and len(calls) == 2
    assert _input_events(db_session, sent["run_id"]) == []


@pytest.mark.parametrize("action", ["analyze", "generate_report"])
def test_analysis_and_report_actions_wait_for_approval_before_business_work(
        db_session, project, monkeypatch, client, configured_chat, action):
    _ready_dataset(db_session, project)
    model_calls, work = [], []
    analysis_id = new_id()

    def model(messages, tools=None):
        model_calls.append(messages)
        return {"content": json.dumps({"action": action, "reason": "用户要求更新研究结果"})}

    def analyze(ctx):
        work.append("analyze")
        return {"analysis_id": analysis_id, "application_count": 1}

    def report(ctx):
        assert ctx.payload["analysis_id"] == analysis_id
        work.append("generate_report")
        return {"report_id": new_id(), "report_version_id": new_id()}

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(jobs, "run_analysis", analyze)
    monkeypatch.setattr(report_service, "generate_report", report)
    sent = _message(client, project, "请分析样本并更新研究结果", action="auto")
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["status"] == "waiting_input", waiting
    assert waiting["input_request"]["kind"] == "execution_confirmation"
    assert action in {item["kind"] for item in waiting["input_request"]["actions"]}
    assert work == []
    _confirm(client, sent["run_id"], waiting)
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert work == (["analyze"] if action == "analyze" else ["analyze", "generate_report"])
    assert len(model_calls) == 1 and len(_input_events(db_session, sent["run_id"])) == 1
    assert all(item["status"] == "succeeded" for item in done["activities"])


def test_cancelling_pending_retrieval_never_searches(
        db_session, project, monkeypatch, client, configured_chat):
    monkeypatch.setattr(model_provider, "chat", lambda *args, **kwargs: {
        "content": None, "tool_calls": [_tool("search_patents", {"q": "landing"}, "pending")]})
    searches = []
    monkeypatch.setattr(data_service, "search_patents", lambda *args: searches.append(args))
    sent = _message(client, project)
    jobs.execute_run(sent["run_id"])
    assert _status(client, sent["run_id"])["status"] == "waiting_input"
    response = client.post(f"/api/runs/{sent['run_id']}/cancel")
    assert response.status_code == 200, response.text
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "cancelled" and done["activities"] == [] and searches == []


def test_retry_reuses_approval_and_anchors_original_user_message(
        db_session, project, monkeypatch, client, configured_chat):
    model_calls, searches = [], []

    def model(messages, tools=None):
        model_calls.append(messages)
        if len(model_calls) == 1:
            return {"content": None, "tool_calls": [_tool("search_patents", {"q": "landing"}, "retry-search")]}
        return {"content": "当前检索式没有命中样本。"}

    def search(*args):
        searches.append(args)
        if len(searches) == 1:
            raise RuntimeError("Controlled temporary search failure")
        return {"items": [], "scope_application_count": 1}

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(data_service, "search_patents", search)
    sent = _message(client, project)
    jobs.execute_run(sent["run_id"])
    _confirm(client, sent["run_id"], _status(client, sent["run_id"]))
    jobs.execute_run(sent["run_id"])
    assert _status(client, sent["run_id"])["status"] == "failed"
    response = client.post(f"/api/runs/{sent['run_id']}/retry")
    assert response.status_code == 202, response.text
    retry_id = response.json()["run_id"]
    db_session.expire_all()
    assert db_session.get(Message, sent["message_id"]).run_id == retry_id
    jobs.execute_run(retry_id)
    done = _status(client, retry_id)
    assert done["status"] == "succeeded", done
    assert done["origin_message_id"] == sent["message_id"]
    assert len(model_calls) == 2 and len(searches) == 2
    assert _input_events(db_session, retry_id) == []
    messages = client.get(f"/api/conversations/{sent['conversation_id']}/messages").json()["items"]
    users = [message for message in messages if message["role"] == "user"]
    assert len(users) == 1 and users[0]["id"] == sent["message_id"] and users[0]["run_id"] == retry_id
    assert len([message for message in messages if message["role"] == "assistant"]) == 1


@pytest.mark.parametrize("change", ["scope", "data"])
def test_stale_execution_confirmation_refreshes_before_approving_changed_project_context(
        db_session, project, monkeypatch, client, configured_chat, change):
    original_data = _ready_dataset(db_session, project)
    original_scope = project.scope_version_id
    model_calls, queries = [], []

    def model(messages, tools=None):
        model_calls.append(messages)
        if len(model_calls) == 1:
            return {"content": None, "tool_calls": [_tool("search_patents", {"q": "landing"}, "frozen-search")]}
        return {"content": "本轮已按确认采用的样本完成检索。"}

    def search(session, supplied_project, filters):
        queries.append(filters)
        return {"items": [], "scope_application_count": 1}

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(data_service, "search_patents", search)
    sent = _message(client, project)
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["status"] == "waiting_input", waiting
    if change == "scope":
        replacement = ScopeVersion(project_id=project.id, number=2, confirmed=True, content={"industry": "电池"})
        db_session.add(replacement)
        db_session.flush()
        project.scope_version_id = replacement.id
    else:
        replacement = DatasetVersion(dataset_id=original_data.dataset_id,
                                     source_file_id=original_data.source_file_id, status="ready")
        db_session.add(replacement)
        db_session.flush()
        project.dataset_version_id = replacement.id
    project.revision += 1
    db_session.flush()
    _confirm(client, sent["run_id"], waiting)
    refreshed = _status(client, sent["run_id"])
    assert refreshed["status"] == "waiting_input", refreshed
    assert refreshed["input_request"]["kind"] == "execution_confirmation"
    assert refreshed["input_request"]["id"] != waiting["input_request"]["id"]
    assert refreshed["input_request"]["context"] != waiting["input_request"]["context"]
    assert all(isinstance(detail, str) for action in refreshed["input_request"]["actions"]
               for detail in action["details"])
    assert queries == [] and len(model_calls) == 1
    _confirm(client, sent["run_id"], refreshed)
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert len(queries) == 1 and len(model_calls) == 2
    assert queries[0]["scope_version_id"] == original_scope
    assert queries[0]["dataset_version_ids"] == [original_data.id]
    db_session.expire_all()
    authorization = db_session.get(ResearchRun, sent["run_id"]).payload["execution_authorization"]
    assert authorization["context"] == refreshed["input_request"]["context"]


def test_project_scope_change_between_searches_requires_approval_without_replaying_saved_work(
        db_session, project, monkeypatch, client, configured_chat):
    model_calls, queries = [], []
    original_scope = project.scope_version_id

    def model(messages, tools=None):
        model_calls.append(messages)
        if len(model_calls) <= 2:
            query = "landing" if len(model_calls) == 1 else "autonomous"
            return {"content": None, "tool_calls": [_tool("search_patents", {"q": query}, query)]}
        return {"content": "两次检索均未命中本轮冻结样本。"}

    def search(session, supplied_project, filters):
        queries.append(filters)
        if len(queries) == 1:
            replacement = ScopeVersion(project_id=supplied_project.id, number=2, confirmed=True,
                                       content={"industry": "电池"})
            session.add(replacement)
            session.flush()
            supplied_project.scope_version_id = replacement.id
            supplied_project.revision += 1
        return {"items": [], "scope_application_count": 1}

    monkeypatch.setattr(model_provider, "chat", model)
    monkeypatch.setattr(data_service, "search_patents", search)
    sent = _message(client, project)
    jobs.execute_run(sent["run_id"])
    first = _status(client, sent["run_id"])
    _confirm(client, sent["run_id"], first)
    jobs.execute_run(sent["run_id"])
    second = _status(client, sent["run_id"])
    assert second["status"] == "waiting_input", second
    assert second["input_request"]["kind"] == "execution_confirmation"
    assert second["input_request"]["id"] != first["input_request"]["id"]
    assert second["input_request"]["context"] != first["input_request"]["context"]
    assert [query["q"] for query in queries] == ["landing"] and len(model_calls) == 2
    assert len(second["activities"]) == 1 and second["activities"][0]["status"] == "succeeded"
    _confirm(client, sent["run_id"], second)
    jobs.execute_run(sent["run_id"])
    done = _status(client, sent["run_id"])
    assert done["status"] == "succeeded", done
    assert [query["q"] for query in queries] == ["landing", "autonomous"] and len(model_calls) == 3
    assert all(query["scope_version_id"] == original_scope for query in queries)
    assert len(_input_events(db_session, sent["run_id"])) == 2
    assert len(done["activities"]) == 2
    assert all(activity["status"] == "succeeded" for activity in done["activities"])


def test_legacy_activity_fallback_uses_saved_tool_names_and_omits_analysis_reads_and_model_steps(
        db_session, project, client):
    sent = _message(client, project)
    run = db_session.get(ResearchRun, sent["run_id"])
    run.status = "succeeded"
    results = {
        "understand_request": {"action": "chat", "reason": "检索资料后解释已有结果"},
        "chat_model_0": {"tool_calls": [
            _tool("search_materials", {"q": "landing"}, "materials"),
            _tool("search_patents", {"q": "landing"}, "patents"),
            _tool("read_analysis", {"view": "overview"}, "overview")]},
        "chat_search_0_0": {"matched_count": 2, "records": []},
        "chat_search_0_1": {"matched_count": 0, "records": []},
        "chat_search_0_2": {"matched_count": 99, "view": "overview", "records": []},
        "chat_model_1": {"content": "已完成解释。"},
    }
    for key, result in results.items():
        db_session.add(RunStep(run_id=run.id, key=key, status="succeeded", fingerprint="f" * 64, result=result))
    db_session.flush()
    done = _status(client, run.id)
    assert [{key: activity[key] for key in ("id", "kind", "status", "matched_count")}
            for activity in done["activities"]] == [
        {"id": "chat_search_0_0", "kind": "search_materials", "status": "succeeded", "matched_count": 2},
        {"id": "chat_search_0_1", "kind": "search_patents", "status": "succeeded", "matched_count": 0}]
    ordinary = _message(client, project, "你能做什么")
    ordinary_run = db_session.get(ResearchRun, ordinary["run_id"])
    ordinary_run.status = "succeeded"
    for key in ("understand_request", "chat_model_0"):
        db_session.add(RunStep(run_id=ordinary_run.id, key=key, status="succeeded", fingerprint="f" * 64,
                              result={"content": "我可以帮助你研究专利样本。"}))
    db_session.flush()
    assert _status(client, ordinary_run.id)["activities"] == []
