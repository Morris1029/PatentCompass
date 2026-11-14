"""Explicit, stateless Responses generation and independent embedding access."""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math

import httpx

from .contracts import CapabilityError


PROTOCOL = "responses-v1"
_CONTROL_FIELDS = {"model", "input", "messages", "tools", "stream", "store", "include",
                   "previous_response_id", "conversation", "background"}


@dataclass(frozen=True)
class GenerationConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60
    extra_body: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60


def _check_config(config, name):
    if not (config.base_url and config.api_key and config.model):
        raise CapabilityError(503, "MODEL_NOT_CONFIGURED", f"尚未配置{name}模型")


def _post(config, route, payload):
    try:
        response = httpx.post(f"{config.base_url.rstrip('/')}/{route}", json=payload,
                              headers={"Authorization": f"Bearer {config.api_key}"}, timeout=config.timeout)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Response must be an object")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        # Upstream bodies may include credentials, prompts or source material.
        raise CapabilityError(502, "MODEL_REQUEST_FAILED", "模型服务调用失败，请检查服务配置或稍后重试") from exc


def response_input(messages):
    """Replay response items rather than reconstructing lossy assistant messages."""
    items = []
    for message in messages:
        if message.get("_generation_protocol") == PROTOCOL:
            replay = message.get("_response_items")
            if not isinstance(replay, list) or not replay:
                raise CapabilityError(422, "INVALID_MODEL_HISTORY", "模型续接记录无效")
            items.extend(deepcopy(replay))
        elif message.get("role") == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
        elif message.get("role") == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
            items.extend({"type": "function_call", "call_id": call["id"],
                          "name": call["function"]["name"], "arguments": call["function"]["arguments"]}
                         for call in message["tool_calls"])
        else:
            items.append({"role": message["role"], "content": message.get("content") or ""})
    return items


def _replay_item(item):
    if item["type"] == "reasoning":
        # Only opaque continuation data is needed; never persist plaintext reasoning.
        return {**{key: deepcopy(item[key]) for key in ("id", "type", "encrypted_content") if key in item},
                "summary": []}
    return deepcopy(item)


def _message(result):
    if not isinstance(result, dict):
        raise CapabilityError(502, "MODEL_RESPONSE_INVALID", "模型服务返回了无效消息")
    status = result.get("status")
    if status == "failed" or result.get("error"):
        raise CapabilityError(502, "MODEL_RESPONSE_FAILED", "模型服务未能完成本次生成")
    if status in {"incomplete", "cancelled"}:
        raise CapabilityError(502, "MODEL_RESPONSE_INCOMPLETE", "模型生成未完成，请重试或调整请求")
    try:
        if status != "completed" or not isinstance(result["output"], list):
            raise ValueError("Missing completed output")
        text, calls, replay = [], [], []
        for item in result["output"]:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise ValueError("Invalid output item")
            if item.get("status") not in (None, "completed"):
                raise ValueError("Incomplete output item")
            if item["type"] == "message":
                if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                    raise ValueError("Invalid assistant message")
                for part in item["content"]:
                    if not isinstance(part, dict):
                        raise ValueError("Invalid message content")
                    if part.get("type") == "refusal":
                        raise CapabilityError(422, "MODEL_REFUSED", "模型拒绝了本次生成，请调整请求")
                    if part.get("type") == "output_text":
                        if not isinstance(part.get("text"), str):
                            raise ValueError("Invalid public text")
                        text.append(part["text"])
            elif item["type"] == "function_call":
                if not all(isinstance(item.get(key), str) and item[key] for key in ("call_id", "name")) or not isinstance(item.get("arguments"), str):
                    raise ValueError("Invalid function call")
                calls.append({"id": item["call_id"], "type": "function", "function": {
                    "name": item["name"], "arguments": item["arguments"]}})
            replay.append(_replay_item(item))
        if len({call["id"] for call in calls}) != len(calls) or not (text or calls):
            raise ValueError("Missing text or unique function calls")
        message = {"role": "assistant", "content": "".join(text) if text else None,
                   "_generation_protocol": PROTOCOL, "_response_items": replay}
        if calls:
            message["tool_calls"] = calls
        if isinstance(result.get("usage"), dict):
            message["usage"] = deepcopy(result["usage"])
        return message
    except (KeyError, TypeError, ValueError) as exc:
        raise CapabilityError(502, "MODEL_RESPONSE_INVALID", "模型服务返回了无效消息") from exc


