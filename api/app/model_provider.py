"""Explicitly configured OpenAI-compatible model adapters; no simulated outputs."""

import math
import json

import httpx

from app.config import get_settings
from app.errors import AppError


def _post(kind: str, payload: dict) -> dict:
    settings = get_settings()
    prefix = "llm" if kind == "chat/completions" else "embedding"
    if not getattr(settings, f"{prefix}_enabled"):
        raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置对话模型" if prefix == "llm" else "尚未配置向量模型")
    base_url = getattr(settings, f"{prefix}_base_url").rstrip("/")
    api_key = getattr(settings, f"{prefix}_api_key")
    options = settings.llm_extra_body if prefix == "llm" else {}
    payload = {**options, **payload, "model": getattr(settings, f"{prefix}_model")}
    try:
        response = httpx.post(f"{base_url}/{kind}", json=payload,
                              headers={"Authorization": f"Bearer {api_key}"}, timeout=settings.model_timeout)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Response must be an object")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        # Provider response bodies can contain credentials or submitted source material.
        raise AppError(502, "MODEL_REQUEST_FAILED", "模型服务调用失败，请检查服务配置或稍后重试") from exc


def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload = {"messages": messages}
    if tools:
        payload["tools"] = tools
    result = _post("chat/completions", payload)
    try:
        message = result["choices"][0]["message"]
        if not isinstance(message, dict) or not (isinstance(message.get("content"), str) or message.get("tool_calls")):
            raise ValueError("Missing model message")
        return message
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AppError(502, "MODEL_RESPONSE_INVALID", "模型服务返回了无效消息") from exc


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


def stream_chat(messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
    settings = get_settings()
    if not settings.llm_enabled:
        raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置对话模型")
    payload = {**settings.llm_extra_body, "messages": messages, "stream": True, "model": settings.llm_model}
    if tools:
        payload["tools"] = tools
    content, calls, finished = [], {}, False
    try:
        with httpx.stream("POST", f"{settings.llm_base_url.rstrip('/')}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {settings.llm_api_key}", "Accept": "text/event-stream"},
                          timeout=settings.model_timeout) as response:
            response.raise_for_status()
            for data in _sse_data(response.iter_lines()):
                if data == "[DONE]":
                    finished = True
                    break
                chunk = json.loads(data)
                if not isinstance(chunk, dict) or chunk.get("error"):
                    raise ValueError("Invalid streaming response")
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text is not None and not isinstance(text, str):
                    raise ValueError("Invalid text delta")
                if text:
                    content.append(text)
                    if on_delta:
                        on_delta(text)
                for fragment in delta.get("tool_calls") or []:
                    index = fragment["index"]
                    if not isinstance(index, int) or index < 0:
                        raise ValueError("Invalid tool index")
                    call = calls.setdefault(index, {"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                    call["id"] += fragment.get("id") or ""
                    function = fragment.get("function") or {}
                    call["function"]["name"] += function.get("name") or ""
                    call["function"]["arguments"] += function.get("arguments") or ""
                finished = finished or choice.get("finish_reason") is not None
        if not finished or not (content or calls):
            raise ValueError("Incomplete streaming response")
        result = {"role": "assistant", "content": "".join(content) if content else None}
        if calls:
            result["tool_calls"] = [calls[index] for index in sorted(calls)]
        return result
    except httpx.HTTPError as exc:
        raise AppError(502, "MODEL_REQUEST_FAILED", "模型服务调用失败，请检查服务配置或稍后重试") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AppError(502, "MODEL_RESPONSE_INVALID", "模型服务返回了无效的流式消息") from exc


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    result = _post("embeddings", {"input": texts})
    try:
        rows = result["data"]
        if len(rows) != len(texts) or sorted(row["index"] for row in rows) != list(range(len(texts))):
            raise ValueError("Embedding count or indices do not match")
        vectors = [row["embedding"] for row in sorted(rows, key=lambda row: row["index"])]
        dimension = len(vectors[0])
        if not dimension or any(len(v) != dimension for v in vectors):
            raise ValueError("Inconsistent embedding dimensions")
        if any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for v in vectors for x in v):
            raise ValueError("Non-finite embedding")
        if any(not any(x != 0 for x in v) for v in vectors):
            raise ValueError("Zero embedding cannot be used for cosine distance")
        return vectors
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AppError(502, "MODEL_RESPONSE_INVALID", "向量模型返回了无效结果") from exc
