"""Protocol tests use explicit gateways and controlled HTTP, without services."""

from contextlib import contextmanager
import json

import httpx
import pytest

from patentcompass_core.contracts import CapabilityError
from patentcompass_core.generation import EmbeddingConfig, EmbeddingGateway, GenerationConfig, PROTOCOL, ResponsesGateway


def _gateway(extra_body=None):
    return ResponsesGateway(GenerationConfig("https://controlled.invalid/v1/", "test-key", "test-model",
                                            extra_body=extra_body or {}))


def _answer(text):
    return {"id": "msg-test", "type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _completed(*items):
    return {"id": "resp-test", "status": "completed", "output": list(items), "usage": {"input_tokens": 4}}


def _post(monkeypatch, results):
    requests = []
    pending = iter(results)

    def post(url, **kwargs):
        requests.append({"url": url, **kwargs})
        return httpx.Response(200, text=json.dumps(next(pending)), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return requests


def _stream(monkeypatch, events):
    requests = []
    lines = []
    for event in events:
        lines.extend(["event: " + event["type"], "data: " + json.dumps(event), ""])

    @contextmanager
    def stream(method, url, **kwargs):
        requests.append({"url": url, **kwargs})
        response = httpx.Response(200, text="\n".join(lines) + "\n", request=httpx.Request(method, url))
        try:
            yield response
        finally:
            response.close()

    monkeypatch.setattr(httpx, "stream", stream)
    return requests


def test_generation_uses_responses_and_blocks_configuration_overrides(monkeypatch):
    requests = _post(monkeypatch, [_completed(_answer("result"))])
    gateway = _gateway({"model": "other", "input": "other", "messages": ["other"], "tools": ["other"],
                        "store": True, "stream": True, "include": [], "previous_response_id": "resp-old",
                        "conversation": "remote", "background": True, "temperature": 0.2})
    result = gateway.chat([{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}])
    assert result["content"] == "result" and result["_generation_protocol"] == PROTOCOL
    assert requests[0]["url"] == "https://controlled.invalid/v1/responses"
    assert requests[0]["json"] == {"model": "test-model", "input": [{"role": "system", "content": "rules"},
        {"role": "user", "content": "question"}], "store": False, "stream": False,
        "include": ["reasoning.encrypted_content"], "temperature": 0.2}


def test_tool_round_replays_all_items_and_matches_outputs_by_call_id(monkeypatch):
    reasoning = {"type": "reasoning", "id": "rs-private", "encrypted_content": "opaque", "content": ["PRIVATE"],
                 "summary": [{"type": "summary_text", "text": "PRIVATE"}]}
    calls = [{"type": "function_call", "id": "fc-item-1", "call_id": "call-1", "name": "search_patents",
              "arguments": '{"q":"landing"}', "status": "completed"},
             {"type": "function_call", "id": "fc-item-2", "call_id": "call-2", "name": "read_analysis",
              "arguments": '{"view":"overview"}', "status": "completed"}]
    requests = _post(monkeypatch, [_completed(reasoning, _answer("Checking."), *calls), _completed(_answer("Done."))])
    gateway = _gateway()
    tools = [{"type": "function", "function": {"name": "search_patents", "parameters": {
        "type": "object", "properties": {"q": {"type": "string"}}}}}]
    question = {"role": "user", "content": "Find landing patents."}
    first = gateway.chat([question], tools)
    assert first["tool_calls"][0]["id"] == "call-1"
    assert "PRIVATE" not in json.dumps(first)
    final = gateway.chat([question, first, {"role": "tool", "tool_call_id": "call-1", "content": "found"},
                          {"role": "tool", "tool_call_id": "call-2", "content": "statistics"}], tools)
    assert final["content"] == "Done."
    assert requests[1]["json"]["input"] == [question,
        {"type": "reasoning", "id": "rs-private", "encrypted_content": "opaque", "summary": []},
        _answer("Checking."), *calls,
        {"type": "function_call_output", "call_id": "call-1", "output": "found"},
        {"type": "function_call_output", "call_id": "call-2", "output": "statistics"}]
    assert requests[0]["json"]["tools"][0]["name"] == "search_patents"
    assert requests[0]["json"]["tools"][0]["strict"] is False


def test_legacy_controlled_tool_messages_are_converted_without_changing_call_id(monkeypatch):
    requests = _post(monkeypatch, [_completed(_answer("Done"))])
    _gateway().chat([{"role": "assistant", "content": None, "tool_calls": [{"id": "call-legacy", "type": "function",
        "function": {"name": "search_patents", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-legacy", "content": "result"}])
    assert requests[0]["json"]["input"] == [{"type": "function_call", "call_id": "call-legacy",
        "name": "search_patents", "arguments": "{}"}, {"type": "function_call_output",
        "call_id": "call-legacy", "output": "result"}]


@pytest.mark.parametrize("response,code", [
    ({"status": "incomplete", "output": [_answer("partial")]}, "MODEL_RESPONSE_INCOMPLETE"),
    ({"status": "failed", "error": {"message": "PRIVATE"}}, "MODEL_RESPONSE_FAILED"),
    ({"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "refusal", "refusal": "PRIVATE"}]}]}, "MODEL_REFUSED"),
    ({"choices": [{"message": {"content": "legacy"}}]}, "MODEL_RESPONSE_INVALID"),
    (_completed({"type": "function_call", "call_id": "call", "name": "tool", "arguments": "{", "status": "in_progress"}),
     "MODEL_RESPONSE_INVALID"),
    (_completed({"type": "message", "role": "assistant", "content": ["bad"]}), "MODEL_RESPONSE_INVALID"),
])
def test_nonstream_failures_never_become_success_or_expose_provider_text(monkeypatch, response, code):
    _post(monkeypatch, [response])
    with pytest.raises(CapabilityError) as failure:
        _gateway().chat([{"role": "user", "content": "question"}])
    assert failure.value.code == code and "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize("event,code", [
    ({"type": "response.failed", "response": {"error": {"message": "PRIVATE"}}}, "MODEL_RESPONSE_FAILED"),
    ({"type": "error", "message": "PRIVATE"}, "MODEL_RESPONSE_FAILED"),
    ({"type": "response.incomplete"}, "MODEL_RESPONSE_INCOMPLETE"),
    ({"type": "response.refusal.delta", "delta": "PRIVATE"}, "MODEL_REFUSED"),
])
def test_stream_failure_events_stop_generation(monkeypatch, event, code):
    _stream(monkeypatch, [event])
    deltas = []
    with pytest.raises(CapabilityError) as failure:
        _gateway().stream_chat([{"role": "user", "content": "question"}], on_delta=deltas.append)
    assert failure.value.code == code and "PRIVATE" not in str(failure.value) and deltas == []


def test_partial_tool_arguments_cannot_be_returned_without_completion(monkeypatch):
    _stream(monkeypatch, [{"type": "response.output_item.added", "output_index": 0, "item": {
        "type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "search_patents", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "output_index": 0, "delta": '{"q":'}])
    with pytest.raises(CapabilityError) as failure:
        _gateway().stream_chat([{"role": "user", "content": "question"}])
    assert failure.value.code == "MODEL_RESPONSE_INVALID"


def test_stream_requires_consistent_public_text_and_completed_response(monkeypatch):
    requests = _stream(monkeypatch, [{"type": "response.output_text.delta", "delta": "你好🧭"},
        {"type": "response.reasoning_text.delta", "delta": "PRIVATE"},
        {"type": "response.completed", "response": _completed(_answer("你好🧭"))}])
    deltas = []
    result = _gateway().stream_chat([{"role": "user", "content": "question"}], on_delta=deltas.append)
    assert result["content"] == "你好🧭" and deltas == ["你好🧭"]
    assert "PRIVATE" not in json.dumps(result) and requests[0]["json"]["store"] is False


def test_embedding_stays_on_its_own_endpoint_and_orders_vectors(monkeypatch):
    requests = _post(monkeypatch, [{"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}])
    gateway = EmbeddingGateway(EmbeddingConfig("https://embedding.invalid/v1", "embed-key", "embedding-model"))
    assert gateway.embed(["one", "two"]) == [[1, 0], [0, 1]]
    assert requests[0]["url"] == "https://embedding.invalid/v1/embeddings"
    assert requests[0]["json"] == {"model": "embedding-model", "input": ["one", "two"]}


@pytest.mark.parametrize("vectors", [[[0, 0]], [[1, float("inf")]], [[True, 1]], [[1, 0], [1]]])
def test_embedding_rejects_invalid_vectors(monkeypatch, vectors):
    _post(monkeypatch, [{"data": [{"index": i, "embedding": vector} for i, vector in enumerate(vectors)]}])
    with pytest.raises(CapabilityError) as failure:
        EmbeddingGateway(EmbeddingConfig("https://embedding.invalid/v1", "key", "model")).embed(["text"] * len(vectors))
    assert failure.value.code == "MODEL_RESPONSE_INVALID"
