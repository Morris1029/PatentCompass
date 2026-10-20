"""Streaming adapter, immediately persisted answer drafts, and recovery behavior."""
from contextlib import contextmanager
import json

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from app import data_service, jobs, model_provider
from app.chat_streaming import stream_for_run
from app.config import get_settings
from app.errors import AppError
from app.main import create_app
from app.models import Message, ResearchRun, RunEvent, RunStep, new_id


@pytest.fixture(autouse=True)
def no_heartbeat_thread(monkeypatch):
    monkeypatch.setattr(jobs, "_heartbeat", lambda *args: None)


@pytest.fixture
def client(db_session):
    with TestClient(create_app(start_dispatcher=False)) as value:
        yield value


@pytest.fixture
def configured_chat(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", "https://controlled.invalid/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test")
    monkeypatch.setattr(settings, "llm_model", "controlled-model")


def _mock_sse(monkeypatch, chunks, done=True):
    requests = []
    lines = [": keepalive", ""]
    for chunk in chunks:
        lines.extend(["data: " + json.dumps(chunk, ensure_ascii=False), ""])
    if done:
        lines.extend(["data: [DONE]", ""])

    @contextmanager
    def stream(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        response = httpx.Response(200, text="\n".join(lines), request=httpx.Request(method, url),
                                  headers={"Content-Type": "text/event-stream"})
        try:
            yield response
        finally:
            response.close()

    monkeypatch.setattr(httpx, "stream", stream)
    return requests


def _chunk(delta, finish_reason=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def _send(client, project):
    response = client.post(f"/api/projects/{project.id}/conversations", json={"initial_message": {
        "content": "请说明已有研究范围", "action": "chat", "semantic_mode": "off"}})
    assert response.status_code == 202, response.text
    return response.json()


def _status(client, run_id):
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _events(db_session, run_id):
    return list(db_session.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)))


@pytest.mark.timeout(10)
def test_waiting_confirmation_stream_delivers_pending_events_then_releases_connection(client, db_session, project):
    sent = _send(client, project)
    run = db_session.get(ResearchRun, sent["run_id"])
    run.status = "waiting_input"
    run.input_request = {"id": new_id(), "kind": "execution_confirmation", "actions": []}
    jobs.emit_event(db_session, run.id, "input.required", run.input_request)
    db_session.flush()
    response = client.get(f"/api/runs/{run.id}/events")
    assert response.status_code == 200
    assert "event: input.required" in response.text and run.input_request["id"] in response.text
    after = max(event.seq for event in _events(db_session, run.id))
    assert client.get(f"/api/runs/{run.id}/events?after={after}").text == ""


def test_adapter_streams_only_public_content_and_reassembles_interleaved_tool_fragments(
        monkeypatch, configured_chat):
    monkeypatch.setattr(get_settings(), "llm_extra_body", {"temperature": 0.2, "stream": False})
    chunks = [
        _chunk({"role": "assistant", "reasoning_content": "PRIVATE_REASONING"}),
        _chunk({"content": "你好🧭"}),
        _chunk({"tool_calls": [
            {"index": 1, "id": "call-", "function": {"name": "search_", "arguments": '{"q":"'}},
            {"index": 0, "id": "read-", "function": {"name": "read_", "arguments": '{"view":"over'}}]}),
        _chunk({"content": "\n**结果**", "reasoning_content": "MORE_PRIVATE_REASONING"}),
        _chunk({"tool_calls": [
            {"index": 0, "id": "analysis", "function": {"name": "analysis", "arguments": 'view"}'}},
            {"index": 1, "id": "patent", "function": {"name": "patents", "arguments": 'landing"}'}}]}, "tool_calls"),
    ]
    requests = _mock_sse(monkeypatch, chunks)
    deltas = []
    tools = [{"type": "function", "function": {"name": "search_patents"}}]
    messages = [{"role": "user", "content": "检索降落"}]
    result = model_provider.stream_chat(messages, tools=tools, on_delta=deltas.append)
    assert deltas == ["你好🧭", "\n**结果**"]
    assert result["role"] == "assistant" and result["content"] == "你好🧭\n**结果**"
    assert result["tool_calls"] == [
        {"id": "read-analysis", "type": "function", "function": {"name": "read_analysis", "arguments": '{"view":"overview"}'}},
        {"id": "call-patent", "type": "function", "function": {"name": "search_patents", "arguments": '{"q":"landing"}'}}]
    assert "PRIVATE_REASONING" not in json.dumps(result)
    assert len(requests) == 1 and requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://controlled.invalid/v1/chat/completions"
    assert requests[0]["json"] == {"temperature": 0.2, "messages": messages, "tools": tools,
                                    "stream": True, "model": "controlled-model"}


def test_adapter_rejects_unfinished_stream_after_exposing_received_content(monkeypatch, configured_chat):
    _mock_sse(monkeypatch, [_chunk({"content": "尚未完成"})], done=False)
    deltas = []
    with pytest.raises(AppError) as failure:
        model_provider.stream_chat([{"role": "user", "content": "说明范围"}], on_delta=deltas.append)
    assert failure.value.code == "MODEL_RESPONSE_INVALID"
    assert deltas == ["尚未完成"]


def test_public_deltas_are_queryable_before_final_message_is_saved_with_unicode_offsets(
        db_session, project, monkeypatch, client, configured_chat):
    sent = _send(client, project)
    fragments = ["你好", "🧭", "，这是**结果**。"]
    drafts = []

    def stream(messages, tools=None, on_delta=None):
        for index, fragment in enumerate(fragments):
            on_delta(fragment)
            state = _status(client, sent["run_id"])
            drafts.append(state["stream"])
            assert state["stream"]["content"] == "".join(fragments[:index + 1])
            assert state["stream"]["status"] == "streaming"
            assert state["stream"]["offset"] == len("".join(fragments[:index + 1]))
            assert db_session.scalar(select(Message.id).where(
                Message.run_id == sent["run_id"], Message.role == "assistant")) is None
        return {"role": "assistant", "content": "".join(fragments)}

    monkeypatch.setattr(model_provider, "stream_chat", stream)
    jobs.execute_run(sent["run_id"])
    completed = _status(client, sent["run_id"])
    assert completed["status"] == "succeeded", completed
    assert completed["stream"]["content"] == "".join(fragments)
    assert completed["stream"]["status"] == "completed"
    assert completed["stream"]["message_id"] == completed["result"]["message_id"]
    deltas = [event.payload for event in _events(db_session, sent["run_id"]) if event.event_type == "message.delta"]
    assert [delta["offset"] for delta in deltas] == [0, 2, 3]
    assert [delta["delta"] for delta in deltas] == fragments
    assert [draft["seq"] for draft in drafts] == sorted({draft["seq"] for draft in drafts})
    cached = db_session.scalar(select(RunStep).where(
        RunStep.run_id == sent["run_id"], RunStep.key == "chat_model_0"))
    assert cached.result["_stream_id"] == completed["stream"]["stream_id"]


def test_tool_round_resets_transitional_draft_before_confirmation_and_keeps_final_answer_only(
        db_session, project, monkeypatch, client, configured_chat):
    calls, searches = [], []

    def stream(messages, tools=None, on_delta=None):
        calls.append(messages)
        if len(calls) == 1:
            on_delta("我先查找相关资料。")
            return {"content": "我先查找相关资料。", "tool_calls": [{"id": "stream-search", "type": "function", "function": {
                "name": "search_patents", "arguments": '{"q":"landing"}'}}]}
        on_delta("检索完成🧭")
        return {"content": "检索完成🧭"}

    def search(*args):
        searches.append(args)
        return {"items": [], "scope_application_count": 1}

    monkeypatch.setattr(model_provider, "stream_chat", stream)
    monkeypatch.setattr(data_service, "search_patents", search)
    sent = _send(client, project)
    jobs.execute_run(sent["run_id"])
    waiting = _status(client, sent["run_id"])
    assert waiting["status"] == "waiting_input", waiting
    assert waiting["input_request"]["kind"] == "execution_confirmation"
    assert waiting["stream"]["status"] == "reset" and waiting["stream"]["content"] == ""
    assert searches == [] and len(calls) == 1
    assert db_session.scalar(select(Message.id).where(
        Message.run_id == sent["run_id"], Message.role == "assistant")) is None
    approved = client.post(f"/api/runs/{sent['run_id']}/inputs", json={
        "input_request_id": waiting["input_request"]["id"], "values": {"confirmed": True}})
    assert approved.status_code == 202, approved.text
    jobs.execute_run(sent["run_id"])
    completed = _status(client, sent["run_id"])
    assert completed["status"] == "succeeded", completed
    assert len(calls) == 2 and len(searches) == 1
    assert completed["stream"]["content"] == "检索完成🧭"
    assistant = db_session.get(Message, completed["result"]["message_id"])
    assert assistant.content == "检索完成🧭"
    events = _events(db_session, sent["run_id"])
    assert len([event for event in events if event.event_type == "message.reset"]) == 1
    assert len([event for event in events if event.event_type == "message.started"]) == 2


def test_reasoning_never_reaches_stream_snapshots_saved_answers_or_public_run_events(
        db_session, project, monkeypatch, client, configured_chat):
    _mock_sse(monkeypatch, [
        _chunk({"reasoning_content": "PRIVATE_CHAIN_OF_THOUGHT"}),
        _chunk({"content": "公开回答"}),
        _chunk({"reasoning_content": "ANOTHER_PRIVATE_THOUGHT"}),
        _chunk({}, "stop")])
    sent = _send(client, project)
    jobs.execute_run(sent["run_id"])
    completed = _status(client, sent["run_id"])
    assert completed["status"] == "succeeded", completed
    assert completed["stream"]["content"] == "公开回答"
    assistant = db_session.get(Message, completed["result"]["message_id"])
    assert assistant.content == "公开回答"
    public = json.dumps({"run": completed, "events": [event.payload for event in _events(db_session, sent["run_id"])]})
    assert "PRIVATE_CHAIN_OF_THOUGHT" not in public and "ANOTHER_PRIVATE_THOUGHT" not in public
    assert "reasoning_content" not in public


def test_retry_uses_completed_model_checkpoint_without_rebroadcasting_deltas(
        db_session, project, monkeypatch, client, configured_chat):
    calls, interrupted = [], []
    original_emit = jobs.emit_event

    def stream(messages, tools=None, on_delta=None):
        calls.append(messages)
        on_delta("已取得")
        on_delta("完整回答🧭")
        return {"content": "已取得完整回答🧭"}

    def interrupt_before_message_commit(session, run_id, event_type, payload):
        if event_type == "message.completed" and not interrupted:
            interrupted.append(run_id)
            raise RuntimeError("Controlled interruption after model checkpoint")
        return original_emit(session, run_id, event_type, payload)

    monkeypatch.setattr(model_provider, "stream_chat", stream)
    monkeypatch.setattr(jobs, "emit_event", interrupt_before_message_commit)
    sent = _send(client, project)
    jobs.execute_run(sent["run_id"])
    failed = _status(client, sent["run_id"])
    assert failed["status"] == "failed", failed
    assert failed["stream"]["status"] == "failed" and failed["stream"]["content"] == "已取得完整回答🧭"
    response = client.post(f"/api/runs/{sent['run_id']}/retry")
    assert response.status_code == 202, response.text
    retry_id = response.json()["run_id"]
    jobs.execute_run(retry_id)
    completed = _status(client, retry_id)
    assert completed["status"] == "succeeded", completed
    assert len(calls) == 1
    assert completed["stream"]["status"] == "completed" and completed["stream"]["content"] == "已取得完整回答🧭"
    assert completed["stream"]["stream_id"].startswith(f"{retry_id}:")
    assert completed["stream"]["stream_id"] != failed["stream"]["stream_id"]
    retry_events = _events(db_session, retry_id)
    assert not any(event.event_type in {"message.started", "message.delta"} for event in retry_events)
    messages = client.get(f"/api/conversations/{sent['conversation_id']}/messages").json()["items"]
    assert len([message for message in messages if message["role"] == "assistant"]) == 1


def test_stream_recovery_ignores_duplicate_offsets_and_superseded_streams(
        db_session, project, context_factory):
    context = context_factory(project, "chat", {})
    run = db_session.get(ResearchRun, context.run_id)
    for event_type, payload in [
        ("message.started", {"stream_id": "first"}),
        ("message.delta", {"stream_id": "first", "offset": 0, "delta": "A🧭"}),
        ("message.delta", {"stream_id": "first", "offset": 0, "delta": "A🧭"}),
        ("message.delta", {"stream_id": "first", "offset": 4, "delta": "future"}),
        ("message.delta", {"stream_id": "first", "offset": 2, "delta": "B"}),
    ]:
        jobs.emit_event(db_session, run.id, event_type, payload)
    partial = stream_for_run(db_session, run)
    assert partial["content"] == "A🧭B" and partial["offset"] == 3
    jobs.emit_event(db_session, run.id, "message.reset", {"stream_id": "first"})
    reset = stream_for_run(db_session, run)
    assert reset["content"] == "" and reset["offset"] == 0 and reset["status"] == "reset"
    jobs.emit_event(db_session, run.id, "message.started", {"stream_id": "second"})
    jobs.emit_event(db_session, run.id, "message.delta", {"stream_id": "first", "offset": 0, "delta": "stale"})
    jobs.emit_event(db_session, run.id, "message.delta", {"stream_id": "second", "offset": 0, "delta": "最终🧭"})
    message_id = new_id()
    jobs.emit_event(db_session, run.id, "message.completed", {
        "stream_id": "second", "content": "最终🧭", "message_id": message_id})
    completed = stream_for_run(db_session, run)
    assert completed["content"] == "最终🧭" and completed["offset"] == 3
    assert completed["status"] == "completed" and completed["message_id"] == message_id