def _sse_data(lines):
    data = []
    for line in lines:
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield "\n".join(data)


class ResponsesGateway:
    def __init__(self, config: GenerationConfig):
        self.config = config

    def _payload(self, messages, tools, stream):
        _check_config(self.config, "对话")
        payload = {key: deepcopy(value) for key, value in self.config.extra_body.items()
                   if key not in _CONTROL_FIELDS}
        payload.update(model=self.config.model, input=response_input(messages), stream=stream, store=False,
                       include=["reasoning.encrypted_content"])
        if tools:
            payload["tools"] = [{"type": "function", **deepcopy(tool.get("function", tool)),
                                 "strict": tool.get("function", tool).get("strict", False)} for tool in tools]
        return payload

    def chat(self, messages, tools=None):
        return _message(_post(self.config, "responses", self._payload(messages, tools, False)))

    def stream_chat(self, messages, tools=None, on_delta=None):
        payload = self._payload(messages, tools, True)
        completed, text = None, []
        try:
            with httpx.stream("POST", f"{self.config.base_url.rstrip('/')}/responses", json=payload,
                              headers={"Authorization": f"Bearer {self.config.api_key}", "Accept": "text/event-stream"},
                              timeout=self.config.timeout) as response:
                response.raise_for_status()
                for data in _sse_data(response.iter_lines()):
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise ValueError("Invalid streaming event")
                    kind = event["type"]
                    if kind in {"error", "response.failed"}:
                        raise CapabilityError(502, "MODEL_RESPONSE_FAILED", "模型服务未能完成本次生成")
                    if kind in {"response.incomplete", "response.cancelled"}:
                        raise CapabilityError(502, "MODEL_RESPONSE_INCOMPLETE", "模型生成未完成，请重试或调整请求")
                    if kind in {"response.refusal.delta", "response.refusal.done"}:
                        raise CapabilityError(422, "MODEL_REFUSED", "模型拒绝了本次生成，请调整请求")
                    if kind == "response.output_text.delta":
                        delta = event["delta"]
                        if not isinstance(delta, str):
                            raise ValueError("Invalid text delta")
                        text.append(delta)
                        if on_delta:
                            on_delta(delta)
                    elif kind == "response.completed":
                        # Completed output is authoritative. Partial tool arguments never execute.
                        completed = _message(event["response"])
                        break
            if completed is None or (text and "".join(text) != (completed["content"] or "")):
                raise ValueError("Missing or inconsistent completed response")
            if not text and completed["content"] and on_delta:
                on_delta(completed["content"])
            return completed
        except httpx.HTTPError as exc:
            raise CapabilityError(502, "MODEL_REQUEST_FAILED", "模型服务调用失败，请检查服务配置或稍后重试") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityError(502, "MODEL_RESPONSE_INVALID", "模型服务返回了无效的流式消息") from exc


class EmbeddingGateway:
    def __init__(self, config: EmbeddingConfig):
        self.config = config

    def embed(self, texts):
        if not texts:
            return []
        _check_config(self.config, "向量")
        result = _post(self.config, "embeddings", {"model": self.config.model, "input": texts})
        try:
            rows = result["data"]
            if len(rows) != len(texts) or sorted(row["index"] for row in rows) != list(range(len(texts))):
                raise ValueError("Embedding count or indices do not match")
            vectors = [row["embedding"] for row in sorted(rows, key=lambda row: row["index"])]
            dimension = len(vectors[0])
            if not dimension or any(len(vector) != dimension for vector in vectors):
                raise ValueError("Inconsistent embedding dimensions")
            if any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for vector in vectors for x in vector):
                raise ValueError("Non-finite embedding")
            if any(not any(x != 0 for x in vector) for vector in vectors):
                raise ValueError("Zero embedding cannot be used for cosine distance")
            return vectors
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise CapabilityError(502, "MODEL_RESPONSE_INVALID", "向量模型返回了无效结果") from exc
