"""The original message API runs the shared prompt/skills/tool loop end to end."""

import asyncio
import base64
from datetime import date
import hashlib
from io import BytesIO
import json
from types import SimpleNamespace
from uuid import uuid4

from docx import Document
from fastapi.testclient import TestClient
from mcp import Client
import pytest
from sqlalchemy import select

from app import agent_runtime, jobs, model_provider, report_service, task_service, tool_service
from app.config import get_settings
from app.errors import AppError
from app.main import create_app
from app.mcp_server import create_mcp
from app.models import AnalysisResult, Message, PatentApplication, ResearchRun, RunEvent
from patentcompass_agent import read_skill, read_system_prompt
from test_portable_agent import source, extraction


@pytest.fixture
def client(db_session, monkeypatch):
    settings = get_settings()
    for key, value in {"llm_model": "controlled-host", "llm_base_url": "https://controlled.invalid", "llm_api_key": "test"}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)
    monkeypatch.setattr(model_provider, "chat", lambda *args, **kwargs: pytest.fail("New conversation called a legacy generation path"))
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


class Director:
    def __init__(self, script):
        self.script, self.started, self.calls = script, False, []

    def __call__(self, messages, tools=None, on_delta=None):
        self.calls.append(json.loads(json.dumps(messages)))
        assert messages[0]["content"] == read_system_prompt()
        assert {tool["function"]["name"] for tool in tools} >= {"prepare_agent_action", "load_agent_skill", "start_agent_task"}
        previous = next((json.loads(item["content"]) for item in reversed(messages) if item["role"] == "tool"), None)
        try:
            step = next(self.script) if not self.started else self.script.send(previous)
            self.started = True
        except StopIteration as done:
            if on_delta:
                on_delta(done.value)
            return {"content": done.value}
        name, arguments = step
        return {"content": None, "tool_calls": [{"id": f"call-{len(self.calls)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def _plan(action, **extra):
    return {"action": action, "reason": "明确的受控用户请求", **extra}


def _analysis(project):
    yield "load_agent_skill", {"name": "patent-semantic-analysis"}
    yield "read_agent_reference", {"name": "patent-semantic-analysis", "reference": "references/extraction.md"}
    prepared = yield "prepare_agent_action", _plan("analyze", semantic_mode="full")
    value = yield "get_agent_context", {"request": {"project_id": project.id}}
    task = yield "start_semantic_analysis", {"request": {"project_id": project.id, "expected_revision": value["context"]["revision"],
        "agent_model": "controlled-host", "skill_version": "1"}, "idempotency_key": "start"}
    assert "error" not in task, task
    for index in range(task["batch_count"]):
        batch = yield "get_semantic_batch", {"request": {"run_id": task["run_id"], "batch_index": index}}
        yield "submit_semantic_batch", {"request": {"run_id": task["run_id"], "batch_index": index,
            "records": extraction(batch)}, "idempotency_key": "batch" + str(index)}
    for dimension in ("elements", "methods", "scenarios"):
        value = yield "get_semantic_taxonomy", {"request": {"run_id": task["run_id"], "dimension": dimension}}
        yield "submit_semantic_taxonomy", {"request": {"run_id": task["run_id"], "dimension": dimension,
            "themes": [{"label": dimension, "members": [row["id"] for row in value["labels"]]}]}, "idempotency_key": dimension}
    yield "finish_semantic_analysis", {"request": {"run_id": task["run_id"]}, "idempotency_key": "finish"}
    value = yield "get_run", {"run_id": task["run_id"]}
    assert value["status"] == "succeeded", value
    return str(task["total_count"]) + "条样本已完成全量语义分析，结果与原文证据已保存。"


def _report(project):
    yield "load_agent_skill", {"name": "patent-navigation-report"}
    prepared = yield "prepare_agent_action", _plan("generate_report", research_intent="new", semantic_mode="off")
    research = prepared["context"]["research_id"]
    value = yield "get_agent_context", {"request": {"project_id": project.id, "research_id": research}}
    ctx = value["context"]
    yield "read_analysis", {"request": {"analysis_id": ctx["analysis_id"], "view": "technology"}}
    task = yield "start_agent_task", {"request": {"project_id": project.id, "research_id": research,
        "expected_revision": ctx["revision"], "action": "generate_report", "analysis_id": ctx["analysis_id"],
        "expected_current_version_id": ctx["current_revision_id"], "expected_adopted_version_id": value["adopted_report_version_id"],
        "summary_text": "本报告采用两条受控样本，候选方向尚需补检。"}, "idempotency_key": "report"}
    result = yield "get_run", {"run_id": task["run_id"]}
    assert result["status"] == "succeeded", result
    return "导航报告已保存为项目研究版本。"


def _qa(project):
    yield "load_agent_skill", {"name": "patent-evidence-qa"}
    prepared = yield "prepare_agent_action", _plan("chat")
    basis = prepared["answer_basis"]
    result = yield "search_patents", {"request": {"dataset_version_ids": basis["dataset_version_ids"], "scope": basis["scope"], "q": "camera"}}
    assert result["items"], result
    return "依据原文，" + result["items"][0]["application_number"] + " 涉及摄像头辅助降落。"


def _revision(project):
    yield "load_agent_skill", {"name": "patent-result-revision"}
    operations = [{"op": "set_title", "text": "统一内置 Agent 验收报告"}]
    prepared = yield "prepare_agent_action", _plan("revise_report", research_intent="continue", operations=operations)
    research = prepared["context"]["research_id"]
    value = yield "get_agent_context", {"request": {"project_id": project.id, "research_id": research}}
    ctx = value["context"]
    task = yield "start_agent_task", {"request": {"project_id": project.id, "research_id": research,
        "action": "revise_report", "expected_revision": ctx["revision"], "base_version_id": ctx["current_revision_id"],
        "expected_current_version_id": ctx["current_revision_id"], "operations": operations}, "idempotency_key": "revision"}
    done = yield "get_run", {"run_id": task["run_id"]}
    assert done["status"] == "succeeded", done
    return "标题已修改，原有证据和报告历史保留。"


def _export(project):
    yield "load_agent_skill", {"name": "patent-navigation-report"}
    prepared = yield "prepare_agent_action", _plan("export")
    research = prepared["context"]["research_id"]
    value = yield "get_agent_context", {"request": {"project_id": project.id, "research_id": research}}
    ctx = value["context"]
    task = yield "start_agent_task", {"request": {"project_id": project.id, "research_id": research,
        "action": "export", "expected_revision": ctx["revision"], "report_version_id": ctx["current_revision_id"]}, "idempotency_key": "export"}
    done = yield "get_run", {"run_id": task["run_id"]}
    assert done["status"] == "succeeded", done
    return "[下载报告](" + done["result"]["download_url"] + ")"


def _chart_style(project, research_ref):
    yield "load_agent_skill", {"name": "patent-result-revision"}
    value = yield "get_agent_context", {"request": {"project_id": project.id, "research_id": research_ref["id"]}}
    chart = next(row for row in value["charts"] if row["kind"] == "yearly")
    yield "prepare_agent_action", _plan("revise_chart", chart_id=chart["id"], chart_mode="style")
    task = yield "start_agent_task", {"request": {"project_id": project.id, "research_id": research_ref["id"],
        "expected_revision": value["context"]["revision"], "action": "revise_chart", "chart_id": chart["id"],
        "base_version_id": chart["version_id"], "expected_current_version_id": chart["version_id"],
        "chart_mode": "style", "chart_option": {"title": {"text": "样本年度分布"}}}, "idempotency_key": "chart-style"}
    result = yield "get_run", {"run_id": task["run_id"]}
    assert result["status"] == "succeeded", result
    return "图表标题已更新，统计和报告冻结图表保持不变。"


def _run_message(client, project, conversation, text, monkeypatch, script, choice=None):
    director = Director(script)
    monkeypatch.setattr(model_provider, "stream_chat", director)
    body = {"content": text, "action": "auto", "semantic_mode": "full"}
    response = (client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": body}) if conversation is None
                else client.post(f"/api/conversations/{conversation}/messages", json=body))
    assert response.status_code == 202, response.text
    sent = response.json()
    confirmed = 0
    for _ in range(5):
        jobs.execute_run(sent["run_id"])
        state = client.get(f"/api/runs/{sent['run_id']}").json()
        if state["status"] != "waiting_input":
            break
        question = state["input_request"]
        assert question["kind"] == "execution_confirmation", question
        values = {"confirmed": True}
        if question.get("research_assignment"):
            values["research_choice"] = choice
        result = client.post(f"/api/runs/{sent['run_id']}/inputs", json={"input_request_id": question["id"],
            "expected_scope_version_id": state["scope_version_id"], "values": values})
        assert result.status_code == 202, result.text
        confirmed += 1
    assert state["status"] == "succeeded", state
    return sent, state, director, confirmed


def test_original_conversation_runs_analysis_qa_report_revision_and_export(client, db_session, source, monkeypatch):
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZHkAAAAASUVORK5CYII=")
    monkeypatch.setattr(report_service, "render_chart", lambda option, path: path.write_bytes(png))
    conversation, states, research_ref = None, [], {}
    for text, script, choice in [("全量语义分析，不要报告", _analysis(source), None),
                                 ("生成独立导航报告", _report(source), "new"),
                                 ("仅修改年度图表的标题", _chart_style(source, research_ref), None),
                                 ("解释摄像头降落证据", _qa(source), None),
                                 ("将标题改为统一内置 Agent 验收报告", _revision(source), "existing"),
                                 ("导出当前报告 Word", _export(source), None)]:
        sent, state, director, confirmed = _run_message(client, source, conversation, text, monkeypatch, script, choice)
        conversation = state["result"]["conversation_id"]
        states.append(state)
        if state["result"].get("research_id"):
            research_ref["id"] = state["result"]["research_id"]
        assert confirmed >= 1
        events = list(db_session.scalars(select(RunEvent).where(RunEvent.run_id == sent["run_id"])))
        started = next(row for row in events if row.event_type == "agent.started")
        assert started.payload["system_prompt_sha256"] == hashlib.sha256(read_system_prompt().encode()).hexdigest()
        assert any(row.event_type == "agent.skill.loaded" for row in events)
        loaded = db_session.get(ResearchRun, sent["run_id"]).payload["agent_runtime"]["skills"]
        assert all(value["content"] == read_skill(name)["content"] for name, value in loaded.items())
    response = client.get(states[-1]["result"]["download_url"])
    assert response.status_code == 200
    document = Document(BytesIO(response.content))
    assert any("统一内置 Agent 验收报告" in row.text for row in document.paragraphs)
    assert document.inline_shapes
    answers = list(db_session.scalars(select(Message).where(Message.conversation_id == conversation, Message.role == "assistant")))
    assert len(answers) == 6


def test_mcp_and_builtin_share_input_contract_and_skill_content(db_session):
    server, _ = create_mcp()
    async def verify():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name, spec in tool_service.catalogue().items():
                actual, expected = tools[name].input_schema, spec.arguments.model_json_schema()
                assert actual["properties"] == expected["properties"], name
                assert set(actual.get("required", [])) == set(expected.get("required", [])), name
                assert actual.get("$defs") == expected.get("$defs"), name
            result = await client.call_tool("load_agent_skill", {"name": "patent-semantic-analysis"})
            assert result.structured_content == read_skill("patent-semantic-analysis")
            bad = await client.call_tool("read_agent_reference", {"name": "patent-semantic-analysis", "reference": "../../app/config.py"})
            assert bad.is_error
    asyncio.run(verify())


def test_confirmation_resumes_pending_tool_without_repeating_model(client, db_session, source, monkeypatch):
    director = Director(_qa(source))
    monkeypatch.setattr(model_provider, "stream_chat", director)
    response = client.post(f"/api/projects/{source.id}/conversations", json={"initial_message": {
        "content": "请检索摄像头原文", "action": "auto"}}).json()
    jobs.execute_run(response["run_id"])
    state = client.get(f"/api/runs/{response['run_id']}").json()
    assert state["status"] == "waiting_input"
    assert len(director.calls) == 3
    assert db_session.get(ResearchRun, response["run_id"]).payload["agent_runtime_version"] == 1
    client.post(f"/api/runs/{response['run_id']}/inputs", json={"input_request_id": state["input_request"]["id"], "values": {"confirmed": True}})
    jobs.execute_run(response["run_id"])
    assert len(director.calls) == 4
    assert client.get(f"/api/runs/{response['run_id']}").json()["status"] == "succeeded"


def test_cancelling_pending_search_never_executes_it(client, db_session, source, monkeypatch):
    director = Director(_qa(source))
    monkeypatch.setattr(model_provider, "stream_chat", director)
    response = client.post(f"/api/projects/{source.id}/conversations", json={"initial_message": {
        "content": "请检索摄像头原文", "action": "auto"}}).json()
    jobs.execute_run(response["run_id"])
    assert client.get(f"/api/runs/{response['run_id']}").json()["status"] == "waiting_input"
    client.post(f"/api/runs/{response['run_id']}/cancel")
    jobs.execute_run(response["run_id"])
    assert len(director.calls) == 3
    assert not list(db_session.scalars(select(Message).where(Message.run_id == response["run_id"], Message.role == "assistant")))


def test_scope_and_project_guards_reject_model_selected_foreign_data(db_session, source, context_factory):
    ctx = context_factory(source, "auto", {"plan": {"action": "chat"}})
    state = {"skills": {"patent-evidence-qa": {}}, "children": []}
    with pytest.raises(AppError) as error:
        agent_runtime._scope_tool(ctx, state, "get_agent_context", {"request": {"project_id": str(uuid4())}})
    assert error.value.code == "PROJECT_NOT_FOUND"
    with pytest.raises(AppError) as error:
        agent_runtime._scope_tool(ctx, state, "search_patents", {"request": {
            "dataset_version_ids": ctx.dataset_version_ids, "scope": {"q": "wider scope"}, "q": "camera"}})
    assert error.value.code == "SCOPE_SNAPSHOT_FIXED"


def test_final_answer_cannot_claim_a_missing_business_artifact():
    ctx = SimpleNamespace(payload={"plan": {"action": "generate_report"}})
    with pytest.raises(AppError) as error:
        agent_runtime._answer(ctx, {"artifacts": {}}, {"content": "报告已生成"})
    assert error.value.code == "AGENT_RESULT_INCOMPLETE"


def test_model_contract_expands_schema_references_without_changing_validation():
    definitions = {tool["function"]["name"]: tool["function"]["parameters"] for tool in agent_runtime.definitions()}
    semantic = definitions["start_semantic_analysis"]["properties"]["request"]
    assert semantic["type"] == "object" and "expected_revision" in semantic["required"]
    assert "$ref" not in json.dumps(definitions)
    with pytest.raises(AppError) as error:
        tool_service.validate_arguments("start_semantic_analysis", {"request": "not an object", "idempotency_key": "test"})
    assert error.value.code == "VALIDATION_ERROR"


def test_explicit_statistical_message_still_works_without_generation_model(client, source, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_model", "")
    response = client.post(f"/api/projects/{source.id}/conversations", json={"initial_message": {
        "content": "计算样本统计", "action": "analyze", "semantic_mode": "off"}}).json()
    jobs.execute_run(response["run_id"])
    state = client.get(f"/api/runs/{response['run_id']}").json()
    assert state["status"] == "waiting_input"
    client.post(f"/api/runs/{response['run_id']}/inputs", json={"input_request_id": state["input_request"]["id"], "values": {"confirmed": True}})
    jobs.execute_run(response["run_id"])
    done = client.get(f"/api/runs/{response['run_id']}").json()
    assert done["status"] == "succeeded" and done["result"]["analysis_id"]


def test_completed_tool_checkpoint_resumes_without_reloading_or_replaying_model(client, db_session, source, monkeypatch):
    def script():
        yield "load_agent_skill", {"name": "patent-evidence-qa"}
        yield "prepare_agent_action", _plan("chat")
        return "可以帮助检索专利和解释证据。"
    director = Director(script())
    monkeypatch.setattr(model_provider, "stream_chat", director)
    original = agent_runtime._save
    crashed = []
    def save(ctx, state):
        original(ctx, state)
        if not crashed and state.get("pending", {}).get("index") == 1:
            crashed.append(True)
            raise RuntimeError("interrupted after tool checkpoint")
    monkeypatch.setattr(agent_runtime, "_save", save)
    sent = client.post(f"/api/projects/{source.id}/conversations", json={"initial_message": {
        "content": "你能做什么", "action": "auto"}}).json()
    jobs.execute_run(sent["run_id"])
    assert client.get(f"/api/runs/{sent['run_id']}").json()["status"] == "failed"
    retry = client.post(f"/api/runs/{sent['run_id']}/retry").json()
    monkeypatch.setattr(agent_runtime, "_save", original)
    jobs.execute_run(retry["run_id"])
    assert client.get(f"/api/runs/{retry['run_id']}").json()["status"] == "succeeded"
    assert len(director.calls) == 3
    loaded = list(db_session.scalars(select(RunEvent).where(RunEvent.run_id.in_([sent["run_id"], retry["run_id"]]),
                                                           RunEvent.event_type == "agent.skill.loaded")))
    assert len(loaded) == 1


def test_multiple_batches_keep_model_context_bounded_after_submission(client, db_session, source, monkeypatch):
    for index in range(20):
        db_session.add(PatentApplication(dataset_version_id=source.dataset_version_id, application_number=f"EXTRA-{index}",
            application_date=date(2024, 1, 1), title="Camera landing", abstract="A camera supports autonomous landing on a moving platform.",
            applicants=["A"], ipc_codes=["G01"], countries=["CN"], source_sheet="Sample", source_row=index + 4))
    db_session.flush()
    _, state, director, _ = _run_message(client, source, None, "全量分析22条样本，不生成报告", monkeypatch, _analysis(source))
    analysis = db_session.get(AnalysisResult, state["result"]["analysis_id"])
    assert analysis.summary["semantic"]["processed_count"] == 22
    for messages in director.calls:
        assert sum(item.get("content", "").count("A camera supports autonomous landing on a moving platform.")
                   for item in messages if isinstance(item.get("content"), str)) <= 20


def test_attachment_only_message_uses_parsed_material_in_shared_runtime(client, source, monkeypatch):
    document = Document()
    document.add_paragraph("研究需求：比较摄像头辅助自主降落的方法。")
    buffer = BytesIO()
    document.save(buffer)
    uploaded = client.post(f"/api/projects/{source.id}/materials", files={"file": ("requirements.docx", buffer.getvalue())}).json()
    jobs.execute_run(uploaded["run_id"])
    def script():
        yield "load_agent_skill", {"name": "patent-evidence-qa"}
        yield "prepare_agent_action", _plan("chat")
        return "已读取研究需求，请说明希望优先回答的研究问题。"
    director = Director(script())
    monkeypatch.setattr(model_provider, "stream_chat", director)
    sent = client.post(f"/api/projects/{source.id}/conversations", json={"initial_message": {
        "attachments": [uploaded["material_id"]], "action": "auto"}}).json()
    jobs.execute_run(sent["run_id"])
    assert client.get(f"/api/runs/{sent['run_id']}").json()["status"] == "succeeded"
    assert "研究需求：比较摄像头" in json.dumps(director.calls[0], ensure_ascii=False)
    assert all(item["content"] for item in director.calls[0] if item["role"] == "user")


def test_cancelling_parent_requests_cancellation_of_unfinished_children(db_session, source, context_factory):
    parent = context_factory(source, "auto", {"agent_runtime_version": 1})
    child = jobs.create_run(db_session, "agent_analysis", source, {"parent_run_id": parent.run_id})
    child.status, child.dispatch_pending = "waiting_input", False
    db_session.flush()
    task_service.cancel_run(db_session, parent.run_id)
    assert child.status == "cancelled" and child.cancellation_requested


def test_known_report_edit_requires_operations_before_original_confirmation():
    with pytest.raises(AppError) as error:
        agent_runtime._prepare(SimpleNamespace(payload={}), _plan("revise_report"))
    assert error.value.code == "REPORT_TARGET_REQUIRED"


def test_runtime_feedback_does_not_replace_the_original_user_request(client, db_session, source, monkeypatch):
    def script():
        yield "load_agent_skill", {"name": "patent-evidence-qa"}
        yield "prepare_agent_action", _plan("chat")
        return "可以帮助研究专利样本。"
    director = Director(script())
    first = []
    def model(messages, tools=None, on_delta=None):
        assert [item["content"] for item in messages if item["role"] == "user"][-1] == "请说明你能做什么"
        if not first:
            first.append(True)
            if on_delta:
                on_delta("任务已完成")
            return {"content": "任务已完成"}
        return director(messages, tools, on_delta)
    monkeypatch.setattr(model_provider, "stream_chat", model)
    sent = client.post(f"/api/projects/{source.id}/conversations", json={"initial_message": {
        "content": "请说明你能做什么", "action": "auto"}}).json()
    jobs.execute_run(sent["run_id"])
    state = client.get(f"/api/runs/{sent['run_id']}").json()
    assert state["status"] == "succeeded", state
    answer = db_session.scalar(select(Message).where(Message.run_id == sent["run_id"], Message.role == "assistant"))
    assert answer.content == "可以帮助研究专利样本。"


def test_approved_report_operations_cannot_be_changed_before_execution(db_session, source, context_factory):
    version = str(uuid4())
    ctx = context_factory(source, "auto", {"plan": {"action": "revise_report", "operations": [{"op": "set_title", "text": "Approved"}]},
                                           "plan_report_version_id": version})
    with pytest.raises(AppError) as error:
        agent_runtime._scope_tool(ctx, {"skills": {"patent-result-revision": {}}, "children": []}, "start_agent_task", {
            "request": {"project_id": source.id, "action": "revise_report", "base_version_id": version,
                        "operations": [{"op": "set_title", "text": "Different"}]}})
    assert error.value.code == "REPORT_OPERATIONS_CHANGED"


def test_explicit_message_action_cannot_be_downgraded_to_chat():
    with pytest.raises(AppError) as error:
        agent_runtime._prepare(SimpleNamespace(payload={}, kind="analyze"), _plan("chat"))
    assert error.value.code == "ACTION_NOT_AUTHORIZED"
